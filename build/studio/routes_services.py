"""Services page: state, uptime, memory, logs, start/stop/restart."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

import audit
import auth
import catalog
import dockerops
import orchestrator

router = APIRouter(prefix="/studio/api/services")


def _known(service: str) -> None:
    if service not in catalog.load()["services"] and service not in catalog.CONTROL_PLANE:
        raise HTTPException(404, f"Unknown service {service!r}")


@router.get("")
def list_services(user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    cat = catalog.load()["services"]
    status = dockerops.status()
    rows = []
    for name in [*catalog.CONTROL_PLANE, *cat]:
        spec = cat.get(name, {})
        st = status.get(name, {})
        rows.append({"name": name, "title": spec.get("title") or catalog.CONTROL_PLANE.get(name, name),
                     "blurb": spec.get("blurb", "Always-on control plane"), "kind": spec.get("kind", "control"),
                     "profile": spec.get("profile") or spec.get("project") or "always on",
                     "capabilities": catalog.capabilities_of_service(name), "ram_estimate_mb": spec.get("ram_mb"),
                     "endpoints": catalog.endpoints(name),
                     **{k: st.get(k) for k in ("state", "health", "started_at", "image")},
                     "control_plane": name in catalog.CONTROL_PLANE})
    return {"services": rows, "docker": dockerops.available(), "can_manage": auth.has_role(user, "admin")}


@router.get("/memory")
def memory(user: auth.User = Depends(auth.require_user)) -> dict[str, int]:
    names = [*catalog.CONTROL_PLANE, *catalog.load()["services"]]
    status = dockerops.status()
    return dockerops.memory_usage([n for n in names if status.get(n, {}).get("state") in orchestrator.UP])


@router.get("/{service}/logs")
def logs(service: str, tail: int = 300, since: float = 0,
         user: auth.User = Depends(auth.require_role("analyst"))) -> dict[str, Any]:
    _known(service)
    lines, last = dockerops.logs(service, tail=max(10, min(tail, 2000)), since=since or None)
    return {"lines": lines, "since": last}


@router.post("/{service}/{action}")
def act(service: str, action: str, request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, str]:
    _known(service)
    if service in catalog.CONTROL_PLANE:
        raise HTTPException(400, "Control-plane services are managed with `make up` / `make down`")
    if action == "start":
        job = orchestrator.start([service], user.name)
    elif action == "stop":
        job = orchestrator.stop([service], user.name)
    elif action == "restart":
        job = orchestrator.start([service], user.name, recreate=True)
    else:
        raise HTTPException(404, f"Unknown action {action!r}")
    audit.record(f"service.{action}", service, user=user.name, peer=request.client.host if request.client else "")
    return {"job": job.id}
