"""Integration: Studio derives the HOST path of /host-project from its own
container mount, and `docker compose config` then resolves bind-mount sources
to host paths. Needs the docker socket and the repo mounted at /host-project;
run as described in docs/site/v2/studio.md (skips otherwise)."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import dockerops
import settings

EXPECTED = os.environ.get("EXPECTED_HOST_ROOT", "")


def _ready() -> bool:
    return Path("/var/run/docker.sock").exists() and Path("/host-project/docker-compose.yml").is_file() and bool(EXPECTED)


def test_derives_host_root_from_own_mount():
    if not _ready():
        print("    (skipped: no docker socket / /host-project mount)")
        return
    os.environ["PROJECT_DIR"] = "/host-project"
    os.environ.pop("HOST_PROJECT_ROOT", None)
    settings.reload()
    me = dockerops.own_container()
    assert me is not None, "could not find this container via the docker API"
    assert dockerops.host_project_root() == EXPECTED.rstrip("/")


def test_compose_config_uses_host_paths():
    if not _ready() or not (shutil.which("docker-compose") or shutil.which("docker")):
        print("    (skipped)")
        return
    argv = dockerops.compose_argv(["config", "--format", "json"], ["workspace", "base"])
    env = dockerops.subprocess_env({"PG_PASS": "x"})
    out = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60)
    assert out.returncode == 0, out.stderr[-800:]
    cfg = json.loads(out.stdout)
    sources = [v["source"] for v in cfg["services"]["appliance"]["volumes"] if v["type"] == "bind"]
    assert sources and all(s.startswith(EXPECTED.rstrip("/") + "/") for s in sources), sources
    assert any(s.endswith("/data/analyst/work") for s in sources)
    assert cfg["services"]["postgres"]["environment"]["POSTGRES_PASSWORD"] == "x", "secrets come from the process env"
