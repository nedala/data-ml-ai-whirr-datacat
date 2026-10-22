"""Composer engine: plan a selection, then start / stop / recreate services
through `docker compose` with vault secrets in the subprocess environment.

Before every start: host data dirs exist (owned by uid 1000), every missing
credential slot of the services being started is generated, and plugin
before_up hooks have rendered their configs."""

from __future__ import annotations

import datetime as dt
import re
import secrets
import string
import subprocess
import threading
import time
from typing import Any

import catalog
import composefile
import dockerops
import jobs
import pluginhost
import settings
from fsutil import ensure_dir, read_json, write_private_json
from vault import PLACEHOLDERS, VaultLocked, store

HEALTH_TIMEOUT = 600
CONNECTIONS = "connections"   # vault namespace for Integration Canvas connection secrets
POLL = 1.5
PHASE_WORDS = {
    "pulling": "pulling", "pulled": "creating", "waiting": "creating", "creating": "creating",
    "created": "starting", "recreate": "creating", "recreated": "starting", "starting": "starting",
    "started": "started", "healthy": "started", "running": "started",
    "stopping": "stopping", "stopped": "stopped", "removing": "stopping", "removed": "stopped",
}
_LINE = re.compile(r"(?:Container\s+(?P<ctr>\S+)|(?P<svc>[a-z][\w.-]*))\s+(?P<word>"
                   + "|".join(w.capitalize() for w in PHASE_WORDS) + r")\b")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ── secrets ──────────────────────────────────────────────────────────────
ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "-_"
POLICIES = ("default", "alnum", "strong")   # strong: alnum, starts with a letter, upper+lower+digit (SQL Server / Oracle)


def generate_secret(length: int = 32, policy: str = "default") -> str:
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    length = max(12, min(int(length), 256))
    if policy == "alnum":
        return "".join(secrets.choice(ALNUM) for _ in range(length))
    if policy == "strong":
        while True:
            value = secrets.choice(string.ascii_letters) + "".join(secrets.choice(ALNUM) for _ in range(length - 1))
            if not policy_error(value, "strong"):
                return value
    return "".join(secrets.choice(URLSAFE) for _ in range(length))


def policy_error(value: str, policy: str) -> str:
    if policy == "any":            # external keys (JSON, tokens): passed verbatim through the process env
        return "" if value.strip() else "Must not be empty"
    if len(value) < 8:
        return "Must be at least 8 characters"
    if any(c in value for c in "$\"'`\\\n"):
        return "Avoid $ quotes backticks and backslashes — they break env interpolation"
    if policy in ("alnum", "strong") and not value.isalnum():
        return "Letters and digits only"
    if policy == "strong" and not (value[0].isalpha() and any(c.isupper() for c in value)
                                   and any(c.islower() for c in value) and any(c.isdigit() for c in value)):
        return "Start with a letter and mix upper case, lower case and digits"
    return ""


def slot_value(service: str, cred: dict[str, Any]) -> str | None:
    return store.get(f"services.{service}.{cred['env']}")


def mark_rotated(data: dict[str, Any], service: str, env: str) -> None:
    data.setdefault("meta", {}).setdefault("rotated", {}).setdefault(service, {})[env] = _now()


def ensure_credentials(services: list[str]) -> list[str]:
    """Fill every empty slot for `services`. Reuses a non-placeholder value
    from a v1 `.env` (stateful volumes were initialised with it)."""
    legacy = settings.dotenv()
    created: list[str] = []

    def fill(data: dict[str, Any]) -> None:
        for svc in services:
            slot = data.setdefault("services", {}).setdefault(svc, {})
            for cred in catalog.cred_slots(svc):
                env = cred["env"]
                if cred.get("user_env") and not slot.get(cred["user_env"]):
                    old_user = legacy.get(cred["user_env"], "")
                    slot[cred["user_env"]] = old_user or cred.get("user", "")
                if slot.get(env) or cred.get("external"):
                    continue                 # external: a key you obtain and set yourself
                old = legacy.get(env, "")
                if old and old not in PLACEHOLDERS:
                    slot[env] = old
                    created.append(f"{cred.get('label', env)} (prehydrated from .env)")
                else:
                    slot[env] = generate_secret(cred.get("length", 32), cred.get("policy", "default"))
                    created.append(cred.get("label", env))
                mark_rotated(data, svc, env)

    store.mutate(fill)
    return created


def secret_env() -> dict[str, str]:
    """Every service secret as ENV_NAME=value for the compose subprocess."""
    env: dict[str, str] = {}
    services = store.get("services", {}) or {}
    for svc, spec in catalog.load()["services"].items():
        for cred in spec.get("creds", []) or []:
            if cred.get("user_env"):
                env[cred["user_env"]] = (services.get(svc) or {}).get(cred["user_env"]) or cred.get("user", "")
    for svc, slot in services.items():
        if svc == catalog.SHARED:
            continue
        for k, v in (slot or {}).items():
            if re.fullmatch(r"[A-Z][A-Z0-9_]*", k) and isinstance(v, str):
                env[k] = v
    # connection secrets (Studio → Credentials → Connection secrets) also go to
    # the Integration Canvas as ONE JSON bundle, so any name an admin adds works
    # without a compose change.
    import json as _json
    env["CANVAS_CONNECTION_SECRETS"] = _json.dumps(
        {k: v for k, v in (services.get(CONNECTIONS) or {}).items() if isinstance(v, str) and v})
    # shared external credentials (GCP_SA_JSON, M365_CLIENT_SECRET, …) go to
    # EVERY compose start; unset ones are passed empty so a stale value in
    # Studio's own env can never leak through. Multi-line JSON is kept verbatim.
    shared = services.get(catalog.SHARED) or {}
    for cred in catalog.shared_slots():
        value = shared.get(cred["env"])
        env[cred["env"]] = value if isinstance(value, str) else ""
    # cloud identities (Studio → Cloud identities): GCP_{BQ,GCS,VERTEX,WORKSPACE}_JSON
    # / _PROJECT per assignment, GCP_IDENTITIES {name: key} for per-connection
    # selection, and GCP_SA_JSON = the workspace identity when it is a service account
    import gcp_identities
    env.update(gcp_identities.env_vars({"gcp_identities": store.get("gcp_identities", {}),
                                        "gcp_assignments": store.get("gcp_assignments", {})}, env.get("GCP_SA_JSON", "")))
    env.update(pluginhost.registry.secret_env())
    return env


# ── host directories ─────────────────────────────────────────────────────
COMPONENT_DIRS = ("mlops", "canvas", "connectors", "harness", "disputes", "ccai", "superset", "trino", "ollama", "mcp",
                  "rag", "qdrant", "qdrant/storage", "qdrant/snapshots", "openwebui",
                  "demo-sources", "demo-sources/minio", "demo-sources/mssql", "demo-sources/oracle")


def required_dirs() -> list[tuple[str, int]]:
    """Folders under DATA_ROOT every workload expects (plan §6), owned by uid 1000."""
    h = settings.user_handle()
    user = [(f"{h}/{d}", 0o700 if d in ("ssh", "browser", "desktop") else 0o755)
            for d in ("work", "hf_cache", "pyuser", "ssh", "logs", "code-server", "browser", "desktop")]
    lake = [(f"lake/{d}", 0o755) for d in ("", "landing", "reference", "exports", "samples")]
    other = [(d, 0o755) for d in ("warehouse", "egress", "tls", *COMPONENT_DIRS)]
    return user + lake + other


def ensure_dirs() -> list[str]:
    root = settings.cfg.data
    return [rel for rel, mode in required_dirs() if ensure_dir(root / rel, mode)]


# ── selection ────────────────────────────────────────────────────────────
def _selection_file():
    return settings.cfg.studio_data / "selection.json"


def load_selection() -> dict[str, Any]:
    return read_json(_selection_file(), {"personas": [], "capabilities": []})


def save_selection(personas: list[str], capabilities: list[str], user: str) -> None:
    write_private_json(_selection_file(), {"personas": personas, "capabilities": capabilities,
                                           "updated": _now(), "by": user})


# ── planning ─────────────────────────────────────────────────────────────
UP = ("running", "starting", "unhealthy")


def plan(personas: list[str], capabilities: list[str] | None) -> dict[str, Any]:
    cat = catalog.load()
    res = catalog.resolve(personas, capabilities)
    defined = composefile.services()
    if defined:
        for name in [n for n in res.services if n not in defined]:
            res.services.pop(name)
            res.hints.append({"service": name, "missing": "compose",
                              "detail": f"{cat['services'][name].get('title', name)} has no compose service yet — skipped"})
    status = dockerops.status()
    composable = catalog.composable_services()
    wanted = set(res.services)
    services, start, stop, keep = [], [], [], []
    for name, why in res.services.items():
        st = status.get(name, {}).get("state", "missing")
        action = "keep" if st in UP else "start"
        (keep if action == "keep" else start).append(name)
        services.append({"name": name, "title": cat["services"][name]["title"], "state": st, "action": action,
                         "ram_mb": cat["services"][name].get("ram_mb", 0), "capabilities": why["capabilities"],
                         "needed_by": why["needed_by"], "auto": not why["capabilities"]})
    for name in cat["services"]:
        if name in composable and name not in wanted and status.get(name, {}).get("state") in UP:
            stop.append(name)
            services.append({"name": name, "title": cat["services"][name]["title"], "state": status[name]["state"],
                             "action": "stop", "ram_mb": cat["services"][name].get("ram_mb", 0),
                             "capabilities": [], "needed_by": [], "auto": False})
    missing = 0
    if store.is_unlocked():
        missing = sum(1 for s in start for c in catalog.cred_slots(s) if not c.get("external") and not slot_value(s, c))
    return {
        "capabilities": res.capabilities, "services": services, "profiles": res.profiles,
        "start": start, "stop": stop, "keep": keep, "hints": res.hints,
        "ram": {"selected_mb": res.ram_mb, "baseline_mb": catalog.CONTROL_PLANE_MB,
                "host_mb": dockerops.host_memory() // (1024 * 1024)},
        "credentials_to_generate": missing, "vault": store.status(),
        "downloads": downloads_needed(start),
    }


# ── images ───────────────────────────────────────────────────────────────
def service_images(service: str) -> list[str]:
    """Image references a catalog service runs."""
    img = composefile.images().get(service)
    return [img] if img else []


def downloads_needed(services: list[str]) -> dict[str, list[str]]:
    """{service: [images not yet on this host]} — they must be pulled first."""
    try:
        out = {}
        for svc in services:
            if missing := dockerops.missing_images(service_images(svc)):
                out[svc] = missing
        return out
    except dockerops.DockerUnavailable:
        return {}


def _ensure_images(job: jobs.Job, services: list[str]) -> list[str]:
    """Pull missing images; return the services that cannot start."""
    blocked: list[str] = []
    for svc, refs in downloads_needed(services).items():
        step = next((s for s in job.steps if s.service == svc), None)
        for ref in refs:
            if step:
                step.phase, step.detail = "pulling", ref
            job.line(f"downloading {ref} …")
            ok, msg = dockerops.pull(ref)
            if not ok:
                hint = dockerops.explain_pull_failure(msg)
                job.line(f"could not download {ref}: {msg[:160]}")
                if step:
                    step.phase, step.detail = "failed", "image download failed"
                if hint not in job.notes:
                    job.notes.append(hint)
                blocked.append(svc)
                break
    if not blocked:
        return []
    # anything that needs a blocked service can't start either
    for svc in services:
        if svc not in blocked and set(catalog.with_needs([svc])) & set(blocked):
            blocked.append(svc)
            if step := next((s for s in job.steps if s.service == svc), None):
                step.phase, step.detail = "failed", "depends on a service that couldn't be downloaded"
    return blocked


# ── compose execution ────────────────────────────────────────────────────
def _profiles(services: list[str]) -> list[str]:
    spec = catalog.load()["services"]
    out: list[str] = []
    for s in catalog.with_needs(services):
        p = spec[s].get("profile")
        if p and p not in out:
            out.append(p)
    return out


def _step(service: str, target: str) -> jobs.Step:
    return jobs.Step(service, catalog.load()["services"].get(service, {}).get("title", service), target)


def _run_compose(job: jobs.Job, argv: list[str], env: dict[str, str], seen: dict[str, str]) -> None:
    job.line("$ " + " ".join(argv))
    # cwd = the project: compose resolves `include: {project_directory: .}`
    # against the working directory (the repo is mounted at its host path)
    cwd = str(settings.cfg.project) if settings.cfg.project.is_dir() else None
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            env=env, bufsize=1, cwd=cwd)
    tail: list[str] = []
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.rstrip()
        if not line:
            continue
        job.line(line)
        tail = (tail + [line])[-15:]
        if m := _LINE.search(line):
            if name := _step_for(job, m.group("ctr") or "", m.group("svc") or ""):
                seen[name] = PHASE_WORDS[m.group("word").lower()]
    if proc.wait() != 0:
        raise RuntimeError("docker compose failed:\n" + "\n".join(tail[-8:]))


def _step_for(job: jobs.Job, container: str, service: str) -> str:
    """Map a compose output subject (container or service name) to a step."""
    names = [s.service for s in job.steps]
    if container:
        base = container.rsplit("-", 1)[0]
        service = next((n for n in names if base.endswith(f"-{n}")), "")
    return service if service in names else ""


def _monitor(job: jobs.Job, steps: list[jobs.Step], seen: dict[str, str], compose_done: threading.Event,
             settle_until: float | None) -> None:
    """Map docker state + compose output onto the phases of `steps`. With
    `settle_until=None` it runs alongside compose and returns when compose
    exits; otherwise it waits for health until that deadline."""
    while True:
        dockerops.invalidate_status()
        status = dockerops.status(max_age=0)
        finished = compose_done.is_set()
        for step in steps:
            if step.phase in ("healthy", "stopped", "failed"):
                continue
            st = status.get(step.service, {})
            state, word = st.get("state", "missing"), seen.get(step.service, "")
            if step.target == "stopped":
                if state in ("exited", "missing"):
                    job.set_phase(step.service, "stopped")
                else:
                    job.set_phase(step.service, "stopping")
            elif state == "running" and (finished or word == "started"):
                job.set_phase(step.service, "healthy", "ready")
            elif state == "unhealthy":
                job.set_phase(step.service, "starting", "health check not passing yet")
            elif state == "starting" or word in ("starting", "started"):
                job.set_phase(step.service, "starting", st.get("health") or "")
            elif word in ("pulling", "creating"):
                job.set_phase(step.service, word)
            elif finished and state == "exited":
                job.set_phase(step.service, "failed", f"exited with code {st.get('exit_code')}")
        if finished:
            pending = [s for s in steps if s.phase not in ("healthy", "stopped", "failed")]
            if settle_until is None or not pending:
                return
            if time.time() > settle_until:
                for s in pending:
                    job.notes.append(f"{s.title} is still starting — it turns green on the Launchpad when ready")
                return
        time.sleep(POLL)


def _compose(job: jobs.Job, target: str, calls: list[list[str]], env: dict[str, str], settle: float) -> None:
    """Run compose invocations with a live watcher over the steps heading to
    `target` (running | stopped), then wait for them to settle."""
    steps = [s for s in job.steps if s.target == target]
    seen: dict[str, str] = {}
    compose_done = threading.Event()
    watcher = threading.Thread(target=_monitor, args=(job, steps, seen, compose_done, None), daemon=True)
    watcher.start()
    try:
        for argv in calls:
            _run_compose(job, argv, env, seen)
    finally:
        compose_done.set()
        watcher.join()
    _monitor(job, steps, seen, compose_done, time.time() + settle)


def _execute(job: jobs.Job, start: list[str], stop: list[str], recreate: bool) -> None:
    if stop:
        calls = [dockerops.compose_argv(["stop", *stop], _profiles(stop))]
        _compose(job, "stopped", calls, dockerops.subprocess_env({}), 120)
    if start:
        blocked = _ensure_images(job, start)
        start = [s for s in start if s not in blocked]
        if start:
            _prepare(job, start)
            up_in_job(job, start, recreate)
    if failed := [s.title for s in job.steps if s.phase == "failed"]:
        job.state, job.error = "failed", "Did not start: " + ", ".join(failed)
        if job.notes:
            job.error += ". " + job.notes[-1]


def up_in_job(job: jobs.Job, services: list[str], recreate: bool) -> None:
    """`compose up` (optionally force-recreate, no deps) inside a running job."""
    flags = ["--force-recreate", "--no-deps"] if recreate else []
    calls = [dockerops.compose_argv(["up", "-d", "--no-build", *flags, *services], _profiles(services))]
    _compose(job, "running", calls, dockerops.subprocess_env(secret_env()), HEALTH_TIMEOUT)


def _prepare(job: jobs.Job, services: list[str]) -> None:
    if created := ensure_dirs():
        job.line(f"created host folders: {', '.join(created)}")
    if generated := ensure_credentials(catalog.with_needs(services)):
        job.line(f"generated {len(generated)} credential(s): {', '.join(generated)}")
        job.notes.append(f"Generated {len(generated)} new credential(s) — see Credentials")
    pluginhost.registry.before_up(services, job.line)


def _require_unlocked() -> None:
    if not store.is_unlocked():
        raise VaultLocked("Unlock the vault first — services need their credentials")


def submit(start: list[str], stop: list[str], user: str, title: str, kind: str,
           recreate: bool = False) -> jobs.Job:
    if start:
        _require_unlocked()
    steps = [_step(s, "running") for s in start] + [_step(s, "stopped") for s in stop]
    return jobs.submit(kind, title, user, steps, lambda job: _execute(job, start, stop, recreate))


def apply(personas: list[str], capabilities: list[str], user: str) -> jobs.Job:
    p = plan(personas, capabilities)
    save_selection(personas, capabilities, user)
    n_start, n_stop = len(p["start"]), len(p["stop"])
    title = "Updating your appliance" if n_start or n_stop else "Re-applying your appliance"
    # `compose up` every wanted service, not just the missing ones: it is
    # idempotent for unchanged containers and recreates any whose config
    # drifted (new env, image, compose edit) or that are crash-looping.
    return submit(p["start"] + p["keep"], p["stop"], user, title, "apply")


def start(services: list[str], user: str, recreate: bool = False) -> jobs.Job:
    full = catalog.with_needs(services)
    status = dockerops.status()
    targets = list(services) if recreate else [s for s in full if status.get(s, {}).get("state") not in UP] or list(services)
    verb = "Recreating" if recreate else "Starting"
    return submit(targets, [], user, f"{verb} {', '.join(services)}", "recreate" if recreate else "start", recreate)


def stop(services: list[str], user: str) -> jobs.Job:
    return submit([], list(services), user, f"Stopping {', '.join(services)}", "stop")
