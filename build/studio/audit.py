"""Append-only audit trail: data/vault/audit.log, one JSON object per line
({ts, user, event, detail, peer})."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from collections import deque
from pathlib import Path
from typing import Any

import settings

log = logging.getLogger("studio.audit")
_lock = threading.Lock()


def _file() -> Path:
    return settings.cfg.vault_dir / "audit.log"


def record(event: str, detail: str = "", user: str = "", peer: str = "") -> None:
    entry = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "user": user, "event": event, "detail": detail, "peer": peer}
    path = _file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except OSError as exc:
        log.error("audit write failed: %s", exc)


def tail(limit: int = 500, query: str = "", event: str = "") -> list[dict[str, Any]]:
    """Newest first. `query` matches any field (case-insensitive); `event`
    is a prefix match (e.g. "auth." or "compose.apply")."""
    q = query.lower().strip()
    out: deque[dict[str, Any]] = deque(maxlen=limit)
    try:
        with open(_file(), encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    entry = {"ts": "", "user": "", "event": "legacy", "detail": line.strip(), "peer": ""}
                if event and not str(entry.get("event", "")).startswith(event):
                    continue
                if q and q not in " ".join(str(v) for v in entry.values()).lower():
                    continue
                out.append(entry)
    except OSError:
        return []
    return list(reversed(out))
