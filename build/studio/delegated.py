"""Delegated Microsoft Graph tokens for signed-in SSO users.

When OIDC_GRAPH_SCOPES is set, the SSO sign-in also asks Entra for delegated
Graph scopes. The refresh token is kept in the vault (encrypted at rest), per
user, and never sent to a browser. In-network services (the MCP server) ask
for a short-lived access token for a named user through an INTERNAL endpoint
authenticated with the MCP bearer token; nginx never routes it.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import requests

import settings
from vault import store

VAULT_KEY = "delegated"           # data["delegated"][<user>] = {...}
_cache: dict[str, tuple[str, float]] = {}
_lock = threading.Lock()


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def save(user: str, tokens: dict[str, Any]) -> bool:
    """Keep the refresh token from an SSO code exchange; True when stored."""
    rt = tokens.get("refresh_token")
    if not rt or not settings.cfg.oidc_graph_scopes:
        return False
    granted = tokens.get("scope") or settings.cfg.oidc_graph_scopes
    user = user.lower()

    def write(data: dict[str, Any]) -> None:
        data.setdefault(VAULT_KEY, {})[user] = {"refresh_token": rt, "scopes": granted, "updated": _now()}

    store.mutate(write)
    if tokens.get("access_token"):
        _cache[user] = (tokens["access_token"], time.time() + int(tokens.get("expires_in") or 3000) - 120)
    return True


def status(user: str) -> dict[str, Any]:
    rec = (store.snapshot().get(VAULT_KEY) or {}).get(user.lower())
    return {"connected": bool(rec), "scopes": (rec or {}).get("scopes", ""), "updated": (rec or {}).get("updated")}


def forget(user: str) -> None:
    def drop(data: dict[str, Any]) -> None:
        (data.get(VAULT_KEY) or {}).pop(user.lower(), None)
    store.mutate(drop)
    _cache.pop(user.lower(), None)


def access_token(user: str) -> dict[str, Any]:
    """A fresh delegated Graph access token for `user` (refresh-token grant;
    the rotated refresh token is saved back). Raises LookupError when the user
    has not connected, PermissionError when Entra refuses (consent revoked …)."""
    user = user.lower()
    with _lock:
        hit = _cache.get(user)
        if hit and hit[1] > time.time():
            return {"access_token": hit[0], "expires_in": int(hit[1] - time.time())}
        rec = (store.snapshot().get(VAULT_KEY) or {}).get(user)
        if not rec:
            raise LookupError(f"{user} has not connected Microsoft 365 (sign in with Microsoft again)")
        c = settings.cfg
        r = requests.post(f"https://login.microsoftonline.com/{c.oidc_tenant}/oauth2/v2.0/token", timeout=20, data={
            "client_id": c.oidc_client_id, "client_secret": c.oidc_client_secret, "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"], "scope": rec.get("scopes") or c.oidc_graph_scopes,
        })
        if r.status_code != 200:
            raise PermissionError(f"Entra refused to refresh the token ({r.status_code}): {r.text[:200]}")
        tok = r.json()
        if tok.get("refresh_token") and tok["refresh_token"] != rec["refresh_token"]:
            def write(data: dict[str, Any]) -> None:
                data.setdefault(VAULT_KEY, {})[user] = {**rec, "refresh_token": tok["refresh_token"], "updated": _now()}
            store.mutate(write)
        exp = time.time() + int(tok.get("expires_in") or 3000) - 120
        _cache[user] = (tok["access_token"], exp)
        return {"access_token": tok["access_token"], "expires_in": int(exp - time.time())}
