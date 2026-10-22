"""Node catalogue: palette metadata, config forms and implementations.

Every node type has a `spec` (shown in the palette and rendered as the
inspector form by the UI) and a `run(ctx, cfg, inputs) -> DataFrame`.
Sources take no input, transforms one (Lookup join: one + optional
`lookup` port), destinations one (loading is done by engine.py with dlt).
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from . import expr as X
from .config import interpolate, lake_dir, secret, sub

F = dict  # field spec shorthand


@dataclass
class NodeType:
    type: str
    category: str  # source | transform | destination
    label: str
    icon: str
    blurb: str
    fields: list[dict]
    run: Callable[..., pd.DataFrame] | None = None
    ports: list[str] = field(default_factory=lambda: ["in"])
    group: str = ""

    def spec(self) -> dict:
        return {"type": self.type, "category": self.category, "label": self.label, "icon": self.icon,
                "blurb": self.blurb, "fields": self.fields, "group": self.group,
                "inputs": [] if self.category == "source" else self.ports,
                "outputs": [] if self.category == "destination" else ["out"]}


REGISTRY: dict[str, NodeType] = {}


def node(type_: str, category: str, label: str, icon: str, blurb: str, fields: list[dict], ports=None, group=""):
    def deco(f):
        REGISTRY[type_] = NodeType(type_, category, label, icon, blurb, fields, f, ports or ["in"], group)
        return f
    return deco


class Ctx:
    """What a node sees while running."""

    def __init__(self, pipeline_id: str, log: Callable[[str], None], limit: int | None = None,
                 state: dict | None = None):
        self.pipeline_id, self.log, self.limit = pipeline_id, log, limit
        self.all_state = state if state is not None else {}
        self.state: dict = {}     # saved state of the node being run (read-only)
        self.pending: dict = {}   # new state for that node, committed only if the run succeeds
        self.node_id = ""
        self.lineage_inputs: list[dict] = []

    def dataset(self, namespace: str, name: str, **facets: Any) -> None:
        d = {"namespace": namespace, "name": name}
        if facets:
            d["facets"] = facets
        if d not in self.lineage_inputs:
            self.lineage_inputs.append(d)


# ── helpers ──────────────────────────────────────────────────────────────
def as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, str):
        return [x.strip() for x in re.split(r"[,\n]", v) if x.strip()]
    return [str(x).strip() for x in v if str(x).strip()]


def as_map(v: Any) -> dict[str, str]:
    if not v:
        return {}
    if isinstance(v, dict):
        return {str(k): str(x) for k, x in v.items()}
    out = {}
    for line in str(v).splitlines():
        m = re.match(r"\s*(.+?)\s*(?:→|->|:|=)\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def jsonable_frame(df: pd.DataFrame) -> pd.DataFrame:
    """dict/list cells → JSON strings so arrow/dlt can type them."""
    for c in df.columns:
        if df[c].dtype == object and df[c].map(lambda v: isinstance(v, (dict, list))).any():
            df[c] = df[c].map(lambda v: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v)
    return df


def read_table_bytes(data: bytes, name: str, fmt: str = "auto", sheet: str | int | None = None,
                     header_row: int = 0, delimiter: str = "") -> pd.DataFrame:
    fmt = (fmt or "auto").lower()
    ext = Path(name).suffix.lower()
    if fmt == "auto":
        fmt = {".csv": "csv", ".txt": "csv", ".tsv": "csv", ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
               ".parquet": "parquet", ".pq": "parquet", ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl"}.get(ext)
        if fmt is None:
            raise ValueError(f"cannot infer the format of {name!r} — choose one")
    if fmt == "csv":
        sep = delimiter or ("\t" if ext == ".tsv" else None)
        return pd.read_csv(io.BytesIO(data), sep=sep, engine="python", header=header_row)
    if fmt == "excel":
        sh: Any = sheet if sheet not in (None, "") else 0
        if isinstance(sh, str) and sh.isdigit():
            sh = int(sh)
        return pd.read_excel(io.BytesIO(data), sheet_name=sh, header=header_row, engine="openpyxl")
    if fmt == "parquet":
        return pd.read_parquet(io.BytesIO(data))
    if fmt == "json":
        obj = json.loads(data)
        if isinstance(obj, dict):
            obj = next((v for v in obj.values() if isinstance(v, list)), [obj])
        return pd.json_normalize(obj)
    if fmt == "jsonl":
        return pd.read_json(io.BytesIO(data), lines=True)
    raise ValueError(f"unknown format {fmt}")


FORMAT = F(key="format", label="Format", type="select", options=["auto", "csv", "excel", "parquet", "json", "jsonl"],
           default="auto")
SHEET = F(key="sheet", label="Excel sheet", type="text", placeholder="first sheet", hint="Name or 0-based index")


def _cap(df: pd.DataFrame, ctx: Ctx) -> pd.DataFrame:
    return df.head(ctx.limit) if ctx.limit else df


# ── sources ──────────────────────────────────────────────────────────────
@node("source.file", "source", "File upload", "upload", "CSV, Excel, Parquet or JSON uploaded to the pipeline",
      [F(key="file", label="File", type="upload", required=True, hint="Stored with the pipeline"), FORMAT, SHEET],
      group="Files")
def src_file(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    p = sub("uploads") / ctx.pipeline_id / Path(cfg["file"]).name
    if not p.is_file():
        raise ValueError(f"uploaded file {cfg['file']!r} not found — upload it again")
    ctx.dataset("file://canvas-uploads", f"{ctx.pipeline_id}/{p.name}")
    return _cap(read_table_bytes(p.read_bytes(), p.name, cfg.get("format", "auto"), cfg.get("sheet")), ctx)


@node("source.lake", "source", "Lake file", "folder", "Files under /data/lake (glob patterns combine files)",
      [F(key="path", label="Path under the lake", type="text", required=True, placeholder="samples/bsp/plans.parquet",
         hint="Relative to /data/lake; * and ** globs allowed"), FORMAT, SHEET],
      group="Files")
def src_lake(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    root = lake_dir().resolve()
    pattern = interpolate(cfg["path"]).lstrip("/")
    files = sorted(p for p in root.glob(pattern) if p.is_file()) if any(c in pattern for c in "*?[") \
        else [root / pattern]
    files = [f for f in files if f.resolve().is_relative_to(root)]
    if not files or not files[0].is_file():
        raise ValueError(f"no lake files match {pattern!r}")
    frames = []
    for f in files:
        df = read_table_bytes(f.read_bytes(), f.name, cfg.get("format", "auto"), cfg.get("sheet"))
        if len(files) > 1:
            df["_source_file"] = f.relative_to(root).as_posix()
        frames.append(df)
        ctx.dataset("file:///data/lake", f.relative_to(root).as_posix())
        if ctx.limit and sum(len(x) for x in frames) >= ctx.limit:
            break
    return _cap(pd.concat(frames, ignore_index=True), ctx)


@node("source.rest", "source", "REST API", "globe", "Any JSON API: auth header from the vault, paging, JSONPath records",
      [F(key="base_url", label="Base URL", type="text", required=True, placeholder="https://api.example.net/v1",
         hint="${CONNECTORS_URL} etc. allowed"),
       F(key="path", label="Path", type="text", placeholder="/items"),
       F(key="params", label="Query parameters (JSON)", type="json", default={}),
       F(key="auth", label="Auth", type="select", options=["none", "bearer", "header", "basic"], default="none"),
       F(key="auth_header", label="Header name", type="text", default="Authorization", show_if={"auth": "header"}),
       F(key="secret_ref", label="Secret (vault reference)", type="secret", placeholder="vault:MY_API_TOKEN",
         show_if={"auth": ["bearer", "header", "basic"]}, hint="basic: secret is user:password"),
       F(key="records_path", label="Records (JSONPath)", type="text", default="$[*]", placeholder="$.data[*]"),
       F(key="pagination", label="Pagination", type="select",
         options=["none", "offset", "page", "cursor", "link_header", "json_link"], default="none"),
       F(key="page_size", label="Page size", type="number", default=100,
         show_if={"pagination": ["offset", "page"]}),
       F(key="limit_param", label="Limit parameter", type="text", default="limit", show_if={"pagination": ["offset", "page"]}),
       F(key="offset_param", label="Offset parameter", type="text", default="offset", show_if={"pagination": "offset"}),
       F(key="page_param", label="Page parameter", type="text", default="page", show_if={"pagination": "page"}),
       F(key="cursor_path", label="Next cursor (JSONPath)", type="text", default="$.next_cursor",
         show_if={"pagination": "cursor"}),
       F(key="cursor_param", label="Cursor parameter", type="text", default="cursor", show_if={"pagination": "cursor"}),
       F(key="next_url_path", label="Next URL (JSONPath)", type="text", default="$.next", show_if={"pagination": "json_link"}),
       F(key="max_pages", label="Max pages", type="number", default=100)],
      group="APIs")
def src_rest(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    import httpx
    from jsonpath_ng.ext import parse as jp

    url = interpolate(cfg["base_url"]).rstrip("/") + ("/" + interpolate(cfg.get("path", "")).lstrip("/")
                                                       if cfg.get("path") else "")
    params = dict(cfg.get("params") or {})
    if isinstance(params, str):
        params = json.loads(params or "{}")
    headers = {"Accept": "application/json"}
    auth = None
    kind = cfg.get("auth", "none")
    if kind != "none":
        sec = secret(cfg.get("secret_ref", ""))
        if kind == "bearer":
            headers["Authorization"] = f"Bearer {sec}"
        elif kind == "header":
            headers[cfg.get("auth_header") or "Authorization"] = sec
        elif kind == "basic":
            u, _, pw = sec.partition(":")
            auth = (u, pw)
    rec = jp(cfg.get("records_path") or "$[*]")
    mode = cfg.get("pagination", "none")
    size = int(cfg.get("page_size") or 100)
    max_pages = int(cfg.get("max_pages") or 100)
    rows: list = []
    page, offset, next_url = 1, 0, None
    with httpx.Client(timeout=60, follow_redirects=True) as c:
        for i in range(max_pages):
            q = dict(params)
            if mode == "offset":
                q.update({cfg.get("limit_param") or "limit": size, cfg.get("offset_param") or "offset": offset})
            elif mode == "page":
                q.update({cfg.get("limit_param") or "limit": size, cfg.get("page_param") or "page": page})
            if next_url:
                r = c.get(next_url, headers=headers, auth=auth)
            else:
                r = c.get(url, params=q, headers=headers, auth=auth)
            r.raise_for_status()
            body = r.json()
            batch = [m.value for m in rec.find(body)]
            rows.extend(batch)
            ctx.log(f"  page {i + 1}: {len(batch)} record(s)")
            if ctx.limit and len(rows) >= ctx.limit:
                break
            if mode in ("offset", "page"):
                if len(batch) < size:
                    break
                offset += size
                page += 1
            elif mode == "cursor":
                cur = [m.value for m in jp(cfg.get("cursor_path") or "$.next_cursor").find(body)]
                if not cur or cur[0] in (None, ""):
                    break
                params[cfg.get("cursor_param") or "cursor"] = cur[0]
            elif mode == "link_header":
                nxt = r.links.get("next", {}).get("url")
                if not nxt:
                    break
                next_url = str(r.url.join(nxt))
            elif mode == "json_link":
                nxt = [m.value for m in jp(cfg.get("next_url_path") or "$.next").find(body)]
                if not nxt or not nxt[0]:
                    break
                next_url = str(r.url.join(nxt[0]))
            else:
                break
    ctx.dataset(re.sub(r"^(https?://[^/]+).*$", r"\1", url), re.sub(r"^https?://[^/]+", "", url) or "/")
    df = pd.json_normalize(rows, max_level=1) if rows and isinstance(rows[0], dict) else pd.DataFrame({"value": rows})
    return _cap(df, ctx)


@node("source.sql", "source", "SQL database", "database", "Postgres, SQL Server or MySQL via a SQLAlchemy URL",
      [F(key="url", label="Connection", type="text", required=True, default="warehouse",
         hint="'warehouse' = the local bsp_edw; else e.g. mssql+pymssql://user@host/db, mysql+pymysql://user@host/db"),
       F(key="password_ref", label="Password (vault reference)", type="secret", placeholder="vault:SALES_DB_PASSWORD"),
       F(key="table", label="Table", type="text", placeholder="raw.service_tickets"),
       F(key="query", label="…or SQL query", type="code", placeholder="select * from raw.invoices where status = 'open'"),
       F(key="incremental_column", label="Incremental column (pushed down)", type="text",
         hint="With an incremental destination: only rows above the saved high-water mark are read")],
      group="Databases")
def src_sql(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    url = cfg.get("url") or "warehouse"
    if url == "warehouse":
        from bsp_common import settings
        u = make_url(settings.get().pg_url)
        ns = f"postgres://{u.host}:{u.port or 5432}"
    else:
        u = make_url(interpolate(url))
        if cfg.get("password_ref"):
            u = u.set(password=secret(cfg["password_ref"]))
        ns = f"{u.get_backend_name()}://{u.host}:{u.port or ''}".rstrip(":")
    if cfg.get("table"):
        parts = [p.strip('"') for p in cfg["table"].split(".")]
        base = "select * from " + ".".join(f'"{p}"' if u.get_backend_name() == "postgresql" else p for p in parts)
        ctx.dataset(ns, f"{u.database}.{cfg['table']}")
    elif cfg.get("query"):
        base = cfg["query"].strip().rstrip(";")
        ctx.dataset(ns, f"{u.database}.query")
    else:
        raise ValueError("set a table or a query")
    params: dict[str, Any] = {}
    col = (cfg.get("incremental_column") or "").strip()
    hw = (ctx.state.get("sql_high_water") or {}).get(col) if col else None
    if col and hw is not None and not ctx.limit:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", col):
            raise ValueError("incremental column must be a plain column name")
        base = f"select * from ({base}) _t where {col} > :hw"
        params["hw"] = hw
        ctx.log(f"  incremental: {col} > {hw}")
    eng = create_engine(u, pool_pre_ping=True)
    try:
        with eng.connect() as conn:
            if ctx.limit:
                it = pd.read_sql_query(text(base), conn, params=params, chunksize=ctx.limit)
                df = next(iter(it), pd.DataFrame())
            else:
                df = pd.read_sql_query(text(base), conn, params=params)
    finally:
        eng.dispose()
    if col and col in df.columns and len(df) and not ctx.limit:
        ctx.pending.setdefault("sql_high_water", {})[col] = _jsonable(df[col].max())
    return df


def _jsonable(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    if hasattr(v, "item"):
        return v.item()
    return v


@node("source.bigquery", "source", "BigQuery", "database", "A BigQuery table or query (mock: local DuckDB)",
      [F(key="table", label="Table", type="text", placeholder="raw.customers", hint="dataset.table"),
       F(key="query", label="…or SQL (BigQuery dialect)", type="code",
         placeholder="select state, count(*) n from `to-be-named-soon.raw.customers` group by 1"),
       F(key="max_rows", label="Max rows", type="number", default=100000)],
      group="Enterprise")
def src_bigquery(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from bsp_connectors import bigquery
    proj = bigquery.project()
    if cfg.get("table"):
        sql = f"select * from `{proj}.{cfg['table']}`"
        ctx.dataset(f"bigquery://{proj}", cfg["table"])
    elif cfg.get("query"):
        sql = cfg["query"]
        ctx.dataset(f"bigquery://{proj}", "query")
    else:
        raise ValueError("set a table or a query")
    n = ctx.limit or int(cfg.get("max_rows") or 100000)
    return pd.DataFrame(bigquery.query(sql, max_rows=n))


@node("source.sharepoint", "source", "SharePoint", "document", "A file (CSV/Excel/…) or a list from a SharePoint site",
      [F(key="mode", label="Read", type="select", options=["file", "list"], default="file"),
       F(key="site", label="Site", type="text", placeholder="default (SHAREPOINT_SITE)",
         hint="Blank = SHAREPOINT_SITE; or contoso.sharepoint.com:/sites/Finance"),
       F(key="folder", label="Folder", type="text", placeholder="Finance/Reports", show_if={"mode": "file"}),
       F(key="pattern", label="File name or pattern", type="text", placeholder="*.xlsx", show_if={"mode": "file"},
         hint="Several matches are combined (with _source_file)"),
       dict(FORMAT, show_if={"mode": "file"}), dict(SHEET, show_if={"mode": "file"}),
       F(key="list_name", label="List", type="text", placeholder="Network Projects", show_if={"mode": "list"})],
      group="Enterprise")
def src_sharepoint(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from bsp_connectors import m365
    site = interpolate(cfg.get("site") or "") or None
    sname = site or "default"
    if cfg.get("mode") == "list":
        rows = m365.list_items(site, cfg["list_name"])
        ctx.dataset(f"sharepoint://{sname}", f"lists/{cfg['list_name']}")
        return _cap(pd.DataFrame([r["fields"] for r in rows]), ctx)
    folder = interpolate(cfg.get("folder") or "")
    pattern = cfg.get("pattern") or "*"
    files = [f for f in m365.list_files(site, folder) if fnmatch.fnmatch(f["name"].lower(), pattern.lower())]
    if not files:
        raise ValueError(f"no files matching {pattern!r} in {folder or '/'}")
    frames = []
    for f in sorted(files, key=lambda f: f["name"]):
        ctx.log(f"  reading {f['name']} ({f.get('size', 0)} bytes)")
        df = read_table_bytes(m365.download_file(site, f["id"]), f["name"], cfg.get("format", "auto"), cfg.get("sheet"))
        if len(files) > 1:
            df["_source_file"] = f["name"]
        frames.append(df)
        ctx.dataset(f"sharepoint://{sname}", f"{folder}/{f['name']}".lstrip("/"))
    return _cap(pd.concat(frames, ignore_index=True), ctx)


@node("source.outlook", "source", "Outlook mailbox", "notes", "Messages as rows (metadata; body optional)",
      [F(key="mailbox", label="Mailbox", type="text", required=True, default="${DISPUTES_MAILBOX}"),
       F(key="folder", label="Folder", type="text", default="inbox"),
       F(key="since", label="Received since", type="text", placeholder="2026-10-01"),
       F(key="unread_only", label="Unread only", type="bool", default=False),
       F(key="top", label="Max messages", type="number", default=500),
       F(key="include_preview", label="Include body preview", type="bool", default=False),
       F(key="include_body", label="Include full body", type="bool", default=False,
         hint="Off = metadata only (recommended for landing)")],
      group="Enterprise")
def src_outlook(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from bsp_connectors import m365
    mb = interpolate(cfg["mailbox"])
    msgs = m365.list_messages(mb, folder=cfg.get("folder") or "inbox", since=cfg.get("since") or None,
                              unread_only=bool(cfg.get("unread_only")), top=ctx.limit or int(cfg.get("top") or 500))
    rows = []
    for m in msgs:
        r = {"message_id": m["id"], "subject": m.get("subject"), "from_name": (m.get("from") or {}).get("name"),
             "from_address": (m.get("from") or {}).get("address"), "received_at": m.get("receivedDateTime"),
             "has_attachments": bool(m.get("hasAttachments")), "is_read": bool(m.get("isRead")),
             "conversation_id": m.get("conversationId"), "importance": m.get("importance"),
             "folder": cfg.get("folder") or "inbox"}
        if cfg.get("include_preview"):
            r["body_preview"] = m.get("bodyPreview")
        if cfg.get("include_body"):
            r["body"] = (m.get("body") or {}).get("content")
        rows.append(r)
    ctx.dataset("outlook://m365", f"{mb}/{cfg.get('folder') or 'inbox'}")
    df = pd.DataFrame(rows, columns=list(rows[0]) if rows else ["message_id", "subject", "from_name", "from_address",
                                                                   "received_at", "has_attachments", "is_read",
                                                                   "conversation_id", "importance", "folder"])
    if len(df):
        df["received_at"] = pd.to_datetime(df["received_at"], utc=True)
    return df


@node("source.atlan", "source", "Atlan assets", "search", "Catalog assets: tables, columns, glossary terms",
      [F(key="query", label="Search", type="text", placeholder="churn"),
       F(key="type_names", label="Asset types", type="list", placeholder="Table, View, AtlasGlossaryTerm"),
       F(key="limit", label="Max assets", type="number", default=200)],
      group="Enterprise")
def src_atlan(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from bsp_connectors import atlan
    hits = atlan.search_assets(cfg.get("query") or "", type_names=as_list(cfg.get("type_names")) or None,
                               limit=ctx.limit or int(cfg.get("limit") or 200))
    rows = []
    for a in hits:
        at = a.get("attributes") or {}
        rows.append({"qualified_name": a.get("qualifiedName"), "type_name": a.get("typeName"), "name": a.get("name"),
                     "description": at.get("description") or at.get("userDescription"),
                     "certificate": at.get("certificateStatus"),
                     "owners": ", ".join((at.get("ownerUsers") or []) + (at.get("ownerGroups") or [])),
                     "classifications": ", ".join(a.get("classificationNames") or []),
                     "terms": ", ".join(m.get("displayText", "") for m in a.get("meanings") or []),
                     "guid": a.get("guid")})
    ctx.dataset("atlan://catalog", f"search:{cfg.get('query') or '*'}")
    return pd.DataFrame(rows)


@node("source.powerbi", "source", "Power BI dataset", "dashboard", "Run a DAX query against a dataset",
      [F(key="workspace", label="Workspace", type="text", required=True, placeholder="Revenue & Churn"),
       F(key="dataset", label="Dataset", type="text", required=True, placeholder="Revenue Model"),
       F(key="dax", label="DAX query", type="code", required=True,
         placeholder="EVALUATE SUMMARIZECOLUMNS('Revenue'[state], \"Revenue\", [Total Revenue])"),
       F(key="clean_names", label="Clean column names", type="bool", default=True, hint="Revenue[state] → state")],
      group="Enterprise")
def src_powerbi(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from bsp_connectors import powerbi
    rows = powerbi.execute_dax(cfg["workspace"], cfg["dataset"], cfg["dax"])
    df = pd.DataFrame(rows)
    if cfg.get("clean_names", True):
        df.columns = [re.sub(r"^.*\[(.+)\]$", r"\1", c) for c in df.columns]
    ctx.dataset("powerbi://dataplatform", f"{cfg['workspace']}/{cfg['dataset']}")
    return _cap(df, ctx)


# ── transforms ───────────────────────────────────────────────────────────
@node("transform.select", "transform", "Select / rename", "filter", "Keep, order and rename columns",
      [F(key="columns", label="Keep columns", type="columns", hint="Blank = all"),
       F(key="rename", label="Rename (old → new, one per line)", type="map")])
def t_select(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"]
    cols = as_list(cfg.get("columns"))
    if cols:
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise ValueError(f"unknown column(s): {', '.join(missing)}")
        df = df[cols]
    return df.rename(columns=as_map(cfg.get("rename")))


@node("transform.filter", "transform", "Filter rows", "filter", "Keep rows where an expression is true",
      [F(key="expression", label="Keep rows where", type="code", required=True,
         placeholder="status == 'open' and amount_due > 100")])
def t_filter(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"]
    mask = X.evaluate(df, cfg["expression"])
    if not isinstance(mask, pd.Series):
        mask = pd.Series([bool(mask)] * len(df), index=df.index)
    return df[mask.fillna(False).astype(bool)].reset_index(drop=True)


CAST_TYPES = ["string", "integer", "float", "boolean", "date", "datetime", "json"]


@node("transform.cast", "transform", "Cast types", "settings", "Set column types (string, integer, float, boolean, date, datetime)",
      [F(key="types", label="Column → type (one per line)", type="map", required=True,
         hint="e.g. amount: float · opened_at: datetime"),
       F(key="errors", label="On bad values", type="select", options=["null", "fail"], default="null")])
def t_cast(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"].copy()
    errors = "coerce" if cfg.get("errors", "null") == "null" else "raise"
    for col, t in as_map(cfg.get("types")).items():
        if col not in df.columns:
            raise ValueError(f"unknown column {col!r}")
        t = t.lower()
        if t in ("int", "integer", "bigint"):
            df[col] = pd.to_numeric(df[col], errors=errors).astype("Int64")
        elif t in ("float", "double", "number", "numeric"):
            df[col] = pd.to_numeric(df[col], errors=errors).astype("float64")
        elif t in ("bool", "boolean"):
            m = {"true": True, "t": True, "1": True, "yes": True, "y": True,
                 "false": False, "f": False, "0": False, "no": False, "n": False}
            df[col] = df[col].map(lambda v: v if isinstance(v, bool) or v is None else m.get(str(v).strip().lower()))\
                .astype("boolean")
        elif t == "date":
            df[col] = pd.to_datetime(df[col], errors=errors).dt.date
        elif t in ("datetime", "timestamp"):
            df[col] = pd.to_datetime(df[col], errors=errors, utc=True)
        elif t in ("string", "text", "str"):
            df[col] = df[col].astype("string")
        elif t == "json":
            df[col] = df[col].map(lambda v: json.dumps(v, default=str) if not isinstance(v, str) else v)
        else:
            raise ValueError(f"unknown type {t!r} (use {', '.join(CAST_TYPES)})")
    return df


@node("transform.dedupe", "transform", "Deduplicate", "copy", "Drop duplicate rows by key columns",
      [F(key="keys", label="Key columns", type="columns", hint="Blank = whole row"),
       F(key="order_by", label="Order by (before dedupe)", type="text", placeholder="received_at"),
       F(key="keep", label="Keep", type="select", options=["last", "first"], default="last")])
def t_dedupe(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"]
    if cfg.get("order_by"):
        df = df.sort_values(cfg["order_by"], kind="stable")
    keys = as_list(cfg.get("keys")) or None
    before = len(df)
    df = df.drop_duplicates(subset=keys, keep=cfg.get("keep", "last")).reset_index(drop=True)
    ctx.log(f"  dedupe removed {before - len(df)} row(s)")
    return df


@node("transform.derive", "transform", "Derive column", "add", "Add or replace a column from a safe expression",
      [F(key="column", label="New column", type="text", required=True, placeholder="arpu"),
       F(key="expression", label="Expression", type="code", required=True, placeholder="round(revenue / subscribers, 2)",
         hint="Functions: lower, upper, round, coalesce, if_, regex_extract, concat, to_date, year, month, …")])
def t_derive(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"].copy()
    df[cfg["column"]] = X.evaluate(df, cfg["expression"])
    return df


def _salt(cfg: dict) -> str:
    if cfg.get("salt_ref"):
        return secret(cfg["salt_ref"])
    import os
    return os.environ.get("CANVAS_PII_SALT", "bsp-canvas")


def _mask(v: Any) -> Any:
    if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
        return v
    s = str(v)
    if "@" in s:
        user, _, dom = s.partition("@")
        return (user[:1] + "***@" + dom) if user else "***@" + dom
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 7:
        return "***-" + digits[-4:]
    return s[:1] + "*" * max(len(s) - 2, 1) + (s[-1:] if len(s) > 2 else "")


@node("transform.pii", "transform", "PII mask / hash", "shield", "Hash, mask, redact, truncate or drop personal data",
      [F(key="columns", label="Columns", type="columns", required=True),
       F(key="method", label="Method", type="select", options=["hash", "mask", "redact", "truncate", "drop"], default="hash",
         hint="hash = salted SHA-256 (joinable) · mask = j***@example.net / ***-1234"),
       F(key="keep_chars", label="Keep first N characters", type="number", default=3, show_if={"method": "truncate"}),
       F(key="salt_ref", label="Hash salt (vault reference)", type="secret", placeholder="vault:CANVAS_PII_SALT",
         show_if={"method": "hash"})])
def t_pii(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"].copy()
    method = cfg.get("method", "hash")
    cols = as_list(cfg.get("columns"))
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"unknown column(s): {', '.join(missing)}")
    if method == "drop":
        return df.drop(columns=cols)
    salt = _salt(cfg) if method == "hash" else ""
    for c in cols:
        s = df[c]
        if method == "hash":
            df[c] = s.map(lambda v: None if pd.isna(v) else hashlib.sha256(f"{salt}|{v}".encode()).hexdigest()[:32])
        elif method == "mask":
            df[c] = s.map(_mask)
        elif method == "redact":
            df[c] = s.map(lambda v: None if pd.isna(v) else "[REDACTED]")
        elif method == "truncate":
            n = int(cfg.get("keep_chars") or 3)
            df[c] = s.map(lambda v: None if pd.isna(v) else str(v)[:n])
    ctx.log(f"  {method}: {', '.join(cols)}")
    return df


@node("transform.join", "transform", "Lookup join", "stacks", "Enrich rows from a second input or a warehouse table",
      [F(key="join_on", label="Join keys", type="columns", required=True, placeholder="ban"),
       F(key="right_on", label="Lookup keys (if named differently)", type="list"),
       F(key="lookup_table", label="Lookup table (if no lookup input)", type="text", placeholder="raw.customers",
         hint="Warehouse schema.table; ignored when a node is wired to the lookup port"),
       F(key="columns", label="Lookup columns to add", type="list", hint="Blank = all"),
       F(key="how", label="Join", type="select", options=["left", "inner"], default="left")],
      ports=["in", "lookup"])
def t_join(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    left = inputs["in"]
    on = as_list(cfg.get("join_on") or cfg.get("on"))  # (YAML 1.1 reads a bare `on:` key as true)
    right_on = as_list(cfg.get("right_on")) or on
    want = as_list(cfg.get("columns"))
    right = inputs.get("lookup")
    if right is None:
        tbl = (cfg.get("lookup_table") or "").strip()
        if not re.fullmatch(r"[A-Za-z_][\w]*\.[A-Za-z_][\w]*", tbl):
            raise ValueError("wire a node to the lookup port or set a lookup table (schema.table)")
        from bsp_common import db
        cols = ", ".join(f'"{c}"' for c in dict.fromkeys(right_on + want)) if want else "*"
        s, t = tbl.split(".")
        right = db.query_df(f'select {cols} from "{s}"."{t}"')
        from bsp_common import settings
        ctx.dataset("postgres://" + settings.env("PG_HOST", "postgres") + ":5432", f"bsp_edw.{tbl}")
    if want:
        right = right[list(dict.fromkeys(right_on + want))]
    right = right.drop_duplicates(subset=right_on)
    out = left.merge(right, how=cfg.get("how", "left"), left_on=on, right_on=right_on, suffixes=("", "_lookup"))
    if right_on != on:
        out = out.drop(columns=[c for c in right_on if c not in on and c in out.columns])
    ctx.log(f"  join: {len(left)} → {len(out)} row(s)")
    return out


@node("transform.flatten", "transform", "Flatten JSON", "stacks", "Expand nested JSON/dict columns into columns",
      [F(key="columns", label="Columns", type="columns", hint="Blank = every dict/JSON column"),
       F(key="sep", label="Separator", type="text", default="_"),
       F(key="explode", label="Explode list columns into rows", type="bool", default=False)])
def t_flatten(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    df = inputs["in"].copy()
    sep = cfg.get("sep") or "_"

    def parse(v: Any) -> Any:
        if isinstance(v, str) and v[:1] in "[{":
            try:
                return json.loads(v)
            except ValueError:
                return v
        return v
    cols = as_list(cfg.get("columns")) or [c for c in df.columns if df[c].map(lambda v: isinstance(parse(v), (dict, list))).any()]
    for c in cols:
        df[c] = df[c].map(parse)
        if cfg.get("explode") and df[c].map(lambda v: isinstance(v, list)).any():
            df = df.explode(c, ignore_index=True)
        if df[c].map(lambda v: isinstance(v, dict)).any():
            flat = pd.json_normalize(df[c].map(lambda v: v if isinstance(v, dict) else {}).tolist(), sep=sep)
            flat.columns = [f"{c}{sep}{x}" for x in flat.columns]
            df = pd.concat([df.drop(columns=[c]).reset_index(drop=True), flat.reset_index(drop=True)], axis=1)
    return df


# ── destinations (loaded by engine.load) ─────────────────────────────────
SYNC = F(key="mode", label="Sync mode", type="select", options=["full_refresh", "append", "incremental", "merge"],
         default="full_refresh",
         hint="full refresh = replace · incremental = append rows above the cursor's high-water mark · merge = upsert on key")
CURSOR = F(key="cursor_column", label="Cursor column", type="text", show_if={"mode": "incremental"}, placeholder="received_at")
PK = F(key="primary_key", label="Primary key", type="columns", show_if={"mode": "merge"}, placeholder="message_id")
TABLE = F(key="table", label="Table", type="text", required=True, placeholder="finance_monthly")


@node("dest.warehouse", "destination", "Warehouse (Postgres)", "database", "Load into the local warehouse (schema landing)",
      [F(key="schema", label="Schema", type="text", default="landing"), TABLE, SYNC, CURSOR, PK], group="Destinations")
def d_warehouse(ctx, cfg, inputs):
    return inputs["in"]


@node("dest.bigquery", "destination", "BigQuery dataset", "cloud", "Load into BigQuery (mock: Postgres schema of the same name)",
      [F(key="dataset", label="Dataset", type="text", default="landing"), TABLE, SYNC, CURSOR, PK], group="Destinations")
def d_bigquery(ctx, cfg, inputs):
    return inputs["in"]


@node("dest.lake", "destination", "Lake (Parquet / CSV)", "folder", "Write files under /data/lake",
      [F(key="folder", label="Folder under the lake", type="text", default="landing/canvas"), TABLE,
       F(key="format", label="File format", type="select", options=["parquet", "csv", "jsonl"], default="parquet"),
       F(key="partition_by", label="Partition by", type="text", placeholder="load_date or a column"),
       dict(SYNC, options=["full_refresh", "append", "incremental"]), CURSOR], group="Destinations")
def d_lake(ctx, cfg, inputs):
    return inputs["in"]


# ── first-class connectors: databases & object storage (reusable Connections) ──
DB_CONN = F(key="connection", label="Connection", type="connection", kinds=["database"], required=True, default="warehouse",
            hint="Manage under Connections (secrets are vault references)")
ST_CONN = F(key="connection", label="Connection", type="connection", kinds=["storage"], required=True)


@node("source.database", "source", "Database table", "database",
      "PostgreSQL · Oracle · SQL Server / Azure SQL · MySQL · BigQuery — table picker, incremental cursor, chunked",
      [DB_CONN,
       F(key="schema", label="Schema", type="db_schema", placeholder="dbo / public / SALES"),
       F(key="table", label="Table", type="db_table", hint="Pick from the schema browser"),
       F(key="columns", label="Columns", type="db_columns", hint="Blank = all columns"),
       F(key="query", label="…or SQL query (query as source)", type="code", placeholder="select * from dbo.orders where region = 'OH'"),
       F(key="cursor_column", label="Incremental cursor", type="text", placeholder="updated_at · rowversion · id",
         hint="Blank = full extract each run; else only rows above the saved high-water mark"),
       F(key="initial_value", label="Initial cursor value", type="text", show_if_set="cursor_column",
         placeholder="2026-01-01"),
       F(key="chunk_size", label="Chunk size (rows)", type="number", default=50000),
       F(key="backend", label="Extraction backend", type="select", options=["sqlalchemy", "pyarrow", "connectorx"],
         default="pyarrow", hint="dlt sql_database backend (connectorx: fastest, Postgres/MySQL/MSSQL)")],
      group="Databases")
def src_database(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from . import connections as C
    conn = C.get(cfg.get("connection") or "warehouse")
    schema = (cfg.get("schema") or "").strip() or None
    table = (cfg.get("table") or "").strip()
    query = (cfg.get("query") or "").strip().rstrip(";")
    cols = as_list(cfg.get("columns"))
    cursor = (cfg.get("cursor_column") or "").strip()
    hw = ctx.state.get("cursor", cfg.get("initial_value") or None) if cursor else None
    ns = C.namespace(conn)
    dbname = (conn.get("params") or {}).get("database") or (conn.get("params") or {}).get("service_name") or "bsp_edw"
    if conn["type"] == "bigquery":
        from bsp_connectors import bigquery
        if not (table or query):
            raise ValueError("pick a table or write a query")
        with C._bq_identity(conn):          # the connection's "Acting as" identity (assigned default when unset)
            proj = bigquery.project()
            sel = ", ".join(f"`{c}`" for c in cols) if cols else "*"
            sql = query or f"select {sel} from `{schema if '.' in (schema or '') else proj + '.' + str(schema)}.{table}`"
            if cursor and hw is not None and not ctx.limit:
                sql = f"select * from ({sql}) where `{cursor}` > '{str(hw).replace(chr(39), '')}'"
            ctx.dataset("bigquery", f"{proj}.{schema}.{table or 'query'}")
            if (conn.get("params") or {}).get("identity"):
                ctx.log(f"  BigQuery: acting as {C.acting_label(conn)}")
            df = pd.DataFrame(bigquery.query(sql, max_rows=ctx.limit or 0))
    elif query or ctx.limit:
        df = _sql_read(conn, schema, table, query, cols, cursor, hw, ctx)
        ctx.dataset(ns, f"{dbname}.{schema + '.' if schema else ''}{table or 'query'}")
    else:
        df = _dlt_sql_table(conn, schema, table, cols, cursor, hw, cfg, ctx)
        ctx.dataset(ns, f"{dbname}.{schema + '.' if schema else ''}{table}")
    if cursor and not ctx.limit:
        real = next((c for c in df.columns if c.lower() == cursor.lower()), None)
        if real is None and len(df):
            raise ValueError(f"cursor column {cursor!r} not in the result")
        if real is not None and hw is not None and len(df):
            from .engine import _cmp_series
            df = df[_cmp_series(df[real], hw).fillna(False)].reset_index(drop=True)
        if real is not None and len(df):
            ctx.pending["cursor"] = _jsonable(df[real].max())
            ctx.log(f"  incremental: {len(df):,} new row(s); high-water mark → {ctx.pending['cursor']}")
        elif hw is not None:
            ctx.log(f"  incremental: no rows above {hw}")
    return df


def _sql_read(conn, schema, table, query, cols, cursor, hw, ctx) -> pd.DataFrame:
    from sqlalchemy import text

    from . import connections as C
    eng = C.engine(conn)
    try:
        prep = eng.dialect.identifier_preparer
        if query:
            base = query
        elif table:
            sel = ", ".join(prep.quote(c) for c in cols) if cols else "*"
            base = f"select {sel} from {(prep.quote_schema(schema) + '.') if schema else ''}{prep.quote(table)}"
        else:
            raise ValueError("pick a table or write a query")
        params = {}
        if cursor and hw is not None and not ctx.limit:
            base = f"select * from ({base}) bsp_q where {prep.quote(cursor)} > :hw"
            params["hw"] = hw
        with eng.connect() as c:
            if ctx.limit:
                it = pd.read_sql_query(text(base), c, params=params, chunksize=ctx.limit)
                return next(iter(it), pd.DataFrame())
            chunks = list(pd.read_sql_query(text(base), c, params=params, chunksize=50000))
            return pd.concat(chunks, ignore_index=True) if chunks else pd.DataFrame()
    finally:
        eng.dispose()


def _dlt_sql_table(conn, schema, table, cols, cursor, hw, cfg, ctx) -> pd.DataFrame:
    """Chunked extraction with dlt's sql_database source (sql_table resource)."""
    from dlt.sources.sql_database import sql_table

    from . import connections as C
    if not table:
        raise ValueError("pick a table or write a query")
    backend = cfg.get("backend") or "pyarrow"
    eng = C.engine(conn)
    kw: dict[str, Any] = {"credentials": eng, "table": table, "schema": schema, "chunk_size": int(cfg.get("chunk_size") or 50000),
                          "backend": backend, "reflection_level": "full_with_precision" if backend != "sqlalchemy" else "full"}
    if cols:
        kw["included_columns"] = cols
    if cursor and hw is not None:
        import dlt
        kw["incremental"] = dlt.sources.incremental(cursor, initial_value=_coerce_hw(hw))
    if backend == "connectorx":
        kw.pop("credentials")
        kw["credentials"] = C.sqlalchemy_url(conn).render_as_string(hide_password=False)
    try:
        frames, n = [], 0
        for chunk in sql_table(**kw):
            df = chunk.to_pandas() if hasattr(chunk, "to_pandas") else pd.DataFrame(chunk if isinstance(chunk, list) else [chunk])
            frames.append(df)
            n += len(df)
            ctx.log(f"  chunk {len(frames)}: {len(df):,} row(s) ({backend})")
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    finally:
        eng.dispose()


def _coerce_hw(hw: Any) -> Any:
    if isinstance(hw, str):
        try:
            from datetime import datetime
            d = datetime.fromisoformat(hw.replace("Z", "+00:00"))
            return d
        except ValueError:
            pass
        if re.fullmatch(r"-?\d+", hw):
            return int(hw)
    return hw


STORAGE_FORMAT = F(key="format", label="Format", type="select", options=["auto", "csv", "jsonl", "parquet", "excel", "json"],
                   default="auto", hint="auto by extension; .gz is decompressed")


@node("source.storage", "source", "Object storage", "cloud",
      "Google Cloud Storage · S3 / MinIO · Azure Blob / ADLS — glob, CSV/JSONL/Parquet/Excel (+gz), incremental by modified time",
      [ST_CONN,
       F(key="bucket", label="Bucket / container", type="bucket", required=True),
       F(key="pattern", label="Path or glob", type="storage_glob", default="**/*.parquet", placeholder="landing/2026/*.csv.gz"),
       STORAGE_FORMAT, SHEET,
       F(key="incremental", label="Only files modified since the last run", type="bool", default=False),
       F(key="add_file_column", label="Add _source_file column", type="bool", default=True)],
      group="Object storage")
def src_storage(ctx: Ctx, cfg: dict, inputs: dict) -> pd.DataFrame:
    from . import connections as C
    conn = C.get(cfg["connection"])
    bucket = interpolate(cfg["bucket"]).strip("/")
    pattern = interpolate(cfg.get("pattern") or "**").lstrip("/")
    files = _list_storage_files(conn, bucket, pattern, ctx)
    since = ctx.state.get("modified_since") if cfg.get("incremental") and not ctx.limit else None
    if since:
        files = [f for f in files if (f["modified"] or "") > since]
        ctx.log(f"  incremental: {len(files)} file(s) modified after {since}")
    if not files:
        if since:
            return pd.DataFrame()
        raise ValueError(f"no objects match {bucket}/{pattern}")
    fs = C.filesystem(conn)
    frames, total = [], 0
    for f in files:
        df = C.read_object(fs, f["path"], cfg.get("format", "auto"), cfg.get("sheet"))
        if cfg.get("add_file_column", True):
            df["_source_file"] = f"{C.scheme(conn)}://{f['path']}"
        frames.append(df)
        total += len(df)
        ctx.log(f"  {f['path']}: {len(df):,} row(s)")
        ctx.dataset(f"{C.scheme(conn)}://{bucket}", f["path"].split("/", 1)[1] if "/" in f["path"] else f["path"])
        if ctx.limit and total >= ctx.limit:
            break
    if cfg.get("incremental") and not ctx.limit:
        ctx.pending["modified_since"] = max((f["modified"] or "") for f in files)
    out = pd.concat(frames, ignore_index=True)
    return out.head(ctx.limit) if ctx.limit else out


def _list_storage_files(conn, bucket, pattern, ctx) -> list[dict]:
    """List objects with dlt's filesystem source (fsspec); falls back to a plain fsspec glob."""
    from . import connections as C
    try:
        from dlt.sources.filesystem import filesystem as dlt_fs
        res = dlt_fs(bucket_url=f"{C.scheme(conn)}://{bucket}", credentials=C.filesystem(conn), file_glob=pattern)
        out = []
        for batch in res:
            for item in (batch if isinstance(batch, list) else [batch]):
                url = item["file_url"]
                path = url.split("://", 1)[1]
                md = item.get("modification_date")
                out.append({"path": path, "name": item["file_name"], "size": item.get("size_in_bytes"),
                            "modified": md.isoformat() if hasattr(md, "isoformat") else md})
        return sorted(out, key=lambda f: f["path"])
    except Exception as e:  # noqa: BLE001 — keep listing working on any dlt API drift
        ctx.log(f"  (dlt filesystem listing unavailable: {type(e).__name__}; using fsspec glob)")
        return C.glob_files(conn, bucket, pattern)


@node("dest.storage", "destination", "Object storage", "cloud",
      "Write Parquet / JSONL / CSV to GCS, S3 / MinIO or Azure Blob, optionally partitioned",
      [ST_CONN,
       F(key="bucket", label="Bucket / container", type="bucket", required=True),
       F(key="prefix", label="Prefix (folder)", type="text", default="landing", required=True),
       TABLE,
       F(key="format", label="File format", type="select", options=["parquet", "jsonl", "csv"], default="parquet"),
       F(key="partition_by", label="Partition by", type="text", placeholder="load_date or a column (e.g. bill_cycle)",
         hint="load_date → …/load_date=YYYY-MM-DD/ · a column → hive-style col=value/ folders"),
       dict(SYNC, options=["full_refresh", "append", "incremental"]), CURSOR],
      group="Destinations")
def d_storage(ctx, cfg, inputs):
    return inputs["in"]


@node("dest.postgres", "destination", "PostgreSQL (connection)", "database",
      "Load into any PostgreSQL connection (dlt postgres destination)",
      [dict(DB_CONN, kinds=["postgres"]), F(key="schema", label="Schema", type="text", default="landing"), TABLE,
       SYNC, CURSOR, PK], group="Destinations")
def d_postgres(ctx, cfg, inputs):
    return inputs["in"]


def palette() -> list[dict]:
    return [t.spec() for t in REGISTRY.values()]
