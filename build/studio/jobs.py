"""Background jobs (compose apply / stop / recreate / rotate). Jobs run one at
a time on a single worker so two compose invocations never race; progress is
polled via GET /studio/api/jobs/<id>."""

from __future__ import annotations

import datetime as dt
import secrets
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

KEEP = 30
TERMINAL = ("done", "failed")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


@dataclass
class Step:
    service: str
    title: str
    target: str            # running | stopped
    phase: str = "queued"  # queued|pulling|creating|starting|healthy|stopping|stopped|failed
    detail: str = ""


@dataclass
class Job:
    id: str
    kind: str
    title: str
    user: str
    state: str = "queued"   # queued | running | done | failed
    created: str = field(default_factory=_now)
    finished: str | None = None
    steps: list[Step] = field(default_factory=list)
    log: deque = field(default_factory=lambda: deque(maxlen=600))
    error: str = ""
    notes: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def line(self, text: str) -> None:
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {text.rstrip()}")

    def step(self, service: str) -> Step | None:
        return next((s for s in self.steps if s.service == service), None)

    def set_phase(self, service: str, phase: str, detail: str = "") -> None:
        s = self.step(service)
        if s and s.phase not in ("failed",):
            s.phase, s.detail = phase, detail or s.detail

    def public(self, log_from: int = 0) -> dict[str, Any]:
        with self.lock:
            lines = list(self.log)
        return {
            "id": self.id, "kind": self.kind, "title": self.title, "user": self.user, "state": self.state,
            "created": self.created, "finished": self.finished, "error": self.error, "notes": self.notes,
            "steps": [s.__dict__ for s in self.steps],
            "log": lines[log_from:], "log_total": len(lines),
        }


_jobs: dict[str, Job] = {}
_order: deque[str] = deque()
_lock = threading.Lock()
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="studio-job")


def submit(kind: str, title: str, user: str, steps: list[Step], fn: Callable[[Job], None]) -> Job:
    job = Job(id=secrets.token_hex(6), kind=kind, title=title, user=user, steps=steps)
    with _lock:
        _jobs[job.id] = job
        _order.append(job.id)
        while len(_order) > KEEP:
            _jobs.pop(_order.popleft(), None)

    def run() -> None:
        job.state = "running"
        try:
            fn(job)
            if job.state == "running":
                job.state = "done"
        except Exception as exc:  # noqa: BLE001 — surfaced to the UI
            job.state, job.error = "failed", str(exc)
            job.line(f"ERROR: {exc}")
        finally:
            job.finished = _now()

    _worker.submit(run)
    return job


def get(job_id: str) -> Job | None:
    return _jobs.get(job_id)


def recent(limit: int = 10) -> list[Job]:
    with _lock:
        ids = list(_order)[-limit:]
    return [j for i in reversed(ids) if (j := _jobs.get(i))]


def active() -> Job | None:
    return next((j for j in recent(KEEP) if j.state not in TERMINAL), None)
