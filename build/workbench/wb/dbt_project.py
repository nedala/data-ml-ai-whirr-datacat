"""dbt Studio — project discovery, path safety, manifest → lineage graph,
run status history and warehouse introspection.

Every file operation goes through :func:`safe_path`, which only resolves
paths inside the chosen project directory. Model names must match
``^[a-z][a-z0-9_]*$``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import yaml

from .config import STATE_DIR

MODEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
LAYERS = ("staging", "intermediate", "marts")
SKIP_DIRS = {"target", "logs", "dbt_packages", "node_modules", "__pycache__", "venv", ".venv"}
WATCHED_SUFFIXES = {".sql", ".yml", ".yaml", ".csv", ".md"}


class StudioError(ValueError):
    """User-facing error (HTTP 400/404)."""

    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


def work_root() -> Path:
    return Path(os.environ.get("WORKBENCH_DBT_ROOT") or Path.home() / "work").resolve()


def dbt_target() -> str:
    return os.environ.get("DBT_TARGET", "").strip() or "postgres"


def check_model_name(name: str) -> str:
    if not isinstance(name, str) or not MODEL_RE.match(name) or len(name) > 63:
        raise StudioError("Model names use lower-case letters, digits and _ and start with a letter (e.g. stg_tickets).")
    return name


def check_ident(value: str, what: str = "name") -> str:
    if not isinstance(value, str) or not IDENT_RE.match(value) or len(value) > 63:
        raise StudioError(f"Invalid {what}: letters, digits and _ only, starting with a letter.")
    return value


# ── projects ─────────────────────────────────────────────────────────
def discover_projects(root: Path | None = None, max_depth: int = 3) -> list[dict]:
    root = root or work_root()
    out: list[dict] = []
    if not root.is_dir():
        return out

    def walk(d: Path, depth: int) -> None:
        if (d / "dbt_project.yml").is_file():
            out.append(project_info(d, root))
            return
        if depth >= max_depth:
            return
        try:
            kids = sorted(p for p in d.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return
        for k in kids:
            if k.name.startswith(".") or k.name in SKIP_DIRS:
                continue
            walk(k, depth + 1)

    walk(root, 0)
    order = {"dbt_project": 0}
    out.sort(key=lambda p: (order.get(p["id"], 9), p["id"]))
    return out


def project_info(d: Path, root: Path | None = None) -> dict:
    root = root or work_root()
    cfg = project_config(d)
    return {
        "id": d.relative_to(root).as_posix(),
        "name": cfg.get("name") or d.name,
        "path": str(d),
        "profile": cfg.get("profile"),
        "has_profiles": (d / "profiles.yml").is_file(),
        "has_manifest": (d / "target" / "manifest.json").is_file(),
        "model_paths": model_paths(d),
    }


def resolve_project(pid: str) -> Path:
    if not isinstance(pid, str) or not pid or pid.startswith("/") or "\\" in pid or len(pid) > 300:
        raise StudioError("Unknown project.", 404)
    if any(part in ("", ".", "..") or part.startswith(".") for part in pid.split("/")):
        raise StudioError("Unknown project.", 404)
    root = work_root()
    d = (root / pid).resolve()
    if not d.is_relative_to(root) or d == root or not (d / "dbt_project.yml").is_file():
        raise StudioError("Unknown project.", 404)
    return d


def safe_path(project: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``project``; refuse anything that escapes it."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        raise StudioError("Invalid path.")
    p = (project / rel).resolve()
    if not p.is_relative_to(project.resolve()):
        raise StudioError("Path is outside the project.")
    return p


def project_config(d: Path) -> dict:
    try:
        cfg = yaml.safe_load((d / "dbt_project.yml").read_text(encoding="utf-8")) or {}
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


def model_paths(d: Path) -> list[str]:
    mp = project_config(d).get("model-paths") or ["models"]
    return [str(x) for x in mp if isinstance(x, str)] or ["models"]


def target_dir(d: Path) -> Path:
    tp = project_config(d).get("target-path") or "target"
    p = (d / str(tp)).resolve()
    return p if p.is_relative_to(d) else d / "target"


def _watched_dirs(d: Path) -> list[Path]:
    cfg = project_config(d)
    keys = ("model-paths", "seed-paths", "snapshot-paths", "macro-paths", "test-paths", "analysis-paths")
    defaults = {"model-paths": ["models"], "seed-paths": ["seeds"], "snapshot-paths": ["snapshots"],
                "macro-paths": ["macros"], "test-paths": ["tests"], "analysis-paths": ["analyses"]}
    dirs = []
    for k in keys:
        for rel in cfg.get(k) or defaults[k]:
            p = (d / str(rel)).resolve()
            if p.is_relative_to(d) and p.is_dir():
                dirs.append(p)
    return dirs


def latest_source_mtime(d: Path) -> float:
    best = (d / "dbt_project.yml").stat().st_mtime
    for base in _watched_dirs(d):
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if not x.startswith(".") and x not in SKIP_DIRS]
            for f in files:
                if Path(f).suffix in WATCHED_SUFFIXES:
                    try:
                        best = max(best, os.stat(os.path.join(root, f)).st_mtime)
                    except OSError:
                        pass
    return best


def load_manifest(d: Path) -> dict | None:
    try:
        with open(target_dir(d) / "manifest.json", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def manifest_is_stale(d: Path) -> bool:
    mf = target_dir(d) / "manifest.json"
    try:
        return latest_source_mtime(d) > mf.stat().st_mtime
    except OSError:
        return True


# ── run status history ───────────────────────────────────────────────
# dbt overwrites target/run_results.json on every command (including show and
# compile), so the latest outcome of run/build/test/seed per node is merged
# into a small store that survives those.
_STATUS_LOCK = threading.Lock()
RECORDED_COMMANDS = {"build", "run", "test", "seed", "snapshot"}


def _status_file(d: Path) -> Path:
    h = hashlib.sha1(str(d).encode()).hexdigest()[:16]
    folder = STATE_DIR / "dbt_status"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{h}.json"


def load_status(d: Path) -> dict:
    merge_run_results(d)
    try:
        return json.loads(_status_file(d).read_text(encoding="utf-8")).get("nodes", {})
    except (OSError, ValueError):
        return {}


def merge_run_results(d: Path) -> int:
    """Fold target/run_results.json into the status store (idempotent)."""
    try:
        rr = json.loads((target_dir(d) / "run_results.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    which = ((rr.get("args") or {}).get("which") or "").lower()
    if which not in RECORDED_COMMANDS:
        return 0
    gen = (rr.get("metadata") or {}).get("generated_at") or ""
    with _STATUS_LOCK:
        sf = _status_file(d)
        try:
            store = json.loads(sf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            store = {"seen": [], "nodes": {}}
        if gen and gen in store.get("seen", []):
            return 0
        n = 0
        for r in rr.get("results") or []:
            uid = r.get("unique_id")
            if not uid:
                continue
            store["nodes"][uid] = {
                "status": r.get("status"),
                "at": gen,
                "execution_time": r.get("execution_time"),
                "failures": r.get("failures"),
                "message": (r.get("message") or "")[:500] if r.get("status") not in ("success", "pass") else "",
                "command": which,
            }
            n += 1
        store["seen"] = (store.get("seen", []) + [gen])[-50:]
        tmp = sf.with_suffix(".tmp")
        tmp.write_text(json.dumps(store), encoding="utf-8")
        tmp.replace(sf)
        return n


# ── graph ────────────────────────────────────────────────────────────
def layer_of(node: dict) -> str:
    rt = node.get("resource_type")
    if rt in ("source", "seed", "snapshot", "exposure", "analysis"):
        return rt
    parts = [p.lower() for p in (node.get("fqn") or [])[1:-1]]
    for lay in LAYERS:
        if lay in parts:
            return lay
    path = (node.get("original_file_path") or "").lower()
    for lay in LAYERS:
        if f"/{lay}/" in f"/{path}":
            return lay
    tags = [t.lower() for t in (node.get("tags") or [])]
    for lay in LAYERS:
        if lay in tags:
            return lay
    return "other"


def _test_kind(t: dict) -> str:
    md = t.get("test_metadata") or {}
    return md.get("name") or "singular"


def build_graph(d: Path) -> dict:
    man = load_manifest(d)
    if man is None:
        return {"nodes": [], "edges": [], "has_manifest": False, "stale": True}
    status = load_status(d)
    nodes: dict[str, dict] = {}
    tests_by_node: dict[str, list[dict]] = {}

    def add(uid: str, n: dict) -> None:
        rt = n.get("resource_type")
        cfg = n.get("config") or {}
        nodes[uid] = {
            "id": uid,
            "name": n.get("name"),
            "type": rt,
            "layer": layer_of(n),
            "path": n.get("original_file_path"),
            "patch_path": (n.get("patch_path") or "").split("://", 1)[-1] or None,
            "materialized": cfg.get("materialized") if rt in ("model", "seed", "snapshot") else None,
            "schema": n.get("schema"),
            "relation": n.get("relation_name"),
            "description": n.get("description") or "",
            "tags": n.get("tags") or [],
            "columns": [{"name": c.get("name"), "description": c.get("description") or ""}
                        for c in (n.get("columns") or {}).values()],
            "source_name": n.get("source_name"),
            "parents": [],
            "status": status.get(uid),
        }

    for uid, n in (man.get("nodes") or {}).items():
        rt = n.get("resource_type")
        if rt in ("model", "seed", "snapshot"):
            add(uid, n)
    for uid, n in (man.get("sources") or {}).items():
        add(uid, n)
    for uid, n in (man.get("exposures") or {}).items():
        add(uid, n)

    edges = []
    for uid, nd in nodes.items():
        src = man["nodes"].get(uid) or man.get("exposures", {}).get(uid) or {}
        for p in (src.get("depends_on") or {}).get("nodes") or []:
            if p in nodes:
                edges.append({"from": p, "to": uid})
                nd["parents"].append(p)

    for uid, t in (man.get("nodes") or {}).items():
        if t.get("resource_type") != "test":
            continue
        targets = [t["attached_node"]] if t.get("attached_node") else [
            p for p in (t.get("depends_on") or {}).get("nodes") or [] if p in nodes]
        st = status.get(uid) or {}
        entry = {"id": uid, "name": t.get("name"), "kind": _test_kind(t), "column": t.get("column_name"),
                 "status": st.get("status"), "failures": st.get("failures"), "at": st.get("at"),
                 "severity": (t.get("config") or {}).get("severity")}
        for tg in targets:
            if tg in nodes:
                tests_by_node.setdefault(tg, []).append(entry)

    for uid, nd in nodes.items():
        ts = tests_by_node.get(uid, [])
        summary = {"total": len(ts), "pass": 0, "fail": 0, "warn": 0, "error": 0, "skipped": 0, "never": 0}
        for t in ts:
            s = t["status"]
            if s in summary:
                summary[s] += 1
            elif s is None:
                summary["never"] += 1
        nd["tests"] = summary
        nd["test_list"] = ts

    meta = man.get("metadata") or {}
    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "has_manifest": True,
        "stale": manifest_is_stale(d),
        "generated_at": meta.get("generated_at"),
        "dbt_version": meta.get("dbt_version"),
        "project_name": meta.get("project_name") or project_config(d).get("name"),
        "target": dbt_target(),
    }


def find_node(d: Path, name: str, kinds=("model",)) -> dict | None:
    man = load_manifest(d) or {}
    for n in (man.get("nodes") or {}).values():
        if n.get("resource_type") in kinds and n.get("name") == name:
            return n
    return None


def find_source(d: Path, source: str, table: str) -> dict | None:
    man = load_manifest(d) or {}
    for n in (man.get("sources") or {}).values():
        if n.get("source_name") == source and n.get("name") == table:
            return n
    return None


def model_file(d: Path, name: str) -> Path | None:
    """The .sql file for a model: from the manifest, else by scanning model paths."""
    check_model_name(name)
    n = find_node(d, name)
    if n and n.get("original_file_path"):
        p = safe_path(d, n["original_file_path"])
        if p.is_file():
            return p
    for mp in model_paths(d):
        base = safe_path(d, mp)
        if not base.is_dir():
            continue
        for p in sorted(base.rglob(f"{name}.sql")):
            if p.is_file() and not any(part in SKIP_DIRS for part in p.relative_to(d).parts):
                return p.resolve()
    return None


# ── warehouse introspection (Postgres) ───────────────────────────────
def pg_connect():
    import psycopg2  # imported lazily: tests run without a database

    return psycopg2.connect(
        host=os.environ.get("PG_HOST", "postgres"), port=int(os.environ.get("PG_PORT", "5432") or 5432),
        dbname=os.environ.get("PG_DB", "bsp_edw"), user=os.environ.get("PG_USER", "analyst"),
        password=os.environ.get("PG_PASS", ""), connect_timeout=5,
        application_name="workbench-dbt-studio")


HIDDEN_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def warehouse_tables() -> dict:
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select table_schema, table_name, table_type from information_schema.tables "
            "where table_schema not in %s and table_schema not like 'pg_%%' "
            "order by table_schema, table_name", (HIDDEN_SCHEMAS,))
        out: dict[str, list[dict]] = {}
        for s, t, ty in cur.fetchall():
            out.setdefault(s, []).append({"name": t, "type": "view" if ty == "VIEW" else "table"})
    return out


def warehouse_columns(schema: str, table: str) -> list[dict]:
    with pg_connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select column_name, data_type from information_schema.columns "
            "where table_schema = %s and table_name = %s order by ordinal_position", (schema, table))
        return [{"name": c, "type": t} for c, t in cur.fetchall()]


def relation_columns(d: Path, *, ref: str | None = None, source: str | None = None,
                     table: str | None = None) -> dict:
    """Columns of a model (ref) or source table: live from the warehouse when
    the relation exists, else the documented columns from the manifest."""
    if ref:
        check_model_name(ref)
        n = find_node(d, ref, ("model", "seed", "snapshot"))
        label = ref
    else:
        check_ident(source or "", "source name")
        check_ident(table or "", "table name")
        n = find_source(d, source, table)
        label = f"{source}.{table}"
    if n is None:
        raise StudioError(f"{label} is not in the project yet — refresh the lineage first.", 404)
    schema = n.get("schema")
    ident = n.get("identifier") if n.get("resource_type") == "source" else (n.get("alias") or n.get("name"))
    documented = [{"name": c.get("name"), "type": c.get("data_type"), "description": c.get("description") or ""}
                  for c in (n.get("columns") or {}).values()]
    live: list[dict] = []
    err = None
    if dbt_target() == "postgres" and schema and ident:
        try:
            live = warehouse_columns(schema, ident)
        except Exception as exc:  # noqa: BLE001
            err = f"warehouse not reachable: {type(exc).__name__}"
    docs = {c["name"]: c for c in documented}
    cols = []
    for c in live:
        cols.append({**c, "description": (docs.get(c["name"]) or {}).get("description", "")})
    seen = {c["name"] for c in cols}
    cols += [c for c in documented if c["name"] not in seen]
    return {"columns": cols, "live": bool(live), "relation": n.get("relation_name"), "error": err,
            "built": bool(live)}
