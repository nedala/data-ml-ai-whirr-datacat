"""Cloud identities API (admin only): named Google Cloud credentials, live
capability tests, per-purpose assignments, access lists (principal switch),
Apply, and “Sign in with Google”. GET /my-identities is for every signed-in
user. Responses carry metadata only — never a key, token or refresh token."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import audit
import auth
import catalog
import composefile
import dockerops
import gcloud_login
import gcp_identities as gi
import jobs
import orchestrator
from vault import store

router = APIRouter(prefix="/studio/api/gcp")


def _peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _impact() -> dict[str, Any]:
    """Env vars that differ from what running containers got at the last
    Apply, and the running services that read them. A container started
    outside Studio (or before an identity was applied) holds empty or older
    values even when the vault is unchanged — compare what it actually has."""
    env = orchestrator.secret_env()
    changed = gi.changed_vars(env)
    status = dockerops.status()
    services: list[str] = []
    for var in changed:
        for svc in composefile.consumers(var):
            if status.get(svc, {}).get("state") in orchestrator.UP and svc not in services:
                services.append(svc)
    for var in gi.env_names():
        want = env.get(var, "")
        for svc in composefile.consumers(var):
            if svc in services or status.get(svc, {}).get("state") not in orchestrator.UP:
                continue
            if _running_env(svc).get(var) != want:
                services.append(svc)
    return {"pending": bool(changed or services), "changed": changed, "recreate": services}


def _running_env(service: str) -> dict[str, str]:
    """The environment a running container actually started with."""
    for container in dockerops.containers_for(service):
        if container.status != "running":
            continue
        pairs = ((container.attrs.get("Config") or {}).get("Env") or [])
        return {k: v for k, _, v in (p.partition("=") for p in pairs)}
    return {}


def _state() -> dict[str, Any]:
    out = gi.listing()
    try:
        out["impact"] = _impact()
    except Exception as exc:  # noqa: BLE001 — docker down must not hide the page
        out["impact"] = {"pending": False, "changed": [], "recreate": [], "error": str(exc)}
    return out


@router.get("/identities")
def list_identities(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    return _state()


@router.post("/identities")
async def add_identity(request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        upload = form.get("file")
        if upload is not None and hasattr(upload, "read"):
            blob = await upload.read(gi.MAX_JSON + 1)
            text = blob.decode("utf-8", "replace")
        else:
            text = str(form.get("json") or "")
        name, project = str(form.get("name") or ""), str(form.get("project") or "")
    else:
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "Send JSON {name, json} or a multipart upload (name, file)") from None
        if not isinstance(body, dict):
            raise HTTPException(400, "Send JSON {name, json}")
        name, text, project = str(body.get("name") or ""), str(body.get("json") or ""), str(body.get("project") or "")
    if not name.strip() and text.strip():
        try:
            info = gi.parse_json(text)
            name = info.get("client_email", "").split("@")[0] or "personal sign-in"
        except gi.IdentityError:
            pass
    ident = gi.add(name, text, user.name, project=project)
    audit.record("gcp_identity.add", f"{ident['name']} ({ident['type']}, {ident['principal'] or 'principal unknown'})",
                 user=user.name, peer=_peer(request))
    return {"identity": ident, **_state()}


@router.delete("/identities/{ident_id}")
def delete_identity(ident_id: str, request: Request, force: bool = False,
                    user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    try:
        done = gi.delete(ident_id, force=force)
    except KeyError:
        raise HTTPException(404, "No such identity") from None
    except gi.IdentityError as exc:
        raise HTTPException(409, str(exc)) from None
    audit.record("gcp_identity.delete", done["name"] + (f" (unassigned {', '.join(done['unassigned'])})"
                                                        if done["unassigned"] else ""), user=user.name, peer=_peer(request))
    return {"deleted": done, **_state()}


class TestBody(BaseModel):
    bucket: str = ""
    dataset: str = ""
    consumers: list[str] | None = None     # None = every consumer's checks


@router.post("/identities/{ident_id}/test")
def test_identity(ident_id: str, request: Request, body: TestBody | None = None,
                  user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    body = body or TestBody()
    try:
        result = gi.run_tests(ident_id, body.bucket, user.name, body.consumers, body.dataset)
    except KeyError:
        raise HTTPException(404, "No such identity") from None
    caps = ", ".join(f"{k}={v}" for k, v in result["capabilities"].items())
    audit.record("gcp_identity.test", f"{ident_id}: {caps}", user=user.name, peer=_peer(request))
    return {"result": result}


@router.post("/consumers/{key}/test")
def test_consumer(key: str, request: Request, body: TestBody | None = None,
                  user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Test what this consumer needs, with the identity it effectively uses."""
    body = body or TestBody()
    key = gi.ALIASES.get(key, key)
    if key not in gi.consumers():
        raise HTTPException(404, "No such consumer")
    ident_id, via = gi.effective(key, store.snapshot())
    if not ident_id:
        raise HTTPException(409, "No identity is assigned (it falls back to GCP_SA_JSON / application default "
                                 "credentials) — assign one first")
    data = store.snapshot()
    result = gi.run_tests(ident_id, body.bucket, user.name, [key], body.dataset, gi.effective_projects(key, data))
    audit.record("gcp_identity.test", f"{key} as {ident_id}: {result['consumers'].get(key, {}).get('verdict')}",
                 user=user.name, peer=_peer(request))
    return {"result": result, "identity": ident_id, "via": via}


class IdentityPatch(BaseModel):
    project: str | None = None
    name: str | None = None
    known_projects: list[Any] | str | None = None


@router.patch("/identities/{ident_id}")
def patch_identity(ident_id: str, body: IdentityPatch, request: Request,
                   user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    try:
        ident = gi.update_identity(ident_id, body.project, body.name, body.known_projects)
    except KeyError:
        raise HTTPException(404, "No such identity") from None
    audit.record("gcp_identity.update", f"{ident_id}: project={ident['project']} name={ident['name']}",
                 user=user.name, peer=_peer(request))
    return {"identity": ident, **_state()}


@router.get("/identities/{ident_id}/projects")
def identity_projects(ident_id: str, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Projects this identity can see (for the project pickers) — ids and names only."""
    try:
        return gi.list_projects(ident_id)
    except KeyError:
        raise HTTPException(404, "No such identity") from None


class ProbeBody(BaseModel):
    projects: list[str] | None = None


@router.post("/identities/{ident_id}/probe")
def probe(ident_id: str, request: Request, body: ProbeBody | None = None,
          user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Datasets visible + jobs.create per project, and a proposed BigQuery · read
    setting (not applied)."""
    try:
        out = gi.probe_projects(ident_id, (body or ProbeBody()).projects)
    except KeyError:
        raise HTTPException(404, "No such identity") from None
    audit.record("gcp_identity.probe", f"{ident_id}: {len(out['projects'])} project(s)", user=user.name, peer=_peer(request))
    return out


class AccessBody(BaseModel):
    mode: str
    users: list[str] | str | None = None


@router.put("/identities/{ident_id}/access")
def put_access(ident_id: str, body: AccessBody, request: Request,
               user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Who may act as this identity (the principal switch in Import & Export,
    the canvas and MCP): owner + admins, an explicit list, or everyone."""
    before = gi.access_of(gi.identities().get(ident_id) or {})
    try:
        ident = gi.set_access(ident_id, body.mode, body.users or [], user.name)
    except KeyError:
        raise HTTPException(404, "No such identity") from None
    a = ident["access"]
    audit.record("gcp_identity.access", f"{ident_id}: {before['mode']}{before['users'] or ''} -> {a['mode']}"
                 + (f" {', '.join(a['users'])}" if a["users"] else ""), user=user.name, peer=_peer(request))
    return {"identity": ident, **_state()}


@router.get("/my-identities")
def my_identities(request: Request, user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    """Identities the signed-in user may act as (name, principal, type,
    projects) and each data tool's assigned default. No secrets."""
    session = auth.read_session(request)
    if not store.is_unlocked():
        raise HTTPException(423, "The vault is locked")
    return gi.my_identities(user.name, user.role, session.email if session else "")


@router.get("/assignments")
def get_assignments(user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    gi.migrate(store)
    return {"assignments": gi.assignments(), "impact": _impact()}


@router.put("/assignments")
def put_assignments(body: dict[str, Any], request: Request,
                    user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    asg = gi.set_assignments({k: (v if isinstance(v, dict) else str(v or "")) for k, v in body.items()})
    audit.record("gcp_identity.assign", ", ".join(f"{k}={v or '-'}" for k, v in asg.items()),
                 user=user.name, peer=_peer(request))
    return {"assignments": asg, "impact": _impact()}


@router.post("/apply")
def apply(request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    """Recreate the running services that read a changed GCP_* variable
    (docked progress, like credential rotation)."""
    if not store.is_unlocked():
        raise HTTPException(423, "Unlock the vault first")
    impact = _impact()
    audit.record("gcp_identity.apply", ", ".join(impact["recreate"]) or "(nothing running)",
                 user=user.name, peer=_peer(request))
    if not impact["recreate"]:
        gi.mark_applied(orchestrator.secret_env())
        return {"job": None, "impact": impact}
    titles = catalog.load()["services"]
    steps = [jobs.Step(s, titles.get(s, {}).get("title", s), "running") for s in impact["recreate"]]

    def run(job: jobs.Job) -> None:
        env = orchestrator.secret_env()
        job.line(f"cloud identities changed: {', '.join(impact['changed'])}")
        orchestrator.up_in_job(job, [s.service for s in steps], recreate=True)
        if failed := [s.title for s in job.steps if s.phase == "failed"]:
            job.state, job.error = "failed", "Did not come back: " + ", ".join(failed)
        else:
            gi.mark_applied(env)

    job = jobs.submit("rotate", "Applying cloud identities", user.name, steps, run)
    return {"job": job.id, "impact": impact}


# ── Sign in with Google (gcloud application-default login) ───────────────
@router.post("/google-login/start")
def google_login_start(request: Request, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    try:
        out = gcloud_login.start(user.name)
    except gcloud_login.LoginError as exc:
        raise HTTPException(502, str(exc)) from None
    audit.record("gcp_identity.google_login.start", "", user=user.name, peer=_peer(request))
    return out


class CodeBody(BaseModel):
    code: str
    name: str = ""
    project: str = ""


@router.post("/google-login/{session}/code")
def google_login_code(session: str, body: CodeBody, request: Request,
                      user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    if body.name.strip():
        gi._clean_name(body.name)          # fail fast, before the one-shot code is spent
    if body.project.strip():
        gi._clean_project(body.project)
    try:
        text = gcloud_login.exchange(session, user.name, body.code)
    except KeyError:
        raise HTTPException(404, "This sign-in expired or was cancelled — start again") from None
    except gcloud_login.LoginError as exc:
        audit.record("gcp_identity.google_login.failed", str(exc)[:120], user=user.name, peer=_peer(request))
        raise HTTPException(400, str(exc)) from None
    import json as _json

    info = _json.loads(text)
    try:
        _token, email = gi._token(info)
    except gi.CheckFailed:
        email = ""
    name = body.name.strip() or email or "Google sign-in"
    taken = {r["name"].lower() for r in gi.identities().values()}
    base, n = name, 2
    while name.lower() in taken and not body.name.strip():
        name, n = f"{base} ({n})", n + 1
    ident = gi.add(name, text, user.name, project=body.project, source="google-login", probe=False)
    if email and not ident["principal"]:
        store.mutate(lambda d: d["gcp_identities"][ident["id"]].__setitem__("principal", email))
        ident["principal"] = email
    audit.record("gcp_identity.add", f"{ident['name']} (authorized_user via Sign in with Google, {email or '?'})",
                 user=user.name, peer=_peer(request))
    return {"identity": ident, **_state()}


@router.delete("/google-login/{session}")
def google_login_cancel(session: str, user: auth.User = Depends(auth.require_admin)) -> dict[str, Any]:
    try:
        gcloud_login.cancel(session, user.name)
    except KeyError:
        pass
    return {"cancelled": True}
