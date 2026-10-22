"""Sign-in, first-run setup, OIDC, vault unlock/lock, step-up, `/studio/authz`."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from itsdangerous import BadSignature
from pydantic import BaseModel

import audit
import auth
import catalog
import dockerops
import oidc
import pages
import settings
import users
from cryptobox import DecryptError
from orchestrator import UP
from vault import auto_mode, MIN_MASTER_LEN, env_password, store

router = APIRouter()


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _audit(request: Request, event: str, detail: str = "", user: str = "") -> None:
    if not user and (s := auth.read_session(request)):
        user = s.user.name
    audit.record(event, detail, user=user, peer=_peer(request))


def safe_next(nxt: str | None) -> str:
    """Only same-origin absolute paths (facade-level, e.g. /superset/)."""
    if not nxt or not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt or "://" in nxt:
        return "/studio/"
    if nxt.startswith("/studio/login") or nxt.startswith("/studio/setup"):
        return "/studio/"
    return nxt


def setup_needed() -> bool:
    return settings.cfg.local_login_allowed and not users.any_local()


def auth_state() -> dict[str, Any]:
    c = settings.cfg
    return {"setup_needed": setup_needed(), "local_login": c.local_login_allowed, "sso": c.oidc_enabled,
            "env": c.appliance_env, "vault": store.status(), "master_from_env": bool(env_password()),
            "legacy_vault": store.legacy_present(), "break_glass": c.break_glass and c.appliance_env != "dev",
            "min_master": MIN_MASTER_LEN, "min_password": users.MIN_PASSWORD,
            "appliance": appliance_info()}


def appliance_info() -> dict[str, str]:
    c = settings.cfg
    return {"id": c.appliance, "persona": c.appliance_persona, "name": settings.appliance_name(),
            "title": f"DataPlatform {settings.appliance_name()} Appliance"}


@router.get("/studio/api/auth/state")
def get_state() -> dict[str, Any]:
    return auth_state()


# ── setup / login / logout ───────────────────────────────────────────────
class SetupBody(BaseModel):
    username: str
    password: str
    master: str = ""


@router.post("/studio/api/auth/setup")
def setup(body: SetupBody, request: Request, response: Response) -> dict[str, Any]:
    auth.check_csrf(request)
    auth.check_throttle(request)
    if not setup_needed():
        raise HTTPException(409, "Studio is already set up — sign in instead")
    master = env_password() or body.master
    if not store.is_unlocked():
        try:
            notes = store.unlock(master) if store.exists() else store.initialize(master)
        except DecryptError:
            auth.record_failure(request)
            raise HTTPException(400, "That passphrase does not open the existing vault") from None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        notes = []
    try:
        users.add(body.username.strip(), "admin", body.password)
    except users.UserError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth.issue(response, body.username.strip(), "local")
    _audit(request, "auth.setup", f"admin {body.username.strip()} created; vault {store.status()}", body.username.strip())
    return {"next": "/studio/#compose", "notes": notes}


class LoginBody(BaseModel):
    username: str
    password: str
    next: str = ""


@router.post("/studio/api/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    auth.check_csrf(request)
    if not settings.cfg.local_login_allowed:
        raise HTTPException(403, "Password sign-in is disabled in this environment — use single sign-on")
    name = body.username.strip()
    auth.check_throttle(request, name)
    user = users.authenticate(name, body.password)
    if not user:
        auth.record_failure(request, name)
        _audit(request, "auth.login_failed", name, name)
        # 400, not 401: BSP.api treats 401 as "session expired" and redirects
        raise HTTPException(400, "Wrong username or password")
    auth.record_success(request, name)
    auth.issue(response, name, "local")
    users.touch_login(name)
    _audit(request, "auth.login", "local" + (" (break-glass)" if settings.cfg.appliance_env != "dev" else ""), name)
    return {"next": safe_next(body.next)}


@router.post("/studio/api/auth/logout")
def logout(request: Request, response: Response) -> dict[str, str]:
    auth.check_csrf(request)
    session = auth.read_session(request)
    if session:
        _audit(request, "auth.logout")
    auth.clear(response, session)
    return {"next": "/studio/login"}


@router.get("/studio/logout")
def logout_link(request: Request) -> RedirectResponse:
    resp = RedirectResponse("/studio/login", status_code=303)
    session = auth.read_session(request)
    if session:
        _audit(request, "auth.logout")
    auth.clear(resp, session)
    return resp


# ── OIDC ─────────────────────────────────────────────────────────────────
OIDC_COOKIE = "studio_oidc"
# The state cookie is host-wide (path=/) so it also reaches a loopback callback
# such as http://localhost:31211/auth/callback (Entra ignores the port of
# localhost redirect URIs — see docs/platform/sso.md). Host-only, HttpOnly,
# SameSite=Lax and short-lived as before.
OIDC_COOKIE_PATH = "/"


def _facade_base() -> str:
    """Absolute facade origin when the IdP calls back on another origin (the
    loopback http callback); empty when the callback is the facade itself."""
    from urllib.parse import urlparse

    if urlparse(settings.cfg.oidc_redirect_uri).path.startswith("/studio/"):
        return ""          # the facade's own callback: stay relative
    return f"https://{settings.cfg.facade_host}:{settings.cfg.facade_port}"


@router.get("/studio/oidc/login")
def oidc_login(next: str = "") -> RedirectResponse:
    if not settings.cfg.oidc_enabled:
        raise HTTPException(404, "Single sign-on is not configured")
    state, nonce = oidc.new_flow()
    resp = RedirectResponse(oidc.authorize_url(state, nonce), status_code=302)
    token = auth.serializer("studio-oidc").dumps({"state": state, "nonce": nonce, "next": safe_next(next)})
    resp.set_cookie(OIDC_COOKIE, token, max_age=oidc.STATE_TTL, httponly=True, secure=True,
                    samesite="lax", path=OIDC_COOKIE_PATH)
    return resp


@router.get("/studio/oidc/callback")
def oidc_callback(request: Request, code: str = "", state: str = "", error: str = "",
                  error_description: str = "") -> RedirectResponse:
    def fail(msg: str) -> RedirectResponse:
        _audit(request, "auth.sso_failed", msg[:200])
        return RedirectResponse(f"{_facade_base()}/studio/login?error={quote(msg[:200])}", status_code=303)

    if error:
        return fail(error_description or error)
    try:
        flow = auth.serializer("studio-oidc").loads(request.cookies.get(OIDC_COOKIE, ""), max_age=oidc.STATE_TTL)
    except BadSignature:
        return fail("Sign-in session expired — please try again")
    if not state or state != flow.get("state"):
        return fail("Sign-in state mismatch — please try again")
    try:
        tokens = oidc.exchange_code(code)
        claims = oidc.validate_id_token(tokens.get("id_token", ""), flow["nonce"], tokens.get("access_token"))
        name, role = oidc.identity(claims)
        email = str(claims.get("email") or claims.get("upn") or (name if "@" in name else ""))
        display = str(claims.get("name") or "")
    except (oidc.OIDCError, OSError, ValueError) as exc:
        return fail(str(exc))
    try:
        import delegated
        if delegated.save(name, tokens):
            _audit(request, "auth.graph_connected", f"delegated scopes: {tokens.get('scope', '')}"[:200], name)
    except Exception as exc:  # noqa: BLE001 — never block sign-in on this
        _audit(request, "auth.graph_connect_failed", str(exc)[:200], name)
    resp = RedirectResponse(_facade_base() + (flow.get("next") or "/studio/"), status_code=303)
    resp.delete_cookie(OIDC_COOKIE, path=OIDC_COOKIE_PATH)
    auth.issue(resp, name, "sso", role, email=email, display=display)
    _audit(request, "auth.login", f"sso role={role}", name)
    return resp


# ── vault ────────────────────────────────────────────────────────────────
class PasswordBody(BaseModel):
    password: str


class PassphraseChangeBody(BaseModel):
    current: str
    new: str


@router.post("/studio/api/vault/passphrase")
def change_passphrase(body: PassphraseChangeBody, request: Request,
                      user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Re-encrypt the vault under a new master passphrase. Every stored secret
    is kept; only the key that wraps them changes."""
    auth.check_csrf(request)
    auth.check_throttle(request, f"vault:{user.name}")
    if not store.is_unlocked():
        raise HTTPException(423, "Unlock the vault first")
    if not store.verify(body.current):
        auth.record_failure(request, f"vault:{user.name}")
        _audit(request, "vault.passphrase_change_failed")
        raise HTTPException(400, "The current master passphrase is not correct")
    if body.new == body.current:
        raise HTTPException(400, "The new passphrase is the same as the current one")
    try:
        store.change_password(body.new)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit(request, "vault.passphrase_changed")
    return {"vault": store.status()}


@router.post("/studio/api/vault/unlock")
def unlock(body: PasswordBody, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    auth.check_throttle(request, f"vault:{user.name}")
    if not store.exists():
        raise HTTPException(409, "The vault has not been created yet")
    if store.is_unlocked():
        return {"vault": "unlocked", "notes": []}
    try:
        notes = store.unlock(body.password)
    except DecryptError:
        auth.record_failure(request, f"vault:{user.name}")
        _audit(request, "vault.unlock_failed")
        raise HTTPException(400, "Wrong master password") from None
    auth.mark_step_up(auth.read_session(request))  # type: ignore[arg-type]
    _audit(request, "vault.unlock", "; ".join(notes))
    return {"vault": "unlocked", "notes": notes}


@router.post("/studio/api/vault/init")
def init_vault(body: PasswordBody, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if store.exists():
        raise HTTPException(409, "The vault already exists — unlock it instead")
    try:
        notes = store.initialize(body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    _audit(request, "vault.init", "; ".join(notes))
    return {"vault": "unlocked", "notes": notes}


@router.post("/studio/api/vault/lock")
def lock(request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    store.lock()
    _audit(request, "vault.lock")
    return {"vault": store.status()}


@router.post("/studio/api/auth/step-up")
def step_up(body: PasswordBody, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    auth.check_throttle(request, f"vault:{user.name}")
    if not store.verify(body.password):
        auth.record_failure(request, f"vault:{user.name}")
        _audit(request, "auth.step_up_failed")
        raise HTTPException(400, "Wrong master password")
    session = auth.read_session(request)
    assert session is not None
    if not store.is_unlocked():
        store.unlock(body.password)
    auth.mark_step_up(session)
    _audit(request, "auth.step_up")
    return {"step_up_seconds": auth.step_up_remaining(session), "vault": store.status()}


# ── nginx auth_request ───────────────────────────────────────────────────
@router.get("/studio/authz")
def authz(request: Request) -> Response:
    session = auth.read_session(request)
    if not session:
        return Response(status_code=401)
    required = request.headers.get("x-required-role", "").strip().lower()
    if required and not auth.has_role(session.user, required):
        return Response(status_code=403)
    return Response(status_code=204, headers=session.identity_headers())


@router.get("/studio/api/me")
def me(request: Request, user: auth.User = Depends(auth.require_user)) -> JSONResponse:
    session = auth.read_session(request)
    status = dockerops.status()
    return JSONResponse({
        "user": {"name": user.name, "role": user.role, "source": session.source if session else ""},
        "env": settings.cfg.appliance_env, "vault": store.status(), "vault_source": store.source, "vault_auto": auto_mode(),
        "appliance": appliance_info(),
        "step_up_seconds": auth.step_up_remaining(session), "pages": pages.nav(user),
        "plugin_errors": pages.plugin_errors(user), "facade_url": settings.facade_url(),
        "running": sum(1 for s in catalog.load()["services"] if status.get(s, {}).get("state") in UP),
    }, headers={"Cache-Control": "no-store"})



# ── internal: delegated Graph tokens for in-network services (MCP) ───────
# nginx blocks /studio/internal/ (routes.d/platform.conf); callers must also
# present the MCP bearer token from the vault.
class GraphTokenBody(BaseModel):
    user: str


@router.post("/studio/internal/graph-token")
def internal_graph_token(body: GraphTokenBody, request: Request) -> dict[str, Any]:
    import hmac

    import delegated
    expected = store.get("services.mcp.MCP_TOKEN") if store.is_unlocked() else None
    got = request.headers.get("authorization", "")
    if not expected or not got.startswith("Bearer ") or not hmac.compare_digest(got[7:].strip(), expected):
        raise HTTPException(401, "unauthorized")
    if request.headers.get("x-forwarded-for"):
        raise HTTPException(404, "not found")          # came through nginx: never serve
    try:
        return delegated.access_token(body.user)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None


@router.get("/studio/api/me/graph")
def my_graph_status(user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    import delegated
    return {"enabled": bool(settings.cfg.oidc_graph_scopes), **delegated.status(user.name)}


@router.delete("/studio/api/me/graph")
def my_graph_disconnect(request: Request, user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    import delegated
    delegated.forget(user.name)
    _audit(request, "auth.graph_disconnected", "", user.name)
    return {"connected": False}


# ── internal: principal switch — short-lived Google tokens for services ──
# POST /studio/internal/gcp-token {identity, user, scopes?, consumer?, session? | grant?}
# Same guard as graph-token: nginx never routes /studio/internal/, requests
# that carry X-Forwarded-For are refused, and the caller presents its own
# service token from the vault. Then the USER must be on the identity's
# access list (gcp_identities.may_act). A service token alone never names a
# user: some of them are reachable from user code (the workspace container runs
# JupyterLab and also carries MCP_TOKEN), so every request must also PROVE the
# user — the user's Studio session cookie (interactive requests), a grant
# Studio signed earlier on a session-proven request (canvas schedules), or,
# for the MCP server, the harness's HMAC caller signature (X-Caller / -Ts /
# -Sig with MCP_DELEGATION_KEY), which Studio re-verifies from the vault.
GCP_TOKEN_CALLERS = {           # service → vault key of its token
    "mcp": "services.mcp.MCP_TOKEN",
    "appliance": "services.appliance.WB_STUDIO_TOKEN",
    "canvas": "services.canvas.CANVAS_STUDIO_TOKEN",
}
CALLER_SKEW = 300


class GcpTokenBody(BaseModel):
    identity: str
    user: str
    scopes: list[str] | str | None = None
    consumer: str = ""
    session: str = ""
    grant: str = ""
    caller_ts: str = ""
    caller_sig: str = ""


def _internal_caller(request: Request) -> str:
    import hmac

    if request.headers.get("x-forwarded-for"):
        raise HTTPException(404, "not found")          # came through nginx: never serve
    got = request.headers.get("authorization", "")
    if not store.is_unlocked() or not got.startswith("Bearer "):
        raise HTTPException(401, "unauthorized")
    tok = got[7:].strip()
    for svc, key in GCP_TOKEN_CALLERS.items():
        expected = store.get(key)
        if expected and hmac.compare_digest(tok, expected):
            return svc
    raise HTTPException(401, "unauthorized")


def _proven_user(svc: str, body: GcpTokenBody, ident_id: str) -> tuple[str, str, str, str]:
    """(user, role, email, how) after the caller's proof rules; 403 otherwise."""
    import gcp_identities as gi

    user = body.user.strip()
    if not user:
        raise HTTPException(403, "no user — a named identity is only used for a signed-in user")
    if body.session:
        s = auth._decode(body.session)
        if not s or s.user.name.lower() != user.lower():
            raise HTTPException(403, "the user's Studio session is not valid — sign in again")
        return s.user.name, s.user.role, s.email, "session"
    if body.grant:
        g = gi.read_grant(body.grant, svc)
        if not g or g[0] != user.lower() or g[1] != ident_id:
            raise HTTPException(403, "the saved identity grant is not valid — save the connection again")
        return user, gi.user_role(user), "", "grant"
    if svc == "mcp" and body.caller_sig:
        import hashlib
        import hmac
        import time as _time

        key = store.get("services.mcp.MCP_DELEGATION_KEY") or ""
        ts = body.caller_ts
        if key and ts.isdigit() and abs(_time.time() - int(ts)) <= CALLER_SKEW:
            good = hmac.new(key.encode(), f"{user.lower()}|{ts}".encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(good, body.caller_sig):
                return user, gi.user_role(user), "", "harness-signed"
        raise HTTPException(403, "the caller signature is not valid")
    raise HTTPException(403, "prove the user: a Studio session, a saved grant, or a signed caller")


def _resolve_identity(name: str) -> str:
    import gcp_identities as gi

    ids = gi.identities()
    if name in ids:
        return name
    for i, r in ids.items():
        if r.get("name", "").lower() == name.strip().lower():
            return i
    raise HTTPException(404, f"no cloud identity {name!r}")


@router.post("/studio/internal/gcp-token")
def internal_gcp_token(body: GcpTokenBody, request: Request) -> dict[str, Any]:
    import gcp_identities as gi

    svc = _internal_caller(request)
    ident_id = _resolve_identity(body.identity)
    user, role, email, how = _proven_user(svc, body, ident_id)
    rec = gi.identities()[ident_id]
    consumer = "".join(ch for ch in body.consumer if ch.isalnum() or ch in "_-")[:40]
    if not gi.may_act(rec, user, role, email):
        audit.record("gcp_token.denied", f"{ident_id} for {user} via {svc}{'/' + consumer if consumer else ''}",
                     user=user, peer=_peer(request))
        raise HTTPException(403, f"{user} may not act as {rec.get('name', ident_id)} — ask a Studio admin to add "
                                 "you to its access list (Studio → Cloud identities)")
    try:
        out = gi.mint(ident_id, body.scopes)
    except gi.IdentityError as exc:
        raise HTTPException(400, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(502, str(exc)) from None
    audit.record("gcp_token.issued", f"{ident_id} for {user} via {svc}{'/' + consumer if consumer else ''} ({how})",
                 user=user, peer=_peer(request))
    return out


@router.post("/studio/internal/gcp-grant")
def internal_gcp_grant(body: GcpTokenBody, request: Request) -> dict[str, Any]:
    """A grant for unattended use (scheduled canvas runs): needs the user's
    session and the access list; returns no Google secret."""
    import gcp_identities as gi

    svc = _internal_caller(request)
    if not body.session:
        raise HTTPException(403, "a grant needs the user's Studio session")
    ident_id = _resolve_identity(body.identity)
    user, role, email, _how = _proven_user(svc, body, ident_id)
    rec = gi.identities()[ident_id]
    if not gi.may_act(rec, user, role, email):
        audit.record("gcp_grant.denied", f"{ident_id} for {user} via {svc}", user=user, peer=_peer(request))
        raise HTTPException(403, f"{user} may not act as {rec.get('name', ident_id)}")
    audit.record("gcp_grant.issued", f"{ident_id} for {user} via {svc}", user=user, peer=_peer(request))
    return {"grant": gi.issue_grant(user, ident_id, svc), "identity": ident_id, "user": user,
            "principal_email": rec.get("principal", "")}
