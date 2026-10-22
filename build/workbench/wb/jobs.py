"""Background job runner with per-queue serialisation and live logs.

Every mutation (package install, sample-data load, warehouse load, export, ...)
is a Job. Jobs on the same queue (``python``, ``data``, ``ingress``)
run one at a time in submission order; different queues run in parallel.
Logs are kept in memory and streamed to the browser over SSE; a summary of
finished jobs (with a log tail) is appended to ``STATE_DIR/jobs.jsonl`` so the
Activity view survives restarts.

Subprocesses are always started from argv lists — never through a shell.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
import traceback
import uuid
from typing import Any, Callable

from .config import STATE_DIR

HISTORY_FILE = STATE_DIR / "jobs.jsonl"
MAX_LINES = 20000
MAX_KEEP = 200


class JobError(RuntimeError):
    """A job step failed; the message is shown to the user."""


class Cancelled(RuntimeError):
    pass


def _now() -> float:
    return time.time()


class Job:
    def __init__(self, queue_name: str, kind: str, title: str, fn: Callable[["Job"], Any],
                 meta: dict | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.queue = queue_name
        self.kind = kind
        self.title = title
        self.meta = meta or {}
        self.fn = fn
        self.status = "queued"
        self.created = _now()
        self.started: float | None = None
        self.finished: float | None = None
        self.lines: list[str] = []
        self.dropped = 0
        self.progress: dict | None = None
        self.result: dict = {}
        self.error: str | None = None
        self.cancel_requested = False
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()

    # ── logging / progress ───────────────────────────────────────────
    def log(self, line: str = "") -> None:
        for ln in str(line).splitlines() or [""]:
            with self._lock:
                self.lines.append(ln.rstrip("\r"))
                if len(self.lines) > MAX_LINES:
                    # keep indices stable for SSE clients: replace head with marker
                    cut = len(self.lines) - MAX_LINES
                    self.dropped += cut
                    del self.lines[:cut]

    def set_progress(self, done: int, total: int | None = None, label: str = "") -> None:
        self.progress = {"done": done, "total": total, "label": label}

    def check_cancel(self) -> None:
        if self.cancel_requested:
            raise Cancelled("Cancelled by user")

    # ── subprocess helper ────────────────────────────────────────────
    def run(self, argv: list[str], env: dict | None = None, cwd: str | None = None,
            check: bool = True, timeout: float | None = 3600, echo: bool = True) -> int:
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            raise JobError("internal: argv must be a list of strings")
        self.check_cancel()
        if echo:
            self.log("$ " + " ".join(_shown(a) for a in argv))
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        full_env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, cwd=cwd, env=full_env,
                                    text=True, bufsize=1, errors="replace",
                                    start_new_session=True)
        except FileNotFoundError as exc:
            raise JobError(f"command not found: {argv[0]}") from exc
        self._proc = proc
        deadline = _now() + timeout if timeout else None
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
                if deadline and _now() > deadline:
                    self._kill()
                    raise JobError(f"timed out after {int(timeout)}s")
                if self.cancel_requested:
                    self._kill()
                    raise Cancelled("Cancelled by user")
            rc = proc.wait()
        finally:
            self._proc = None
        if self.cancel_requested:
            raise Cancelled("Cancelled by user")
        if check and rc != 0:
            raise JobError(f"{os.path.basename(argv[0])} exited with code {rc}")
        return rc

    def _kill(self) -> None:
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except Exception:
                p.terminate()
            try:
                p.wait(5)
            except Exception:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except Exception:
                    p.kill()

    def cancel(self) -> None:
        self.cancel_requested = True
        self._kill()

    # ── views ────────────────────────────────────────────────────────
    @property
    def done(self) -> bool:
        return self.status in ("succeeded", "failed", "cancelled")

    def summary(self) -> dict:
        return {
            "id": self.id, "queue": self.queue, "kind": self.kind, "title": self.title,
            "meta": self.meta, "status": self.status, "created": self.created,
            "started": self.started, "finished": self.finished, "progress": self.progress,
            "result": self.result, "error": self.error, "lines": self.dropped + len(self.lines),
        }

    def lines_from(self, start: int) -> tuple[list[str], int]:
        with self._lock:
            idx = max(0, start - self.dropped)
            out = self.lines[idx:]
            return out, self.dropped + len(self.lines)


def _shown(a: str) -> str:
    return a if a and all(c.isalnum() or c in "-_=./:@+,<>~![]" for c in a) else json.dumps(a)


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.order: list[str] = []
        self.queues: dict[str, queue.Queue] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.listeners: list[Callable[[Job], None]] = []
        self._lock = threading.Lock()
        self.history: list[dict] = self._load_history()

    def _load_history(self) -> list[dict]:
        out: list[dict] = []
        try:
            with open(HISTORY_FILE, encoding="utf-8") as fh:
                for line in fh.readlines()[-MAX_KEEP:]:
                    try:
                        rec = json.loads(line)
                        if rec.get("status") in ("queued", "running"):
                            rec["status"] = "failed"
                            rec["error"] = "interrupted (workbench restarted)"
                        out.append(rec)
                    except ValueError:
                        pass
        except OSError:
            pass
        return out

    def submit(self, queue_name: str, kind: str, title: str, fn: Callable[[Job], Any],
               meta: dict | None = None) -> Job:
        job = Job(queue_name, kind, title, fn, meta)
        with self._lock:
            self.jobs[job.id] = job
            self.order.append(job.id)
            while len(self.order) > MAX_KEEP:
                old = self.order.pop(0)
                if self.jobs.get(old) and self.jobs[old].done:
                    self.jobs.pop(old, None)
            q = self.queues.get(queue_name)
            if q is None:
                q = self.queues[queue_name] = queue.Queue()
                t = threading.Thread(target=self._worker, args=(queue_name, q), daemon=True,
                                     name=f"jobs-{queue_name}")
                self.workers[queue_name] = t
                t.start()
        pending = sum(1 for j in self.jobs.values() if j.queue == queue_name and not j.done) - 1
        job.log(f"Queued ({pending} ahead)" if pending > 0 else "Starting…")
        q.put(job)
        return job

    def _worker(self, name: str, q: queue.Queue) -> None:
        while True:
            job: Job = q.get()
            if job.cancel_requested:
                job.status = "cancelled"
                job.finished = _now()
                self._finish(job)
                continue
            job.status = "running"
            job.started = _now()
            try:
                res = job.fn(job)
                if isinstance(res, dict):
                    job.result.update(res)
                job.status = "succeeded"
                job.log("")
                job.log("✓ Done")
            except Cancelled as exc:
                job.status = "cancelled"
                job.error = str(exc)
                job.log(f"✗ {exc}")
            except JobError as exc:
                job.status = "failed"
                job.error = str(exc)
                job.log(f"✗ {exc}")
            except Exception as exc:  # noqa: BLE001
                job.status = "failed"
                job.error = f"{type(exc).__name__}: {exc}"
                job.log(traceback.format_exc())
                job.log(f"✗ {job.error}")
            job.finished = _now()
            self._finish(job)

    def _finish(self, job: Job) -> None:
        rec = job.summary()
        tail, _ = job.lines_from(0)
        rec["log_tail"] = tail[-400:]
        self.history.append(rec)
        self.history = self.history[-MAX_KEEP:]
        try:
            with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
            # compact occasionally
            if HISTORY_FILE.stat().st_size > 8_000_000:
                with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
                    for r in self.history:
                        fh.write(json.dumps(r, default=str) + "\n")
        except OSError:
            pass
        for cb in list(self.listeners):
            try:
                cb(job)
            except Exception:  # noqa: BLE001
                pass

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def list(self, queues: set[str] | None = None, limit: int = 50) -> list[dict]:
        live = {j.id: j.summary() for j in self.jobs.values()}
        hist = {r["id"]: r for r in self.history if r.get("id") not in live}
        allr = list(hist.values()) + list(live.values())
        if queues:
            allr = [r for r in allr if r.get("queue") in queues]
        allr.sort(key=lambda r: r.get("created") or 0, reverse=True)
        return [{k: v for k, v in r.items() if k != "log_tail"} for r in allr[:limit]]

    def history_log(self, job_id: str) -> list[str] | None:
        for r in reversed(self.history):
            if r.get("id") == job_id:
                return r.get("log_tail") or []
        return None

    def active(self, queue_name: str) -> list[Job]:
        return [j for j in self.jobs.values() if j.queue == queue_name and not j.done]


MANAGER = JobManager()
