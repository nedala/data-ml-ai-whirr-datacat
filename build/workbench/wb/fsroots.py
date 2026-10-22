"""Safe access to the two file roots the analyst can browse:

    lake  — the shared lake (LAKE_DIR, /data/lake): landing/ reference/ exports/ samples/
    work  — the analyst's own workspace (/home/analyst/work)

Every user-supplied path is relative to a root. ``resolve`` rejects absolute
paths, ``..`` segments, NUL bytes and anything whose fully resolved location
(symlinks followed) is not inside the root, so neither crafted paths nor
symlinks can reach the rest of the filesystem.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path, PurePosixPath

from . import config


class PathError(ValueError):
    """Bad or unsafe path — shown to the user (HTTP 400/404/409)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


ROOT_LABELS = {"lake": "Shared lake", "work": "My work"}
HIDDEN = {".ipynb_checkpoints", "__pycache__", ".templates-manifest.json", ".DS_Store", ".git"}
NAME_MAX = 255
_BAD_NAME = re.compile(r"[\x00-\x1f/\\]")

TABLE_EXT = {".csv", ".tsv", ".txt", ".parquet", ".pq", ".xlsx", ".xlsm", ".xls", ".json", ".jsonl", ".ndjson"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
TEXT_EXT = {".txt", ".md", ".sql", ".py", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".log", ".r", ".sh",
            ".ipynb", ".xml", ".html", ".css", ".js", ".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".env.example"}


def roots() -> dict[str, Path]:
    return {"lake": config.LAKE_DIR, "work": config.WORK_DIR}


def root_dir(root: str) -> Path:
    r = roots().get(root)
    if r is None:
        raise PathError(f"unknown location {root!r}")
    return r


def clean_rel(rel: str | None) -> str:
    """Normalise a user-supplied relative path ('' = the root itself)."""
    rel = (rel or "").replace("\\", "/")
    if "\x00" in rel:
        raise PathError("invalid path")
    parts = [p for p in PurePosixPath(rel.strip("/")).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise PathError("'..' is not allowed in paths")
    if len("/".join(parts)) > 4096:
        raise PathError("path too long")
    return "/".join(parts)


def resolve(root: str, rel: str | None, *, must_exist: bool = True) -> Path:
    """Map (root, relative path) to an absolute path that is guaranteed to be
    inside the root after symlink resolution."""
    base = root_dir(root)
    try:
        base_real = base.resolve(strict=True)
    except OSError as exc:
        raise PathError(f"{ROOT_LABELS[root]} is not available ({base})", 503) from exc
    rel = clean_rel(rel)
    p = base_real / rel if rel else base_real
    real = p.resolve(strict=False)
    if real != base_real and not real.is_relative_to(base_real):
        raise PathError("path is outside the allowed folder", 403)
    if must_exist and not os.path.lexists(p):
        raise PathError("not found", 404)
    return real


def entry(root: str, rel: str | None) -> Path:
    """The directory entry itself (a symlink is NOT followed), inside a
    verified parent folder — used to rename/delete links safely."""
    rel = clean_rel(rel)
    if not rel:
        return resolve(root, "")
    parent_rel, _, name = rel.rpartition("/")
    parent = resolve(root, parent_rel)
    p = parent / name
    if not os.path.lexists(p):
        raise PathError("not found", 404)
    if not p.is_symlink():
        resolve(root, rel)
    return p


def rel_of(root: str, p: Path) -> str:
    return p.resolve().relative_to(root_dir(root).resolve()).as_posix()


def check_name(name: str) -> str:
    """A single file/folder name (no separators)."""
    if not isinstance(name, str):
        raise PathError("name must be text")
    name = name.strip()
    if not name or name in (".", "..") or _BAD_NAME.search(name) or len(name.encode()) > NAME_MAX:
        raise PathError("names can't be empty, '.', '..', or contain '/', '\\' or control characters")
    return name


def kind_of(name: str, is_dir: bool) -> str:
    if is_dir:
        return "folder"
    ext = Path(name).suffix.lower()
    if ext in (".csv", ".tsv", ".parquet", ".pq", ".xlsx", ".xlsm", ".xls", ".jsonl", ".ndjson"):
        return "table"
    if ext == ".json":
        return "table"
    if ext in IMAGE_EXT:
        return "image"
    if ext in (".py", ".sql", ".ipynb", ".yml", ".yaml", ".toml", ".sh", ".r", ".js", ".html"):
        return "code"
    if ext in TEXT_EXT:
        return "text"
    return "file"


def protected(root: str, rel: str) -> bool:
    """The root itself and the standard lake folders cannot be renamed/deleted."""
    rel = clean_rel(rel)
    return rel == "" or (root == "lake" and rel in config.LAKE_FOLDERS)


def listing(root: str, rel: str, show_hidden: bool = False, limit: int = 5000) -> dict:
    d = resolve(root, rel)
    if not d.is_dir():
        raise PathError("not a folder", 400)
    base_real = root_dir(root).resolve()
    entries = []
    truncated = False
    with os.scandir(d) as it:
        for e in it:
            if not show_hidden and (e.name.startswith(".") or e.name in HIDDEN):
                continue
            try:
                st = e.stat(follow_symlinks=True)
            except OSError:
                continue  # dangling link
            if e.is_symlink():
                real = Path(e.path).resolve()
                if real != base_real and not real.is_relative_to(base_real):
                    continue  # never expose links that escape the root
            is_dir = stat.S_ISDIR(st.st_mode)
            entries.append({"name": e.name, "type": "dir" if is_dir else "file",
                            "size": None if is_dir else st.st_size, "mtime": st.st_mtime,
                            "kind": kind_of(e.name, is_dir)})
            if len(entries) >= limit:
                truncated = True
                break
    entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower()))
    rel = clean_rel(rel)
    crumbs = [{"name": ROOT_LABELS[root], "path": ""}]
    acc = []
    for part in rel.split("/") if rel else []:
        acc.append(part)
        crumbs.append({"name": part, "path": "/".join(acc)})
    return {"root": root, "path": rel, "crumbs": crumbs, "entries": entries, "truncated": truncated,
            "protected": protected(root, rel)}


def mkdir(root: str, rel: str, name: str) -> str:
    parent = resolve(root, rel)
    if not parent.is_dir():
        raise PathError("not a folder")
    target = resolve(root, f"{clean_rel(rel)}/{check_name(name)}", must_exist=False)
    if os.path.lexists(target):
        raise PathError(f"'{name}' already exists", 409)
    target.mkdir()
    return rel_of(root, target)


def rename(root: str, rel: str, new_name: str) -> str:
    rel = clean_rel(rel)
    if protected(root, rel):
        raise PathError("this folder can't be renamed", 403)
    src = entry(root, rel)
    parent_rel = rel.rpartition("/")[0]
    new_name = check_name(new_name)
    dst = src.parent / new_name
    if dst.name == src.name:
        return rel
    if os.path.lexists(dst) and dst.name.lower() != src.name.lower():
        raise PathError(f"'{new_name}' already exists here", 409)
    os.rename(src, dst)
    return f"{parent_rel}/{new_name}".lstrip("/")


def delete(root: str, rels: list[str]) -> list[str]:
    targets = []
    for rel in rels:
        rel = clean_rel(rel)
        if protected(root, rel):
            raise PathError(f"'{rel or ROOT_LABELS[root]}' can't be deleted", 403)
        # a link inside the root is removed itself, never followed
        targets.append((rel, entry(root, rel)))
    done = []
    for rel, p in targets:
        if p.is_symlink() or not p.is_dir():
            p.unlink()
        else:
            shutil.rmtree(p)
        done.append(rel)
    return done


def unique_name(folder: Path, name: str) -> str:
    """'tickets.csv' → 'tickets (2).csv' if taken."""
    if not os.path.lexists(folder / name):
        return name
    stem, ext = os.path.splitext(name)
    for i in range(2, 10000):
        cand = f"{stem} ({i}){ext}"
        if not os.path.lexists(folder / cand):
            return cand
    raise PathError("too many files with this name", 409)


def safe_filename(name: str, default: str = "download") -> str:
    """Turn an arbitrary string (URL segment, export name) into a safe file name."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f]", "", name).strip().strip(".")
    name = re.sub(r"[^A-Za-z0-9._ ()+-]", "_", name)
    return (name or default)[:200]


def ensure_layout() -> None:
    for sub in config.LAKE_FOLDERS:
        try:
            (config.LAKE_DIR / sub).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
