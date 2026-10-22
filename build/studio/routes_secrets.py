"""Credentials and configuration backup/restore."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import audit
import auth
import backup
import credentials
import orchestrator
from vault import store
import catalog
import dockerops
import re

router = APIRouter(prefix="/studio/api")


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


# ── credentials ──────────────────────────────────────────────────────────
@router.get("/credentials")
def list_credentials(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    return {"slots": credentials.slots(), "policies": list(orchestrator.POLICIES)}


class SlotRef(BaseModel):
    kind: str = "service"
    service: str
    env: str


@router.post("/credentials/reveal")
def reveal(ref: SlotRef, request: Request, user: auth.User = Depends(auth.require_step_up)) -> dict[str, str]:
    value = credentials.reveal(ref.kind, ref.service, ref.env)
    audit.record("credential.reveal", f"{ref.kind}:{ref.service}.{ref.env}", user=user.name, peer=_peer(request))
    return {"value": value}


@router.get("/credentials/impact")
def impact(service: str, env: str, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    return credentials.impact(service, env)


class RotateBody(BaseModel):
    service: str
    env: str
    value: str | None = None      # custom value; None = generate
    length: int = 32
    policy: str = "default"


@router.post("/credentials/rotate")
def rotate(body: RotateBody, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    credentials.find_slot(body.service, body.env)
    value = body.value if body.value is not None else orchestrator.generate_secret(body.length, body.policy)
    job = credentials.rotate(body.service, body.env, value, user.name)
    audit.record("credential.rotate", f"{body.service}.{body.env} ({'custom' if body.value is not None else 'generated'})",
                 user=user.name, peer=_peer(request))
    return {"job": job.id}


# ── backup / restore ─────────────────────────────────────────────────────
class ExportBody(BaseModel):
    passphrase: str


@router.post("/backup/export")
def export(body: ExportBody, request: Request, user: auth.User = Depends(auth.require_step_up)) -> Response:
    name, blob = backup.export(body.passphrase, user.name)
    audit.record("backup.export", name, user=user.name, peer=_peer(request))
    return Response(blob, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


@router.post("/backup/preview")
async def preview(request: Request, file: UploadFile = File(...), passphrase: str = Form(...),
                  user: auth.User = Depends(auth.require_step_up)) -> dict[str, Any]:
    blob = await file.read(20 * 1024 * 1024 + 1)
    if len(blob) > 20 * 1024 * 1024:
        raise HTTPException(413, "Backup file is larger than 20 MB")
    payload = backup.decrypt(blob, passphrase)
    audit.record("backup.preview", file.filename or "", user=user.name, peer=_peer(request))
    return {"token": backup.stage(payload, user.name), "diff": backup.preview(payload, user.name)}


class ApplyBody(BaseModel):
    token: str
    sections: list[str]
    confirm_overwrite: bool = False


@router.post("/backup/apply")
def apply(body: ApplyBody, request: Request, user: auth.User = Depends(auth.require_step_up)) -> dict[str, Any]:
    unknown = set(body.sections) - set(backup.SECTIONS)
    if unknown:
        raise HTTPException(400, f"Unknown sections: {', '.join(sorted(unknown))}")
    if "vault" in body.sections and store.is_unlocked() and not body.confirm_overwrite:
        raise HTTPException(409, "Importing replaces the secrets in your unlocked vault — confirm to continue")
    payload = backup.take(body.token, user.name)
    done = backup.apply(payload, body.sections, user.name)
    audit.record("backup.import", ", ".join(done), user=user.name, peer=_peer(request))
    return {"applied": done, "selection": payload.get("selection") if "selection" in body.sections else None}


# ── connection secrets (Integration Canvas `vault:NAME` references) ──────
_CONN_NAME = re.compile(r"[A-Z][A-Z0-9_]{1,63}")
_CONSUMER = "canvas"


def _reserved() -> set[str]:
    names = {c["env"] for c in catalog.shared_slots()}
    for spec in catalog.load()["services"].values():
        names |= {c["env"] for c in spec.get("creds", []) or []}
    return names


def _recreate_consumer(user: str) -> str | None:
    """Restart the canvas (if it runs) so it sees the new secret set."""
    if dockerops.status().get(_CONSUMER, {}).get("state") in orchestrator.UP:
        return orchestrator.start([_CONSUMER], user, recreate=True).id
    return None


@router.get("/connection-secrets")
def list_connection_secrets(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    data = store.snapshot()
    vals = (data.get("services", {}) or {}).get(orchestrator.CONNECTIONS, {}) or {}
    rotated = (data.get("meta", {}).get("rotated", {}) or {}).get(orchestrator.CONNECTIONS, {}) or {}
    return {"secrets": [{"name": k, "set": bool(v), "updated": rotated.get(k)} for k, v in sorted(vals.items())]}


class ConnectionSecretBody(BaseModel):
    value: str


@router.put("/connection-secrets/{name}")
def put_connection_secret(name: str, body: ConnectionSecretBody, request: Request,
                          user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if not _CONN_NAME.fullmatch(name):
        raise HTTPException(400, "Use UPPER_CASE letters, digits and _ (e.g. CARE_MSSQL_PASSWORD)")
    if name in _reserved():
        raise HTTPException(400, f"{name} is a built-in credential — set it in the tables above")
    if not body.value.strip():
        raise HTTPException(400, "The value is empty")

    def write(data: dict[str, Any]) -> None:
        data.setdefault("services", {}).setdefault(orchestrator.CONNECTIONS, {})[name] = body.value
        orchestrator.mark_rotated(data, orchestrator.CONNECTIONS, name)

    store.mutate(write)
    audit.record("connection_secret.set", name, user=user.name, peer=_peer(request))
    return {"name": name, "job": _recreate_consumer(user.name)}


@router.delete("/connection-secrets/{name}")
def delete_connection_secret(name: str, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if not store.get(f"services.{orchestrator.CONNECTIONS}.{name}"):
        raise HTTPException(404, f"No connection secret {name}")
    store.delete(f"services.{orchestrator.CONNECTIONS}.{name}")
    audit.record("connection_secret.delete", name, user=user.name, peer=_peer(request))
    return {"name": name, "job": _recreate_consumer(user.name)}
