"""px-proxy admin API (port 8081, appliance-net only; Studio is the client).

Auth: ``Authorization: Bearer <token>`` on everything except /health and
/ca.pem. Token = EGRESS_ADMIN_TOKEN env, else <data>/egress/.admin-token
(created 0600 on first start; Studio reads it from the host data dir). Without
the token a workload on appliance-net could rewrite its own allowlist.

  GET  /health                 liveness + mode (no auth)
  GET  /ca.pem                 egress CA public cert (no auth)
  GET  /policy                 {policy, version, problems, …}
  PUT  /policy                 {policy, base_version?} → validate + save (409 on stale base_version)
  POST /rules                  {rules:[…]} upsert by pattern
  DELETE /rules                {patterns:[…]}
  GET  /decisions?limit=&allowed=true|false&host=&action=&since=
  GET  /stats?hours=24
  POST /test                   {url} → would it be allowed + why
  GET  /trust                  egress CA + corporate roots + /trust files
  GET  /tunnels                tunnel status
"""

from __future__ import annotations

import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from decisions import DecisionLog
from policy import Policy, PolicyError, now_iso, parse_target, rule_from_dict
from store import PolicyStore

log = logging.getLogger("px-proxy.admin")


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def build_app(*, store: PolicyStore, dlog: DecisionLog, token: str, trust_info: Callable[[], dict],
              tunnels_status: Callable[[], list], ca_pem: Callable[[], bytes], meta: dict) -> web.Application:

    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path in ("/health", "/ca.pem"):
            return await handler(request)
        got = request.headers.get("Authorization", "")
        if not (got.startswith("Bearer ") and hmac.compare_digest(got[7:].strip(), token)):
            return _json({"error": "unauthorized"}, 401)
        try:
            return await handler(request)
        except PolicyError as e:
            return _json({"error": "invalid_policy", "problems": e.problems}, 422)
        except PermissionError as e:
            return _json({"error": "read_only", "detail": str(e)}, 409)
        except (ValueError, KeyError, TypeError) as e:
            return _json({"error": "bad_request", "detail": str(e)}, 400)

    app = web.Application(middlewares=[auth], client_max_size=4 * 1024 * 1024)
    r = app.router

    async def health(_):
        snap = store.snapshot()
        return _json({"ok": True, "mode": snap["mode"], "rules": len(snap["policy"]["rules"]),
                      "version": snap["version"], "problems": len(snap["problems"]),
                      "upstream": meta.get("upstream_display"), "uptime_s": int(time.time() - meta["started"]),
                      "ready": Path(meta["trust_dir"], "ready").exists()})

    async def get_ca(_):
        return web.Response(body=ca_pem(), content_type="application/x-pem-file",
                            headers={"Content-Disposition": 'attachment; filename="bsp-appliance-egress-ca.pem"'})

    async def get_policy(_):
        return _json(store.snapshot())

    async def put_policy(request: web.Request):
        body = await request.json()
        data = body.get("policy", body) if isinstance(body, dict) else body
        pol = Policy.from_dict(data, strict=True)
        try:
            store.save(pol, base_version=body.get("base_version") if isinstance(body, dict) else None)
        except ValueError as e:
            if str(e) == "conflict":
                return _json({"error": "conflict", "detail": "policy changed since you loaded it",
                              "version": store.version}, 409)
            raise
        return _json(store.snapshot())

    async def post_rules(request: web.Request):
        body = await request.json()
        raw_rules = body.get("rules") or []
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("rules must be a non-empty list")
        new, problems = [], []
        for i, rr in enumerate(raw_rules):
            try:
                rule = rule_from_dict(rr)
                rule.added_at = rule.added_at or now_iso()
                new.append(rule)
            except ValueError as e:
                pat = rr.get("pattern") if isinstance(rr, dict) else rr
                problems.append(f"{pat!r}: {e}")
        if problems:
            raise PolicyError(problems)
        pol = Policy.from_dict(store.policy.to_dict(), strict=False)
        added, updated = pol.upsert_rules(new)
        store.save(pol)
        return _json({"added": added, "updated": updated, **store.snapshot()})

    async def delete_rules(request: web.Request):
        body = await request.json() if request.can_read_body else {}
        pats = body.get("patterns") or ([request.query["pattern"]] if "pattern" in request.query else [])
        if not pats:
            raise ValueError("patterns required")
        pol = Policy.from_dict(store.policy.to_dict(), strict=False)
        removed = pol.remove_rules(pats)
        store.save(pol)
        return _json({"removed": removed, **store.snapshot()})

    async def decisions(request: web.Request):
        q = request.query
        allowed = {"true": True, "false": False}.get(q.get("allowed", "").lower())
        since = float(q["since"]) if q.get("since") else None
        items = dlog.query(limit=min(int(q.get("limit", 200)), 5000), allowed=allowed, host=q.get("host"),
                           since=since, action=q.get("action") or None)
        return _json({"items": items, "count": len(items)})

    async def stats(request: web.Request):
        hours = float(request.query.get("hours", 24))
        s = dlog.stats(hours=max(1.0, min(hours, 168.0)))
        s["mode"] = store.policy.mode
        s["rules"] = len(store.policy.rules)
        return _json(s)

    async def test(request: web.Request):
        body = await request.json()
        target = body.get("url") or body.get("host") or ""
        host, port, scheme = parse_target(target)
        if not host:
            raise ValueError("url/host required")
        d = store.policy.decide(host, port)
        out = {"url": target, "host": host, "port": port, "scheme": scheme, **d.to_dict()}
        if d.rule:
            out["rule_detail"] = d.rule.to_dict()
        out["explanation"] = (
            f"Allowed by rule {d.rule.pattern} ({d.rule.category})" if d.matched else
            ("Would be blocked (audit mode lets it through and logs would-block)" if d.allowed else
             ("Host matches a rule but port %d is not in its ports list" % port if d.reason == "port-not-allowed"
              else "Blocked: no rule matches this host")))
        return _json(out)

    async def trust(_):
        return _json(trust_info())

    async def tunnels(_):
        return _json({"tunnels": tunnels_status()})

    r.add_get("/health", health)
    r.add_get("/ca.pem", get_ca)
    r.add_get("/policy", get_policy)
    r.add_put("/policy", put_policy)
    r.add_post("/rules", post_rules)
    r.add_delete("/rules", delete_rules)
    r.add_get("/decisions", decisions)
    r.add_get("/stats", stats)
    r.add_post("/test", test)
    r.add_get("/trust", trust)
    r.add_get("/tunnels", tunnels)
    return app
