"""Docker / compose plumbing.

Host-path subtlety: bind-mount sources are resolved by the Docker *daemon*
on the host, so `docker compose --project-directory` must be the HOST path
of the repo: HOST_PROJECT_ROOT (exported by the Makefile) or, failing that,
the source of this container's own project mount. Compose also reads the
`include:`d fragments relative to that directory (and the working
directory), so compose.yml mounts the repo at the same path inside Studio
(PROJECT_DIR = HOST_PROJECT_ROOT) and compose runs with cwd = the project."""

from __future__ import annotations

import datetime as dt
import logging
import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import catalog
import composefile
import settings

log = logging.getLogger("studio.docker")

try:
    import docker
    from docker.errors import DockerException, NotFound
except ImportError:  # pragma: no cover — the image ships the SDK
    docker = None
    DockerException = NotFound = Exception  # type: ignore[misc,assignment]

_client_lock = threading.Lock()
_client: Any = None


class DockerUnavailable(RuntimeError):
    pass


def client() -> Any:
    global _client
    with _client_lock:
        if _client is None:
            if docker is None:
                raise DockerUnavailable("docker SDK not installed")
            try:
                _client = docker.from_env(timeout=30)
                _client.ping()
            except DockerException as exc:
                _client = None
                raise DockerUnavailable(f"Docker is not reachable: {exc}") from exc
        return _client


def available() -> bool:
    try:
        client()
        return True
    except DockerUnavailable:
        return False


# ── host project root ────────────────────────────────────────────────────
def derive_host_root(env_value: str, mounts: list[dict[str, Any]] | None, project: Path) -> str:
    """HOST_PROJECT_ROOT if set, else the host source of the mount at
    `project`, else `project` itself (Studio running directly on the host)."""
    if env_value:
        return env_value.rstrip("/") or "/"
    for m in mounts or []:
        if m.get("Destination") == str(project) and m.get("Source"):
            return str(m["Source"]).rstrip("/") or "/"
    return str(project)


def own_container() -> Any | None:
    ids = [os.environ.get("HOSTNAME", "")]
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            if "/containers/" in line and "/hostname" in line:
                ids.append(line.split("/containers/")[1].split("/")[0])
    except OSError:
        pass
    for cid in filter(None, ids):
        try:
            return client().containers.get(cid)
        except (NotFound, DockerException, DockerUnavailable):
            continue
    return None


_host_root: dict[str, str] = {}


def host_project_root() -> str:
    cfg = settings.cfg
    key = f"{cfg.host_project_root}|{cfg.project}"
    if key not in _host_root:
        mounts = None
        if not cfg.host_project_root:
            me = own_container()
            mounts = me.attrs.get("Mounts", []) if me is not None else None
        _host_root[key] = derive_host_root(cfg.host_project_root, mounts, cfg.project)
    return _host_root[key]


# ── compose CLI ──────────────────────────────────────────────────────────
def compose_cli() -> list[str]:
    if shutil.which("docker"):
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    raise DockerUnavailable("neither `docker compose` nor `docker-compose` is installed")


def compose_argv(args: list[str], profiles: list[str] | None = None,
                 cli: list[str] | None = None, host_root: str | None = None) -> list[str]:
    """`docker compose -f docker-compose.yml -p <project> --env-file … …` for
    this appliance: the same project name, env files (.env, then
    appliances/<APPLIANCE>.env) and project directory `make up` used, so
    profile services land in the same project with the same DATA_ROOT."""
    cfg = settings.cfg
    argv = [*(cli or compose_cli()), "-f", str(cfg.project / composefile.MAIN_FILE),
            "--project-directory", host_root or host_project_root(), "-p", cfg.compose_project]
    # compose looks for .env in --project-directory, which is a host path that
    # does not exist inside this container — point at the files explicitly.
    for env_file in settings.env_files():
        argv += ["--env-file", str(env_file)]
    for p in profiles or []:
        argv += ["--profile", p]
    return argv + args


# Per-appliance settings Studio was started with; they must reach every
# compose call (process env beats --env-file in compose interpolation).
APPLIANCE_ENV_KEYS = ("COMPOSE_PROJECT_NAME", "FACADE_PORT", "DATA_ROOT", "APPLIANCE", "APPLIANCE_PERSONA",
                      "APPLIANCE_NETWORK", "APPLIANCE_ENV", "USER_HANDLE", "PUBLIC_FACADE_HOST", "HOST_PROJECT_ROOT")


def subprocess_env(extra: dict[str, str]) -> dict[str, str]:
    """Studio's env minus its own secrets and container-only paths, plus
    the appliance settings and `extra` (vault secrets)."""
    drop = {"APPLIANCE_MASTER_PASSWORD", "APPLIANCE_MASTER_PASSWORD_FILE", "OIDC_CLIENT_SECRET",
            "DATA_DIR", "VAULT_DIR", "PROJECT_DIR", "STUDIO_SESSION_SECRET_FILE"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    cfg = settings.cfg
    env["COMPOSE_PROJECT_NAME"] = cfg.compose_project
    env["DATA_ROOT"] = cfg.data_root
    env["APPLIANCE_PERSONA"] = cfg.appliance_persona
    env.update(extra)
    return env


# ── status ───────────────────────────────────────────────────────────────
def _iso(ts: str | None) -> str | None:
    if not ts or ts.startswith("0001-"):
        return None
    return ts[:19] + "Z"  # docker reports RFC 3339 UTC with nanoseconds


def _container_state(c: Any) -> dict[str, Any]:
    st = c.attrs.get("State", {})
    health = (st.get("Health") or {}).get("Status")
    status = st.get("Status", c.status)
    if status == "running":
        state = {"starting": "starting", "unhealthy": "unhealthy"}.get(health or "", "running")
    elif status in ("created", "restarting"):
        state = "starting"
    else:
        state = "exited"
    return {"state": state, "health": health, "started_at": _iso(st.get("StartedAt")) if status == "running" else None,
            "image": (c.attrs.get("Config") or {}).get("Image", ""), "exit_code": st.get("ExitCode"),
            "container": c.name, "id": c.id[:12]}


_status_cache: dict[str, Any] = {"at": 0.0, "data": None}
_status_lock = threading.Lock()


def project_containers() -> dict[str, list[Any]]:
    """{compose service: [containers]} for the appliance project."""
    cfg = settings.cfg
    out: dict[str, list[Any]] = {}
    for c in client().containers.list(all=True, filters={"label": "com.docker.compose.project"}):
        labels = c.labels or {}
        if labels.get("com.docker.compose.project") != cfg.compose_project:
            continue
        out.setdefault(labels.get("com.docker.compose.service", c.name), []).append(c)
    return out


def status(max_age: float = 1.5) -> dict[str, dict[str, Any]]:
    """Every catalog + control-plane service → state dict (see core.compose)."""
    with _status_lock:
        if _status_cache["data"] is not None and time.time() - _status_cache["at"] < max_age:
            return _status_cache["data"]
    cat = catalog.load()["services"]
    missing = {"state": "missing", "health": None, "started_at": None, "image": ""}
    try:
        by_service = project_containers()
    except DockerUnavailable:
        by_service = {}
    out: dict[str, dict[str, Any]] = {}
    for name in [*catalog.CONTROL_PLANE, *cat]:
        cs = by_service.get(name, [])
        out[name] = _container_state(cs[0]) if cs else dict(missing)
    with _status_lock:
        _status_cache.update(at=time.time(), data=out)
    return out


def invalidate_status() -> None:
    with _status_lock:
        _status_cache["at"] = 0.0


def containers_for(service: str) -> list[Any]:
    return project_containers().get(service, [])


def memory_usage(services: list[str]) -> dict[str, int]:
    """Resident memory (bytes) per service; slow-ish, fetched on demand."""
    def one(svc: str) -> tuple[str, int]:
        total = 0
        for c in containers_for(svc):
            if c.status != "running":
                continue
            try:
                stats = c.stats(stream=False, one_shot=True)
                mem = stats.get("memory_stats", {})
                total += max(0, int(mem.get("usage", 0)) - int((mem.get("stats") or {}).get("inactive_file", 0)))
            except (DockerException, ValueError):
                continue
        return svc, total

    with ThreadPoolExecutor(max_workers=8) as pool:
        return dict(pool.map(one, services))


_info: dict[str, Any] = {"at": 0.0, "data": {}}


def info() -> dict[str, Any]:
    """`docker info`, cached for a minute (host RAM, host name)."""
    if time.time() - _info["at"] > 60:
        try:
            _info.update(data=client().info(), at=time.time())
        except (DockerUnavailable, DockerException):
            return {}
    return _info["data"]


def host_memory() -> int:
    return int(info().get("MemTotal", 0))


def host_name() -> str:
    return str(info().get("Name", ""))


def logs(service: str, tail: int = 300, since: float | None = None) -> tuple[list[str], float]:
    """(lines, last-timestamp) across the service's containers."""
    lines: list[tuple[str, str]] = []
    multi = len(cs := containers_for(service)) > 1
    for c in cs:
        kwargs: dict[str, Any] = {"timestamps": True, "tail": tail}
        if since:
            kwargs["since"] = since
        raw = c.logs(**kwargs).decode("utf-8", errors="replace")
        prefix = f"[{c.labels.get('com.docker.compose.service', c.name)}] " if multi else ""
        for line in raw.splitlines():
            ts, _, text = line.partition(" ")
            lines.append((ts, prefix + text))
    lines.sort(key=lambda t: t[0])
    lines = lines[-tail:]
    last = since or 0.0
    if lines:
        try:
            last = dt.datetime.fromisoformat(lines[-1][0][:26].rstrip("Z")).replace(
                tzinfo=dt.timezone.utc).timestamp() + 1e-6
        except ValueError:
            pass
    return [t for _, t in lines], last


def exec_in(service: str, argv: list[str], user: str | None = None, timeout: int = 120) -> tuple[int, str]:
    try:
        running = [c for c in containers_for(service) if c.status == "running"]
    except (DockerUnavailable, DockerException) as exc:
        return 125, str(exc)
    if not running:
        return 125, f"{service} is not running"
    result: dict[str, Any] = {}

    def run() -> None:
        try:
            code, out = running[0].exec_run(argv, user=user or "", demux=False)
            result.update(code=code, out=(out or b"").decode("utf-8", errors="replace"))
        except DockerException as exc:
            result.update(code=126, out=str(exc))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return 124, f"timed out after {timeout}s"
    return int(result.get("code") or 0), result.get("out", "")


# ── images ───────────────────────────────────────────────────────────────
def missing_images(refs: list[str]) -> list[str]:
    """Image references not present in the local Docker image store."""
    import docker  # noqa: PLC0415

    cli = client()
    out = []
    for ref in dict.fromkeys(refs):
        try:
            cli.images.get(ref)
        except docker.errors.ImageNotFound:
            out.append(ref)
        except docker.errors.APIError:
            out.append(ref)
    return out


def pull(ref: str) -> tuple[bool, str]:
    """Pull one image through the Docker daemon (which uses Docker Desktop's
    proxy settings, not the appliance egress proxy)."""
    import docker  # noqa: PLC0415

    repo, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "latest")
    try:
        client().images.pull(repo, tag=tag or "latest")
        return True, ""
    except docker.errors.APIError as exc:
        return False, str(getattr(exc, "explanation", "") or exc)


def explain_pull_failure(message: str) -> str:
    """Plain-language cause + fix for the usual Docker Hub pull failures."""
    low = message.lower()
    if "bad gateway" in low or "proxyconnect" in low or "proxy" in low:
        return ("Docker can't reach Docker Hub through its configured proxy (Bad Gateway). "
                "Fix: Docker Desktop → Settings → Resources → Proxies → turn off the manual proxy "
                "(or repair the proxy it points at, e.g. px on :3128), Apply & restart, then Apply here again.")
    if "no such host" in low or "timeout" in low or "i/o timeout" in low:
        return "Docker can't reach the registry (DNS/network). Check the host's internet/VPN, then Apply again."
    if "unauthorized" in low or "denied" in low:
        return "The registry refused the pull (authentication). Run `docker login` for that registry, then Apply again."
    return f"Docker couldn't download the image: {message[:200]}"
