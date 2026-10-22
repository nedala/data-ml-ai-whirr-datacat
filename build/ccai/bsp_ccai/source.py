"""SharePoint source adapter.

Uses the connectors SDK (`bsp_connectors.m365`, plan §4.1) — mock or live.
When the SDK cannot be imported (or CCAI_SOURCE=stub) a thin stub reads the
same fixture tree (fixtures/m365/sites/<site-or-default>/<folder>/…) so the
service and its tests work stand-alone; uploads then land under
${CCAI_DATA_DIR}/published/."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
from datetime import datetime, timezone
from pathlib import Path

from . import config

log = logging.getLogger("ccai.source")


class ConnectorSource:
    kind = "connectors"

    def __init__(self) -> None:
        from bsp_connectors import m365, mode_of  # noqa: F401  (import errors → stub)

        self._m365 = m365
        self._mode_of = mode_of

    @property
    def mode(self) -> str:
        try:
            return self._mode_of("m365")
        except Exception:
            return "mock"

    def list_files(self, site, folder, recursive=True):
        return self._m365.list_files(site, folder, recursive=recursive)

    def download_file(self, site, item_id):
        return self._m365.download_file(site, item_id)

    def upload_file(self, site, folder, name, data):
        return self._m365.upload_file(site, folder, name, data)


class StubSource:
    """Read-only view of the connectors fixture tree with Graph-shaped items."""

    kind = "stub"
    mode = "mock"

    def __init__(self, reason: str = "") -> None:
        self.reason = reason
        cands = [config.env("CCAI_FIXTURES_DIR"), config.env("CONNECTORS_FIXTURES_DIR") and
                 str(Path(config.env("CONNECTORS_FIXTURES_DIR")) / "m365" / "sites"),
                 "/opt/connectors/fixtures/m365/sites", str(config.REPO / "build/connectors/fixtures/m365/sites")]
        self.root = next((Path(c) for c in cands if c and Path(c).is_dir()), Path(cands[-1]))

    def _site_dir(self, site) -> Path:
        s = (site or "default").strip().rstrip("/").split("/")[-1] or "default"
        d = self.root / s
        return d if d.is_dir() else self.root / "default"

    @staticmethod
    def _id(rel: str) -> str:
        return "stub-" + hashlib.sha1(rel.encode()).hexdigest()[:24]

    def list_files(self, site, folder, recursive=True):
        base = self._site_dir(site)
        d = base / folder.strip("/")
        if not d.is_dir():
            raise FileNotFoundError(f"folder {folder!r} not found under {base}")
        out = []
        for p in sorted(d.rglob("*") if recursive else d.iterdir()):
            if not p.is_file() or p.name.startswith("."):
                continue
            rel = p.relative_to(base).as_posix()
            st = p.stat()
            out.append({"id": self._id(rel), "name": p.name, "size": st.st_size,
                        "lastModifiedDateTime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                        "eTag": f'"{{{hashlib.md5(f"{rel}{st.st_mtime}{st.st_size}".encode()).hexdigest()}}},1"',
                        "webUrl": f"stub://{rel}", "file": {"mimeType": mimetypes.guess_type(p.name)[0] or "application/octet-stream"},
                        "parentPath": p.parent.relative_to(base).as_posix()})
        return out

    def download_file(self, site, item_id):
        base = self._site_dir(site)
        for p in base.rglob("*"):
            if p.is_file() and self._id(p.relative_to(base).as_posix()) == item_id:
                return p.read_bytes()
        raise FileNotFoundError(item_id)

    def upload_file(self, site, folder, name, data):
        dest = config.data_path("published", folder.strip("/")) / name
        dest.write_bytes(data)
        return {"id": self._id(f"published/{folder}/{name}"), "name": name, "size": len(data),
                "webUrl": f"file://{dest}", "parentPath": folder}


_source = None


def get():
    global _source
    if _source is None:
        if config.env("CCAI_SOURCE") == "stub":
            _source = StubSource("CCAI_SOURCE=stub")
        else:
            try:
                _source = ConnectorSource()
            except Exception as e:  # SDK not on the path yet
                log.warning("bsp_connectors unavailable (%s) — using the fixture stub", e)
                _source = StubSource(f"bsp_connectors not importable: {e}")
    return _source


def describe() -> dict:
    s = get()
    return {"adapter": s.kind, "mode": s.mode, "site": config.sharepoint_site() or "(default site)",
            "folder": config.sharepoint_folder(), "publish_folder": config.publish_folder(),
            "note": getattr(s, "reason", "")}
