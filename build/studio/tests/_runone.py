"""Run one test module in a fresh temp project (see run.py)."""

from __future__ import annotations

import importlib
import inspect
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(os.environ.get("STUDIO_TEST_REPO", "/repo"))


HERE = Path(__file__).resolve().parent


def prepare_project() -> Path:
    """Temp project = the real root/base/platform compose files + fixture
    component fragments, and the real core catalog + fixture catalog.d (so
    tests never depend on other components' work in progress)."""
    project = Path(tempfile.mkdtemp(prefix="studiotest-"))
    for name in ("docker-compose.yml", "compose.base.yml", "build/platform.compose.yml"):
        if (REPO / name).is_file():
            (project / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(REPO / name, project / name)
    (project / "build/superset").mkdir(parents=True, exist_ok=True)
    shutil.copy(HERE / "fixtures" / "components.compose.yml", project / "build/superset/compose.yml")
    cat_dir = project / "catalog"
    shutil.copytree(HERE / "fixtures" / "catalog.d", cat_dir / "catalog.d")
    shutil.copy(HERE.parent / "catalog.yaml", cat_dir / "catalog.yaml")
    os.environ["STUDIO_CATALOG"] = str(cat_dir / "catalog.yaml")
    os.environ.update({"PROJECT_DIR": str(project), "DATA_DIR": str(project / "data"),
                       "VAULT_DIR": str(project / "data" / "vault"), "APPLIANCE_ENV": "dev",
                       "VAULT_MODE": os.environ.get("STUDIO_TEST_VAULT_MODE", "passphrase"),
                       "STUDIO_SESSION_SECRET_FILE": str(project / "data" / "vault" / "session.key")})
    for key in ("APPLIANCE_MASTER_PASSWORD", "APPLIANCE_MASTER_PASSWORD_FILE", "HOST_PROJECT_ROOT", "SSO_PROVIDER",
                "APPLIANCE", "APPLIANCE_PERSONA", "DATA_ROOT", "COMPOSE_PROJECT_NAME"):
        os.environ.pop(key, None)
    return project


def main(name: str) -> int:
    logging.basicConfig(level=logging.WARNING)
    project = prepare_project()
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    module = importlib.import_module(f"tests.{name}")
    tests = [(n, f) for n, f in vars(module).items() if n.startswith("test_") and inspect.isfunction(f)]
    tests.sort(key=lambda t: t[1].__code__.co_firstlineno)
    failures = 0
    for n, fn in tests:
        t0 = time.time()
        try:
            fn(project) if fn.__code__.co_argcount else fn()
            print(f"  ✓ {n} ({time.time() - t0:.2f}s)", flush=True)
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"  ✗ {n}\n" + "".join("      " + l for l in traceback.format_exc().splitlines(True)), flush=True)
    shutil.rmtree(project, ignore_errors=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
