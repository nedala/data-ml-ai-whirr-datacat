"""Studio users: data/vault/users.json (0600). Local users carry an scrypt
hash; SSO users (source "sso") are stored only to pin a role that overrides
the group mapping. `epoch` is bumped on password reset so outstanding
sessions die with it; removing a user kills them too."""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import os
import re
import threading
from pathlib import Path
from typing import Any

import settings
from cryptobox import b64d, b64e
from fsutil import read_json, write_private_json

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@-]{1,63}$")
MIN_PASSWORD = 10
_N, _R, _P = 2 ** 14, 8, 1

_lock = threading.RLock()
_cache: dict[str, Any] = {"mtime": None, "users": {}}


class UserError(ValueError):
    pass


def _file() -> Path:
    return settings.cfg.vault_dir / "users.json"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${b64e(salt)}${b64e(digest)}"


def check_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=b64d(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(b64d(digest)))
        return hmac.compare_digest(got, b64d(digest))
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(os.urandom(12).hex())


def _load() -> dict[str, dict[str, Any]]:
    """Cached by mtime — /studio/authz reads this on every request."""
    path = _file()
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        _cache.update(mtime=None, users={})
        return {}
    if _cache["mtime"] != mtime:
        doc = read_json(path, {"users": []})
        _cache.update(mtime=mtime, users={u["name"]: u for u in doc.get("users", [])})
    return _cache["users"]


def _save(users: dict[str, dict[str, Any]]) -> None:
    write_private_json(_file(), {"users": sorted(users.values(), key=lambda u: u["name"])})
    _cache["mtime"] = None


def any_local() -> bool:
    return any(u.get("source") == "local" for u in _load().values())


def get(name: str) -> dict[str, Any] | None:
    return _load().get(name)


def public(u: dict[str, Any]) -> dict[str, Any]:
    return {k: u.get(k) for k in ("name", "role", "source", "created", "updated", "last_login")}


def list_all() -> list[dict[str, Any]]:
    return [public(u) for u in sorted(_load().values(), key=lambda u: u["name"])]


def _validate(name: str, role: str) -> None:
    if not NAME_RE.match(name or ""):
        raise UserError("Username: 2–64 characters, letters, digits and . _ @ -")
    if role not in settings.ROLES:
        raise UserError(f"Role must be one of {', '.join(settings.ROLES)}")


def _validate_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise UserError(f"Password must be at least {MIN_PASSWORD} characters")


def add(name: str, role: str, password: str | None, source: str = "local") -> dict[str, Any]:
    _validate(name, role)
    with _lock:
        users = dict(_load())
        if name in users:
            raise UserError(f"User {name!r} already exists")
        entry: dict[str, Any] = {"name": name, "role": role, "source": source,
                                 "created": _now(), "updated": _now(), "epoch": 0}
        if source == "local":
            _validate_password(password or "")
            entry["hash"] = hash_password(password or "")
        users[name] = entry
        _save(users)
        return public(entry)


def _admins(users: dict[str, dict[str, Any]]) -> list[str]:
    return [n for n, u in users.items() if u.get("role") == "admin"]


def remove(name: str) -> None:
    with _lock:
        users = dict(_load())
        if name not in users:
            raise UserError("No such user")
        if users[name].get("role") == "admin" and _admins(users) == [name] and not settings.cfg.oidc_enabled:
            raise UserError("Cannot remove the last admin")
        del users[name]
        _save(users)


def set_role(name: str, role: str) -> None:
    _validate(name, role)
    with _lock:
        users = dict(_load())
        if name not in users:
            raise UserError("No such user")
        if users[name]["role"] == "admin" and role != "admin" and _admins(users) == [name] \
                and not settings.cfg.oidc_enabled:
            raise UserError("Cannot demote the last admin")
        users[name] = {**users[name], "role": role, "updated": _now()}
        _save(users)


def set_password(name: str, password: str) -> None:
    _validate_password(password)
    with _lock:
        users = dict(_load())
        if name not in users or users[name].get("source") != "local":
            raise UserError("Only local users have a password")
        users[name] = {**users[name], "hash": hash_password(password), "updated": _now(),
                       "epoch": users[name].get("epoch", 0) + 1}
        _save(users)


def authenticate(name: str, password: str) -> dict[str, Any] | None:
    u = _load().get(name)
    if not u or u.get("source") != "local" or not check_password(password, u.get("hash", "")):
        # equalise timing for unknown users
        if not u:
            check_password(password, _DUMMY_HASH)
        return None
    return u


def touch_login(name: str) -> None:
    with _lock:
        users = dict(_load())
        if name in users:
            users[name] = {**users[name], "last_login": _now()}
            _save(users)
