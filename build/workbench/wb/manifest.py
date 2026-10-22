"""Line-based manifest for the Python package manager (requirements.lock).

    # header comments
    polars==1.2.0  # pinned
    httpx>=0.27  # requested

Entries are keyed by a canonical name; order is preserved. ``pinned`` entries
always carry an exact ``==`` version and are restored verbatim.
"""
from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

_LOCK = threading.Lock()


class Manifest:
    def __init__(self, path: Path, title: str, key: Callable[[str], str],
                 name_of: Callable[[str], str]):
        self.path = Path(path)
        self.title = title
        self.key = key            # canonicaliser for names
        self.name_of = name_of    # spec text → bare name

    def read_text(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def entries(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for raw in self.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            spec, _, comment = line.partition("#")
            spec = spec.strip()
            if not spec:
                continue
            try:
                name = self.name_of(spec)
            except ValueError:
                continue
            out[self.key(name)] = {"name": name, "spec": spec,
                                   "pinned": "pinned" in comment.lower()}
        return out

    def write(self, entries: dict[str, dict]) -> None:
        lines = [
            f"# {self.title} — managed by Workbench Packages (/pkg/).",
            "# One requirement per line; '# pinned' entries are restored at that exact version.",
            f"# updated {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}",
        ]
        for e in sorted(entries.values(), key=lambda e: e["name"].lower()):
            lines.append(f"{e['spec']}  # {'pinned' if e.get('pinned') else 'requested'}")
        self.write_text("\n".join(lines) + "\n")

    def write_text(self, text: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".manifest-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, self.path)

    def update(self, fn: Callable[[dict[str, dict]], None]) -> dict[str, dict]:
        with _LOCK:
            e = self.entries()
            fn(e)
            self.write(e)
            return e
