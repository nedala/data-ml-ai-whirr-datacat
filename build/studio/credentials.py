"""Credential slots (catalog `creds`): listing, reveal, rotation. Rotation
order matters: plugin ROTATE hooks run first (they connect with the *current*
vault value), then the vault is updated, then every running container that
reads the variable is recreated."""

from __future__ import annotations

from typing import Any

import catalog
import composefile
import dockerops
import jobs
import orchestrator
import pluginhost
import settings
from vault import store

# Applied only when the service's data volume is first initialised and no
# live-rotation hook exists: rotating would leave the service on the old value.
INIT_ONLY: set[str] = set()
SHARED_ROOT_MIN = 3   # this many running consumers ⇒ "shared root credential" warning


class CredentialError(ValueError):
    pass


def find_slot(service: str, env: str) -> dict[str, Any]:
    for cred in catalog.cred_slots(service):
        if cred["env"] == env:
            return cred
    raise CredentialError(f"{service} has no credential {env}")


def _running(status: dict[str, dict[str, Any]], service: str) -> bool:
    return status.get(service, {}).get("state") in orchestrator.UP


def slots() -> list[dict[str, Any]]:
    cat = {**catalog.load()["services"], catalog.SHARED: catalog.service_spec(catalog.SHARED)}
    status = dockerops.status()
    data = store.snapshot()
    values, rotated = data.get("services", {}), data.get("meta", {}).get("rotated", {})
    out = []
    local = _appliance_services(status)
    for svc, spec in cat.items():
        for cred in spec.get("creds", []) or []:
            env, slot = cred["env"], values.get(svc, {})
            user = slot.get(cred["user_env"]) if cred.get("user_env") else None
            out.append({
                "kind": "service", "service": svc, "service_title": spec.get("title", svc), "env": env,
                "label": cred.get("label", env), "user": user or cred.get("user", ""),
                "user_env": cred.get("user_env"), "hidden": bool(cred.get("hidden")),
                "policy": cred.get("policy", "default"), "length": cred.get("length", 32),
                "set": bool(slot.get(env)), "rotated": rotated.get(svc, {}).get(env),
                "running": _running(status, svc),
                "live_rotation": bool(pluginhost.registry.rotators(env)), "init_only": env in INIT_ONLY,
                "external": bool(cred.get("external")), "multiline": bool(cred.get("multiline")),
                "hint": cred.get("hint", ""), "shared": svc == catalog.SHARED,
                "in_appliance": svc == catalog.SHARED or svc in local,
            })
    return out


def _appliance_services(status: dict[str, dict[str, Any]]) -> set[str]:
    """Services that belong to THIS appliance: running, or part of the saved
    selection (or the appliance persona's default when nothing is saved)."""
    out = {s for s in status if _running(status, s)}
    try:
        sel = orchestrator.load_selection() or {}
        caps = sel.get("capabilities")
        if caps is None:
            import settings
            persona = settings.cfg.appliance_persona
            caps = catalog.persona_capabilities([persona]) if persona in catalog.load()["personas"] else []
        out |= set(catalog.resolve(capabilities=[c for c in caps if c in catalog.load()["capabilities"]]).services)
    except Exception:  # noqa: BLE001 — a bad selection must not hide the page
        pass
    return out


def file_cred(app_id: str, env: str) -> dict[str, Any]:
    """A launchpad credential that a side stack keeps in its own secrets file (catalog app `cred_file`)."""
    for app in catalog.load()["apps"]:
        cf = app.get("cred_file") if isinstance(app.get("cred_file"), dict) else None
        if app.get("id") == app_id and cf and cf.get("env") == env:
            return cf
    raise CredentialError(f"{app_id} has no credential {env}")


def reveal_file(app_id: str, env: str) -> str:
    cf = file_cred(app_id, env)
    path = (settings.cfg.data / str(cf["path"])).resolve()
    if settings.cfg.data.resolve() not in path.parents:
        raise CredentialError("credential file is outside the data root")
    try:
        for line in path.read_text().splitlines():
            if line.startswith(f"{env}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    raise CredentialError("Not available yet. It is created the first time the stack starts (make datahub-up).")


def reveal(kind: str, service: str, env: str) -> str:
    if service.startswith("file:"):
        return reveal_file(service[5:], env)
    find_slot(service, env)
    value = store.get(f"services.{service}.{env}")
    if not value:
        raise CredentialError("Not generated yet — it is created automatically the first time the service starts")
    return str(value)


def impact(service: str, env: str) -> dict[str, Any]:
    find_slot(service, env)
    status = dockerops.status()
    consumers = [s for s in composefile.consumers(env) if _running(status, s)]
    hooks = [name for name, _ in pluginhost.registry.rotators(env)]
    spec = catalog.service_spec(service)
    has_value = bool(store.get(f"services.{service}.{env}"))
    warning, blocking = "", False
    if env in INIT_ONLY and has_value:
        blocking = True
        warning = (f"Applies on first start only — {spec.get('title', service)} reads it when its workspace volume "
                   f"is created. To change it, reset the {spec.get('title', service)} workspace (or change the "
                   "password inside the app and record it here with “Set custom value”).")
    elif len(consumers) >= SHARED_ROOT_MIN and spec.get("kind") == "base":
        warning = (f"Shared root credential: {len(consumers)} running services read it and will ALL be recreated "
                   f"({', '.join(consumers)}). Expect a short outage; running queries and notebook kernels "
                   "connected to them will be interrupted.")
    elif _running(status, service) and not hooks and spec.get("kind") == "base":
        warning = ("This service keeps state and has no live-rotation hook: it will be recreated with the "
                   "new value, but a database initialised with the old one may keep it.")
    elif has_value and not _running(status, service) and hooks:
        warning = (f"{spec.get('title', service)} is not running, so the new value cannot be applied to it "
                   "live. If it has run before, start it first and rotate again.")
    return {"recreate": consumers, "hooks": hooks, "running": _running(status, service), "warning": warning,
            "strong": blocking or len(consumers) >= SHARED_ROOT_MIN, "generate_blocked": blocking}


def rotate(service: str, env: str, value: str, user: str) -> jobs.Job:
    cred = find_slot(service, env)
    if problem := orchestrator.policy_error(value, cred.get("policy", "default")):
        raise CredentialError(problem)
    info = impact(service, env)
    titles = catalog.load()["services"]
    steps = [jobs.Step(s, titles.get(s, {}).get("title", s), "running") for s in info["recreate"]]
    hooks = pluginhost.registry.rotators(env) if info["running"] else []

    def run(job: jobs.Job) -> None:
        for plugin, fn in hooks:
            job.line(f"plugin {plugin}: applying new {env} to the running service")
            fn(value)

        def write(data: dict[str, Any]) -> None:
            data.setdefault("services", {}).setdefault(service, {})[env] = value
            orchestrator.mark_rotated(data, service, env)

        store.mutate(write)
        job.line(f"vault updated: services.{service}.{env}")
        if steps:
            orchestrator.up_in_job(job, [s.service for s in steps], recreate=True)
        if failed := [s.title for s in job.steps if s.phase == "failed"]:
            job.state, job.error = "failed", "Did not come back: " + ", ".join(failed)

    return jobs.submit("rotate", f"Rotating {cred.get('label', env)}", user, steps, run)


# ── bootstrap: seed empty shared slots from .env, once ───────────────────
# `.env` may carry bootstrap values for the shared credentials so a fresh
# appliance starts connected: `NAME=value`, or `NAME_FILE=path` (relative to
# the project root) for multi-line secrets such as a service-account key.
# They are copied into the vault ONLY while the slot is empty; from then on
# the vault is the source of truth and Studio → Credentials edits are never
# overwritten. Values are never logged.
def seed_from_env(vault: Any) -> list[str]:
    import settings

    env = settings.dotenv()
    seeded: list[str] = []
    shared = (vault.get("services", {}) or {}).get(catalog.SHARED, {}) or {}
    for name, spec in (catalog.load().get("shared_credentials") or {}).items():
        if shared.get(name):
            continue
        value, origin = env.get(name, ""), name
        if not value and env.get(f"{name}_FILE"):
            path = (settings.cfg.project / env[f"{name}_FILE"]).resolve()
            try:
                value, origin = path.read_text(encoding="utf-8").strip(), f"{name}_FILE"
            except OSError as exc:
                seeded.append(f"{name}: could not read {name}_FILE ({exc.strerror})")
                continue
        if not value:
            continue

        def write(data: dict[str, Any], name: str = name, value: str = value) -> None:
            data.setdefault("services", {}).setdefault(catalog.SHARED, {})[name] = value
            orchestrator.mark_rotated(data, catalog.SHARED, name)

        vault.mutate(write)
        seeded.append(f"{spec.get('label', name)} seeded from .env ({origin})")
    if seeded:
        import audit
        audit.record("credential.seed", "; ".join(seeded), user="studio")
    return seeded


store.on_open.append(seed_from_env)
