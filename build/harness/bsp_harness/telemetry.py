"""Per-call telemetry → Postgres ai.llm_calls (created if missing), JSONL fallback when
the warehouse is down, plus an in-memory ring for the UI and budget counters."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from .config import data_dir

log = logging.getLogger("bsp_harness.telemetry")

DDL = """
CREATE SCHEMA IF NOT EXISTS ai;
CREATE TABLE IF NOT EXISTS ai.llm_calls (
    id                bigserial PRIMARY KEY,
    ts                timestamptz NOT NULL DEFAULT now(),
    request_id        text,
    endpoint          text,            -- chat | embeddings
    alias             text,            -- requested model (alias or provider/model)
    provider          text,
    model             text,
    attempt           int,             -- 1 = first route/try
    status            text,            -- ok | error | skipped
    error             text,
    latency_ms        int,
    ttft_ms           int,
    prompt_tokens     int,
    completion_tokens int,
    total_tokens      int,
    cost_usd          numeric(12,6),
    stream            boolean,
    json_mode         boolean,
    has_images        boolean,
    has_tools         boolean,
    data_classification text,
    caller            text,
    preview           text
);
CREATE INDEX IF NOT EXISTS llm_calls_ts_idx ON ai.llm_calls (ts);
CREATE TABLE IF NOT EXISTS ai.eval_runs (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    run_id      text,
    suite       text,
    route       text,
    n_cases     int,
    score       double precision,
    latency_p50_ms int,
    errors      int,
    details     jsonb
);
"""
COLS = ("ts", "request_id", "endpoint", "alias", "provider", "model", "attempt", "status", "error", "latency_ms",
        "ttft_ms", "prompt_tokens", "completion_tokens", "total_tokens", "cost_usd", "stream", "json_mode", "has_images",
        "has_tools", "data_classification", "caller", "preview")

RING: deque = deque(maxlen=5000)
_lock = threading.Lock()
_ddl_lock = threading.Lock()
_db_state = {"ok": None, "checked": 0.0, "ddl": False, "error": ""}
_spend: dict[tuple[str, str], dict[str, float]] = {}   # (day, provider) → {"usd", "tokens"}


def db_enabled() -> bool:
    return os.environ.get("HARNESS_TELEMETRY_DB", "1") != "0"


def connect():
    import psycopg2  # noqa: PLC0415

    from bsp_common import settings  # noqa: PLC0415

    return psycopg2.connect(settings.get().pg_dsn, connect_timeout=3, application_name="bsp-harness")


def ensure_schema() -> bool:
    if not db_enabled():
        return False
    now = time.monotonic()
    if _db_state["ok"] and _db_state["ddl"] and now - _db_state["checked"] < 60:
        return True
    if _db_state["ok"] is False and now - _db_state["checked"] < 30:
        return False
    try:
        with _ddl_lock, connect() as conn, conn.cursor() as cur:
            if not _db_state["ddl"]:
                cur.execute(DDL)
                _db_state["ddl"] = True
        _db_state.update(ok=True, checked=now, error="")
        return True
    except Exception as e:  # noqa: BLE001
        _db_state.update(ok=False, checked=now, error=f"{type(e).__name__}: {str(e).strip()[:200]}")
        return False


def jsonl_path(day: dt.date | None = None) -> Path:
    d = data_dir() / "telemetry"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"llm_calls-{(day or dt.date.today()).isoformat()}.jsonl"


def _cost(provider: str, model: str, pt: int, ct: int) -> float:
    from .config import CONFIG  # noqa: PLC0415

    m = CONFIG.model_info(provider, model)
    return round(pt / 1000 * float(m.get("cost_in_per_1k") or 0) + ct / 1000 * float(m.get("cost_out_per_1k") or 0), 6)


def record(**row: Any) -> dict:
    row.setdefault("ts", dt.datetime.now(dt.timezone.utc).isoformat())
    pt, ct = int(row.get("prompt_tokens") or 0), int(row.get("completion_tokens") or 0)
    row["prompt_tokens"], row["completion_tokens"], row["total_tokens"] = pt, ct, pt + ct
    if row.get("provider") and row.get("status") == "ok":
        row["cost_usd"] = _cost(row["provider"], row.get("model") or "", pt, ct)
        key = (row["ts"][:10], row["provider"])
        with _lock:
            s = _spend.setdefault(key, {"usd": 0.0, "tokens": 0})
            s["usd"] += row["cost_usd"]
            s["tokens"] += pt + ct
    full = {c: row.get(c) for c in COLS}
    RING.append(full)
    threading.Thread(target=_persist, args=(full,), daemon=True).start()
    return full


def _persist(row: dict) -> None:
    if ensure_schema():
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(f"INSERT INTO ai.llm_calls ({', '.join(COLS)}) VALUES ({', '.join(['%s'] * len(COLS))})",
                            [row[c] for c in COLS])
            return
        except Exception as e:  # noqa: BLE001
            _db_state.update(ok=False, checked=time.monotonic(), error=f"{type(e).__name__}: {str(e)[:200]}")
    try:
        with _lock, jsonl_path().open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        log.warning("telemetry JSONL write failed: %s", e)


def spend_today(provider: str) -> dict[str, float]:
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    with _lock:
        return dict(_spend.get((day, provider), {"usd": 0.0, "tokens": 0}))


def load_spend() -> None:
    """Seed today's budget counters from the DB at startup (so restarts don't reset caps)."""
    if not ensure_schema():
        return
    try:
        with connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT provider, coalesce(sum(cost_usd),0), coalesce(sum(total_tokens),0) FROM ai.llm_calls "
                        "WHERE status='ok' AND ts >= date_trunc('day', now() at time zone 'utc') GROUP BY provider")
            day = dt.datetime.now(dt.timezone.utc).date().isoformat()
            with _lock:
                for prov, usd, toks in cur.fetchall():
                    _spend[(day, prov)] = {"usd": float(usd), "tokens": int(toks)}
    except Exception as e:  # noqa: BLE001
        log.info("budget counters not loaded: %s", e)


def recent_rows(hours: int = 24, limit: int = 20000) -> tuple[list[dict], str]:
    """Rows for the telemetry view: DB if reachable, else JSONL files + ring."""
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    if ensure_schema():
        try:
            with connect() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT {', '.join(COLS)} FROM ai.llm_calls WHERE ts >= %s ORDER BY ts DESC LIMIT %s",
                            (since, limit))
                rows = [dict(zip(COLS, r)) for r in cur.fetchall()]
            for r in rows:
                r["ts"] = r["ts"].isoformat() if hasattr(r["ts"], "isoformat") else r["ts"]
                r["cost_usd"] = float(r["cost_usd"]) if r["cost_usd"] is not None else None
            return rows, "postgres ai.llm_calls"
        except Exception as e:  # noqa: BLE001
            log.info("telemetry query failed, using JSONL: %s", e)
    rows: dict[tuple, dict] = {}
    days = {(since + dt.timedelta(days=i)).date() for i in range((hours // 24) + 2)}
    for d in sorted(days):
        p = jsonl_path(d)
        if p.exists():
            for line in p.read_text().splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                rows[(r.get("ts"), r.get("request_id"), r.get("attempt"))] = r
    for r in list(RING):
        rows[(r.get("ts"), r.get("request_id"), r.get("attempt"))] = r
    out = [r for r in rows.values() if str(r.get("ts", "")) >= since.isoformat()]
    out.sort(key=lambda r: r.get("ts", ""), reverse=True)
    return out[:limit], "JSONL fallback (warehouse unreachable)" + (f": {_db_state['error']}" if _db_state["error"] else "")


def _pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return round(s[lo] + (s[hi] - s[lo]) * (k - lo), 1)


def summary(hours: int = 24, bucket_min: int = 60) -> dict:
    rows, source = recent_rows(hours)
    calls = [r for r in rows if r.get("status") != "skipped"]

    def agg(group: list[dict]) -> dict:
        ok = [r for r in group if r.get("status") == "ok"]
        lat = [float(r["latency_ms"]) for r in ok if r.get("latency_ms") is not None]
        return {"calls": len(group), "ok": len(ok), "errors": len(group) - len(ok),
                "p50_ms": _pct(lat, 0.5), "p95_ms": _pct(lat, 0.95),
                "prompt_tokens": sum(int(r.get("prompt_tokens") or 0) for r in ok),
                "completion_tokens": sum(int(r.get("completion_tokens") or 0) for r in ok),
                "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in ok), 4)}

    def by(key: str) -> list[dict]:
        groups: dict[str, list] = {}
        for r in calls:
            groups.setdefault(str(r.get(key) or "—"), []).append(r)
        return sorted([{key: k, **agg(v)} for k, v in groups.items()], key=lambda x: -x["calls"])

    series: dict[str, dict] = {}
    for r in calls:
        try:
            t = dt.datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
        except (KeyError, ValueError):
            continue
        b = t.replace(minute=(t.minute // bucket_min) * bucket_min if bucket_min < 60 else 0, second=0, microsecond=0)
        s = series.setdefault(b.isoformat(), {"t": b.isoformat(), "calls": 0, "errors": 0, "tokens": 0, "lat": []})
        s["calls"] += 1
        if r.get("status") == "ok":
            s["tokens"] += int(r.get("total_tokens") or 0)
            if r.get("latency_ms") is not None:
                s["lat"].append(float(r["latency_ms"]))
        else:
            s["errors"] += 1
    ser = []
    for k in sorted(series):
        s = series[k]
        ser.append({"t": s["t"], "calls": s["calls"], "errors": s["errors"], "tokens": s["tokens"],
                    "p50_ms": _pct(s["lat"], 0.5), "p95_ms": _pct(s["lat"], 0.95)})
    return {"source": source, "hours": hours, "total": agg(calls), "by_alias": by("alias"), "by_provider": by("provider"),
            "by_model": by("model"), "series": ser,
            "recent": [{k: r.get(k) for k in ("ts", "endpoint", "alias", "provider", "model", "status", "error",
                                              "latency_ms", "total_tokens", "attempt", "caller")} for r in rows[:100]]}
