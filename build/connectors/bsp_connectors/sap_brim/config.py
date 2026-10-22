"""Settings for the SAP BRIM connector (env only; secrets never logged)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass
class Config:
    mode: str
    base_url: str
    user: str
    password: str
    client: str
    verify_tls: bool
    cdp_url: str
    concurrency: int
    slow_mo_ms: int
    highlight: bool
    keep_windows: bool
    screen: tuple[int, int]
    screenshot_dir: str
    timeout_s: float


def mode() -> str:
    m = env("SAP_BRIM_MODE", "mock").lower()
    return m if m in ("api", "playwright", "mock") else "mock"


def _mock_base() -> str:
    return env("CONNECTORS_URL", "http://connectors:8092/connectors").rstrip("/") + "/sap_brim"


def cfg() -> Config:
    m = mode()
    base = env("SAP_BRIM_BASE_URL") if m != "mock" else ""
    w, _, h = env("BROWSER_SCREEN", "1920x1080").partition("x")
    return Config(
        mode=m,
        base_url=(base or _mock_base()).rstrip("/"),
        user=env("SAP_BRIM_USER", "" if base else "BRIM_TECH_USER"),
        password=env("SAP_BRIM_PASSWORD", "" if base else "mock"),
        client=env("SAP_BRIM_CLIENT"),
        verify_tls=env("SAP_BRIM_VERIFY_TLS", "true").lower() not in ("0", "false", "no"),
        cdp_url=env("BROWSER_CDP_URL", "http://browser:9223"),
        concurrency=int(env("SAP_BRIM_CONCURRENCY", "4")),
        slow_mo_ms=int(env("SAP_BRIM_SLOWMO_MS", "250")),
        highlight=env("SAP_BRIM_HIGHLIGHT", "true").lower() not in ("0", "false", "no"),
        keep_windows=env("SAP_BRIM_KEEP_WINDOWS", "false").lower() in ("1", "true", "yes"),
        screen=(int(w or 1920), int(h or 1080)),
        screenshot_dir=env("SAP_BRIM_SCREENSHOT_DIR", "/data/disputes/screenshots"),
        timeout_s=float(env("SAP_BRIM_TIMEOUT", "30")),
    )


def ui_url() -> str:
    """Entry URL of the dispute-entry web UI driven by the Playwright adapter."""
    return env("SAP_BRIM_UI_URL") or cfg().base_url + "/ui/"


@lru_cache(maxsize=1)
def field_map() -> dict[str, Any]:
    import yaml

    p = Path(env("SAP_BRIM_FIELD_MAP") or HERE / "field_map.yaml")
    return yaml.safe_load(p.read_text())
