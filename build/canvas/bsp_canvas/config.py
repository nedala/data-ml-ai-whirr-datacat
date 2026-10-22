"""Paths and small helpers shared by the canvas service."""

from __future__ import annotations

import os
import re
from pathlib import Path

PKG = Path(__file__).resolve().parent
STATIC = PKG.parent / "static"
EXAMPLES = PKG.parent / "examples"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def canvas_dir() -> Path:
    p = Path(env("CANVAS_DIR", "/data/canvas"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def sub(name: str) -> Path:
    p = canvas_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def lake_dir() -> Path:
    return Path(env("LAKE_DIR", "/data/lake"))


def ui_kit_dir() -> Path:
    p = Path(env("UI_KIT_DIR", "/opt/ui-kit"))
    return p if p.is_dir() else PKG.parents[1] / "ui-kit"


# ${VAR} interpolation in node config is limited to non-secret settings;
# secrets are referenced explicitly as vault:NAME (auth headers, SQL URLs).
INTERP_ALLOWED = {"CONNECTORS_URL", "GCP_PROJECT_ID", "LAKE_DIR", "PUBLIC_FACADE_URL", "DISPUTES_MAILBOX",
                  "SHAREPOINT_SITE", "CCAI_SHAREPOINT_SITE", "CCAI_SHAREPOINT_FOLDER", "POWERBI_WORKSPACE_ID",
                  "PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "USER_HANDLE"}


DEFAULTS = {"CONNECTORS_URL": "http://connectors:8092/connectors", "DISPUTES_MAILBOX": "billing-disputes@example.net",
            "GCP_PROJECT_ID": "to-be-named-soon", "LAKE_DIR": "/data/lake", "CCAI_SHAREPOINT_FOLDER": "CCAI/CallLogs"}


def interpolate(value: str) -> str:
    def rep(m: re.Match) -> str:
        name, default = m.group(1), m.group(3) or ""
        if name in INTERP_ALLOWED or name.startswith("CANVAS_VAR_"):
            return env(name, default) or DEFAULTS.get(name, default)
        raise ValueError(f"${{{name}}} is not an allowed variable (secrets use vault:NAME)")
    return re.sub(r"\$\{([A-Z][A-Z0-9_]*)(:-([^}]*))?\}", rep, value)


SECRETS_HELP = "Studio → Credentials → Connection secrets"


def _connection_secrets() -> dict[str, str]:
    """Connection secrets Studio delivers as one JSON bundle
    (CANVAS_CONNECTION_SECRETS), so any name an admin adds works without a
    compose change."""
    import json

    try:
        data = json.loads(os.environ.get("CANVAS_CONNECTION_SECRETS") or "{}")
        return {str(k): str(v) for k, v in data.items() if v} if isinstance(data, dict) else {}
    except ValueError:
        return {}


def secret_available(name: str) -> bool:
    return bool(os.environ.get(name) or _connection_secrets().get(name))


def secret(ref: str) -> str:
    """Resolve a vault reference. `vault:NAME` reads NAME from the process
    environment or the connection-secrets bundle (both injected by Studio from
    the vault); never logged."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref.startswith("vault:"):
        name = ref[6:].strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError(f"bad vault reference {ref!r}")
        val = os.environ.get(name, "") or _connection_secrets().get(name, "")
        if not val:
            raise ValueError(f"secret {name} is not set — add it in {SECRETS_HELP}")
        return val
    raise ValueError("secrets must be vault references (vault:NAME), not literal values")
