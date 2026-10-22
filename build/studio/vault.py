"""The appliance vault (plan §3): data/vault/vault.json, AES-256-GCM with a
scrypt-derived key. The master password comes from APPLIANCE_MASTER_PASSWORD,
APPLIANCE_MASTER_PASSWORD_FILE or the Studio Unlock screen; the derived key is
held in memory only.

Plaintext layout::

    {"services":   {<service>: {<ENV_NAME>: value}},
     "legacy":     {<old key>: {...}}           # migrated values with no slot here
     "meta":       {"rotated": {<service>: {<ENV_NAME>: iso}}}}
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import catalog as _catalog
import cryptobox
import settings
from cryptobox import DecryptError
from fsutil import read_json, write_private_json

log = logging.getLogger("studio.vault")

LEGACY_USER_ENV = {"rustfs": "RUSTFS_ACCESS_KEY", "minio": "RUSTFS_ACCESS_KEY", "sqlserver": "ODBC_USER"}
PLACEHOLDERS = {"", "changeme", "admin", "changeme-superset-insecure"}
MIN_MASTER_LEN = 12


class VaultLocked(RuntimeError):
    """Raised when the vault is locked; Studio maps it to HTTP 423."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _vault_file() -> Path:
    return settings.cfg.vault_dir / "vault.json"


def _legacy_files() -> tuple[Path, Path]:
    d = settings.cfg.vault_dir
    return d / "master.key", d / "secrets.enc"


def _walk(data: dict, path: str, create: bool = False) -> tuple[dict | None, str]:
    parts = [p for p in path.split(".") if p]
    if not parts:
        raise KeyError("empty vault path")
    node: Any = data
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return None, parts[-1]
        if part not in node:
            if not create:
                return None, parts[-1]
            node[part] = {}
        node = node[part]
    return (node if isinstance(node, dict) else None), parts[-1]


def auto_mode() -> bool:
    """VAULT_MODE=auto (default): the vault stays encrypted at rest, but its key
    is a random file Studio manages (vault/master.key, 0600) — no master
    passphrase, no unlock screen, no step-up prompts. VAULT_MODE=passphrase
    restores the typed master passphrase."""
    return os.environ.get("VAULT_MODE", "auto").strip().lower() != "passphrase"


def key_file() -> Path:
    return _vault_file().parent / "master.key"


def env_password() -> str:
    pw = os.environ.get("APPLIANCE_MASTER_PASSWORD", "")
    if pw:
        return pw
    file = os.environ.get("APPLIANCE_MASTER_PASSWORD_FILE", "")
    if file:
        try:
            return Path(file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("cannot read APPLIANCE_MASTER_PASSWORD_FILE: %s", exc)
    if auto_mode():
        kf = key_file()
        try:
            if kf.is_file():
                return kf.read_text(encoding="utf-8").strip()
            if not _vault_file().is_file():      # new appliance: create the key with the vault
                import secrets as _secrets

                kf.parent.mkdir(parents=True, exist_ok=True)
                key = _secrets.token_urlsafe(36)
                fd = os.open(kf, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(key)
                return key
        except OSError as exc:
            log.warning("vault key file %s: %s", kf, exc)
    return ""


class VaultStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._key: bytes | None = None
        self._envelope: dict[str, Any] | None = None
        self._data: dict[str, Any] | None = None
        self.source: str | None = None   # env | unlock | setup
        # Called with the store after every successful unlock / initialize
        # (e.g. seeding empty credential slots from .env). Notes they return
        # are appended to the unlock notes; a failing hook never blocks unlock.
        self.on_open: list[Any] = []

    # ── state ──────────────────────────────────────────────────────────
    def exists(self) -> bool:
        return _vault_file().is_file()

    def is_unlocked(self) -> bool:
        return self._data is not None

    def status(self) -> str:
        if self.is_unlocked():
            return "unlocked"
        return "locked" if self.exists() else "uninitialized"

    def legacy_present(self) -> bool:
        key, enc = _legacy_files()
        return key.is_file() and enc.is_file()

    # ── lifecycle ──────────────────────────────────────────────────────
    def initialize(self, password: str, source: str = "setup") -> list[str]:
        """Create an empty vault (migrating v1 secrets if present)."""
        if len(password) < MIN_MASTER_LEN:
            raise ValueError(f"master passphrase must be at least {MIN_MASTER_LEN} characters")
        with self._lock:
            if self.exists():
                raise FileExistsError("vault already exists")
            salt = os.urandom(16)
            self._key = cryptobox.derive_key(password, salt)
            self._envelope = {"salt": cryptobox.b64e(salt)}
            self._data = {"services": {}, "meta": {"created": _now()}}
            self.source = source
            self._persist()
            return self._migrate_legacy() + self._run_open_hooks()

    def unlock(self, password: str, source: str = "unlock") -> list[str]:
        """Decrypt with `password`. Raises DecryptError. Returns migration notes."""
        with self._lock:
            envelope = read_json(_vault_file(), None)
            if not envelope:
                raise FileNotFoundError("vault not initialised")
            key = cryptobox.envelope_key(envelope, password)
            data = json.loads(cryptobox.decrypt_with_key(envelope, key))
            self._key, self._envelope, self._data, self.source = key, envelope, data, source
            return self._migrate_legacy() + self._run_open_hooks()

    def _run_open_hooks(self) -> list[str]:
        notes: list[str] = []
        for hook in self.on_open:
            try:
                notes.extend(hook(self) or [])
            except Exception as exc:  # noqa: BLE001 — never block unlock
                log.error("vault open hook %s failed: %s", getattr(hook, "__name__", hook), exc)
        return notes

    def auto_unlock(self) -> bool:
        """Unlock (or create) from the environment, if a password is provided."""
        pw = env_password()
        if not pw:
            return False
        try:
            if self.exists():
                self.unlock(pw, source="env")
            else:
                self.initialize(pw, source="env")
            return True
        except (DecryptError, ValueError) as exc:
            log.error("vault auto-unlock failed: %s", exc)
            return False

    def lock(self) -> None:
        with self._lock:
            self._key = self._envelope = self._data = None
            self.source = None

    def verify(self, password: str) -> bool:
        """True if `password` is the master password (no state change)."""
        envelope = self._envelope if self._key else read_json(_vault_file(), None)
        if not envelope:
            return False
        try:
            key = cryptobox.envelope_key(envelope, password)
        except DecryptError:
            return False
        if self._key:
            return cryptobox.same_key(key, self._key)
        try:
            cryptobox.decrypt_with_key(envelope, key)
            return True
        except DecryptError:
            return False

    def change_password(self, new_password: str) -> None:
        if len(new_password) < MIN_MASTER_LEN:
            raise ValueError(f"master passphrase must be at least {MIN_MASTER_LEN} characters")
        with self._lock:
            self._require()
            salt = os.urandom(16)
            self._key = cryptobox.derive_key(new_password, salt)
            self._envelope = {"salt": cryptobox.b64e(salt)}
            self._persist()

    # ── data access ────────────────────────────────────────────────────
    def _require(self) -> dict[str, Any]:
        if self._data is None:
            raise VaultLocked("vault is locked")
        return self._data

    def get(self, path: str, default: Any = None) -> Any:
        with self._lock:
            parent, leaf = _walk(self._require(), path)
            if parent is None or leaf not in parent:
                return default
            return copy.deepcopy(parent[leaf])

    def set(self, path: str, value: Any) -> None:
        with self._lock:
            parent, leaf = _walk(self._require(), path, create=True)
            if parent is None:
                raise KeyError(f"{path}: parent is not an object")
            parent[leaf] = copy.deepcopy(value)
            self._persist()

    def delete(self, path: str) -> None:
        with self._lock:
            parent, leaf = _walk(self._require(), path)
            if parent is not None and leaf in parent:
                del parent[leaf]
                self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._require())

    def mutate(self, fn: Callable[[dict[str, Any]], None]) -> None:
        """Apply several changes under one lock and persist once."""
        with self._lock:
            data = copy.deepcopy(self._require())
            fn(data)
            self._data = data
            self._persist()

    # ── internals ──────────────────────────────────────────────────────
    def _persist(self) -> None:
        assert self._key is not None and self._envelope is not None and self._data is not None
        salt = cryptobox.b64d(self._envelope["salt"])
        envelope = cryptobox.encrypt_with_key(self._key, salt, json.dumps(self._data).encode())
        write_private_json(_vault_file(), {"v": 2, **envelope})
        self._envelope = envelope

    def _migrate_legacy(self) -> list[str]:
        if not self.legacy_present():
            return []
        key_file, enc_file = _legacy_files()
        try:
            from cryptography.fernet import Fernet, InvalidToken
            old = json.loads(Fernet(key_file.read_bytes().strip()).decrypt(enc_file.read_bytes()))
        except (InvalidToken, ValueError, OSError) as exc:
            log.error("legacy vault migration failed: %s", exc)
            return [f"Legacy vault could not be decrypted: {exc}"]
        notes = merge_legacy(self._require(), old.get("services", {}))
        self._persist()
        for f in (key_file, enc_file):
            f.rename(f.with_name(f.name + ".migrated"))
        notes.append("Legacy v1 vault migrated; old files renamed *.migrated")
        log.info("legacy vault migrated (%d notes)", len(notes))
        return notes


def env_owner_map() -> dict[str, str]:
    """ENV_NAME -> catalog service that owns the credential slot."""
    out: dict[str, str] = {}
    for svc, spec in _catalog.load()["services"].items():
        for cred in spec.get("creds", []):
            out[cred["env"]] = svc
            if cred.get("user_env"):
                out[cred["user_env"]] = svc
    return out


def merge_legacy(data: dict[str, Any], old_services: dict[str, dict]) -> list[str]:
    """Fold v1 `services.<wizard key>.{ENV|username}` into v2 layout. Existing
    v2 values win; values with no v2 slot are kept under `legacy.`."""
    owners = env_owner_map()
    services = data.setdefault("services", {})
    notes: list[str] = []
    for old_key, entry in old_services.items():
        if not isinstance(entry, dict):
            continue
        for name, value in entry.items():
            if not isinstance(value, str) or not value:
                continue
            if name == "username":
                name = LEGACY_USER_ENV.get(old_key, "")
                if not name:
                    continue
            elif value in PLACEHOLDERS:
                continue
            owner = owners.get(name)
            if owner is None:
                data.setdefault("legacy", {}).setdefault(old_key, {})[name] = value
                notes.append(f"{old_key}.{name} kept under legacy.{old_key}")
                continue
            slot = services.setdefault(owner, {})
            if name not in slot:
                slot[name] = value
                notes.append(f"{name} → services.{owner}")
    return notes


store = VaultStore()
