"""Act as the signed-in user on Microsoft Graph (delegated access).

The AI harness sends "acting as <user>" with an HMAC signature
(X-Caller / X-Caller-Ts / X-Caller-Sig, key MCP_DELEGATION_KEY — shared by the
harness and this server only). With a valid signature we ask Studio's
internal endpoint for that user's short-lived Graph token (Studio holds the
refresh token from their SSO sign-in, encrypted in the vault) and call Graph
`/me/...` — the user's own mail, calendar, files and chats, with exactly their
own permissions. No signature, no delegated token: tools fall back to the
app-level connector (mock or live) and its mailbox allow-list.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

import httpx

from .common import UserError, env

GRAPH = "https://graph.microsoft.com/v1.0"
MAX_SKEW = 300
_cache: dict[str, tuple[str, float]] = {}


def caller() -> str:
    """The verified user this request acts for, or "" when unsigned / invalid."""
    key = env("MCP_DELEGATION_KEY")
    if not key:
        return ""
    try:
        from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

        h = {k.lower(): v for k, v in (get_http_headers(include_all=True) or {}).items()}
    except Exception:  # noqa: BLE001
        return ""
    user, ts, sig = (h.get("x-caller") or "").lower(), h.get("x-caller-ts") or "", h.get("x-caller-sig") or ""
    if not (user and ts.isdigit() and sig) or abs(time.time() - int(ts)) > MAX_SKEW:
        return ""
    good = hmac.new(key.encode(), f"{user}|{ts}".encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(good, sig) else ""


def token(user: str) -> str | None:
    """Delegated Graph access token for `user`, or None if they have not connected."""
    hit = _cache.get(user)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        r = httpx.post(env("STUDIO_INTERNAL_URL", "http://studio:8010") + "/studio/internal/graph-token",
                       json={"user": user}, headers={"Authorization": f"Bearer {env('MCP_TOKEN')}"},
                       timeout=20, trust_env=False)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    _cache[user] = (d["access_token"], time.time() + int(d.get("expires_in") or 600) - 60)
    return d["access_token"]


def active() -> tuple[str, str] | None:
    """(user, token) when this request can act as a connected user."""
    u = caller()
    t = token(u) if u else None
    return (u, t) if u and t else None


def wants_self(mailbox: str | None, user: str) -> bool:
    return (mailbox or "").strip().lower() in ("", "me", "my", "mine", "self", user)


def graph(tok: str, method: str, path: str, params: dict | None = None, json: Any = None,
          limit: int | None = None) -> Any:
    """Call Graph as the user; with `limit`, follow @odata.nextLink and return items."""
    headers = {"Authorization": f"Bearer {tok}", "ConsistencyLevel": "eventual"}
    url, items = GRAPH + path, []
    with httpx.Client(timeout=30) as c:     # egress via px-proxy (HTTPS_PROXY env)
        while url:
            r = c.request(method, url, params=params, json=json, headers=headers)
            if r.status_code == 403:
                raise UserError("Microsoft 365 refused this for your account (403): the delegated permission for it "
                                "may not be granted — sign in with Microsoft again, or ask IT to allow it")
            if r.status_code == 401:
                _cache.clear()
                raise UserError("your Microsoft 365 session expired — sign in with Microsoft again")
            if r.status_code >= 400:
                raise UserError(f"Microsoft Graph {r.status_code}: {r.text[:200]}")
            data = r.json() if r.content else {}
            if limit is None:
                return data
            items += data.get("value", [])
            url = data.get("@odata.nextLink") if len(items) < limit else None
            params = None
    return items[:limit]
