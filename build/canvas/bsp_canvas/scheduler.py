"""In-service cron scheduler: every pipeline with `schedule: {cron, enabled: true}`
runs when its cron fires (UTC unless schedule.timezone is set). No catch-up for
times missed while the service was down; a pipeline already running is skipped."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from . import engine, store

log = logging.getLogger("bsp_canvas.scheduler")
TICK = 15.0
_state: dict[str, datetime] = {}
_stop = threading.Event()
_thread: threading.Thread | None = None


def next_fire(cron: str, after: datetime, tz: str = "UTC") -> datetime:
    from zoneinfo import ZoneInfo

    from croniter import croniter
    z = ZoneInfo(tz or "UTC")
    return croniter(cron, after.astimezone(z)).get_next(datetime).astimezone(timezone.utc)


def valid(cron: str) -> bool:
    from croniter import croniter
    return bool(cron) and croniter.is_valid(cron)


def tick(now: datetime | None = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    fired = []
    for p in store.list_pipelines():
        sch = p.get("schedule") or {}
        cron, pid = (sch.get("cron") or "").strip(), p["id"]
        if not (sch.get("enabled") and valid(cron)):
            _state.pop(pid, None)
            continue
        base = _state.setdefault(pid, now)
        try:
            due = next_fire(cron, base, sch.get("timezone", "UTC"))
        except Exception as e:  # bad timezone etc.
            log.warning("schedule %s: %s", pid, e)
            continue
        if due <= now:
            _state[pid] = now
            if engine.is_running(pid):
                log.info("schedule %s: still running, skipped", pid)
                continue
            try:
                engine.start_run(pid, trigger="schedule")
                fired.append(pid)
            except Exception as e:
                log.warning("schedule %s: %s", pid, e)
    return fired


def _loop() -> None:
    while not _stop.wait(TICK):
        try:
            tick()
        except Exception:
            log.exception("scheduler tick failed")


def start() -> None:
    global _thread
    if _thread is None or not _thread.is_alive():
        _stop.clear()
        _thread = threading.Thread(target=_loop, daemon=True, name="canvas-scheduler")
        _thread.start()


def stop() -> None:
    _stop.set()


def upcoming(p: dict) -> str | None:
    sch = p.get("schedule") or {}
    if not (sch.get("enabled") and valid(sch.get("cron", ""))):
        return None
    try:
        return next_fire(sch["cron"], datetime.now(timezone.utc), sch.get("timezone", "UTC")).isoformat()
    except Exception:
        return None
