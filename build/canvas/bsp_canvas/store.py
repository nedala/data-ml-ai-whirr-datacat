"""Persistence under ${CANVAS_DIR} (default /data/canvas):

    pipelines/<id>.yaml        pipeline definitions (git-friendly YAML)
    state/<id>.json            per-pipeline sync state (cursors, high-water marks)
    runs/<id>/<run_id>.json    run records;  runs/<id>/<run_id>.log  run logs
    lineage/<run_id>.json      OpenLineage-shaped run events
    uploads/<id>/…             files uploaded to File-upload sources
    connections.yaml           reusable connections (connections.py)
"""

from __future__ import annotations

import json
import re
import shutil
import threading
from datetime import datetime, timezone
from typing import Any

import yaml

from .config import EXAMPLES, canvas_dir, sub

_lock = threading.RLock()
ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")[:-4] + "Z"


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "pipeline"


def _p(pid: str):
    if not ID_RE.match(pid or ""):
        raise ValueError(f"bad pipeline id {pid!r}")
    return sub("pipelines") / f"{pid}.yaml"


def list_pipelines() -> list[dict]:
    out = []
    for f in sorted(sub("pipelines").glob("*.yaml")):
        try:
            d = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            continue
        d.setdefault("id", f.stem)
        out.append(d)
    return out


def get(pid: str) -> dict:
    p = _p(pid)
    if not p.is_file():
        raise KeyError(pid)
    d = yaml.safe_load(p.read_text()) or {}
    d["id"] = pid
    return d


def save(doc: dict) -> dict:
    pid = doc.get("id") or slug(doc.get("name", ""))
    doc = {"id": pid, "name": doc.get("name") or pid, "description": doc.get("description", ""),
           "tags": doc.get("tags", []), "schedule": doc.get("schedule") or {"cron": "", "enabled": False},
           "nodes": doc.get("nodes", []), "edges": doc.get("edges", []),
           **{k: v for k, v in doc.items() if k in ("example", "created_at")}}
    doc.setdefault("created_at", now())
    doc["updated_at"] = now()
    with _lock:
        _p(pid).write_text("# DataPlatform integration canvas pipeline — edit in /canvas/ or by hand.\n" +
                           yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=120))
    return doc


def delete(pid: str) -> None:
    with _lock:
        _p(pid).unlink(missing_ok=True)
        (sub("state") / f"{pid}.json").unlink(missing_ok=True)
        shutil.rmtree(sub("runs") / pid, ignore_errors=True)
        shutil.rmtree(sub("uploads") / pid, ignore_errors=True)


def seed_examples(force: bool = False) -> list[str]:
    """Copy shipped examples into pipelines/ (never overwriting user edits)."""
    seeded = []
    conns = EXAMPLES / "connections.yaml"
    if conns.is_file() and not (canvas_dir() / "connections.yaml").exists():
        shutil.copyfile(conns, canvas_dir() / "connections.yaml")
    for f in sorted(EXAMPLES.glob("*.yaml")):
        if f.name == "connections.yaml":
            continue
        dest = sub("pipelines") / f.name
        if force or not dest.exists():
            shutil.copyfile(f, dest)
            seeded.append(f.stem)
    return seeded


# ── state ────────────────────────────────────────────────────────────────
def get_state(pid: str) -> dict:
    try:
        return json.loads((sub("state") / f"{pid}.json").read_text())
    except (OSError, ValueError):
        return {}


def put_state(pid: str, state: dict) -> None:
    with _lock:
        (sub("state") / f"{pid}.json").write_text(json.dumps(state, indent=2, default=str))


def reset_state(pid: str) -> None:
    (sub("state") / f"{pid}.json").unlink(missing_ok=True)


# ── runs ─────────────────────────────────────────────────────────────────
def run_dir(pid: str):
    d = sub("runs") / pid
    d.mkdir(parents=True, exist_ok=True)
    return d


def put_run(rec: dict) -> None:
    with _lock:
        (run_dir(rec["pipeline_id"]) / f"{rec['run_id']}.json").write_text(json.dumps(rec, indent=2, default=str))


def get_run(pid: str, run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9\-_]+", run_id):
        raise KeyError(run_id)
    f = run_dir(pid) / f"{run_id}.json"
    if not f.is_file():
        raise KeyError(run_id)
    return json.loads(f.read_text())


def list_runs(pid: str | None = None, limit: int = 50) -> list[dict]:
    dirs = [run_dir(pid)] if pid else [d for d in sub("runs").iterdir() if d.is_dir()]
    recs = []
    for d in dirs:
        for f in d.glob("*.json"):
            try:
                recs.append(json.loads(f.read_text()))
            except ValueError:
                continue
    recs.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return recs[:limit]


def append_log(pid: str, run_id: str, line: str) -> None:
    with open(run_dir(pid) / f"{run_id}.log", "a", encoding="utf-8") as fh:
        fh.write(line.rstrip("\n") + "\n")


def read_log(pid: str, run_id: str, offset: int = 0) -> tuple[list[str], int]:
    f = run_dir(pid) / f"{run_id}.log"
    if not f.is_file():
        return [], offset
    lines = f.read_text(encoding="utf-8").splitlines()
    return lines[offset:], len(lines)


def put_lineage(run_id: str, events: list[dict]) -> None:
    (sub("lineage") / f"{run_id}.json").write_text(json.dumps(events, indent=2, default=str))


def get_lineage(run_id: str) -> list[dict]:
    try:
        return json.loads((sub("lineage") / f"{run_id}.json").read_text())
    except (OSError, ValueError):
        return []


def export_yaml(pid: str) -> str:
    return _p(pid).read_text()


def import_yaml(text: str) -> dict:
    d = yaml.safe_load(text)
    if not isinstance(d, dict) or "nodes" not in d:
        raise ValueError("not a canvas pipeline YAML (needs nodes:)")
    return d


def to_jsonable(v: Any) -> Any:
    return json.loads(json.dumps(v, default=str))
