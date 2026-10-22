"""Isolated environment for Workbench tests — import BEFORE anything from wb.

    docker run --rm -v $PWD:/repo --entrypoint bash bsp/appliance:latest \
      -c 'cp -r /repo/build/workbench /tmp/wb && cd /tmp/wb && python -m pytest -q'

(``python -m unittest discover -s tests -t .`` works too.) Live warehouse
tests (test_warehouse_live) run only when WBTEST_PG=1 and PG_HOST / PG_PORT /
PG_DB / PG_USER / PG_PASS point at a throwaway Postgres.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="wbtest-"))
os.environ["WORKBENCH_STATE_DIR"] = str(TMP / "state")
os.environ["LAKE_DIR"] = str(TMP / "lake")
os.environ["WORKBENCH_WORK_DIR"] = str(TMP / "work")
for _d in ("lake/landing", "lake/reference", "lake/exports", "lake/samples", "work"):
    (TMP / _d).mkdir(parents=True, exist_ok=True)
os.environ.setdefault("WORKBENCH_TEMPLATES", "/opt/templates")
os.environ.setdefault("UI_KIT_DIR", "/opt/ui-kit")
# live Postgres settings, captured before any test module overrides PG_*
PG_LIVE = {k: os.environ[k] for k in ("PG_HOST", "PG_PORT", "PG_DB", "PG_USER", "PG_PASS") if k in os.environ}
if os.environ.get("WBTEST_PG") != "1":
    os.environ["PG_HOST"] = "127.0.0.1"
    os.environ["PG_PORT"] = "1"
    os.environ.setdefault("PG_PASS", "unused")
for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(k, None)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

H = {"X-Studio": "1"}


class FakeJob:
    """Stands in for wb.jobs.Job when a job function is called directly."""

    def __init__(self, jid: str = "abcdef012345"):
        self.id = jid
        self.lines: list[str] = []
        self.progress = None
        self.cancel_requested = False

    def log(self, line: str = "") -> None:
        self.lines.extend(str(line).splitlines() or [""])

    def set_progress(self, done, total=None, label=""):
        self.progress = (done, total, label)

    def check_cancel(self) -> None:
        if self.cancel_requested:
            from wb.jobs import Cancelled
            raise Cancelled("Cancelled by user")


def wait_job(client, base: str, job_id: str, timeout: float = 120) -> dict:
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get(f"{base}/api/jobs/{job_id}").json()
        if s["status"] in ("succeeded", "failed", "cancelled"):
            return s
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish")
