"""Egress policy model: parse, validate and match the central allowlist.

Pure Python (stdlib + PyYAML), no mitmproxy import, so the Studio plugin's
validator, the unit tests and the proxy share exactly one implementation of
the matching semantics.

Schema (data/egress/allowlist.yaml)::

    mode: enforce | audit
    rules:
      - pattern: "*.pypi.org" | "pypi.org" | "cdn-lfs*.huggingface.co" | "regex:^...$"
        category: PyPI
        reason: Python packages
        added_by: system
        added_at: 2026-10-06T00:00:00Z
        ports: [443]            # optional; default [80, 443]; "any" = every port
        tls: intercept          # optional; "passthrough" = do not decrypt (pinned clients)
        enabled: true           # optional
    tcp_tunnels:
      - name: dwh
        listen_port: 14330
        target_host: sql01.dataplatform.com
        target_port: 1433
        reason: on-prem SQL Server

Matching
    * ``pypi.org``                exact host only (the apex is NOT implied by ``*.``)
    * ``*.pypi.org``              one or more labels in front of pypi.org
    * ``cdn-lfs*.huggingface.co`` ``*`` inside a label matches within that label
    * ``regex:<re>``              Python ``re.fullmatch`` against the lower-cased host
Hosts are normalised (lower-case, trailing dot stripped, IDNA). IP literals
only match a rule that names them (or a regex).
"""

from __future__ import annotations

import datetime as _dt
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

try:  # PyYAML is in the image; keep import errors explicit for tests.
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

DEFAULT_PORTS = (80, 443)
MODES = ("enforce", "audit")
RESERVED_PORTS = {3128, 8081}
_LABEL = r"[a-z0-9_](?:[a-z0-9_-]{0,61}[a-z0-9_])?"
_HOST_RE = re.compile(rf"^(?:{_LABEL}\.)*{_LABEL}$")
_GLOB_CHARS = re.compile(r"^[a-z0-9_.*-]+$")


class PolicyError(ValueError):
    """Raised with a list of human-readable problems."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_host(host: str | None) -> str:
    h = (host or "").strip().lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    h = h.rstrip(".")
    if h and not _is_ip(h):
        try:
            h = h.encode("idna").decode("ascii")
        except (UnicodeError, ValueError):
            pass
    return h


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Translate a rule pattern to an anchored regex. Raises ValueError."""
    p = (pattern or "").strip()
    if not p:
        raise ValueError("empty pattern")
    if p.startswith("regex:"):
        body = p[len("regex:"):]
        if not body:
            raise ValueError("empty regex")
        try:
            rx = re.compile(body, re.IGNORECASE)
        except re.error as e:
            raise ValueError(f"invalid regex: {e}") from None
        # A regex that matches everything defeats the allowlist.
        for probe in ("example.com", "evil.example", "a.b.c.d.e"):
            if rx.fullmatch(probe):
                raise ValueError("regex is too broad (matches arbitrary hosts)")
        return rx
    p = p.lower().rstrip(".")
    if _is_ip(p):
        return re.compile(re.escape(p))
    if not _GLOB_CHARS.match(p):
        raise ValueError("only letters, digits, '-', '_', '.', '*' allowed (or use regex:)")
    labels = p.split(".")
    if any(lbl == "" for lbl in labels):
        raise ValueError("empty label (double dot?)")
    # Wildcards: a whole leading '*' label = one-or-more labels; '*' inside a
    # label = within that label only. Never in the registrable suffix.
    fixed = [lbl for lbl in labels if "*" not in lbl]
    if "*" in "".join(labels[-2:]):
        raise ValueError("wildcards are not allowed in the last two labels (too broad)")
    if len(fixed) < 2:
        raise ValueError("pattern must name at least a registrable domain (e.g. *.example.com)")
    parts: list[str] = []
    for i, lbl in enumerate(labels):
        if lbl == "*" and i == 0:
            parts.append(r"(?:[a-z0-9_-]+\.)+")
            continue
        if lbl == "*":
            rx = r"[a-z0-9_-]+"
        else:
            rx = "[a-z0-9_-]*".join(re.escape(x) for x in lbl.split("*"))
        parts.append(rx + ("" if i == len(labels) - 1 else r"\."))
    return re.compile("".join(parts))


def _parse_ports(v: Any) -> tuple[int, ...] | None:
    """None = any port."""
    if v is None:
        return DEFAULT_PORTS
    if isinstance(v, str):
        if v.strip().lower() in ("any", "*", "all"):
            return None
        v = [x for x in re.split(r"[,\s]+", v) if x]
    if isinstance(v, int):
        v = [v]
    out = []
    for x in v:
        n = int(x)
        if not 1 <= n <= 65535:
            raise ValueError(f"port {n} out of range")
        out.append(n)
    if not out:
        raise ValueError("ports list is empty (use 'any' for all ports)")
    return tuple(sorted(set(out)))


@dataclass
class Rule:
    pattern: str
    category: str = "Custom"
    reason: str = ""
    added_by: str = ""
    added_at: str = ""
    ports: tuple[int, ...] | None = DEFAULT_PORTS
    tls: str = "intercept"
    enabled: bool = True
    # "allow" (default) or "deny". A matching deny rule wins over every allow
    # rule, on any port and in audit mode too — e.g. `*.google.com` allowed
    # but `drive.google.com` denied.
    action: str = "allow"
    rx: re.Pattern[str] | None = field(default=None, repr=False, compare=False)

    def matches_host(self, host: str) -> bool:
        return bool(self.enabled and self.rx is not None and self.rx.fullmatch(host))

    def allows_port(self, port: int | None) -> bool:
        return self.ports is None or port is None or port in self.ports

    def to_dict(self) -> dict:
        d: dict[str, Any] = {"pattern": self.pattern, "category": self.category, "reason": self.reason,
                             "added_by": self.added_by, "added_at": self.added_at,
                             "ports": "any" if self.ports is None else list(self.ports)}
        if self.tls != "intercept":
            d["tls"] = self.tls
        if not self.enabled:
            d["enabled"] = False
        if self.action != "allow":
            d["action"] = self.action
        return d


@dataclass
class Tunnel:
    name: str
    listen_port: int
    target_host: str
    target_port: int
    reason: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "listen_port": self.listen_port, "target_host": self.target_host,
                "target_port": self.target_port, "reason": self.reason}


@dataclass
class Decision:
    allowed: bool          # final verdict (True in audit mode even if no rule)
    matched: bool          # a rule matched host+port
    rule: Rule | None
    reason: str            # "rule" | "no-matching-rule" | "port-not-allowed" | ...
    mode: str

    @property
    def action(self) -> str:
        if self.rule is not None and self.rule.action == "deny":
            return "block"
        if self.matched:
            return "allow"
        return "audit-allow" if self.allowed else "block"

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "matched": self.matched, "action": self.action,
                "reason": self.reason, "mode": self.mode,
                "rule": self.rule.pattern if self.rule else None,
                "category": self.rule.category if self.rule else None,
                "tls": self.rule.tls if self.rule else None}


@dataclass
class Policy:
    mode: str = "enforce"
    rules: list[Rule] = field(default_factory=list)
    tcp_tunnels: list[Tunnel] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # unknown top-level keys, preserved

    # ── construction ────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, data: dict | None, *, strict: bool = True) -> "Policy":
        """Parse + validate. strict=True raises PolicyError listing every
        problem; strict=False drops bad rules (used on hot-reload so a typo
        in one rule never disables the whole allowlist)."""
        data = data or {}
        if not isinstance(data, dict):
            raise PolicyError(["policy must be a mapping"])
        problems: list[str] = []
        mode = str(data.get("mode") or "enforce").strip().lower()
        if mode not in MODES:
            problems.append(f"mode must be one of {MODES}, got {mode!r}")
            mode = "enforce"
        rules: list[Rule] = []
        seen: set[str] = set()
        for i, raw in enumerate(data.get("rules") or []):
            try:
                r = rule_from_dict(raw)
            except ValueError as e:
                pat = raw.get("pattern") if isinstance(raw, dict) else raw
                problems.append(f"rule #{i + 1} ({pat!r}): {e}")
                continue
            key = r.pattern.lower()
            if key in seen:
                problems.append(f"rule #{i + 1} ({r.pattern!r}): duplicate pattern")
                continue
            seen.add(key)
            rules.append(r)
        tunnels: list[Tunnel] = []
        ports_used: set[int] = set()
        names: set[str] = set()
        for i, raw in enumerate(data.get("tcp_tunnels") or []):
            try:
                t = tunnel_from_dict(raw)
                if t.listen_port in ports_used:
                    raise ValueError(f"listen_port {t.listen_port} used twice")
                if t.name in names:
                    raise ValueError(f"name {t.name!r} used twice")
                ports_used.add(t.listen_port)
                names.add(t.name)
                tunnels.append(t)
            except (ValueError, TypeError) as e:
                problems.append(f"tunnel #{i + 1}: {e}")
        if problems and strict:
            raise PolicyError(problems)
        extra = {k: v for k, v in data.items() if k not in ("mode", "rules", "tcp_tunnels")}
        pol = cls(mode=mode, rules=rules, tcp_tunnels=tunnels, extra=extra)
        pol.problems = problems  # type: ignore[attr-defined]
        return pol

    @classmethod
    def from_yaml(cls, text: str, *, strict: bool = True) -> "Policy":
        if yaml is None:  # pragma: no cover
            raise RuntimeError("PyYAML not installed")
        try:
            data = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise PolicyError([f"YAML parse error: {e}"]) from None
        return cls.from_dict(data, strict=strict)

    def to_dict(self) -> dict:
        d = {"mode": self.mode, "rules": [r.to_dict() for r in self.rules if r.added_by != ENV_TAG],
             "tcp_tunnels": [t.to_dict() for t in self.tcp_tunnels]}
        d.update(self.extra)
        return d

    def to_yaml(self) -> str:
        header = ("# DataPlatform appliance — egress allowlist (managed in Studio → Egress).\n"
                  "# Hot-reloaded by px-proxy. Schema: build/px-proxy/policy.py\n")
        return header + yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True, width=120)

    # ── matching ────────────────────────────────────────────────────
    def match(self, host: str, port: int | None = None) -> tuple[Rule | None, str]:
        h = normalize_host(host)
        for r in self.rules:
            if r.action == "deny" and r.matches_host(h):
                return r, "denied-by-rule"
        host_hit: Rule | None = None
        for r in self.rules:
            if r.action == "allow" and r.matches_host(h):
                if r.allows_port(port):
                    return r, "rule"
                host_hit = host_hit or r
        if host_hit:
            return None, "port-not-allowed"
        return None, "no-matching-rule"

    def decide(self, host: str, port: int | None = None) -> Decision:
        rule, why = self.match(host, port)
        if rule and rule.action == "deny":
            return Decision(False, True, rule, why, self.mode)
        if rule:
            return Decision(True, True, rule, why, self.mode)
        return Decision(self.mode == "audit", False, None, why, self.mode)

    # ── edits (used by admin API) ───────────────────────────────────
    def upsert_rules(self, new: Iterable[Rule]) -> tuple[list[str], list[str]]:
        added, updated = [], []
        by_key = {r.pattern.lower(): i for i, r in enumerate(self.rules)}
        for r in new:
            k = r.pattern.lower()
            if k in by_key:
                old = self.rules[by_key[k]]
                # keep provenance of the original rule unless explicitly changed
                r.added_at = old.added_at or r.added_at
                r.added_by = old.added_by or r.added_by
                self.rules[by_key[k]] = r
                updated.append(r.pattern)
            else:
                by_key[k] = len(self.rules)
                self.rules.append(r)
                added.append(r.pattern)
        return added, updated

    def remove_rules(self, patterns: Iterable[str]) -> list[str]:
        keys = {p.lower() for p in patterns}
        removed = [r.pattern for r in self.rules if r.pattern.lower() in keys]
        self.rules = [r for r in self.rules if r.pattern.lower() not in keys]
        return removed


ENV_TAG = "env"   # rules derived from the environment: in memory only, never written to the file
ENV_HOST_VARS = ("SAP_BRIM_BASE_URL", "ATLAN_BASE_URL", "SHAREPOINT_SITE", "OLLAMA_BASE_URL")


def env_rules(environ: dict[str, str] | None = None, existing: "Policy | None" = None) -> list[Rule]:
    """Allow rules for hosts configured through the environment: the SAP BRIM
    host (SAP_BRIM_BASE_URL), a self-hosted Atlan, … plus EGRESS_EXTRA_HOSTS
    (comma-separated `host[:port]`). Hosts already allowed are skipped."""
    import os  # noqa: PLC0415
    from urllib.parse import urlsplit  # noqa: PLC0415

    environ = dict(os.environ if environ is None else environ)
    wanted: list[tuple[str, int | None, str]] = []
    for var in ENV_HOST_VARS:
        url = (environ.get(var) or "").strip()
        if not url or "://" not in url:
            continue
        u = urlsplit(url)
        if u.hostname and u.hostname not in ("localhost", "127.0.0.1") and "." in u.hostname:
            wanted.append((u.hostname, u.port, var))
    for item in (environ.get("EGRESS_EXTRA_HOSTS") or "").split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.rpartition(":") if item.count(":") == 1 and item.rsplit(":", 1)[1].isdigit() else (item, "", "")
        wanted.append((host, int(port) if port else None, "EGRESS_EXTRA_HOSTS"))
    out: list[Rule] = []
    seen: set[str] = set()
    for host, port, var in wanted:
        key = host.lower()
        if key in seen:
            continue
        if existing is not None and existing.decide(host, port or 443).matched:
            continue
        seen.add(key)
        try:
            r = rule_from_dict({"pattern": host, "category": "Configured hosts", "reason": f"from {var}",
                                "added_by": ENV_TAG, "ports": sorted({port, 443}) if port else [80, 443]})
        except PolicyError:
            continue
        out.append(r)
    return out


def rule_from_dict(raw: Any) -> Rule:
    if isinstance(raw, str):
        raw = {"pattern": raw}
    if not isinstance(raw, dict):
        raise ValueError("rule must be a mapping or a pattern string")
    pattern = str(raw.get("pattern") or "").strip()
    rx = compile_pattern(pattern)
    if not pattern.startswith("regex:"):
        pattern = pattern.lower().rstrip(".")
    tls = str(raw.get("tls") or "intercept").lower()
    if tls not in ("intercept", "passthrough"):
        raise ValueError("tls must be 'intercept' or 'passthrough'")
    action = str(raw.get("action") or "allow").lower()
    if action not in ("allow", "deny"):
        raise ValueError("action must be 'allow' or 'deny'")
    added_at = raw.get("added_at") or ""
    if isinstance(added_at, (_dt.date, _dt.datetime)):
        added_at = added_at.isoformat()
    return Rule(pattern=pattern, category=str(raw.get("category") or "Custom").strip(),
                reason=str(raw.get("reason") or "").strip(), added_by=str(raw.get("added_by") or "").strip(),
                added_at=str(added_at), ports=_parse_ports(raw.get("ports")), tls=tls,
                enabled=bool(raw.get("enabled", True)), action=action, rx=rx)


def tunnel_from_dict(raw: Any) -> Tunnel:
    if not isinstance(raw, dict):
        raise ValueError("tunnel must be a mapping")
    name = str(raw.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", name):
        raise ValueError("name must be 1-40 chars [A-Za-z0-9_.-]")
    lp = int(raw.get("listen_port"))
    if not 1024 < lp <= 65535:
        raise ValueError("listen_port must be 1025-65535 (px-proxy runs as non-root)")
    if lp in RESERVED_PORTS:
        raise ValueError(f"listen_port {lp} is reserved by px-proxy")
    th = normalize_host(str(raw.get("target_host") or ""))
    if not th or not (_HOST_RE.match(th) or _is_ip(th)):
        raise ValueError("target_host must be a hostname or IP")
    tp = int(raw.get("target_port"))
    if not 1 <= tp <= 65535:
        raise ValueError("target_port out of range")
    return Tunnel(name=name, listen_port=lp, target_host=th, target_port=tp,
                  reason=str(raw.get("reason") or "").strip())


def parse_target(url_or_host: str) -> tuple[str, int, str]:
    """'https://x.y/z' | 'x.y:8443' | 'x.y' → (host, port, scheme)."""
    from urllib.parse import urlsplit

    s = (url_or_host or "").strip()
    if "://" not in s:
        s = "https://" + s
    u = urlsplit(s)
    scheme = (u.scheme or "https").lower()
    port = u.port or {"http": 80, "https": 443, "ws": 80, "wss": 443}.get(scheme, 443)
    return normalize_host(u.hostname or ""), port, scheme
