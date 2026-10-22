"""/pkg/api — Python packages, sample data, manifests."""
from __future__ import annotations

import platform
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import pypkgs, samples
from . import specs as S
from .config import PY_MANIFEST, PY_USER_SITE, PY_USERBASE, PYTHON
from .jobs import MANAGER

router = APIRouter(prefix="/pkg/api")


class Specs(BaseModel):
    specs: list[str] = Field(default_factory=list, max_length=500)
    pin: list[str] = Field(default_factory=list, max_length=500)
    upgrade: list[str] = Field(default_factory=list, max_length=500)


class Names(BaseModel):
    names: list[str] = Field(default_factory=list, max_length=1000)
    pinned: bool | None = None


class Text(BaseModel):
    text: str | None = Field(default=None, max_length=2_000_000)


class Hydrate(BaseModel):
    ids: list[str] = Field(max_length=200)
    target: str = "warehouse"


def _bad(exc: Exception):
    raise HTTPException(422, str(exc))


def _job(queue: str, kind: str, title: str, fn, meta: dict | None = None) -> dict:
    j = MANAGER.submit(queue, kind, title, fn, meta)
    return {"job": j.summary()}


def _short(items: list[str], n: int = 3) -> str:
    return ", ".join(items[:n]) + (f" +{len(items) - n}" if len(items) > n else "")


@router.get("/health")
async def health():
    return {"ok": True, "service": "workbench"}


@router.get("/status")
def status():
    return {
        "python": {"available": True, "version": platform.python_version(), "executable": PYTHON,
                   "installer": pypkgs.installer(), "userbase": str(PY_USERBASE), "site": str(PY_USER_SITE),
                   "manifest": str(PY_MANIFEST)},
    }


# ── Python ───────────────────────────────────────────────────────────────
@router.get("/python/packages")
async def py_packages():
    user = await run_in_threadpool(pypkgs.list_user)
    return {"user": user, "installer": pypkgs.installer(), "userbase": str(PY_USERBASE),
            "total_size": sum(u.get("size") or 0 for u in user)}


@router.get("/python/platform")
async def py_platform():
    return {"base": await run_in_threadpool(pypkgs.list_platform)}


@router.get("/python/search")
async def py_search(q: str = ""):
    return await run_in_threadpool(pypkgs.search, q[:100])


@router.get("/python/pypi/{name}")
async def py_info(name: str):
    try:
        return await run_in_threadpool(pypkgs.pypi_info, name)
    except S.SpecError as exc:
        _bad(exc)


@router.post("/python/resolve")
async def py_resolve(body: Specs):
    try:
        return await run_in_threadpool(pypkgs.plan, body.specs, body.upgrade)
    except S.SpecError as exc:
        _bad(exc)


@router.post("/python/install")
def py_install(body: Specs):
    try:
        fn = pypkgs.install_job(body.specs, body.pin)
    except S.SpecError as exc:
        _bad(exc)
    names = [S.py_spec(s)["text"] for s in body.specs]
    if not names:
        _bad(S.SpecError("nothing to install"))
    return _job("python", "install", f"Install {_short(names)}", fn, {"ecosystem": "python", "specs": names})


@router.post("/python/upgrade")
def py_upgrade(body: Names):
    try:
        names = [S.py_name(n) for n in body.names]
        fn = pypkgs.install_job(names, upgrade=names, reason="upgrade")
    except S.SpecError as exc:
        _bad(exc)
    return _job("python", "upgrade", f"Upgrade {_short(names)}", fn, {"ecosystem": "python", "names": names})


@router.post("/python/remove")
def py_remove(body: Names):
    try:
        fn = pypkgs.remove_job(body.names)
    except S.SpecError as exc:
        _bad(exc)
    return _job("python", "remove", f"Remove {_short(body.names)}", fn, {"ecosystem": "python", "names": body.names})


@router.post("/python/pin")
def py_pin(body: Names):
    try:
        pypkgs.set_pin(body.names, bool(body.pinned))
    except S.SpecError as exc:
        _bad(exc)
    return {"ok": True}


@router.get("/python/orphans")
async def py_orphans():
    return {"orphans": await run_in_threadpool(pypkgs.orphans)}


@router.get("/python/check")
async def py_check():
    return await run_in_threadpool(pypkgs.check)


@router.get("/python/manifest")
def py_manifest(download: bool = False):
    headers = {"Content-Disposition": 'attachment; filename="requirements.lock"'} if download else {}
    return PlainTextResponse(pypkgs.manifest_text(), headers=headers)


@router.post("/python/manifest/restore")
def py_restore(body: Text):
    if body.text is not None:
        _validate_py_manifest(body.text)
    return _job("python", "restore", "Restore Python manifest", pypkgs.restore_job(body.text), {"ecosystem": "python"})


@router.post("/python/requirements/parse")
def py_parse(body: Text):
    return S.parse_requirements(body.text or "")


def _validate_py_manifest(text: str) -> None:
    bad = S.parse_requirements(text)["rejected"]
    if bad:
        raise HTTPException(422, "invalid manifest line: " + bad[0]["line"] + " — " + bad[0]["reason"])


# ── sample data ──────────────────────────────────────────────────────────
@router.get("/samples")
async def samples_list():
    return await run_in_threadpool(samples.list_datasets)


@router.post("/samples/hydrate")
def samples_hydrate(body: Hydrate):
    try:
        fn = samples.hydrate_job(body.ids, body.target)
    except S.SpecError as exc:
        _bad(exc)
    return _job("data", "hydrate", f"Sample data {_short(body.ids, 2)} → {samples.TARGET_LABELS[body.target]}", fn,
                {"ids": body.ids, "target": body.target})


# ── combined manifest (Studio backups) ───────────────────────────────────
@router.get("/manifest")
def manifest_all():
    return {"python": pypkgs.manifest_text()}


@router.post("/manifest/restore")
def manifest_restore(body: dict[str, Any] = Body(...)):
    py = body.get("python")
    if not (isinstance(py, str) and py.strip()):
        return {"jobs": []}
    _validate_py_manifest(py)
    return {"jobs": [_job("python", "restore", "Restore Python manifest", pypkgs.restore_job(py), {"ecosystem": "python"})["job"]]}
