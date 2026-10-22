"""Atomic file writes with explicit ownership (Studio runs as root; the files
it writes are read by non-root containers)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

ANALYST_UID = 1000
ANALYST_GID = 1000


def chown(path: Path, uid: int, gid: int) -> None:
    if os.geteuid() != 0:
        return  # only root can give files away; tests and dev hosts run unprivileged
    try:
        os.chown(path, uid, gid)
    except OSError:
        pass


def write_file(path: Path, content: str | bytes, mode: int = 0o644,
               uid: int = ANALYST_UID, gid: int = ANALYST_GID) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode() if isinstance(content, str) else content
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        chown(Path(tmp), uid, gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def write_private_json(path: Path, obj: Any) -> None:
    """0600 root-owned JSON (vault, users, selection)."""
    write_file(path, json.dumps(obj, indent=2, sort_keys=True), mode=0o600, uid=0, gid=0)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def ensure_dir(path: Path, mode: int = 0o755, uid: int = ANALYST_UID, gid: int = ANALYST_GID) -> bool:
    """mkdir -p, then set owner/mode on the leaf. Returns True if created."""
    path = Path(path)
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    if created:
        try:
            os.chmod(path, mode)
        except OSError:
            pass
    chown(path, uid, gid)
    return created
