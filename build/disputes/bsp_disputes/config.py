"""Dispute Desk settings (env only)."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default




class Settings:
    mailbox = property(lambda s: env("DISPUTES_MAILBOX", "billing-disputes@example.net"))
    data_dir = property(lambda s: Path(env("DISPUTES_DATA_DIR", "/data/disputes")))
    poll_seconds = property(lambda s: int(env("DISPUTES_POLL_SECONDS", "300")))
    auto_submit = property(lambda s: env("DISPUTES_AUTO_SUBMIT", "false").lower() in ("1", "true", "yes"))
    auto_approve_threshold = property(lambda s: float(env("DISPUTES_AUTO_APPROVE_THRESHOLD", "0.85")))
    sla_hours = property(lambda s: float(env("DISPUTES_SLA_HOURS", "48")))
    use_llm = property(lambda s: env("DISPUTES_USE_LLM", "auto").lower())  # auto | off
    processed_folder = property(lambda s: env("DISPUTES_PROCESSED_FOLDER", "Processed"))
    review_folder = property(lambda s: env("DISPUTES_REVIEW_FOLDER", "Needs review"))
    move_messages = property(lambda s: env("DISPUTES_MOVE_MESSAGES", "true").lower() in ("1", "true", "yes"))
    concurrency = property(lambda s: int(env("SAP_BRIM_CONCURRENCY", "4")))
    browser_path = property(lambda s: env("DISPUTES_BROWSER_PATH", "/browser/"))
    eml_dir = property(lambda s: env("DISPUTES_EML_DIR"))


@lru_cache(maxsize=1)
def get() -> Settings:
    s = Settings()
    os.environ.setdefault("SAP_BRIM_SCREENSHOT_DIR", str(s.data_dir / "screenshots"))
    return s


@lru_cache(maxsize=1)
def reason_codes() -> dict:
    p = Path(env("DISPUTES_REASON_CODES") or HERE / "reason_codes.yaml")
    return yaml.safe_load(p.read_text())["categories"]
