"""Users, audit log and certificates."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

import audit
import auth
import certs
import dockerops
import settings
import users

router = APIRouter(prefix="/studio/api")
public = APIRouter()


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


# ── users ────────────────────────────────────────────────────────────────
@router.get("/users")
def list_users(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    return {"users": users.list_all(), "roles": list(settings.ROLES), "sso": settings.cfg.oidc_enabled,
            "local_login": settings.cfg.local_login_allowed, "me": user.name,
            "admin_group": settings.cfg.oidc_admin_group,
            "admin_users": sorted(settings.cfg.oidc_admin_users)}


class NewUser(BaseModel):
    name: str
    role: str
    password: str | None = None
    source: str = "local"


@router.post("/users")
def add_user(body: NewUser, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if body.source not in ("local", "sso"):
        raise HTTPException(400, "source must be local or sso")
    created = users.add(body.name.strip(), body.role, body.password, body.source)
    audit.record("user.add", f"{body.name} role={body.role} source={body.source}", user=user.name, peer=_peer(request))
    return created


class RoleBody(BaseModel):
    role: str


@router.post("/users/{name}/role")
def set_role(name: str, body: RoleBody, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    users.set_role(name, body.role)
    audit.record("user.role", f"{name} → {body.role}", user=user.name, peer=_peer(request))
    return {"ok": "true"}


class PasswordBody(BaseModel):
    password: str


@router.post("/users/{name}/password")
def reset_password(name: str, body: PasswordBody, request: Request,
                   user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    users.set_password(name, body.password)
    audit.record("user.password_reset", name, user=user.name, peer=_peer(request))
    return {"ok": "true"}


@router.delete("/users/{name}")
def remove_user(name: str, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    if name == user.name:
        raise HTTPException(400, "You cannot remove yourself")
    users.remove(name)
    audit.record("user.remove", name, user=user.name, peer=_peer(request))
    return {"ok": "true"}


# ── audit ────────────────────────────────────────────────────────────────
@router.get("/audit")
def audit_log(q: str = "", event: str = "", limit: int = 500,
              user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    return {"entries": audit.tail(max(1, min(limit, 5000)), q, event)}


# ── certificates ─────────────────────────────────────────────────────────
@router.get("/certs")
def cert_info(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    info = certs.describe()
    info["default_hostnames"] = certs.default_hostnames(dockerops.host_name())
    info["facade_url"] = settings.facade_url()
    return info


class Hostnames(BaseModel):
    hostnames: list[str]


def _reload_nginx() -> str:
    code, out = dockerops.exec_in("nginx", ["/usr/sbin/nginx", "-c", "/etc/nginx/nginx.conf", "-s", "reload"], timeout=20)
    return "nginx reloaded" if code == 0 else f"nginx reload failed ({out.strip()[:160]}) — restart it with `docker compose restart nginx`"


@router.post("/certs/reissue")
def reissue(body: Hostnames, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if settings.cfg.appliance_env != "dev":
        raise HTTPException(400, "The appliance CA only issues certificates in dev — upload one instead")
    info = certs.issue_server_cert(body.hostnames)
    reload = _reload_nginx()
    audit.record("certs.reissue", ", ".join(info["server"]["sans"]), user=user.name, peer=_peer(request))
    return {**info, "reload": reload}


@router.post("/certs/upload")
async def upload(request: Request, cert: UploadFile = File(...), key: UploadFile = File(...),
                 user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    warnings = certs.install_upload(await cert.read(1 << 20), await key.read(1 << 20))
    reload = _reload_nginx()
    audit.record("certs.upload", cert.filename or "", user=user.name, peer=_peer(request))
    return {**certs.describe(), "warnings": warnings, "reload": reload}


@public.get("/studio/ca.pem")
def ca_pem() -> Response:
    path = certs.paths()["ca"]
    if not path.is_file():
        raise HTTPException(404, "No appliance CA in this environment")
    return Response(path.read_bytes(), media_type="application/x-pem-file",
                    headers={"Content-Disposition": 'attachment; filename="appliance-ca.pem"'})
