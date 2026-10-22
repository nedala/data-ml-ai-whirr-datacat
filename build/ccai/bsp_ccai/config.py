"""Service configuration (env) and portal-editable settings defaults."""

from __future__ import annotations

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
REPO = PKG_DIR.parents[2]  # build/ccai/bsp_ccai → repo root (when run from a checkout)
PREFIX = "/ccai"


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _first_existing(*paths: str | Path) -> Path:
    for p in paths:
        if p and Path(p).exists():
            return Path(p)
    return Path(paths[-1])


DATA_DIR = Path(env("CCAI_DATA_DIR", "/data/ccai"))
TAXONOMY_PATH = _first_existing(env("CCAI_TAXONOMY"), "/opt/taxonomy/ccai_taxonomy.yaml",
                                REPO / "config" / "taxonomy" / "ccai_taxonomy.yaml")
UI_KIT_DIR = _first_existing("/opt/ui-kit", REPO / "build" / "ui-kit")
STATIC_DIR = PKG_DIR / "static"


def sharepoint_site() -> str | None:
    return env("CCAI_SHAREPOINT_SITE") or None


def sharepoint_folder() -> str:
    return env("CCAI_SHAREPOINT_FOLDER", "CCAI/CallLogs")


def publish_folder() -> str:
    return env("CCAI_PUBLISH_FOLDER", "CCAI/Tagged")


def data_path(*parts: str) -> Path:
    p = DATA_DIR.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


# Editable in the portal (Settings), persisted in ccai.settings.
DEFAULT_SETTINGS: dict = {
    "schedule_enabled": env("CCAI_SCHEDULER", "1") not in ("0", "false", "no"),
    "schedule_minutes": int(env("CCAI_SCHEDULE_MINUTES", "60")),
    "confidence_threshold": float(env("CCAI_CONFIDENCE_THRESHOLD", "0.6")),
    "route": env("CCAI_ROUTE", "auto"),            # auto | llm | offline
    "classify_alias": env("CCAI_CLASSIFY_ALIAS", "classify-default"),
    "embed_alias": env("CCAI_EMBED_ALIAS", "embed-default"),
    "concurrency": int(env("CCAI_CONCURRENCY", "4")),
    "publish_on_complete": False,
    "publish_format": "xlsx",
}
