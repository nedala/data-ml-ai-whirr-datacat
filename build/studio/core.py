"""Studio core — the ONLY module plugins import from.

CONTRACT (plan/v2-architecture.md §4). The Studio workstream implements the
bodies; plugin workstreams (connectors, egress, services hooks) code against
these signatures. Do not change a signature without updating every plugin.

Plugin modules (build/studio/plugins/*.py) may define, besides `router` and
`PAGE`:

    def export_backup() -> dict                 # included in config export
    def import_backup(data: dict) -> None        # restore
    def secret_env() -> dict[str, str]           # extra env for compose calls
    def before_up(services: list[str]) -> None   # render configs before start
    ROTATE: dict[str, Callable[[str], None]]     # ENV_NAME -> apply new value
                                                 # to a RUNNING service (ALTER
                                                 # ROLE, reset-password, …).
                                                 # Studio recreates the
                                                 # affected containers after.
    def health() -> list[dict]                   # optional status items for
                                                 # the Studio home page:
                                                 # {"label","state":"ok|warning|error","detail"}
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import requests
from fastapi import Request

import audit as _audit
import auth as _auth
import catalog as _catalog
import dockerops as _docker
import fsutil as _fs
import orchestrator as _orch
import settings as _settings
import vault as _vault

# ── paths ────────────────────────────────────────────────────────────────
class _Paths:
    """Resolved at access time (settings can be reloaded)."""

    @property
    def PROJECT(self) -> Path:   # repo root as seen by Studio (/host-project)
        return _settings.cfg.project

    @property
    def DATA(self) -> Path:      # <project>/data
        return _settings.cfg.data

    @property
    def VAULT(self) -> Path:     # <project>/data/vault
        return _settings.cfg.vault_dir

    @property
    def CATALOG(self) -> Path:   # build/studio/catalog.yaml
        return _settings.cfg.catalog


paths = _Paths()


# ── identity ─────────────────────────────────────────────────────────────
User = _auth.User   # @dataclass: name: str, role: str (viewer | analyst | modeler | admin)


def require_user(request: Request) -> User:
    """FastAPI dependency: signed-in user or HTTP 401."""
    return _auth.require_user(request)


def require_admin(request: Request) -> User:
    """FastAPI dependency: admin or HTTP 401/403. Also enforces the
    `X-Studio: 1` CSRF header on non-GET requests."""
    return _auth.require_admin(request)


def require_step_up(request: Request) -> User:
    """FastAPI dependency for secret reveal/export: admin AND master password
    re-entered within the last 5 minutes, else HTTP 428 {"step_up": true}."""
    return _auth.require_step_up(request)


# ── vault ────────────────────────────────────────────────────────────────
class vault:
    @staticmethod
    def is_unlocked() -> bool:
        return _vault.store.is_unlocked()

    @staticmethod
    def get(path: str, default: Any = None) -> Any:
        """Dotted path into the decrypted vault, e.g. 'services.postgres.PG_PASS'
        or 'services.shared.GCP_SA_JSON'. Raises VaultLocked if locked."""
        return _vault.store.get(path, default)

    @staticmethod
    def set(path: str, value: Any) -> None:
        """Set + persist (re-encrypt) atomically."""
        _vault.store.set(path, value)

    @staticmethod
    def delete(path: str) -> None:
        _vault.store.delete(path)


VaultLocked = _vault.VaultLocked   # RuntimeError subclass; Studio maps it to HTTP 423


def generate_secret(length: int = 32, policy: str = "default") -> str:
    """Crypto-random secret. policy: "default" (urlsafe), "alnum" or "strong"."""
    return _orch.generate_secret(length, policy)


# ── catalog ──────────────────────────────────────────────────────────────
def catalog() -> dict:
    """Parsed build/studio/catalog.yaml (cached; reloaded when mtime changes)."""
    return _catalog.load()


# ── docker / compose ─────────────────────────────────────────────────────
class compose:
    @staticmethod
    def status() -> dict[str, dict]:
        """{service: {"state": running|starting|exited|missing|unhealthy,
        "health": str|None, "started_at": iso|None, "image": str}} for every
        catalog service."""
        return _docker.status()

    @staticmethod
    def up(services: list[str], recreate: bool = False) -> dict:
        """Start services (+ deps) with every vault secret injected via the
        process env. Runs plugin before_up hooks first. Returns a job id:
        {"job": "<id>"} — progress via GET /studio/api/jobs/<id>."""
        return {"job": _orch.start(services, user="plugin", recreate=recreate).id}

    @staticmethod
    def stop(services: list[str]) -> dict:
        return {"job": _orch.stop(services, user="plugin").id}

    @staticmethod
    def restart(services: list[str]) -> dict:
        """Recreate (not just docker restart) so changed env/secrets apply."""
        return {"job": _orch.start(services, user="plugin", recreate=True).id}


def exec_in(service: str, argv: list[str], user: str | None = None,
            timeout: int = 120) -> tuple[int, str]:
    """docker exec into the running container of a compose service."""
    return _docker.exec_in(service, argv, user=user, timeout=timeout)


def http_internal(method: str, url: str, **kw) -> Any:
    """requests-style call to an internal service (no proxy). Returns
    requests.Response."""
    kw.setdefault("timeout", 30)
    with requests.Session() as s:
        s.trust_env = False   # never route appliance-internal calls via px-proxy
        return s.request(method, url, **kw)


# ── misc ─────────────────────────────────────────────────────────────────
def audit(event: str, detail: str = "", request: Request | None = None) -> None:
    """Append to data/vault/audit.log: ts, user, event, detail, peer."""
    user, peer = "", ""
    if request is not None:
        session = _auth.read_session(request)
        user = session.user.name if session else ""
        peer = request.client.host if request.client else ""
    _audit.record(event, detail, user=user, peer=peer)


def write_file(path: Path, content: str | bytes, mode: int = 0o644,
               uid: int = 1000, gid: int = 1000) -> None:
    """Atomic write owned by the analyst uid (Studio runs as root)."""
    _fs.write_file(path, content, mode=mode, uid=uid, gid=gid)


Rotator = Callable[[str], None]
