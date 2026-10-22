"""Studio plugin: egress policy (px-proxy allowlist, decisions, trust).

Backend for the "Egress policy" page. Talks to the px-proxy admin API
(http://px-proxy:8081, bearer token) through core.http_internal. When px-proxy
is down, policy reads/writes fall back to the allowlist FILE
(data/egress/allowlist.yaml) — px-proxy hot-reloads it when it comes back.

Contract used by other plugins (connectors):
    POST /studio/api/egress/allow  {"hosts": ["bigquery.googleapis.com", "https://x.y/z"],
                                    "reason": "BigQuery connector"}
    → {"added": [...], "updated": [...], "rules": N}
    (also importable: allow_hosts(hosts, reason, added_by=..., category=...))
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

import core
from core import User, paths, require_admin, require_user

router = APIRouter(prefix="/studio/api/egress")
PAGE = {"id": "egress", "title": "Egress policy", "icon": "shield",
        "script": "/studio/static/pages/egress.js", "order": 60, "admin": True, "group": "Configure"}

ADMIN_URL = os.environ.get("PX_PROXY_ADMIN_URL", "http://px-proxy:8081").rstrip("/")
TIMEOUT = 8


# ── shared policy logic (one implementation: build/px-proxy/policy.py) ──
def _load_policy_module():
    for cand in (os.environ.get("PX_PROXY_DIR", ""), str(paths.PROJECT / "build" / "px-proxy"), "/opt/px-proxy"):
        f = Path(cand) / "policy.py" if cand else None
        if f and f.is_file():
            spec = importlib.util.spec_from_file_location("px_policy", f)
            mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
            sys.modules["px_policy"] = mod  # dataclasses resolve their module via sys.modules
            spec.loader.exec_module(mod)  # type: ignore[union-attr]
            return mod
    return None


_policy_mod = None


def _pm():
    global _policy_mod
    if _policy_mod is None:
        try:
            _policy_mod = _load_policy_module() or False
        except Exception:  # noqa: BLE001
            _policy_mod = False
    return _policy_mod or None


def _allowlist_path() -> Path:
    return paths.DATA / "egress" / "allowlist.yaml"


def _token() -> str:
    tok = os.environ.get("EGRESS_ADMIN_TOKEN", "").strip()
    if tok:
        return tok
    f = paths.DATA / "egress" / ".admin-token"
    try:
        return f.read_text().strip()
    except OSError:
        return ""


class ProxyDown(Exception):
    pass


def _px(method: str, path: str, **kw) -> Any:
    """Call the px-proxy admin API; raise HTTPException on API errors,
    ProxyDown when unreachable."""
    headers = kw.pop("headers", {})
    headers["Authorization"] = f"Bearer {_token()}"
    try:
        r = core.http_internal(method, ADMIN_URL + path, headers=headers, timeout=kw.pop("timeout", TIMEOUT), **kw)
    except Exception as e:  # noqa: BLE001 — connection refused / DNS / timeout
        raise ProxyDown(str(e)) from None
    if r.status_code == 401:
        raise HTTPException(502, "px-proxy rejected Studio's admin token (data/egress/.admin-token or EGRESS_ADMIN_TOKEN mismatch)")
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            body = {"detail": r.text[:300]}
        detail = body.get("problems") or body.get("detail") or body.get("error") or r.reason
        raise HTTPException(r.status_code, detail if isinstance(detail, str) else "; ".join(map(str, detail)))
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.content


# ── file fallback ────────────────────────────────────────────────────────
def _file_policy() -> dict:
    p = _allowlist_path()
    if not p.is_file():
        default = paths.PROJECT / "build" / "px-proxy" / "allowlist.default.yaml"
        text = default.read_text(encoding="utf-8") if default.is_file() else "mode: enforce\nrules: []\n"
    else:
        text = p.read_text(encoding="utf-8")
    import hashlib

    data = yaml.safe_load(text) or {}
    return {"policy": data, "version": hashlib.sha256(text.encode()).hexdigest()[:12], "mode": data.get("mode", "enforce"),
            "problems": [], "source": "file", "path": str(p)}


def _validate(policy: dict) -> dict:
    pm = _pm()
    if pm:
        try:
            return pm.Policy.from_dict(policy, strict=True).to_dict()
        except pm.PolicyError as e:
            raise HTTPException(422, "; ".join(e.problems)) from None
    # minimal fallback validation
    for r in policy.get("rules") or []:
        pat = (r.get("pattern") if isinstance(r, dict) else str(r)) or ""
        if not pat or pat in ("*", "*.*") or (not pat.startswith("regex:") and not re.fullmatch(r"[a-z0-9_.*-]+", pat.lower())):
            raise HTTPException(422, f"invalid pattern {pat!r}")
    return policy


def _file_save(policy: dict) -> dict:
    policy = _validate(policy)
    header = ("# DataPlatform appliance — egress allowlist (managed in Studio → Egress).\n"
              "# Hot-reloaded by px-proxy. Schema: build/px-proxy/policy.py\n")
    core.write_file(_allowlist_path(), header + yaml.safe_dump(policy, sort_keys=False, allow_unicode=True, width=120))
    return _file_policy()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _host_of(s: str) -> str:
    pm = _pm()
    if pm:
        return pm.parse_target(s)[0]
    s = s.strip().lower()
    s = re.sub(r"^[a-z]+://", "", s)
    return s.split("/")[0].split(":")[0].rstrip(".")


# ── policy API ───────────────────────────────────────────────────────────
def get_policy() -> dict:
    try:
        return {**_px("GET", "/policy"), "source": "proxy"}
    except ProxyDown:
        return _file_policy()


@router.get("/policy")
def api_get_policy(user: User = Depends(require_admin)):
    return get_policy()


@router.put("/policy")
async def api_put_policy(request: Request, user: User = Depends(require_admin)):
    body = await request.json()
    policy = body.get("policy") if isinstance(body, dict) and "policy" in body else body
    if not isinstance(policy, dict):
        raise HTTPException(400, "policy object required")
    try:
        out = {**_px("PUT", "/policy", json={"policy": policy, "base_version": body.get("base_version")}), "source": "proxy"}
    except ProxyDown:
        out = _file_save(policy)
    core.audit("egress.policy.replace", f"mode={policy.get('mode')} rules={len(policy.get('rules') or [])}", request)
    return out


@router.put("/mode")
async def api_set_mode(request: Request, user: User = Depends(require_admin)):
    body = await request.json()
    mode = str(body.get("mode", "")).lower()
    if mode not in ("enforce", "audit"):
        raise HTTPException(400, "mode must be enforce or audit")
    cur = get_policy()
    pol = dict(cur["policy"])
    pol["mode"] = mode
    try:
        out = {**_px("PUT", "/policy", json={"policy": pol, "base_version": cur.get("version")}), "source": "proxy"}
    except ProxyDown:
        out = _file_save(pol)
    core.audit("egress.mode", mode, request)
    return out


def allow_hosts(hosts: list[str], reason: str, added_by: str = "studio", category: str = "Connectors",
                ports: Any = None) -> dict:
    """Add exact-host allow rules (idempotent). Used by POST /allow and by the
    connectors plugin. Patterns (``*.x.com``, ``regex:``) are accepted as-is."""
    rules = []
    for h in hosts or []:
        h = str(h).strip()
        if not h:
            continue
        pattern = h.lower() if (h.startswith("*.") or "*" in h or h.startswith("regex:")) else _host_of(h)
        rule = {"pattern": pattern, "category": category or "Connectors", "reason": reason or "",
                "added_by": added_by, "added_at": _now()}
        if ports is not None:
            rule["ports"] = ports
        rules.append(rule)
    if not rules:
        raise HTTPException(400, "hosts required")
    try:
        out = _px("POST", "/rules", json={"rules": rules})
        return {"added": out.get("added", []), "updated": out.get("updated", []),
                "rules": len(out.get("policy", {}).get("rules", [])), "source": "proxy"}
    except ProxyDown:
        cur = _file_policy()["policy"]
        existing = {str(r.get("pattern", "")).lower(): r for r in cur.get("rules") or [] if isinstance(r, dict)}
        added, updated = [], []
        for r in rules:
            k = r["pattern"].lower()
            if k in existing:
                existing[k].update({kk: vv for kk, vv in r.items() if kk not in ("added_at", "added_by")})
                updated.append(r["pattern"])
            else:
                cur.setdefault("rules", []).append(r)
                added.append(r["pattern"])
        _file_save(cur)
        return {"added": added, "updated": updated, "rules": len(cur.get("rules", [])), "source": "file"}


@router.post("/allow")
async def api_allow(request: Request, user: User = Depends(require_admin)):
    body = await request.json()
    hosts = body.get("hosts")
    if isinstance(hosts, str):
        hosts = [hosts]
    out = allow_hosts(hosts or [], body.get("reason", ""), added_by=user.name,
                      category=body.get("category") or "Connectors", ports=body.get("ports"))
    core.audit("egress.allow", f"{','.join(out['added'] + out['updated'])} reason={body.get('reason', '')}", request)
    return out


@router.post("/rules")
async def api_upsert_rule(request: Request, user: User = Depends(require_admin)):
    """Add or edit one rule from the drawer: {pattern, category, reason, ports, tls}."""
    body = await request.json()
    rule = {k: body.get(k) for k in ("pattern", "category", "reason", "ports", "tls", "enabled", "action") if body.get(k) is not None}
    rule.setdefault("added_by", user.name)
    rule.setdefault("added_at", _now())
    if body.get("replace"):  # pattern renamed in the editor
        try:
            _px("DELETE", "/rules", json={"patterns": [body["replace"]]})
        except ProxyDown:
            pass
    try:
        out = _px("POST", "/rules", json={"rules": [rule]})
    except ProxyDown:
        cur = _file_policy()["policy"]
        cur["rules"] = [r for r in cur.get("rules") or [] if str(r.get("pattern", "")).lower() not in
                        {str(rule.get("pattern", "")).lower(), str(body.get("replace") or "").lower()}] + [rule]
        out = _file_save(cur)
    core.audit("egress.rule.upsert", str(rule.get("pattern")), request)
    return out


@router.delete("/rules")
def api_delete_rule(request: Request, pattern: str, user: User = Depends(require_admin)):
    try:
        out = _px("DELETE", "/rules", json={"patterns": [pattern]})
    except ProxyDown:
        cur = _file_policy()["policy"]
        cur["rules"] = [r for r in cur.get("rules") or [] if str(r.get("pattern", "")).lower() != pattern.lower()]
        out = _file_save(cur)
    core.audit("egress.rule.delete", pattern, request)
    return out


@router.put("/tunnels")
async def api_put_tunnels(request: Request, user: User = Depends(require_admin)):
    body = await request.json()
    tunnels = body.get("tcp_tunnels", body.get("tunnels"))
    if not isinstance(tunnels, list):
        raise HTTPException(400, "tcp_tunnels list required")
    cur = get_policy()
    pol = dict(cur["policy"])
    pol["tcp_tunnels"] = tunnels
    try:
        out = _px("PUT", "/policy", json={"policy": pol, "base_version": cur.get("version")})
    except ProxyDown:
        out = _file_save(pol)
    core.audit("egress.tunnels", ",".join(f"{t.get('name')}:{t.get('listen_port')}" for t in tunnels), request)
    return out


@router.get("/tunnels")
def api_tunnels(user: User = Depends(require_admin)):
    try:
        return _px("GET", "/tunnels")
    except ProxyDown:
        return {"tunnels": [{**t, "listening": None} for t in _file_policy()["policy"].get("tcp_tunnels") or []],
                "proxy_down": True}


# ── decisions / stats ────────────────────────────────────────────────────
def _file_decisions(limit: int) -> list[dict]:
    f = paths.DATA / "egress" / "decisions.jsonl"
    if not f.is_file():
        return []
    with f.open("rb") as fh:
        size = fh.seek(0, 2)
        fh.seek(max(0, size - 2_000_000))
        lines = fh.read().splitlines()[-limit:]
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


@router.get("/decisions")
def api_decisions(limit: int = 200, allowed: str | None = None, host: str | None = None,
                  action: str | None = None, since: float | None = None, user: User = Depends(require_admin)):
    params = {"limit": max(1, min(limit, 2000))}
    if allowed in ("true", "false"):
        params["allowed"] = allowed
    if host:
        params["host"] = host
    if action:
        params["action"] = action
    if since:
        params["since"] = since
    try:
        return _px("GET", "/decisions", params=params)
    except ProxyDown:
        items = _file_decisions(params["limit"])
        if allowed in ("true", "false"):
            items = [d for d in items if bool(d.get("allowed")) == (allowed == "true")]
        if host:
            items = [d for d in items if host.lower() in (d.get("host") or "")]
        return {"items": items, "count": len(items), "proxy_down": True}


@router.get("/stats")
def api_stats(hours: float = 24, user: User = Depends(require_admin)):
    try:
        return _px("GET", "/stats", params={"hours": hours})
    except ProxyDown:
        raise HTTPException(503, "px-proxy is not reachable") from None


@router.get("/top-blocked")
def api_top_blocked(hours: float = 24, limit: int = 20, user: User = Depends(require_admin)):
    try:
        s = _px("GET", "/stats", params={"hours": hours})
    except ProxyDown:
        raise HTTPException(503, "px-proxy is not reachable") from None
    pm = _pm()
    items = []
    for b in s.get("top_blocked", [])[:limit]:
        b = dict(b)
        host = b.get("host") or ""
        b["suggested_pattern"] = host
        parts = host.split(".")
        if pm and len(parts) > 2:
            b["suggested_wildcard"] = "*." + ".".join(parts[1:]) if len(parts) > 3 else None
        items.append(b)
    return {"items": items, "hours": hours, "mode": s.get("mode")}


@router.post("/test")
async def api_test(request: Request, user: User = Depends(require_admin)):
    body = await request.json()
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(400, "url required")
    try:
        return _px("POST", "/test", json={"url": url})
    except ProxyDown:
        pm = _pm()
        if not pm:
            raise HTTPException(503, "px-proxy is not reachable") from None
        pol = pm.Policy.from_dict(_file_policy()["policy"], strict=False)
        host, port, scheme = pm.parse_target(url)
        d = pol.decide(host, port)
        return {"url": url, "host": host, "port": port, "scheme": scheme, **d.to_dict(), "proxy_down": True,
                "explanation": f"Allowed by rule {d.rule.pattern}" if d.matched else "No rule matches (px-proxy offline — evaluated from file)"}


@router.post("/validate")
async def api_validate(request: Request, user: User = Depends(require_admin)):
    """Live pattern validation for the add-rule drawer: {pattern, hosts:[…]}
    → {ok, error, matches:[hosts that match]}."""
    body = await request.json()
    pattern = str(body.get("pattern") or "").strip()
    hosts = [str(h) for h in body.get("hosts") or []][:500]
    pm = _pm()
    if not pm:
        return {"ok": True, "error": None, "matches": [], "unchecked": True}
    try:
        rx = pm.compile_pattern(pattern)
    except ValueError as e:
        return {"ok": False, "error": str(e), "matches": []}
    return {"ok": True, "error": None, "matches": [h for h in hosts if rx.fullmatch(pm.normalize_host(h))]}


# ── trust ────────────────────────────────────────────────────────────────
@router.get("/trust")
def api_trust(user: User = Depends(require_admin)):
    try:
        return _px("GET", "/trust")
    except ProxyDown:
        raise HTTPException(503, "px-proxy is not reachable") from None


@router.get("/ca.pem")
def api_ca(user: User = Depends(require_user)):
    try:
        pem = core.http_internal("GET", ADMIN_URL + "/ca.pem", timeout=TIMEOUT)
        if pem.status_code != 200:
            raise HTTPException(502, "px-proxy did not return the CA")
        data = pem.content
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "px-proxy is not reachable") from None
    return Response(data, media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="bsp-appliance-egress-ca.pem"'})


# ── backup / health ──────────────────────────────────────────────────────
def export_backup() -> dict:
    return {"allowlist": get_policy().get("policy")}


def import_backup(data: dict) -> None:
    pol = (data or {}).get("allowlist")
    if not isinstance(pol, dict):
        return
    try:
        _px("PUT", "/policy", json={"policy": pol})
    except ProxyDown:
        _file_save(pol)


def health() -> list[dict]:
    env = os.environ.get("APPLIANCE_ENV", "dev")
    items: list[dict] = []
    try:
        h = _px("GET", "/health", timeout=3)
    except (ProxyDown, HTTPException) as e:
        detail = getattr(e, "detail", None) or str(e)
        return [{"label": "Egress proxy", "state": "error", "detail": f"px-proxy unreachable — all egress is blocked ({detail[:120]})"}]
    mode = h.get("mode")
    items.append({"label": "Egress proxy", "state": "ok" if mode == "enforce" else "warning",
                  "detail": f"{mode} · {h.get('rules')} rules" + (f" · via {h['upstream']}" if h.get("upstream") else " · direct")
                            + (f" · {h['problems']} rule problem(s)" if h.get("problems") else "")})
    try:
        t = _px("GET", "/trust", timeout=3)
        ca = t.get("egress_ca", {})
        corp = t.get("corporate", [])
        items.append({"label": "Egress CA", "state": "ok" if ca.get("days_left", 0) > 60 else "warning",
                      "detail": f"{ca.get('cn')} · expires {str(ca.get('not_after', ''))[:10]}"})
        if not corp:
            st = "warning" if (env == "dev" and t.get("upstream_proxy")) else "ok"
            items.append({"label": "Corporate roots", "state": st,
                          "detail": "none loaded" + (" — behind a TLS-inspecting corporate proxy run `make corporate-ca`" if st == "warning" else "")})
        else:
            soon = min(c.get("days_left", 9999) for c in corp)
            items.append({"label": "Corporate roots", "state": "ok" if soon > 30 else "warning",
                          "detail": f"{len(corp)} loaded ({', '.join(c.get('cn', '?') for c in corp)[:80]})"})
    except (ProxyDown, HTTPException):
        pass
    return items
