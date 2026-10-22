"""Shared plumbing for every connector: env, mode resolution, fixture and
state directories, errors. No secrets are ever logged or returned."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

PKG_DIR = Path(__file__).resolve().parent


class ConnectorError(RuntimeError):
    """A connector call failed (carries the HTTP status when there is one)."""

    def __init__(self, message: str, status: int | None = None, system: str = ""):
        super().__init__(message)
        self.status, self.system = status, system


class NotFound(ConnectorError, LookupError):
    """The mailbox / message / item / asset does not exist."""


class NotConfigured(ConnectorError):
    """Live mode was requested but the credentials are missing."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def fixtures_dir(system: str = "") -> Path:
    base = Path(env("CONNECTORS_FIXTURES_DIR") or PKG_DIR.parent / "fixtures")
    return base / system if system else base


_state_lock = threading.Lock()


def state_dir(system: str) -> Path:
    """Mutable mock state: ${CONNECTORS_STATE_DIR:-/data/connectors}/<system>/ —
    falls back to a temp dir when that is not writable (e.g. host tests)."""
    for base in (env("CONNECTORS_STATE_DIR"), "/data/connectors",
                 str(Path(tempfile.gettempdir()) / "bsp-connectors")):
        if not base:
            continue
        p = Path(base) / system
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except OSError:
            continue
    raise ConnectorError("no writable state directory for connectors")


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, data: Any) -> None:
    with _state_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)


# ── mode resolution ──────────────────────────────────────────────────────
# A system is live when its credentials are configured; `<SYSTEM>_MODE=mock`
# forces the mock even then (and `=live` forces live, failing loudly).
REQUIRED_ENV: dict[str, list[str]] = {
    "m365": ["M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET"],
    "powerbi": ["M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET"],
    "atlan": ["ATLAN_BASE_URL", "ATLAN_API_TOKEN"],
    "bigquery": [],  # GCP_SA_JSON or GOOGLE_APPLICATION_CREDENTIALS (see bsp_common.gcp)
}
OPTIONAL_ENV: dict[str, list[str]] = {
    "m365": ["SHAREPOINT_SITE", "DISPUTES_MAILBOX", "CCAI_SHAREPOINT_SITE", "CCAI_SHAREPOINT_FOLDER"],
    "powerbi": ["POWERBI_WORKSPACE_ID"],
    "atlan": ["ATLAN_CONNECTION_QN"],
    "bigquery": ["GCP_PROJECT_ID", "GCP_REGION", "BQ_LOCATION", "GCP_BQ_JSON", "GCP_SA_JSON"],
    "sap_brim": ["SAP_BRIM_BASE_URL", "SAP_BRIM_MODE", "SAP_BRIM_USER", "SAP_BRIM_PASSWORD"],
}


def _configured(system: str) -> bool:
    if system == "bigquery":
        return bool(env("GCP_BQ_JSON") or env("GCP_SA_JSON") or env("GOOGLE_APPLICATION_CREDENTIALS"))
    if system == "atlan":
        # Base URL is non-secret and has a DataPlatform default; the token is the gate.
        return bool(env("ATLAN_API_TOKEN") and (env("ATLAN_BASE_URL") or "https://atlan.example.com"))
    req = REQUIRED_ENV.get(system)
    return bool(req) and all(env(k) for k in req)


def mode_of(system: str) -> str:
    """"mock" | "live" for m365, atlan, bigquery, powerbi, sap_brim."""
    system = system.lower().replace("-", "_")
    if system == "sap_brim":
        try:
            from . import sap_brim  # owned by build/disputes
            fn = getattr(sap_brim, "mode_of", None) or getattr(sap_brim, "mode", None)
            if callable(fn):
                m = fn() if fn.__code__.co_argcount == 0 else fn("sap_brim")
                return "mock" if str(m) == "mock" else "live"
        except Exception:
            pass
        m = env("SAP_BRIM_MODE", "mock").lower()
        return "live" if m in ("api", "playwright") and env("SAP_BRIM_BASE_URL") else "mock"
    if system not in REQUIRED_ENV:
        raise ValueError(f"unknown connector system {system!r}")
    forced = env(f"{system.upper()}_MODE").lower()
    if forced == "mock":
        return "mock"
    if forced == "live":
        return "live"
    return "live" if _configured(system) else "mock"


def require_live(system: str) -> None:
    if not _configured(system):
        need = REQUIRED_ENV.get(system) or ["GCP_SA_JSON"]
        raise NotConfigured(f"{system}: live mode needs {', '.join(need)} (set them in Studio → Credentials)",
                            system=system)


def config_report(system: str) -> dict[str, Any]:
    """Which settings are present — names only, never values."""
    keys = REQUIRED_ENV.get(system, []) + OPTIONAL_ENV.get(system, [])
    return {k: bool(env(k)) for k in keys}


def public_url(path: str) -> str:
    base = env("PUBLIC_FACADE_URL", "https://localhost:31201").rstrip("/")
    return base + "/" + path.lstrip("/")
