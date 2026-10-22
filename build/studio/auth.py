"""Sessions, roles, CSRF, login rate limiting and master-password step-up.

Session = itsdangerous-signed cookie `studio_session` (Secure, HttpOnly,
SameSite=Lax, path=/ so nginx can forward it to /studio/authz for every app).
Payload: {u: name, r: role (SSO only), s: local|sso, sid, e: epoch}. Roles are
resolved live from users.json (mtime-cached), so removing a user or changing
a role takes effect on the next request without a vault decrypt."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer

import settings
import users
from fsutil import write_file

COOKIE = "studio_session"


@dataclass
class User:
    name: str
    role: str  # viewer | analyst | modeler | admin


@dataclass
class Session:
    user: User
    source: str
    sid: str
    email: str = ""
    display: str = ""

    def identity_headers(self) -> dict[str, str]:
        """Headers /studio/authz returns for nginx to forward to apps."""
        domain = settings._env("STUDIO_LOCAL_EMAIL_DOMAIN", "local")
        email = self.email or (self.user.name if "@" in self.user.name else f"{self.user.name}@{domain}")
        name = self.display or self.user.name
        safe = lambda v: "".join(ch for ch in v if 32 <= ord(ch) < 127)[:256]  # noqa: E731 — header-safe
        return {"X-Auth-User": safe(self.user.name), "X-Auth-Email": safe(email),
                "X-Auth-Name": safe(name), "X-Auth-Role": self.user.role}


# ── signing key ──────────────────────────────────────────────────────────
_ser_lock = threading.Lock()
_ser: dict[str, object] = {}


def _secret() -> str:
    path: Path = settings.cfg.session_secret_file
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_hex(32)
    write_file(path, value + "\n", mode=0o600, uid=0, gid=0)
    return value


def serializer(purpose: str = "studio-session") -> URLSafeTimedSerializer:
    key = f"{settings.cfg.session_secret_file}:{purpose}"
    with _ser_lock:
        if key not in _ser:
            _ser[key] = URLSafeTimedSerializer(_secret(), salt=purpose)
        return _ser[key]  # type: ignore[return-value]


# ── sessions ─────────────────────────────────────────────────────────────
_revoked: dict[str, float] = {}
_step_up: dict[str, float] = {}


def _cookie_kwargs() -> dict:
    # host-only (no Domain) and path=/: browsers do not bind cookies to a
    # port, so the same session gates the facade and the dedicated ports
    # (e.g. Open WebUI's gated server) on the same host.
    return {"httponly": True, "secure": True, "samesite": "lax", "path": "/"}


def issue(response: Response, name: str, source: str, role: str = "", email: str = "", display: str = "") -> str:
    sid = secrets.token_urlsafe(16)
    epoch = (users.get(name) or {}).get("epoch", 0)
    payload = {"u": name, "r": role, "s": source, "sid": sid, "e": epoch}
    if email:
        payload["m"] = email
    if display:
        payload["n"] = display
    token = serializer().dumps(payload)
    response.set_cookie(COOKIE, token, max_age=settings.SESSION_TTL, **_cookie_kwargs())
    return sid


def clear(response: Response, session: Session | None) -> None:
    if session:
        _revoked[session.sid] = time.time() + settings.SESSION_TTL
        _step_up.pop(session.sid, None)
    response.delete_cookie(COOKIE, **_cookie_kwargs())


def read_session(request: Request) -> Session | None:
    cached = getattr(request.state, "studio_session", False)
    if cached is not False:
        return cached  # type: ignore[return-value]
    session = _decode(request.cookies.get(COOKIE, ""))
    request.state.studio_session = session
    return session


def _decode(raw: str) -> Session | None:
    if not raw:
        return None
    try:
        data = serializer().loads(raw, max_age=settings.SESSION_TTL)
    except BadSignature:
        return None
    sid, name, source = data.get("sid", ""), data.get("u", ""), data.get("s", "")
    if not sid or not name or sid in _revoked:
        return None
    stored = users.get(name)
    if source == "local":
        if not settings.cfg.local_login_allowed:
            return None
        if not stored or stored.get("source") != "local" or stored.get("epoch", 0) != data.get("e", 0):
            return None
        role = stored["role"]
    elif source == "sso":
        role = stored["role"] if stored and stored.get("source") == "sso" else data.get("r", "")
    else:
        return None
    if role not in settings.ROLE_RANK:
        return None
    email = data.get("m", "") or (stored or {}).get("email", "")
    return Session(User(name, role), source, sid, email=email, display=data.get("n", "") or (stored or {}).get("display", ""))


def has_role(user: User, minimum: str) -> bool:
    return settings.ROLE_RANK.get(user.role, 0) >= settings.ROLE_RANK.get(minimum, 99)


# ── step-up ──────────────────────────────────────────────────────────────
def mark_step_up(session: Session) -> None:
    _step_up[session.sid] = time.time()


def step_up_remaining(session: Session | None) -> int:
    if not session:
        return 0
    left = _step_up.get(session.sid, 0) + settings.STEP_UP_TTL - time.time()
    return max(0, int(left))


# ── rate limiting ────────────────────────────────────────────────────────
class RateLimiter:
    """Failure counter per key over a sliding window."""

    def __init__(self, max_failures: int, window: int) -> None:
        self.max, self.window = max_failures, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def retry_after(self, *keys: str) -> int:
        now = time.time()
        with self._lock:
            waits = [int(self.window - (now - hits[0])) + 1
                     for k in keys if len(hits := self._prune(k, now)) >= self.max]
        return max(waits, default=0)

    def fail(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for k in keys:
                self._hits.setdefault(k, []).append(now)

    def reset(self, *keys: str) -> None:
        with self._lock:
            for k in keys:
                self._hits.pop(k, None)


user_limiter = RateLimiter(5, 300)
ip_limiter = RateLimiter(20, 300)


def throttle_keys(request: Request, name: str = "") -> tuple[list[str], list[str]]:
    peer = request.client.host if request.client else "?"
    return ([f"u:{name.lower()}"] if name else []), [f"ip:{peer}"]


def check_throttle(request: Request, name: str = "") -> None:
    ukeys, ikeys = throttle_keys(request, name)
    wait = max(user_limiter.retry_after(*ukeys), ip_limiter.retry_after(*ikeys))
    if wait:
        raise HTTPException(429, f"Too many attempts — try again in {wait // 60 + 1} min",
                            headers={"Retry-After": str(wait)})


def record_failure(request: Request, name: str = "") -> None:
    ukeys, ikeys = throttle_keys(request, name)
    user_limiter.fail(*ukeys)
    ip_limiter.fail(*ikeys)


def record_success(request: Request, name: str = "") -> None:
    ukeys, _ = throttle_keys(request, name)
    user_limiter.reset(*ukeys)


# ── FastAPI dependencies ─────────────────────────────────────────────────
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def check_csrf(request: Request) -> None:
    if request.method not in SAFE_METHODS and request.headers.get("x-studio") != "1":
        raise HTTPException(403, "missing X-Studio header")


def require_user(request: Request) -> User:
    session = read_session(request)
    if not session:
        raise HTTPException(401, "Sign in required")
    check_csrf(request)
    return session.user


def require_role(minimum: str):
    def dependency(request: Request) -> User:
        user = require_user(request)
        if not has_role(user, minimum):
            raise HTTPException(403, f"Requires the {minimum} role")
        return user
    return dependency


def require_admin(request: Request) -> User:
    return require_role("admin")(request)


def require_step_up(request: Request) -> User:
    user = require_admin(request)
    from vault import auto_mode

    if auto_mode():          # no master passphrase to confirm: an admin session is enough
        return user
    if not step_up_remaining(read_session(request)):
        raise HTTPException(428, {"step_up": True, "message": "Confirm your master password"})
    return user
