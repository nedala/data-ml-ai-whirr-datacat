"""Decision log: JSONL with size-based rotation + an in-memory ring for the
admin API (recent tail, 24h stats, top-blocked hosts)."""

from __future__ import annotations

import collections
import json
import logging
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger("px-proxy.decisions")


class DecisionLog:
    def __init__(self, path: Path, max_bytes: int = 20 * 1024 * 1024, backups: int = 3, ring: int = 20_000,
                 stdout: bool = False):
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self.stdout = stdout
        self.ring: collections.deque[dict] = collections.deque(maxlen=ring)
        self._lock = threading.Lock()
        self._names: dict[str, str] = {}
        self._pending: set[str] = set()
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="rdns")
        self.started = time.time()
        self.counters: collections.Counter[str] = collections.Counter()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._load_tail()

    # ── persistence ─────────────────────────────────────────────────
    def _load_tail(self) -> None:
        """Warm the ring from the current file (+ .1) so stats survive restarts."""
        cutoff = time.time() - 26 * 3600
        for p in (self.path.with_name(self.path.name + ".1"), self.path):
            if not p.exists():
                continue
            try:
                with p.open("rb") as f:
                    size = f.seek(0, 2)
                    f.seek(max(0, size - 16 * 1024 * 1024))
                    if f.tell():
                        f.readline()
                    for line in f:
                        try:
                            d = json.loads(line)
                        except ValueError:
                            continue
                        if d.get("t", 0) >= cutoff:
                            self.ring.append(d)
            except OSError as e:
                log.warning("cannot read %s: %s", p, e)

    def _rotate(self) -> None:
        for i in range(self.backups, 0, -1):
            src = self.path if i == 1 else self.path.with_name(f"{self.path.name}.{i - 1}")
            dst = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, dst)

    def record(self, **d: Any) -> dict:
        now = time.time()
        d.setdefault("t", round(now, 3))
        d.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)))
        ip = d.get("client")
        if ip:
            name = self.client_name(ip)
            if name:
                d["client_name"] = name
        line = json.dumps(d, separators=(",", ":"), default=str) + "\n"
        with self._lock:
            self.ring.append(d)
            self.counters[d.get("action", "?")] += 1
            try:
                if self.path.exists() and self.path.stat().st_size + len(line) > self.max_bytes:
                    self._rotate()
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError as e:
                log.error("decision log write failed: %s", e)
            if self.stdout:
                sys.stdout.write("EGRESS " + line)
                sys.stdout.flush()
        return d

    # ── client names (Docker DNS answers PTR for container IPs) ────
    def client_name(self, ip: str) -> str | None:
        if ip in self._names:
            return self._names[ip] or None
        if ip not in self._pending and not ip.startswith("127."):
            self._pending.add(ip)
            self._pool.submit(self._resolve, ip)
        return None

    def _resolve(self, ip: str) -> None:
        try:
            name = socket.gethostbyaddr(ip)[0]
            # "bsp_data-appliance-1.bsp-data-net" → service-ish name
            name = name.split(".")[0]
        except OSError:
            name = ""
        self._names[ip] = name
        self._pending.discard(ip)

    # ── queries ─────────────────────────────────────────────────────
    def query(self, limit: int = 200, allowed: bool | None = None, host: str | None = None,
              since: float | None = None, action: str | None = None) -> list[dict]:
        out = []
        host = (host or "").lower().strip()
        with self._lock:
            items = list(self.ring)
        for d in reversed(items):
            if since and d.get("t", 0) < since:
                break
            if allowed is not None and bool(d.get("allowed")) != allowed:
                continue
            if action and d.get("action") != action:
                continue
            if host and host not in (d.get("host") or ""):
                continue
            out.append(d)
            if len(out) >= limit:
                break
        return out

    def stats(self, hours: float = 24) -> dict:
        since = time.time() - hours * 3600
        with self._lock:
            items = [d for d in self.ring if d.get("t", 0) >= since]
        by_action: collections.Counter[str] = collections.Counter()
        blocked: dict[str, dict] = {}
        allowed_hosts: collections.Counter[str] = collections.Counter()
        categories: collections.Counter[str] = collections.Counter()
        clients: collections.Counter[str] = collections.Counter()
        bytes_total = 0
        tls_client_fail = 0
        upstream_errors = 0
        for d in items:
            a = d.get("action", "?")
            by_action[a] += 1
            bytes_total += int(d.get("bytes_in") or 0) + int(d.get("bytes_out") or 0)
            if d.get("error") == "client-tls-failed":
                tls_client_fail += 1
            elif d.get("error"):
                upstream_errors += 1
            if a in ("block", "audit-allow"):
                key = f'{d.get("host")}:{d.get("port")}'
                b = blocked.setdefault(key, {"host": d.get("host"), "port": d.get("port"), "count": 0,
                                             "last": d.get("ts"), "clients": set(), "reason": d.get("reason")})
                b["count"] += 1
                b["last"] = d.get("ts")
                b["clients"].add(d.get("client_name") or d.get("client") or "?")
            else:
                allowed_hosts[d.get("host") or "?"] += 1
                categories[d.get("category") or "?"] += 1
            clients[d.get("client_name") or d.get("client") or "?"] += 1
        top_blocked = sorted(blocked.values(), key=lambda x: -x["count"])[:50]
        for b in top_blocked:
            b["clients"] = sorted(b["clients"])[:10]
        # hourly buckets for a sparkline
        buckets = int(min(hours, 168))
        series = [{"allowed": 0, "blocked": 0} for _ in range(buckets)]
        now = time.time()
        for d in items:
            i = buckets - 1 - int((now - d.get("t", now)) // 3600)
            if 0 <= i < buckets:
                series[i]["blocked" if d.get("action") in ("block", "audit-allow") else "allowed"] += 1
        return {
            "hours": hours,
            "total": len(items),
            "allowed": by_action.get("allow", 0),
            "blocked": by_action.get("block", 0),
            "would_block": by_action.get("audit-allow", 0),
            "bytes": bytes_total,
            "client_tls_failures": tls_client_fail,
            "upstream_errors": upstream_errors,
            "top_blocked": top_blocked,
            "top_allowed": [{"host": h, "count": c} for h, c in allowed_hosts.most_common(20)],
            "by_category": dict(categories.most_common()),
            "clients": dict(clients.most_common(20)),
            "series": series,
            "since_start": dict(self.counters),
            "uptime_s": int(now - self.started),
        }
