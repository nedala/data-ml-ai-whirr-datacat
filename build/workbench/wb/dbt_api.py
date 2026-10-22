"""dbt Studio API (/dbt/api/…) — visual dbt development for analysts.

Projects are folders under the workspace (``~/work``) that contain a
``dbt_project.yml``. dbt commands run as background jobs on the ``dbt``
queue (logs stream over SSE via /dbt/api/jobs) with
``DBT_PROFILES_DIR=<project>`` and ``DBT_TARGET`` (default ``postgres``).
Preview rows are kept in memory only, never written to the job history.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import dbt_project as dp
from . import dbt_sql, dbt_yaml
from .jobs import MANAGER, Cancelled, Job, JobError

router = APIRouter(prefix="/dbt/api")
QUEUE = "dbt"
DBT_BIN = os.environ.get("WORKBENCH_DBT", "dbt")
MAX_SQL = 200_000
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

ACTIONS = {"parse", "build", "run", "test", "seed", "compile", "show", "docs", "deps", "snapshot"}
SELECTABLE = {"build", "run", "test", "seed", "compile", "show", "snapshot"}

_OUTPUTS: "OrderedDict[str, dict]" = OrderedDict()
_OUT_LOCK = threading.Lock()


def _err(exc: dp.StudioError):
    return HTTPException(exc.status, str(exc))


def _project(p: str) -> Path:
    try:
        return dp.resolve_project(p)
    except dp.StudioError as exc:
        raise _err(exc) from exc


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except dp.StudioError as exc:
        raise _err(exc) from exc


def _remember(job_id: str, data: dict) -> None:
    with _OUT_LOCK:
        _OUTPUTS[job_id] = data
        while len(_OUTPUTS) > 30:
            _OUTPUTS.popitem(last=False)


# ── dbt subprocess with JSON logs ───────────────────────────────────
def dbt_env(project: Path) -> dict:
    env = {
        "DBT_TARGET": dp.dbt_target(),
        "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
        "DBT_VERSION_CHECK": "false",
        "DBT_USE_COLORS": "false",
        "PYTHONWARNINGS": "ignore",
    }
    if (project / "profiles.yml").is_file():
        env["DBT_PROFILES_DIR"] = str(project)
    return env


def _fmt_event(ev: dict) -> str | None:
    info = ev.get("info") or {}
    name = info.get("name")
    msg = ANSI_RE.sub("", str(info.get("msg") or ""))
    if name == "ShowNode":
        data = ev.get("data") or {}
        try:
            n = len(json.loads(data.get("preview") or "[]"))
        except ValueError:
            n = "?"
        return f"Previewed {n} row(s) of {data.get('node_name')}"
    if name == "CompiledNode":
        return f"Compiled {(ev.get('data') or {}).get('node_name')}"
    ts = str(info.get("ts") or "")[11:19]
    return f"{ts}  {msg}" if ts and msg else msg


def run_dbt(job: Job, project: Path, args: list[str], capture: dict | None = None,
            timeout: float = 3600) -> int:
    """Run ``dbt <args> --log-format json`` streaming readable lines into the job."""
    argv = [os.environ.get("WORKBENCH_DBT", DBT_BIN), *args, "--log-format", "json"]
    job.check_cancel()
    job.log("$ dbt " + " ".join(a if re.fullmatch(r"[\w+.:/=@,-]+", a) else json.dumps(a) for a in args))
    env = os.environ.copy()
    env.update(dbt_env(project))
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(project), env=env, text=True, bufsize=1, errors="replace",
                                start_new_session=True)
    except FileNotFoundError as exc:
        raise JobError("dbt is not installed in this workspace image") from exc
    job._proc = proc
    deadline = time.time() + timeout
    assert proc.stdout is not None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            ev = None
            if line.startswith("{"):
                try:
                    ev = json.loads(line)
                except ValueError:
                    ev = None
            if ev is None:
                if line.strip():
                    job.log(ANSI_RE.sub("", line))
            else:
                name = (ev.get("info") or {}).get("name")
                if capture is not None and name == "ShowNode":
                    capture["preview"] = (ev.get("data") or {}).get("preview")
                    capture["node"] = (ev.get("data") or {}).get("node_name")
                elif capture is not None and name == "CompiledNode":
                    capture["compiled"] = (ev.get("data") or {}).get("compiled")
                    capture["node"] = (ev.get("data") or {}).get("node_name")
                text = _fmt_event(ev)
                if text is not None and (text.strip() or name == "Formatting"):
                    job.log(text)
            if time.time() > deadline:
                job._kill()
                raise JobError(f"timed out after {int(timeout)}s")
            if job.cancel_requested:
                job._kill()
                raise Cancelled("Cancelled by user")
        rc = proc.wait()
    finally:
        job._proc = None
        proc.stdout.close()
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    if job.cancel_requested:
        raise Cancelled("Cancelled by user")
    return rc


class RunReq(BaseModel):
    action: str
    select: str | None = None
    upstream: bool = False
    downstream: bool = False
    full_refresh: bool = False
    inline_sql: str | None = Field(default=None, max_length=MAX_SQL)
    limit: int = 100


def build_args(req: RunReq) -> list[str]:
    a = req.action
    if a not in ACTIONS:
        raise dp.StudioError(f"Unknown action {a!r}.")
    if a == "docs":
        return ["docs", "generate"]
    args = [a]
    if req.inline_sql is not None:
        if a not in ("show", "compile"):
            raise dp.StudioError("Inline SQL is only for preview and compile.")
        sql = req.inline_sql.strip().rstrip(";").strip()
        if not sql:
            raise dp.StudioError("Nothing to run — the SQL is empty.")
        args += ["--inline", sql]
    elif req.select:
        if a not in SELECTABLE:
            raise dp.StudioError(f"{a} does not take a selection.")
        name = req.select
        if name.startswith(("tag:", "path:")) and re.fullmatch(r"(tag|path):[\w./-]+", name) and ".." not in name:
            sel = name
        else:
            dp.check_model_name(name)
            sel = ("+" if req.upstream else "") + name + ("+" if req.downstream else "")
        args += ["--select", sel]
    elif a in ("show", "compile"):
        raise dp.StudioError("Pick a model to preview or compile.")
    if a == "show":
        args += ["--limit", str(min(max(int(req.limit or 100), 1), 1000)), "--output", "json"]
    if req.full_refresh and a in ("build", "run", "seed"):
        args.append("--full-refresh")
    return args


TITLES = {"parse": "Refresh lineage", "build": "Build", "run": "Run", "test": "Test", "seed": "Load seeds",
          "compile": "Compile", "show": "Preview", "docs": "Generate docs", "deps": "Install packages",
          "snapshot": "Snapshot"}


def submit(project: Path, pid: str, req: RunReq) -> Job:
    args = build_args(req)
    what = req.select if req.select else ("inline SQL" if req.inline_sql is not None else "")
    title = f"{TITLES[req.action]}{' ' + what if what else ''} · {pid}"

    def fn(job: Job):
        capture: dict = {}
        rc = run_dbt(job, project, args, capture)
        n = dp.merge_run_results(project) if req.action in dp.RECORDED_COMMANDS else 0
        out = {"project": pid, "action": req.action, "select": req.select, "recorded": n}
        if capture:
            _remember(job.id, {k: v for k, v in capture.items()})
            out["has_output"] = True
        if rc != 0:
            raise JobError(f"dbt {req.action} finished with errors (exit {rc}) — see the log above")
        return out

    return MANAGER.submit(QUEUE, f"dbt-{req.action}", title, fn,
                          {"project": pid, "action": req.action, "select": req.select})


# ── routes ──────────────────────────────────────────────────────────
@router.get("/projects")
def projects():
    root = dp.work_root()
    try:   # Studio cloud identity the bigquery target writes with (name only)
        from bsp_common import gcp
        gcp_identity = gcp.assigned_name("bigquery_write")
    except Exception:  # noqa: BLE001
        gcp_identity = ""
    return {"root": str(root), "target": dp.dbt_target(), "projects": dp.discover_projects(root),
            "gcp_identity": gcp_identity}


@router.get("/graph")
def graph(p: str):
    d = _project(p)
    return _guard(dp.build_graph, d)


@router.get("/model")
def get_model(p: str, name: str):
    d = _project(p)
    _guard(dp.check_model_name, name)
    f = _guard(dp.model_file, d, name)
    if f is None:
        raise HTTPException(404, f"Model {name} not found in {p}.")
    node = dp.find_node(d, name) or {}
    props = _guard(dbt_yaml.get_model_props, d, name)
    return {
        "name": name, "path": f.relative_to(d).as_posix(), "sql": f.read_text(encoding="utf-8"),
        "mtime": f.stat().st_mtime, "props": props,
        "materialized": (node.get("config") or {}).get("materialized"),
        "relation": node.get("relation_name"), "in_manifest": bool(node),
        "layer": dp.layer_of(node) if node else next((x for x in dp.LAYERS if x in f.relative_to(d).parts), "other"),
    }


class SqlBody(BaseModel):
    sql: str = Field(max_length=MAX_SQL)
    mtime: float | None = None


@router.put("/model/sql")
def save_sql(p: str, name: str, body: SqlBody):
    d = _project(p)
    f = _guard(dp.model_file, d, name)
    if f is None:
        raise HTTPException(404, f"Model {name} not found.")
    if body.mtime and abs(f.stat().st_mtime - body.mtime) > 1e-3:
        raise HTTPException(409, "This model changed on disk since you opened it (VS Code or another tab). Reload it first.")
    text = body.sql if body.sql.endswith("\n") else body.sql + "\n"
    tmp = f.with_name(f".{f.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, f)
    return {"saved": True, "path": f.relative_to(d).as_posix(), "mtime": f.stat().st_mtime}


class TestSpec(BaseModel):
    severity: str | None = None
    values: list[str] | None = None
    to: str | None = None
    field: str | None = None


class ColumnSpec(BaseModel):
    name: str
    description: str = ""
    tests: dict[str, TestSpec | None] = {}


class PropsBody(BaseModel):
    description: str | None = None
    materialized: str | None = None
    unique_key: str | None = None
    tags: list[str] = []
    columns: list[ColumnSpec] = []


def _props_dict(body: PropsBody) -> dict:
    cols = []
    for c in body.columns:
        tests = {k: (v.model_dump(exclude_none=True) if v is not None else None) for k, v in c.tests.items()}
        cols.append({"name": c.name, "description": c.description, "tests": {k: v for k, v in tests.items() if v is not None}})
    return {"description": body.description or "", "materialized": body.materialized, "unique_key": body.unique_key,
            "tags": body.tags, "columns": cols}


@router.put("/model/props")
def save_props(p: str, name: str, body: PropsBody):
    d = _project(p)
    _guard(dp.check_model_name, name)
    if _guard(dp.model_file, d, name) is None:
        raise HTTPException(404, f"Model {name} not found.")
    props = _guard(dbt_yaml.set_model_props, d, name, _props_dict(body))
    sql = _guard(dp.model_file, d, name).read_text(encoding="utf-8")
    warn = None
    if body.materialized and re.search(r"config\s*\([^)]*materialized\s*=", sql):
        warn = "The model's SQL sets materialized in {{ config(...) }}, which wins over this setting."
    return {"props": props, "warning": warn}


@router.get("/columns")
def columns(p: str, ref: str | None = None, source: str | None = None, table: str | None = None):
    d = _project(p)
    if not ref and not (source and table):
        raise HTTPException(400, "Give ref=<model> or source=<source>&table=<table>.")
    return _guard(dp.relation_columns, d, ref=ref, source=source, table=table)


class WizardBody(BaseModel):
    spec: dict
    overwrite: bool = False


@router.post("/wizard/sql")
def wizard_sql(p: str, body: WizardBody):
    _project(p)
    return {"sql": _guard(dbt_sql.generate_sql, body.spec)}


@router.post("/wizard")
def wizard_create(p: str, body: WizardBody):
    d = _project(p)
    spec = body.spec
    sql = _guard(dbt_sql.generate_sql, spec)
    name, layer = spec["name"], spec["layer"]
    existing = _guard(dp.model_file, d, name)
    if existing is not None and not body.overwrite:
        raise HTTPException(409, f"A model called {name} already exists ({existing.relative_to(d).as_posix()}).")
    base = _guard(dp.safe_path, d, dp.model_paths(d)[0])
    target = existing or _guard(dp.safe_path, d, f"{base.relative_to(d).as_posix()}/{layer}/{name}.sql")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(sql, encoding="utf-8")
    props = None
    if str(spec.get("description") or "").strip():
        props = _guard(dbt_yaml.set_model_props, d, name, {"description": spec.get("description")})
    return {"name": name, "path": target.relative_to(d).as_posix(), "sql": sql, "props": props}


@router.get("/warehouse/tables")
def wh_tables():
    if dp.dbt_target() != "postgres":
        raise HTTPException(400, f"Browsing tables needs the postgres target (current: {dp.dbt_target()}).")
    try:
        return {"schemas": dp.warehouse_tables()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach the warehouse ({type(exc).__name__}).") from exc


@router.get("/warehouse/columns")
def wh_columns(schema: str, table: str):
    _guard(dp.check_ident, schema, "schema")
    _guard(dp.check_ident, table, "table")
    try:
        return {"columns": dp.warehouse_columns(schema, table)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, f"Could not reach the warehouse ({type(exc).__name__}).") from exc


class SourceBody(BaseModel):
    source: str
    schema_name: str = Field(alias="schema")
    tables: list[str]
    description: str = ""


@router.post("/sources")
def add_source(p: str, body: SourceBody):
    d = _project(p)
    return _guard(dbt_yaml.add_source, d, body.source, body.schema_name, body.tables, body.description)


@router.post("/run")
def run(p: str, req: RunReq):
    d = _project(p)
    job = _guard(submit, d, p, req)
    return job.summary()


@router.get("/output/{job_id}")
def output(job_id: str):
    with _OUT_LOCK:
        data = _OUTPUTS.get(job_id)
    if data is None:
        raise HTTPException(404, "No output for this job (it may have been cleared after a restart).")
    out = dict(data)
    if isinstance(out.get("preview"), str):
        try:
            rows = json.loads(out["preview"])
            out["rows"] = rows
            out["columns"] = list(rows[0].keys()) if rows else []
        except ValueError:
            out["text"] = out["preview"]
        out.pop("preview", None)
    return out
