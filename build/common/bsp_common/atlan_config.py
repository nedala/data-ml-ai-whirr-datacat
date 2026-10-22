"""Atlan ↔ appliance configuration (base URL + BigQuery identity mapping).

The MCP catalog and the notebooks use this to answer one question: when an
Atlan asset maps to a physical BigQuery table, which Studio cloud identity and
which execution project should run the query?

Resolution order for a BigQuery table:
  1. `bigquery.connections[<Atlan qualifiedName>]`
  2. `bigquery.projects[<data project>]`
  3. `bigquery.default`
  4. env overrides: ATLAN_BQ_IDENTITY / ATLAN_BQ_PROJECT / ATLAN_BQ_EXEC_PROJECT
  5. built-in DataPlatform defaults (data.analyst@example.com / example-project)

The config file is optional. It is read from `ATLAN_MAP_FILE`, then
`/opt/config/catalog/atlan.yaml`, then `config/catalog/atlan.yaml`.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://atlan.example.com"
DEFAULT_IDENTITY = "data.analyst@example.com"
DEFAULT_EXEC_PROJECT = "example-project"

_lock = threading.Lock()
_cache: tuple[float, dict[str, Any]] | None = None


def _paths() -> list[Path]:
    out: list[Path] = []
    env = os.environ.get("ATLAN_MAP_FILE", "").strip()
    if env:
        out.append(Path(env))
    out += [Path("/opt/config/catalog/atlan.yaml"), Path("config/catalog/atlan.yaml")]
    return out


def load() -> dict[str, Any]:
    """Parsed YAML (cached by mtime); {} when no file is present."""
    global _cache
    for path in _paths():
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        with _lock:
            if _cache is not None and _cache[0] == mtime:
                return _cache[1]
            try:
                import yaml  # noqa: PLC0415
                data = yaml.safe_load(path.read_text()) or {}
            except Exception:  # noqa: BLE001 — malformed file must not break the app
                data = {}
            _cache = (mtime, data if isinstance(data, dict) else {})
            return _cache[1]
    return {}


def base_url() -> str:
    """ATLAN_BASE_URL → config base_url → DataPlatform tenant → default."""
    env = os.environ.get("ATLAN_BASE_URL", "").strip()
    if env:
        return env.rstrip("/")
    cfg = str(load().get("base_url") or "").strip()
    return (cfg or DEFAULT_BASE_URL).rstrip("/")


def lookup(connection: str = "", project: str = "", default_project: str = "") -> dict[str, str]:
    """{identity, project, exec_project} for a BigQuery table/connection.

    `identity` is a Studio identity name or id. The caller must still be on that
    identity's access list; Studio issues the token.
    """
    bq = load().get("bigquery") or {}
    entry: dict[str, Any] = {}
    if connection:
        entry = (bq.get("connections") or {}).get(connection) or {}
    if not entry and project:
        entry = (bq.get("projects") or {}).get(project) or {}
    if not entry:
        entry = bq.get("default") or {}

    identity = (entry.get("identity") or os.environ.get("ATLAN_BQ_IDENTITY", "")
                or DEFAULT_IDENTITY).strip()
    exec_project = (entry.get("exec_project") or os.environ.get("ATLAN_BQ_EXEC_PROJECT", "")
                    or DEFAULT_EXEC_PROJECT).strip()
    data_project = (entry.get("project") or project or default_project
                    or os.environ.get("ATLAN_BQ_PROJECT", "")).strip()
    return {"identity": identity, "project": data_project,
            "exec_project": exec_project or data_project}


def identity_for(project: str = "", connection: str = "") -> str:
    """Just the configured identity (the common case for notebooks/MCP)."""
    return lookup(connection=connection, project=project)["identity"]
