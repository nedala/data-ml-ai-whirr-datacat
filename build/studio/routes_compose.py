"""Composer, jobs and launchpad APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import audit
import auth
import catalog
import certs
import composefile
import dockerops
import jobs
import orchestrator
import pluginhost
import settings
from vault import store

router = APIRouter(prefix="/studio/api")


@router.get("/compose/catalog")
def compose_catalog(user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    cat = catalog.load()
    personas = []
    for k, v in cat["personas"].items():
        caps = v.get("capabilities", [])
        personas.append({"id": k, **v, "capabilities": [c for c in caps if c in cat["capabilities"]],
                         "missing": [c for c in caps if c not in cat["capabilities"]]})
    return {
        "personas": personas,
        "default_persona": catalog.default_persona(),
        "appliance_name": settings.appliance_name(),
        "capabilities": [{"id": k, **v} for k, v in cat["capabilities"].items()],
        "services": {k: {"title": v.get("title", k), "blurb": v.get("blurb", ""), "ram_mb": v.get("ram_mb", 0)}
                     for k, v in cat["services"].items()},
        "selection": orchestrator.load_selection(),
        "can_apply": auth.has_role(user, "analyst"),
    }


class Selection(BaseModel):
    personas: list[str] = []
    capabilities: list[str] | None = None


def _plan(sel: Selection) -> dict[str, Any]:
    try:
        return orchestrator.plan(sel.personas, sel.capabilities)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/compose/plan")
def compose_plan(sel: Selection, user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    return _plan(sel)


@router.post("/compose/apply")
def compose_apply(sel: Selection, request: Request,
                  user: auth.User = Depends(auth.require_role("analyst"))) -> dict[str, Any]:
    _plan(sel)  # validates names
    caps = sel.capabilities if sel.capabilities is not None else catalog.persona_capabilities(sel.personas)
    job = orchestrator.apply(sel.personas, caps, user.name)
    audit.record("compose.apply", f"personas={','.join(sel.personas) or '-'} capabilities={','.join(caps)} "
                 f"start={','.join(s.service for s in job.steps if s.target == 'running') or '-'} "
                 f"stop={','.join(s.service for s in job.steps if s.target == 'stopped') or '-'}",
                 user=user.name, peer=request.client.host if request.client else "")
    return {"job": job.id}


@router.get("/jobs")
def list_jobs(user: auth.User = Depends(auth.require_user)) -> list[dict[str, Any]]:
    return [{k: v for k, v in j.public().items() if k not in ("log",)} for j in jobs.recent()]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, log_from: int = 0, user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "No such job (Studio may have restarted)")
    return job.public(log_from)


# ── launchpad ────────────────────────────────────────────────────────────
def _probe(target: str, timeout: float = 0.8) -> bool:
    import socket  # noqa: PLC0415
    host, _, port = target.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _cred_owner(env: str) -> tuple[str, dict[str, Any]] | None:
    for svc in catalog.load()["services"]:
        for cred in catalog.cred_slots(svc):
            if cred["env"] == env:
                return svc, cred
    return None


def health_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    vs = store.status()
    items.append({"id": "vault", "label": "Vault", "state": {"unlocked": "ok", "locked": "warning"}.get(vs, "error"),
                  "detail": {"unlocked": "Unlocked", "locked": "Locked — unlock to start services",
                             "uninitialized": "Not created"}[vs]})
    if not dockerops.available():
        items.append({"id": "docker", "label": "Docker", "state": "error", "detail": "Docker socket not reachable"})
    else:
        px = dockerops.status().get("px-proxy", {})
        ok = px.get("state") == "running"
        items.append({"id": "egress", "label": "Egress proxy", "state": "ok" if ok else "error",
                      "detail": "Allowlist enforced" if ok else f"px-proxy is {px.get('state', 'missing')}"})
    try:
        info = certs.describe()
    except ValueError as exc:
        info = {"server": None, "termination": "nginx", "error": str(exc)}
    if info.get("termination") == "ingress":
        items.append({"id": "tls", "label": "TLS", "state": "ok", "detail": "Terminated by the ingress"})
    elif info.get("server"):
        days = info["server"]["days_left"]
        items.append({"id": "tls", "label": "TLS certificate", "state": "ok" if days > 30 else ("warning" if days > 0 else "error"),
                      "detail": f"Expires in {days} days" if days > 0 else "Expired"})
    else:
        items.append({"id": "tls", "label": "TLS certificate", "state": "error", "detail": "No facade certificate"})
    for err in catalog.load().get("fragment_errors", []):
        items.append({"label": "Catalog fragment", "state": "warning", "detail": f"skipped {err}"})
    for err in pluginhost.registry.errors:
        items.append({"label": f"{err['plugin']} plugin", "state": "warning", "detail": f"{err['where']}: {err['error']}"})
    plugin_items = pluginhost.registry.health()
    overridden = {i.get("label") for i in plugin_items}   # a plugin may own a built-in check (e.g. egress)
    return [i for i in items if i["label"] not in overridden] + plugin_items


_marker_cache: dict[str, tuple[float, bool]] = {}


def _app_enabled(app: dict[str, Any], status: dict[str, Any]) -> bool:
    """`requires_env: VAR` — hidden when VAR is 0/false; when the app's
    service runs, also hidden unless /opt/appliance-features/<app id> exists
    in its container (marker written by the image build, e.g. Antigravity)."""
    var = app.get("requires_env")
    if not var:
        return True
    value = (settings._env(var) or settings.dotenv().get(var, "1")).strip().lower()
    if value in ("0", "false", "no", "off"):
        return False
    svc = app.get("service", "")
    if status.get(svc, {}).get("state") != "running":
        return True
    import time  # noqa: PLC0415
    hit = _marker_cache.get(app["id"])
    if hit and time.time() - hit[0] < 60:
        return hit[1]
    code, _ = dockerops.exec_in(svc, ["test", "-e", f"/opt/appliance-features/{app['id']}"], timeout=10)
    _marker_cache[app["id"]] = (time.time(), code == 0)
    return code == 0


@router.get("/launchpad")
def launchpad(user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    cat = catalog.load()
    status = dockerops.status()
    apps = []
    for app in cat["apps"]:
        if not _app_enabled(app, status):
            continue
        svc = app["service"]
        st = status.get(svc, {})
        cred = None
        if app.get("cred") and (owner := _cred_owner(app["cred"])):
            cred = {"service": owner[0], "env": app["cred"], "label": owner[1].get("label", app["cred"]),
                    "user": owner[1].get("user", "")}
        elif isinstance(app.get("cred_file"), dict):
            cf = app["cred_file"]
            cred = {"service": f"file:{app['id']}", "env": cf["env"], "label": cf.get("label", cf["env"]),
                    "user": cf.get("user", "")}
        if app.get("probe"):   # side stacks: running when their front door answers
            st = {"state": "running" if _probe(str(app["probe"])) else "missing"}
        caps = catalog.capabilities_of_service(svc)
        alt = app.get("alt") if isinstance(app.get("alt"), dict) else None
        if alt:
            alt = {"label": alt.get("label", "Open"), "url": composefile._interpolate(str(alt.get("url", "")))}
        apps.append({**{k: app.get(k) for k in ("id", "title", "icon", "blurb", "service", "group")}, "alt": alt,
                     "path": composefile._interpolate(str(app.get("path", ""))),
                     "state": st.get("state", "missing"), "started_at": st.get("started_at"),
                     "cred": cred, "capability": caps[0] if caps else None})
    sel = orchestrator.load_selection()
    connect = [{"service": s, "title": spec.get("title", s), "endpoints": catalog.endpoints(s)}
               for s, spec in cat["services"].items()
               if spec.get("endpoints") and status.get(s, {}).get("state") in orchestrator.UP]
    lead = catalog.default_persona()
    order = cat["personas"].get(lead, {}).get("groups", [])
    groups = sorted(cat.get("app_groups", []),
                    key=lambda g: order.index(g["id"]) if g.get("id") in order else len(order))
    return {"apps": apps, "groups": groups, "connect": connect,
            "persona": lead, "appliance_name": settings.appliance_name(), "health": health_items(), "selection": sel,
            "personas": {k: v["title"] for k, v in cat["personas"].items()},
            "running": sum(1 for s in cat["services"] if status.get(s, {}).get("state") in orchestrator.UP),
            "active_job": (j.id if (j := jobs.active()) else None),
            "is_admin": auth.has_role(user, "admin")}
