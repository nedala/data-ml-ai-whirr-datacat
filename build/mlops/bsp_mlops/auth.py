"""Who is calling — shared by the model gateway (/serve) and the control tower (/mlops).

Two kinds of caller are accepted:

  people     signed in through Studio (Entra ID SSO or a local account). nginx runs the
             Studio gate (`auth_request /_authz`) and overwrites X-Auth-User / X-Auth-Role
             (viewer | analyst | modeler | admin) on every proxied request. Set
             MLOPS_TRUSTED_PEERS=nginx to accept those headers only from nginx (see
             trusted_peer() for the uvicorn proxy-header caveat).
  services   bearer tokens (`Authorization: Bearer <token>`). The appliance keeps one
             service token for its own callers (tower, simulator, notebooks, retrain
             orchestrator) plus named client tokens issued by an admin; only sha256 digests
             are stored (`<data root>/mlops/secrets/gateway_tokens.json`, mode 0600). The
             plaintext service token lives next to it (0600) so internal callers that share
             the mlops data mount can read it. This file store is the in-appliance stand-in
             for GCP Secret Manager (see docs/ml-scientist/capability-evidence.md).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

SECRETS_DIR = config.MLOPS_DIR / "secrets"
SERVICE_TOKEN_FILE = SECRETS_DIR / "gateway_service.token"
TOKENS_FILE = SECRETS_DIR / "gateway_tokens.json"
ROLE_RANK = {"viewer": 1, "analyst": 2, "modeler": 3, "admin": 4}
_lock = threading.Lock()
_facade = {"ips": set(), "at": 0.0}


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _write_private(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p.parent, 0o700)
    except OSError:
        pass
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, p)


def service_token(create: bool = False) -> str | None:
    """The appliance's own service token (created by the gateway on first start)."""
    env = os.environ.get("GATEWAY_SERVICE_TOKEN", "").strip()
    if env:
        return env
    try:
        return SERVICE_TOKEN_FILE.read_text().strip() or None
    except OSError:
        pass
    if not create:
        return None
    with _lock:
        if SERVICE_TOKEN_FILE.exists():
            return SERVICE_TOKEN_FILE.read_text().strip()
        tok = secrets.token_urlsafe(32)
        _write_private(SERVICE_TOKEN_FILE, tok)
        reg = _load()
        reg["service"] = {"sha256": _sha(tok), "created_at": datetime.now(timezone.utc).isoformat(), "created_by": "gateway",
                          "scope": "service"}
        _save(reg)
        return tok


def _load() -> dict:
    try:
        return json.loads(TOKENS_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save(reg: dict) -> None:
    _write_private(TOKENS_FILE, json.dumps(reg, indent=2))


def ensure_service_token() -> None:
    tok = service_token(create=True)
    reg = _load()
    if tok and reg.get("service", {}).get("sha256") != _sha(tok):
        reg["service"] = {"sha256": _sha(tok), "created_at": datetime.now(timezone.utc).isoformat(), "created_by": "gateway",
                          "scope": "service"}
        _save(reg)


def verify_token(token: str) -> str | None:
    """→ the token's name ('service' or a client name), or None."""
    if not token:
        return None
    d = _sha(token)
    for name, t in _load().items():
        if secrets.compare_digest(t.get("sha256", ""), d) and not t.get("revoked"):
            return name
    return None


def issue_token(name: str, created_by: str, scope: str = "score") -> str:
    if not name.replace("-", "").replace("_", "").isalnum() or name == "service":
        raise ValueError("token name: letters, digits, - and _ only (and not 'service')")
    tok = "bsp_" + secrets.token_urlsafe(32)
    with _lock:
        reg = _load()
        reg[name] = {"sha256": _sha(tok), "created_at": datetime.now(timezone.utc).isoformat(), "created_by": created_by,
                     "scope": scope}
        _save(reg)
    return tok


def revoke_token(name: str, by: str) -> bool:
    with _lock:
        reg = _load()
        if name not in reg or name == "service":
            return False
        reg[name].update(revoked=True, revoked_by=by, revoked_at=datetime.now(timezone.utc).isoformat())
        _save(reg)
        return True


def list_tokens() -> list[dict]:
    return [{"name": k, **{x: v.get(x) for x in ("created_at", "created_by", "scope", "revoked", "revoked_at")},
             "sha256_prefix": v.get("sha256", "")[:12]} for k, v in _load().items()]


def client_headers() -> dict:
    """Headers an internal caller sends to the gateway."""
    tok = service_token()
    return {"X-Studio": "1", **({"Authorization": f"Bearer {tok}"} if tok else {})}


# ── people (Studio gate via nginx) ────────────────────────────────────────
def trusted_peer(client_host: str | None) -> bool:
    """X-Auth-* headers are trusted from any peer unless MLOPS_TRUSTED_PEERS
    names the peers allowed to send them (e.g. `nginx`). The services run
    uvicorn with --proxy-headers --forwarded-allow-ips '*', which rewrites the
    peer address from X-Forwarded-For, so the peer check only becomes
    meaningful once forwarded-allow-ips is narrowed to nginx — see
    docs/ml-scientist/capability-evidence.md (G · identity assertion)."""
    peers = [p.strip() for p in os.environ.get("MLOPS_TRUSTED_PEERS", "").split(",") if p.strip()]
    if not peers:
        return True
    if time.time() - _facade["at"] > 60:
        ips: set[str] = set()
        for p in peers:
            try:
                ips |= set(socket.gethostbyname_ex(p)[2])
            except OSError:
                ips.add(p)
        _facade["ips"], _facade["at"] = ips, time.time()
    return bool(client_host) and client_host in _facade["ips"]


def identity(request) -> dict:
    """{user, role, source}: source = sso-gate (Studio gate headers set by nginx),
    token (bearer token), or anonymous."""
    host = request.client.host if request.client else None
    user = (request.headers.get("x-auth-user") or "").strip()
    if user and trusted_peer(host):
        return {"user": user, "role": (request.headers.get("x-auth-role") or "").strip() or "analyst", "source": "sso-gate"}
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        name = verify_token(auth[7:].strip())
        if name:
            return {"user": f"token:{name}", "role": "service" if name == "service" else "client", "source": "token"}
    return {"user": None, "role": None, "source": "anonymous"}


def has_role(role: str | None, minimum: str) -> bool:
    return ROLE_RANK.get(role or "", 0) >= ROLE_RANK.get(minimum, 99)
