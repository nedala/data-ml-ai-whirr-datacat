"""Environment-driven configuration. Every path can be overridden by env so
the app is testable outside the workspace container."""
from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path


def _env(name: str, default: str) -> str:
    v = os.environ.get(name, "")
    return v if v.strip() else default


HERE = Path(__file__).resolve().parent.parent
STATIC_DIR = HERE / "static"
UI_KIT_DIR = Path(_env("UI_KIT_DIR", "/opt/ui-kit"))

# Python user layer (shared by JupyterLab and code-server).
PYTHON = _env("WORKBENCH_PYTHON", "/usr/local/bin/python3" if Path("/usr/local/bin/python3").exists() else sys.executable)
PY_USERBASE = Path(_env("PYTHONUSERBASE", "/data/pyuser"))
PY_VER = f"{sys.version_info.major}.{sys.version_info.minor}"
PY_USER_SITE = PY_USERBASE / "lib" / f"python{PY_VER}" / "site-packages"
PY_MANIFEST = PY_USERBASE / "requirements.lock"
PYPI_JSON = _env("WORKBENCH_PYPI_JSON", "https://pypi.org/pypi").rstrip("/")
PYPI_SIMPLE = _env("WORKBENCH_PYPI_SIMPLE", "https://pypi.org/simple/")
# Extra index/installer args (admin config, never user input), e.g.
# "--index-url https://artifactory/.../simple" or "--no-index --find-links /wheels".
INDEX_ARGS = shlex.split(os.environ.get("WORKBENCH_INDEX_ARGS", ""))
INSTALLER = _env("WORKBENCH_INSTALLER", "auto")  # auto | uv | pip

# Templates (work_templates, read-only)
TEMPLATES = Path(_env("WORKBENCH_TEMPLATES", "/opt/templates"))

# Files: the shared lake and the analyst's own workspace.
LAKE_DIR = Path(_env("LAKE_DIR", "/data/lake"))
WORK_DIR = Path(_env("WORKBENCH_WORK_DIR", _env("PERSIST_VOL", "/home/analyst/work")))
LAKE_FOLDERS = ("landing", "reference", "exports", "samples")

# Limits
UPLOAD_MAX_BYTES = int(_env("WORKBENCH_UPLOAD_MAX_BYTES", str(5 * 1024 ** 3)))
URL_MAX_BYTES = int(_env("WORKBENCH_URL_MAX_BYTES", str(2 * 1024 ** 3)))
TEXT_PREVIEW_BYTES = int(_env("WORKBENCH_TEXT_PREVIEW_BYTES", str(256 * 1024)))
IMAGE_PREVIEW_BYTES = int(_env("WORKBENCH_IMAGE_PREVIEW_BYTES", str(25 * 1024 ** 2)))
PREVIEW_ROWS = int(_env("WORKBENCH_PREVIEW_ROWS", "100"))
EXPORT_MAX_ROWS = int(_env("WORKBENCH_EXPORT_MAX_ROWS", "5000000"))
QUERY_TIMEOUT_S = int(_env("WORKBENCH_QUERY_TIMEOUT_S", "120"))
EXPORT_TIMEOUT_S = int(_env("WORKBENCH_EXPORT_TIMEOUT_S", "1800"))


def pg_params() -> dict:
    """Warehouse connection parameters (read at call time; PG_PASS is a secret
    injected by Studio and never logged)."""
    return {
        "host": _env("PG_HOST", "postgres"),
        "port": int(_env("PG_PORT", "5432")),
        "dbname": _env("PG_DB", "bsp_edw"),
        "user": _env("PG_USER", "analyst"),
        "password": os.environ.get("PG_PASS", ""),
    }


def bigquery_params() -> dict | None:
    """BigQuery settings of the ASSIGNED identity (Studio → Cloud identities →
    BigQuery · read), or None when no credential is configured: the data
    projects to browse, the execution (job / billing) project and the
    identity's name. GCP_PROJECT_ID is only the last fallback."""
    if not (os.environ.get("GCP_BQ_JSON", "").strip() or os.environ.get("GCP_SA_JSON", "").strip()
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()):
        return None
    fallback = _env("GCP_PROJECT_ID", "to-be-named-soon")
    try:
        from bsp_common import gcp
    except ImportError:          # outside the appliance image
        return {"project": fallback, "exec_project": fallback, "data_projects": [fallback],
                "location": _env("BQ_LOCATION", "US"), "identity": "", "principal": ""}
    data = gcp.data_projects("bigquery_read", fallback)
    name = gcp.assigned_name("bigquery_read")
    principal = gcp.principal("bigquery_read") or (gcp.identity_meta(name).get("principal", "") if name else "")
    return {"project": data[0] if data else gcp.project("bigquery_read", fallback),
            "exec_project": gcp.exec_project("bigquery_read", fallback), "data_projects": data,
            "location": _env("BQ_LOCATION", "US"), "identity": name, "principal": principal}


def trino_params() -> dict:
    """Trino coordinator (the `trino` capability; no auth inside appliance-net)."""
    return {"host": _env("TRINO_HOST", "trino"), "port": int(_env("TRINO_PORT", "8080")),
            "user": _env("TRINO_USER", _env("USER_HANDLE", "analyst")), "catalog": _env("TRINO_CATALOG", "warehouse"),
            "http_scheme": "http", "source": "bsp-workbench"}


def _state_dir() -> Path:
    for cand in (os.environ.get("WORKBENCH_STATE_DIR"), "/data/logs/workbench",
                 str(Path.home() / ".cache" / "workbench"), "/tmp/workbench"):
        if not cand:
            continue
        p = Path(cand)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / ".probe"
            probe.write_text("ok")
            probe.unlink()
            return p
        except OSError:
            continue
    return Path("/tmp")


STATE_DIR = _state_dir()
