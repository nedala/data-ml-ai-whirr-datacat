"""Pipeline engine: validate → execute the DAG (pandas) → load with dlt.

Extract: enterprise sources through bsp_connectors, databases through dlt's
sql_database source (SQLAlchemy / pyarrow backends) and object storage through
dlt's filesystem source (fsspec). Transform: pandas. Load: dlt destinations
(postgres, bigquery, filesystem) with full refresh / append / incremental /
merge; column names are normalised with dlt's snake_case convention so the
preview shows what lands.
"""

from __future__ import annotations

import json
import threading
import time
import traceback
import uuid
from collections import defaultdict
from typing import Any, Callable

import pandas as pd

from . import lineage, store
from .config import interpolate, lake_dir, sub
from .nodes import REGISTRY, Ctx, as_list, jsonable_frame

_running: dict[str, str] = {}  # pipeline_id → run_id
_run_lock = threading.Lock()


# ── validation ───────────────────────────────────────────────────────────
def validate(doc: dict) -> list[dict]:
    issues: list[dict] = []
    nodes = {n.get("id"): n for n in doc.get("nodes", [])}

    def add(node: str | None, msg: str, level: str = "error") -> None:
        issues.append({"node": node, "level": level, "message": msg})

    if not nodes:
        add(None, "add at least one source and one destination")
        return issues
    ins: dict[str, list[dict]] = defaultdict(list)
    outs: dict[str, list[dict]] = defaultdict(list)
    for e in doc.get("edges", []):
        if e.get("from") not in nodes or e.get("to") not in nodes:
            add(None, f"edge {e.get('from')} → {e.get('to')} references a missing node")
            continue
        ins[e["to"]].append(e)
        outs[e["from"]].append(e)
    for nid, n in nodes.items():
        t = REGISTRY.get(n.get("type"))
        if t is None:
            add(nid, f"unknown node type {n.get('type')!r}")
            continue
        cfg = n.get("config") or {}
        for f in t.fields:
            if f.get("required") and cfg.get(f["key"]) in (None, "", [], {}) and _visible(f, cfg, t):
                add(nid, f"{t.label}: “{f['label']}” is required")
        main = [e for e in ins[nid] if e.get("port", "in") == "in"]
        if t.category == "source" and ins[nid]:
            add(nid, f"{t.label} is a source and cannot have inputs")
        if t.category != "source" and len(main) != 1:
            add(nid, f"{t.label} needs exactly one input (has {len(main)})")
        if t.category == "destination" and outs[nid]:
            add(nid, f"{t.label} is a destination and cannot have outputs")
        if t.category != "destination" and not outs[nid]:
            add(nid, f"{n.get('label') or t.label} is not connected to anything downstream", "warning")
        if t.category == "destination":
            mode = cfg.get("mode", "full_refresh")
            if mode == "incremental" and not cfg.get("cursor_column"):
                add(nid, "incremental sync needs a cursor column")
            if mode == "merge" and not as_list(cfg.get("primary_key")):
                add(nid, "merge sync needs a primary key")
        if n.get("type") in ("transform.filter", "transform.derive") and cfg.get("expression"):
            from .expr import ExprError, parse
            try:
                parse(cfg["expression"])
            except ExprError as e:
                add(nid, f"expression: {e}")
    try:
        topo(doc)
    except ValueError as e:
        add(None, str(e))
    if not any(REGISTRY.get(n.get("type")) and REGISTRY[n["type"]].category == "destination" for n in nodes.values()):
        add(None, "add a destination (warehouse, BigQuery, lake or object storage)")
    return issues


def _visible(f: dict, cfg: dict, t) -> bool:
    cond = f.get("show_if")
    if not cond:
        return True
    for k, v in cond.items():
        default = next((x.get("default") for x in t.fields if x["key"] == k), None)
        cur = cfg.get(k, default)
        if (cur not in v) if isinstance(v, list) else (cur != v):
            return False
    return True


def topo(doc: dict) -> list[str]:
    nodes = [n["id"] for n in doc.get("nodes", [])]
    deps: dict[str, set[str]] = {n: set() for n in nodes}
    for e in doc.get("edges", []):
        if e.get("to") in deps and e.get("from") in deps:
            deps[e["to"]].add(e["from"])
    order, done = [], set()
    while len(order) < len(nodes):
        ready = [n for n in nodes if n not in done and deps[n] <= done]
        if not ready:
            raise ValueError("the pipeline has a cycle")
        for n in ready:
            order.append(n)
            done.add(n)
    return order


def ancestors(doc: dict, target: str) -> set[str]:
    up: dict[str, set[str]] = defaultdict(set)
    for e in doc.get("edges", []):
        up[e["to"]].add(e["from"])
    seen, stack = {target}, [target]
    while stack:
        for p in up[stack.pop()]:
            if p not in seen:
                seen.add(p)
                stack.append(p)
    return seen


# ── execution ────────────────────────────────────────────────────────────
def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    from dlt.common.normalizers.naming.snake_case import NamingConvention
    nc = NamingConvention()
    cols, seen = [], {}
    for c in df.columns:
        n = nc.normalize_identifier(str(c))
        if n in seen:
            seen[n] += 1
            n = f"{n}_{seen[n]}"
        else:
            seen[n] = 0
        cols.append(n)
    df = df.copy()
    df.columns = cols
    return df


def norm_name(name: str) -> str:
    from dlt.common.normalizers.naming.snake_case import NamingConvention
    return NamingConvention().normalize_identifier(name)


def execute(doc: dict, ctx: Ctx, target: str | None = None) -> dict[str, pd.DataFrame]:
    nodes = {n["id"]: n for n in doc["nodes"]}
    want = ancestors(doc, target) if target else set(nodes)
    results: dict[str, pd.DataFrame] = {}
    pending: dict[str, dict] = {}
    for nid in topo(doc):
        if nid not in want:
            continue
        n = nodes[nid]
        t = REGISTRY[n["type"]]
        inputs = {e.get("port", "in"): results[e["from"]] for e in doc.get("edges", []) if e["to"] == nid}
        cfg = n.get("config") or {}
        ctx.node_id = nid
        ctx.state = (ctx.all_state.get("nodes") or {}).get(nid, {})
        ctx.pending = {}
        t0 = time.time()
        ctx.log(f"▶ {n.get('label') or t.label} [{t.type}]")
        try:
            df = t.run(ctx, cfg, inputs)
        except Exception as e:
            raise NodeError(nid, f"{n.get('label') or t.label}: {type(e).__name__}: {e}") from e
        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)
        results[nid] = df
        if ctx.pending:
            pending[nid] = ctx.pending
        ctx.log(f"  {len(df):,} row(s) × {len(df.columns)} column(s) in {time.time() - t0:.2f}s")
    ctx.all_state.setdefault("_pending_nodes", {}).update(pending)
    return results


class NodeError(RuntimeError):
    def __init__(self, node: str, msg: str):
        super().__init__(msg)
        self.node = node


def _dtype_label(s: pd.Series) -> str:
    k = s.dtype.kind
    return {"i": "integer", "u": "integer", "f": "float", "b": "boolean", "M": "datetime", "m": "duration"}.get(
        k, "boolean" if str(s.dtype) == "boolean" else ("integer" if str(s.dtype).startswith("Int") else "string"))


def frame_preview(df: pd.DataFrame, limit: int = 50) -> dict:
    head = df.head(limit)
    head = head.astype(object).where(pd.notna(head), None)
    rows = json.loads(head.to_json(orient="records", date_format="iso", default_handler=str))
    return {"columns": [{"name": str(c), "type": _dtype_label(df[c]), "landed_as": norm_name(str(c))} for c in df.columns],
            "rows": rows, "row_count": len(df)}


def preview(doc: dict, node_id: str, limit: int = 50) -> dict:
    logs: list[str] = []
    ctx = Ctx(doc.get("id") or "preview", logs.append, limit=max(limit, 200), state=store.get_state(doc["id"]) if doc.get("id")
              and store.ID_RE.match(doc["id"]) else {})
    t0 = time.time()
    res = execute(doc, ctx, target=node_id)
    out = frame_preview(res[node_id], limit)
    out.update(logs=logs, ms=int((time.time() - t0) * 1000), sample=True)
    return out


# ── loading ──────────────────────────────────────────────────────────────
def to_arrow(df: pd.DataFrame):
    import pyarrow as pa
    df = jsonable_frame(df.copy())
    for c in df.columns:
        if df[c].dtype == object:
            try:
                pa.array(df[c], from_pandas=True)
            except (pa.ArrowInvalid, pa.ArrowTypeError, TypeError):
                df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and pd.isna(v)) else str(v))
    return pa.Table.from_pandas(df, preserve_index=False)


def _pg_dlt_url(conn: dict | None = None) -> str:
    from . import connections
    url = connections.sqlalchemy_url(conn or connections.WAREHOUSE)
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def _cmp_series(s: pd.Series, hw: Any) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(s):
        h = pd.to_datetime(hw, utc=True)
        s2 = s.dt.tz_localize("UTC") if s.dt.tz is None else s
        return s2 > h
    if pd.api.types.is_numeric_dtype(s):
        return pd.to_numeric(s, errors="coerce") > float(hw)
    return s.astype("string") > str(hw)


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


def load(node: dict, df: pd.DataFrame, ctx: Ctx, run_id: str) -> dict:
    """Load one destination. Returns {rows, target, namespace, name, fields}."""
    import dlt

    cfg = node.get("config") or {}
    t = node["type"]
    df = normalize_columns(df)
    mode = cfg.get("mode", "full_refresh")
    table = norm_name(interpolate(cfg["table"]))
    nstate = (ctx.all_state.get("nodes") or {}).get(node["id"], {})
    pend: dict = {}
    if mode == "incremental":
        cur = norm_name(cfg["cursor_column"])
        if cur not in df.columns:
            raise ValueError(f"cursor column {cfg['cursor_column']!r} not in the data")
        hw = nstate.get("cursor")
        if hw is not None:
            before = len(df)
            df = df[_cmp_series(df[cur], hw).fillna(False)]
            ctx.log(f"  incremental: {cur} > {hw} → {len(df)} of {before} row(s) are new")
        if len(df):
            pend["cursor"] = _jsonable(df[cur].max())
    pk = [norm_name(c) for c in as_list(cfg.get("primary_key"))]
    wd: Any = {"full_refresh": "replace", "append": "append", "incremental": "append", "merge": "merge"}[mode]
    fields = [(c, _dtype_label(df[c])) for c in df.columns]
    if t == "dest.lake" or t == "dest.storage":
        return _load_files(node, df, ctx, run_id, table, mode, fields, pend)
    if t == "dest.warehouse":
        dest = dlt.destinations.postgres(credentials=_pg_dlt_url())
        dataset = interpolate(cfg.get("schema") or "landing")
        ns, name = lineage_ns_warehouse(), f"bsp_edw.{dataset}.{table}"
    elif t == "dest.postgres":
        from . import connections
        conn = connections.get(cfg.get("connection") or "warehouse")
        dest = dlt.destinations.postgres(credentials=_pg_dlt_url(conn))
        dataset = interpolate(cfg.get("schema") or "landing")
        ns = connections.namespace(conn)
        name = f"{(conn.get('params') or {}).get('database', 'bsp_edw')}.{dataset}.{table}"
    elif t == "dest.bigquery":
        from bsp_connectors import bigquery, mode_of
        dataset = interpolate(cfg.get("dataset") or "landing")
        if mode_of("bigquery") == "live":
            from bsp_common import gcp
            from .config import env
            creds = gcp.info("bigquery_write")   # Studio → Cloud identities: BigQuery · write (+ its data project)
            creds = {**creds, "project_id": gcp.project("bigquery_write")} if creds else None
            dest = dlt.destinations.bigquery(credentials=creds, location=env("BQ_LOCATION", "US"))
        else:
            dest = dlt.destinations.postgres(credentials=_pg_dlt_url())
            ctx.log(f"  BigQuery is in mock mode — loading Postgres schema {dataset!r} (visible to the BigQuery mock)")
        from bsp_common import gcp as _gcp
        ns, name = "bigquery", f"{_gcp.project('bigquery_write')}.{dataset}.{table}"
    else:
        raise ValueError(f"unknown destination {t}")
    if not len(df) and mode != "full_refresh":
        ctx.log("  nothing new to load")
        ctx.all_state.setdefault("_pending_nodes", {}).setdefault(node["id"], {}).update(pend)
        return {"rows": 0, "target": name, "namespace": ns, "name": name, "fields": fields}
    pipe = dlt.pipeline(pipeline_name=f"canvas_{ctx.pipeline_id}_{node['id']}".replace("-", "_"), destination=dest,
                        dataset_name=dataset, pipelines_dir=str(sub("dlt")))
    hints: dict[str, Any] = {"name": table, "write_disposition": wd}
    if pk:
        hints["primary_key"] = pk
    if mode == "merge" and not pk:
        raise ValueError("merge needs a primary key")
    res = dlt.resource(to_arrow(df), **hints)
    info = pipe.run(res, loader_file_format="parquet" if t == "dest.bigquery" else None)
    ctx.log(f"  dlt: loaded {len(df):,} row(s) into {name} ({wd}); load id {info.loads_ids[-1] if info.loads_ids else '-'}")
    ctx.all_state.setdefault("_pending_nodes", {}).setdefault(node["id"], {}).update(pend)
    return {"rows": len(df), "target": name, "namespace": ns, "name": name, "fields": fields}


def lineage_ns_warehouse() -> str:
    from .config import env
    return f"postgres://{env('PG_HOST', 'postgres')}:{env('PG_PORT', '5432')}"


def _load_files(node: dict, df: pd.DataFrame, ctx: Ctx, run_id: str, table: str, mode: str, fields, pend) -> dict:
    """Lake (local) and object storage (GCS / S3 / Azure) via dlt's filesystem destination;
    partitioning by a column uses pyarrow's hive-style dataset writer on the same fsspec filesystem."""
    import dlt

    from . import connections
    cfg = node.get("config") or {}
    fmt = cfg.get("format", "parquet")
    part = (cfg.get("partition_by") or "").strip()
    if node["type"] == "dest.lake":
        folder = interpolate(cfg.get("folder") or "landing/canvas").strip("/")
        root = lake_dir() / folder
        root.parent.mkdir(parents=True, exist_ok=True)  # dlt creates (and marks) the dataset folder itself
        bucket_url, creds, fs, base = root.as_uri(), None, None, str(root)
        ns, name = "file:///data/lake", f"{folder}/{table}"
    else:
        conn = connections.get(cfg["connection"])
        bucket = interpolate(cfg["bucket"]).strip("/")
        prefix = interpolate(cfg.get("prefix") or "").strip("/")
        base = f"{bucket}/{prefix}" if prefix else bucket
        bucket_url = f"{connections.scheme(conn)}://{base}"
        creds, fs = connections.dlt_credentials(conn), None
        ns, name = f"{connections.scheme(conn)}://{bucket}", f"{prefix + '/' if prefix else ''}{table}"
    if not len(df):
        ctx.log("  nothing new to write")
        ctx.all_state.setdefault("_pending_nodes", {}).setdefault(node["id"], {}).update(pend)
        return {"rows": 0, "target": bucket_url + "/" + table, "namespace": ns, "name": name, "fields": fields}
    if part and part != "load_date":
        import pyarrow.dataset as pds
        from pyarrow.fs import FSSpecHandler, LocalFileSystem, PyFileSystem
        col = norm_name(part)
        if col not in df.columns:
            raise ValueError(f"partition column {part!r} not in the data")
        if node["type"] == "dest.lake":
            pafs, path = LocalFileSystem(), f"{base}/{table}"
        else:
            pafs, path = PyFileSystem(FSSpecHandler(connections.filesystem(connections.get(cfg["connection"])))), f"{base}/{table}"
        ext = {"parquet": "parquet", "csv": "csv", "jsonl": "jsonl"}[fmt]
        if fmt == "jsonl":
            raise ValueError("column partitioning supports parquet or csv")
        pds.write_dataset(to_arrow(df), path, filesystem=pafs, format=fmt, partitioning=[col], partitioning_flavor="hive",
                          basename_template=f"part-{run_id}-{{i}}.{ext}",
                          existing_data_behavior="delete_matching" if mode == "full_refresh" else "overwrite_or_ignore")
        ctx.log(f"  wrote {len(df):,} row(s) to {path} partitioned by {col}")
    else:
        # dlt filesystem: <bucket_url>/<dataset>/<table>/…  — the last folder segment is the dataset,
        # so lake folder landing/canvas → /data/lake/landing/canvas/<table>/<load_id>.<file_id>.parquet
        parent_url, _, dataset = bucket_url.rstrip("/").rpartition("/")
        if parent_url.endswith(":/") or parent_url.endswith("://") or not dataset:
            raise ValueError("set a folder / prefix (e.g. landing) — files land in <bucket>/<prefix>/<table>/")
        layout = "{table_name}/" + ("load_date={YYYY}-{MM}-{DD}/" if part == "load_date" else "") + "{load_id}.{file_id}.{ext}"
        kw: dict[str, Any] = {"bucket_url": parent_url, "layout": layout}
        if creds is not None:
            kw["credentials"] = creds
        dest = dlt.destinations.filesystem(**kw)
        pipe = dlt.pipeline(pipeline_name=f"canvas_{ctx.pipeline_id}_{node['id']}".replace("-", "_"), destination=dest,
                            dataset_name=dataset, pipelines_dir=str(sub("dlt")))
        wd = "replace" if mode == "full_refresh" else "append"
        pipe.run(dlt.resource(to_arrow(df), name=table, write_disposition=wd),
                 loader_file_format="jsonl" if fmt == "jsonl" else ("csv" if fmt == "csv" else "parquet"))
        ctx.log(f"  dlt filesystem: wrote {len(df):,} row(s) to {bucket_url}/{table}/ ({fmt}, {wd})")
    ctx.all_state.setdefault("_pending_nodes", {}).setdefault(node["id"], {}).update(pend)
    return {"rows": len(df), "target": f"{bucket_url}/{table}", "namespace": ns, "name": name, "fields": fields}


# ── runs ─────────────────────────────────────────────────────────────────
def _acting_user() -> str:
    try:
        from bsp_common import gcp
        return gcp.acting_user()
    except Exception:  # noqa: BLE001
        return ""


def start_run(pid: str, trigger: str = "manual", wait: bool = False) -> dict:
    doc = store.get(pid)
    issues = [i for i in validate(doc) if i["level"] == "error"]
    if issues:
        raise ValueError("; ".join(i["message"] for i in issues))
    with _run_lock:
        if pid in _running:
            raise RuntimeError(f"pipeline {pid} is already running ({_running[pid]})")
        run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        _running[pid] = run_id
    rec = {"run_id": run_id, "pipeline_id": pid, "pipeline_name": doc.get("name"), "status": "running",
           "trigger": trigger, "started_at": store.now(), "ended_at": None, "nodes": {}, "loaded": [], "error": None,
           "started_by": _acting_user()}
    store.put_run(rec)
    # a run someone starts acts as them (their request's principal context);
    # scheduled runs have none → connections fall back to their saved grants
    import contextvars
    th = threading.Thread(target=contextvars.copy_context().run, args=(_run, doc, rec), daemon=True, name=f"run-{run_id}")
    th.start()
    if wait:
        th.join()
        return store.get_run(pid, run_id)
    return rec


def _run(doc: dict, rec: dict) -> None:
    pid, run_id = doc["id"], rec["run_id"]

    def logf(msg: str) -> None:
        store.append_log(pid, run_id, f"{time.strftime('%H:%M:%S')} {msg}")

    state = store.get_state(pid)
    ctx = Ctx(pid, logf, limit=None, state=state)
    events = []
    try:
        logf(f"run {run_id} · pipeline {doc.get('name')} · trigger {rec['trigger']}")
        events.append(lineage.event("START", run_id, doc, [], []))
        dests = [n for n in doc["nodes"] if REGISTRY[n["type"]].category == "destination"]
        results = execute(doc, ctx)
        rec["nodes"] = {nid: len(df) for nid, df in results.items()}
        outputs = []
        for d in dests:
            logf(f"⇣ load {d.get('label') or d['type']}")
            r = load(d, results[d["id"]], ctx, run_id)
            rec["loaded"].append({"node": d["id"], "target": r["target"], "rows": r["rows"]})
            outputs.append({"namespace": r["namespace"], "name": r["name"], "fields": r["fields"], "rows": r["rows"]})
        nodes_state = state.setdefault("nodes", {})
        for nid, p in (state.pop("_pending_nodes", {}) or {}).items():
            merged = nodes_state.setdefault(nid, {})
            for k, v in p.items():
                if isinstance(v, dict) and isinstance(merged.get(k), dict):
                    merged[k].update(v)
                else:
                    merged[k] = v
        state["last_success"] = store.now()
        store.put_state(pid, state)
        rec["status"] = "success"
        events.append(lineage.event("COMPLETE", run_id, doc, ctx.lineage_inputs, outputs,
                                    stats={"rows_loaded": sum(o["rows"] for o in outputs)}))
        logf(f"✓ success — {sum(o['rows'] for o in outputs):,} row(s) loaded")
    except Exception as e:
        rec["status"] = "failed"
        rec["error"] = str(e)
        rec["failed_node"] = getattr(e, "node", None)
        logf(f"✗ failed: {e}")
        for line in traceback.format_exc().splitlines()[-6:]:
            logf("  " + line)
        events.append(lineage.event("FAIL", run_id, doc, ctx.lineage_inputs, [], error=str(e)))
    finally:
        rec["ended_at"] = store.now()
        try:
            rec["lineage"] = lineage.emit(run_id, events, logf)
        except Exception as e:  # lineage never fails a run
            logf(f"lineage: {e}")
        store.put_run(rec)
        with _run_lock:
            _running.pop(pid, None)


def is_running(pid: str) -> str | None:
    return _running.get(pid)


def reset_dlt(pid: str) -> dict:
    """Forget everything dlt remembers for a pipeline so the next run is a clean
    full load: its local working folders (incl. pending load packages) and its
    bookkeeping rows in the warehouse (_dlt_pipeline_state / _dlt_version) —
    otherwise dlt believes dropped destination tables still exist."""
    import re
    import shutil

    prefix = f"canvas_{pid}_".replace("-", "_")
    removed = 0
    root = sub("dlt")
    for d in root.iterdir() if root.exists() else []:
        if d.is_dir() and d.name.startswith(prefix):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    rows = 0
    try:
        from bsp_common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cur.execute("""select table_schema, table_name from information_schema.tables
                           where table_name in ('_dlt_pipeline_state', '_dlt_version')""")
            for schema, table in cur.fetchall():
                if not re.fullmatch(r"[a-z_][a-z0-9_]*", schema):
                    continue
                col = "pipeline_name" if table == "_dlt_pipeline_state" else "schema_name"
                cur.execute(f'delete from "{schema}"."{table}" where {col} like %s', (prefix + "%",))
                rows += cur.rowcount
    except Exception as e:  # noqa: BLE001 — local reset still helps; report it
        return {"local_dirs": removed, "warehouse_rows": rows, "warning": f"{type(e).__name__}: {e}"[:200]}
    return {"local_dirs": removed, "warehouse_rows": rows}
