"""Entra ID (OIDC authorization-code flow) with full id_token validation:
RS256 signature against the tenant JWKS, issuer, audience, expiry, nonce and
at_hash. The v1 wizard skipped signature verification — never again."""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from typing import Any

import requests
from jose import jwt
from jose.exceptions import JOSEError

import settings

AUTHORITY = "https://login.microsoftonline.com"
JWKS_TTL = 3600
STATE_TTL = 600


class OIDCError(RuntimeError):
    pass


_jwks_lock = threading.Lock()
_jwks: dict[str, Any] = {"keys": [], "fetched": 0.0}


def _tenant_url(path: str) -> str:
    return f"{AUTHORITY}/{settings.cfg.oidc_tenant}/{path}"


def issuer() -> str:
    return _tenant_url("v2.0")


def scopes() -> str:
    """Identity scopes, plus delegated Microsoft Graph scopes when OIDC_GRAPH_SCOPES is
    set (e.g. "User.Read Mail.Read Calendars.Read Files.Read.All Chat.Read
    offline_access"). Delegated scopes act AS the signed-in user, with only their
    own access; whether a user may consent depends on the tenant's consent policy."""
    extra = settings.cfg.oidc_graph_scopes
    return "openid profile email" + (f" {extra}" if extra else "")


def authorize_url(state: str, nonce: str) -> str:
    c = settings.cfg
    q = urllib.parse.urlencode({
        "client_id": c.oidc_client_id, "response_type": "code", "redirect_uri": c.oidc_redirect_uri,
        "response_mode": "query", "scope": scopes(), "state": state, "nonce": nonce,
    })
    return f"{_tenant_url('oauth2/v2.0/authorize')}?{q}"


def new_flow() -> tuple[str, str]:
    return secrets.token_urlsafe(24), secrets.token_urlsafe(24)


def exchange_code(code: str) -> dict[str, Any]:
    c = settings.cfg
    r = requests.post(_tenant_url("oauth2/v2.0/token"), timeout=20, data={
        "client_id": c.oidc_client_id, "client_secret": c.oidc_client_secret, "code": code,
        "redirect_uri": c.oidc_redirect_uri, "grant_type": "authorization_code",
        "scope": scopes(),
    })
    if r.status_code != 200:
        raise OIDCError(f"token endpoint returned {r.status_code}: {r.text[:200]}")
    return r.json()


def fetch_jwks() -> list[dict[str, Any]]:
    r = requests.get(_tenant_url("discovery/v2.0/keys"), timeout=15)
    r.raise_for_status()
    return r.json().get("keys", [])


def _signing_key(kid: str) -> dict[str, Any]:
    with _jwks_lock:
        for attempt in range(2):
            stale = time.time() - _jwks["fetched"] > JWKS_TTL
            if attempt or stale or not _jwks["keys"]:
                _jwks.update(keys=fetch_jwks(), fetched=time.time())
            for key in _jwks["keys"]:
                if key.get("kid") == kid:
                    return key
    raise OIDCError("id_token signed with an unknown key")


def validate_id_token(id_token: str, nonce: str, access_token: str | None = None) -> dict[str, Any]:
    try:
        header = jwt.get_unverified_header(id_token)
        if header.get("alg") != "RS256":
            raise OIDCError(f"unexpected id_token alg {header.get('alg')!r}")
        claims = jwt.decode(id_token, _signing_key(header.get("kid", "")), algorithms=["RS256"],
                            audience=settings.cfg.oidc_client_id, issuer=issuer(),
                            access_token=access_token)
    except JOSEError as exc:
        raise OIDCError(f"id_token rejected: {exc}") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OIDCError("id_token nonce mismatch")
    return claims


def identity(claims: dict[str, Any]) -> tuple[str, str]:
    """(username, role) from validated claims."""
    name = claims.get("preferred_username") or claims.get("upn") or claims.get("email") or claims.get("oid")
    if not name:
        raise OIDCError("id_token carries no usable username claim")
    name = str(name).lower()
    group = settings.cfg.oidc_admin_group
    member_of = set(claims.get("groups") or []) | set(claims.get("roles") or [])
    emails = {str(claims.get(k, "")).lower() for k in ("preferred_username", "upn", "email")} - {""}
    is_admin = (bool(group) and group in member_of) or bool(emails & settings.cfg.oidc_admin_users)
    return name, "admin" if is_admin else "analyst"
