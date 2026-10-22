"""/files/api — a local file browser over the shared lake (/data/lake) and the
analyst's workspace (/home/analyst/work).

Every path parameter is (root, relative path) and goes through
``fsroots.resolve`` — no absolute paths, no '..', no symlink escapes.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import urllib.parse

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, fsroots, tabular
from .fsroots import PathError

router = APIRouter(prefix="/files/api")


def _err(exc: Exception):
    if isinstance(exc, PathError):
        raise HTTPException(exc.status, str(exc))
    if isinstance(exc, tabular.TabularError):
        raise HTTPException(422, str(exc))
    if isinstance(exc, PermissionError):
        raise HTTPException(403, "permission denied — this file or folder isn't writable by the analyst user")
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(404, "not found")
    if isinstance(exc, OSError):
        raise HTTPException(500, f"file system error: {exc.strerror or exc}")
    raise exc


async def _call(fn, *a, **kw):
    try:
        return await run_in_threadpool(fn, *a, **kw)
    except (PathError, tabular.TabularError, OSError) as exc:
        _err(exc)


class MkDir(BaseModel):
    root: str
    path: str = ""
    name: str = Field(max_length=255)


class Rename(BaseModel):
    root: str
    path: str
    new_name: str = Field(max_length=255)


class Delete(BaseModel):
    root: str
    paths: list[str] = Field(min_length=1, max_length=1000)


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/status")
def status():
    fsroots.ensure_layout()
    out = []
    for rid, path in fsroots.roots().items():
        ok = path.is_dir()
        free = None
        if ok:
            try:
                free = shutil.disk_usage(path).free
            except OSError:
                pass
        out.append({"id": rid, "label": fsroots.ROOT_LABELS[rid], "path": str(path), "exists": ok,
                    "writable": ok and os.access(path, os.W_OK), "free": free,
                    "folders": [f for f in config.LAKE_FOLDERS if (path / f).is_dir()] if rid == "lake" else []})
    return {"roots": out, "limits": {"upload_max_bytes": config.UPLOAD_MAX_BYTES,
                                     "text_preview_bytes": config.TEXT_PREVIEW_BYTES}}


@router.get("/list")
async def list_dir(root: str, path: str = "", hidden: bool = False):
    return await _call(fsroots.listing, root, path, hidden)


@router.post("/mkdir")
async def mkdir(body: MkDir):
    return {"path": await _call(fsroots.mkdir, body.root, body.path, body.name)}


@router.post("/rename")
async def rename(body: Rename):
    return {"path": await _call(fsroots.rename, body.root, body.path, body.new_name)}


@router.post("/delete")
async def delete(body: Delete):
    return {"deleted": await _call(fsroots.delete, body.root, body.paths)}


def _disposition(name: str, inline: bool = False) -> str:
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_").replace("?", "_")
    return f'{"inline" if inline else "attachment"}; filename="{ascii_name}"; filename*=UTF-8\'\'{urllib.parse.quote(name)}'


@router.get("/download")
def download(root: str, path: str):
    try:
        p = fsroots.resolve(root, path)
    except PathError as exc:
        _err(exc)
    if not p.is_file():
        raise HTTPException(400, "only files can be downloaded")
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Content-Disposition": _disposition(p.name), "Cache-Control": "no-store"})


IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml", ".bmp": "image/bmp"}


@router.get("/raw")
def raw(root: str, path: str):
    """Inline images for the preview pane only (everything else downloads).
    A sandbox CSP stops an SVG from running script even if opened directly."""
    try:
        p = fsroots.resolve(root, path)
    except PathError as exc:
        _err(exc)
    ct = IMAGE_TYPES.get(p.suffix.lower())
    if not p.is_file() or not ct:
        raise HTTPException(400, "inline preview is only available for images")
    if p.stat().st_size > config.IMAGE_PREVIEW_BYTES:
        raise HTTPException(413, "image too large to preview — download it instead")
    return FileResponse(p, media_type=ct, headers={
        "Content-Disposition": _disposition(p.name, inline=True),
        "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox",
        "Cache-Control": "no-store"})


def _preview(root: str, path: str, sheet: str | None, rows: int) -> dict:
    p = fsroots.resolve(root, path)
    if not p.is_file():
        raise PathError("not a file")
    st = p.stat()
    base = {"name": p.name, "size": st.st_size, "mtime": st.st_mtime, "kind": fsroots.kind_of(p.name, False)}
    ext = p.suffix.lower()
    if ext in IMAGE_TYPES:
        return {**base, "type": "image"}
    if tabular.fmt_of(p) and ext != ".txt":
        try:
            t = tabular.inspect(p, sheet=sheet, rows=rows)
            return {**base, "type": "table", **t, "loadable": True}
        except tabular.TabularError as exc:
            if ext not in (".csv", ".tsv", ".json", ".jsonl", ".ndjson"):
                return {**base, "type": "none", "message": str(exc)}
            base["message"] = str(exc)
    if ext in fsroots.TEXT_EXT or ext == "" or _looks_text(p):
        with open(p, "rb") as fh:
            data = fh.read(config.TEXT_PREVIEW_BYTES + 1)
        return {**base, "type": "text", "text": data[: config.TEXT_PREVIEW_BYTES].decode("utf-8", "replace"),
                "truncated": len(data) > config.TEXT_PREVIEW_BYTES}
    return {**base, "type": "none", "message": base.get("message") or "No preview for this file type — download it to open it."}


def _looks_text(p) -> bool:
    try:
        with open(p, "rb") as fh:
            head = fh.read(4096)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


@router.get("/preview")
async def preview(root: str, path: str, sheet: str | None = None, rows: int = 100):
    return await _call(_preview, root, path, sheet, max(1, min(rows, 500)))


@router.put("/upload")
async def upload(request: Request, root: str, path: str = "", name: str = "", overwrite: bool = False):
    """Streamed upload: the raw request body is written to a temp file in the
    target folder and moved into place when complete (no partial files)."""
    try:
        folder = fsroots.resolve(root, path)
        if not folder.is_dir():
            raise PathError("upload target is not a folder")
        name = fsroots.check_name(name)
        target = fsroots.resolve(root, f"{fsroots.clean_rel(path)}/{name}", must_exist=False)
    except PathError as exc:
        _err(exc)
    if target.exists() and not overwrite:
        raise HTTPException(409, f"'{name}' already exists")
    if target.exists() and target.is_dir():
        raise HTTPException(409, f"'{name}' is a folder")
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        n = int(declared)
        if n > config.UPLOAD_MAX_BYTES:
            raise HTTPException(413, f"file is larger than the {config.UPLOAD_MAX_BYTES / 1024 ** 3:.0f} GB upload limit")
        try:
            if shutil.disk_usage(folder).free < n + 64 * 1024 ** 2:
                raise HTTPException(507, "not enough free disk space for this file")
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(dir=str(folder), prefix=".upload-", suffix=".part")
    got = 0
    try:
        with os.fdopen(fd, "wb") as fh:
            async for chunk in request.stream():
                got += len(chunk)
                if got > config.UPLOAD_MAX_BYTES:
                    raise HTTPException(413, "file is larger than the upload limit")
                fh.write(chunk)
        os.chmod(tmp, 0o664)
        os.replace(tmp, target)
        tmp = None
    except HTTPException:
        raise
    except OSError as exc:
        _err(exc)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    rel = f"{fsroots.clean_rel(path)}/{name}".lstrip("/")
    return {"root": root, "path": rel, "size": got, "name": name}
