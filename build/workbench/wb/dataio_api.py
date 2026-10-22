"""/ingress/api — Import & Export.

IN    POST /inspect             file → columns, inferred types, first rows
      POST /load                file → landing.<table> (job)
      POST /url                 https URL → lake/landing (job, through the egress proxy)
      GET  /bigquery/datasets · /bigquery/tables?dataset=   browse BigQuery
      POST /bigquery/preview    BigQuery table / query → first rows
      POST /bigquery/load       BigQuery table / query → landing.<table> (job)
           all four take identity= / project= / exec_project= (the "Acting as" picker):
           identity "" = the assigned BigQuery · read identity; a named identity is
           resolved by Studio for the signed-in user (gate header + session cookie)
      GET  /trino/catalogs · /trino/tables?catalog=&schema=  browse Trino
      POST /trino/preview       Trino query → first rows
      POST /trino/load          Trino query → landing.<table> (job)
      GET  /samples, POST /samples/load   synthetic ISP dataset (python -m bsp_seed)
OUT   POST /query               read-only SELECT → first rows
      POST /export              SELECT / table → CSV | Excel | Parquet (job) → download or lake/exports
      GET  /export/{token}      download a finished export
WAREHOUSE  GET /warehouse/schemas · /warehouse/tables · /warehouse/table · DELETE /warehouse/landing/{table}
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import config, dataio, fsroots, samples, tabular
from . import specs as S
from . import warehouse as W
from .files_api import _disposition
from .jobs import MANAGER

router = APIRouter(prefix="/ingress/api")


def _fail(exc: Exception):
    if isinstance(exc, fsroots.PathError):
        raise HTTPException(exc.status, str(exc))
    if isinstance(exc, (tabular.TabularError, S.SpecError)):
        raise HTTPException(422, str(exc))
    if isinstance(exc, W.WarehouseError):
        raise HTTPException(getattr(exc, "status", 400), str(exc))
    raise exc


async def _call(fn, *a, **kw):
    try:
        return await run_in_threadpool(fn, *a, **kw)
    except (fsroots.PathError, tabular.TabularError, S.SpecError, W.WarehouseError) as exc:
        _fail(exc)


def _job(queue: str, kind: str, title: str, make, meta: dict | None = None) -> dict:
    try:
        fn = make()
    except (fsroots.PathError, tabular.TabularError, S.SpecError, W.WarehouseError) as exc:
        _fail(exc)
    return {"job": MANAGER.submit(queue, kind, title, fn, meta).summary()}


class ReadOpts(BaseModel):
    sheet: str | None = Field(default=None, max_length=200)
    header: bool = True
    delimiter: str = "auto"
    encoding: str = "utf-8"
    skip_rows: int = Field(default=0, ge=0, le=10000)


class Inspect(BaseModel):
    root: str
    path: str = Field(max_length=4096)
    options: ReadOpts = Field(default_factory=ReadOpts)
    rows: int = Field(default=50, ge=1, le=500)


class Column(BaseModel):
    name: str = Field(max_length=500)
    target: str = Field(max_length=63)
    type: str = Field(max_length=20)
    include: bool = True


class Load(BaseModel):
    root: str
    path: str = Field(max_length=4096)
    table: str = Field(max_length=63)
    mode: str = "replace"
    columns: list[Column] = Field(default_factory=list, max_length=2000)
    options: ReadOpts = Field(default_factory=ReadOpts)


class Url(BaseModel):
    url: str = Field(max_length=4000)


class Sql(BaseModel):
    sql: str = Field(max_length=200_000)
    limit: int = Field(default=200, ge=1, le=5000)


class BqPrincipal(BaseModel):
    identity: str = Field(default="", max_length=80)      # "" = the assigned identity
    project: str = Field(default="", max_length=64)       # data project
    exec_project: str = Field(default="", max_length=64)  # job / billing project (named identities)


class BqQuery(BqPrincipal):
    sql: str | None = Field(default=None, max_length=200_000)
    dataset: str | None = Field(default=None, max_length=1024)
    source_table: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=5000)


class BqLoad(BqPrincipal):
    sql: str | None = Field(default=None, max_length=200_000)
    dataset: str | None = Field(default=None, max_length=1024)
    source_table: str | None = Field(default=None, max_length=1024)
    table: str = Field(max_length=63)
    mode: str = "replace"


def principal(request: Request, identity: str = "", project: str = "", exec_project: str = "") -> dict:
    """The acting principal for a BigQuery call. A named identity needs the
    signed-in user from the Studio gate (X-Auth-User) AND their Studio
    session cookie, which Studio verifies before it issues a token — a forged
    header from inside the network is not enough."""
    pr = {"identity": (identity or "").strip(), "project": (project or "").strip(),
          "exec_project": (exec_project or "").strip()}
    if pr["identity"]:
        user = (request.headers.get("x-auth-user") or "").strip()
        session = request.cookies.get("studio_session", "")
        if not user or not session:
            raise HTTPException(403, "Acting as a named Google identity needs a signed-in user — open this page "
                                     "through the appliance front door")
        pr |= {"user": user, "session": session}
    return pr


class SqlLoad(BaseModel):
    sql: str = Field(max_length=200_000)
    table: str = Field(max_length=63)
    mode: str = "replace"


class Export(BaseModel):
    sql: str | None = Field(default=None, max_length=200_000)
    schema_name: str | None = Field(default=None, alias="schema", max_length=63)
    table: str | None = Field(default=None, max_length=63)
    format: str = "csv"
    destination: str = "download"
    filename: str | None = Field(default=None, max_length=200)
    overwrite: bool = False

    model_config = {"populate_by_name": True}


class SampleLoad(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    target: str = "warehouse"


@router.get("/health")
async def health():
    return {"ok": True}


@router.get("/status")
async def status():
    wh = await run_in_threadpool(W.status)
    trino = await run_in_threadpool(dataio.trino_status)
    return {"warehouse": wh, "bigquery": dataio.bigquery_status(), "trino": trino,
            "proxy": bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")),
            "lake": str(config.LAKE_DIR), "landing_schema": W.LANDING,
            "limits": {"upload_max_bytes": config.UPLOAD_MAX_BYTES, "export_max_rows": config.EXPORT_MAX_ROWS,
                       "query_timeout_s": config.QUERY_TIMEOUT_S}}


# ── IN ──────────────────────────────────────────────────────────────────
def _inspect(body: Inspect) -> dict:
    p = fsroots.resolve(body.root, body.path)
    if not p.is_file():
        raise fsroots.PathError("choose a file, not a folder")
    o = body.options
    out = tabular.inspect(p, sheet=o.sheet, header=o.header, delimiter=o.delimiter, encoding=o.encoding,
                          skip_rows=o.skip_rows, rows=body.rows)
    out["suggested_table"] = tabular.table_name(p.name)
    try:
        out["table_exists"] = W.table_exists(W.LANDING, out["suggested_table"])
    except Exception:  # noqa: BLE001 — the warehouse may be down; inspection still works
        out["table_exists"] = None
    return out


@router.post("/inspect")
async def inspect(body: Inspect):
    return await _call(_inspect, body)


@router.get("/table-exists")
async def table_exists(table: str):
    try:
        W.check_table_name(table)
    except W.WarehouseError as exc:
        raise HTTPException(422, str(exc))
    return {"exists": await _call(W.table_exists, W.LANDING, table)}


@router.post("/load")
def load(body: Load):
    cols = [c.model_dump() for c in body.columns]
    return _job("ingress", "load", f"Load {os.path.basename(body.path)} → landing.{body.table}",
                lambda: dataio.load_file_job(body.root, body.path, body.table, body.mode, cols, body.options.model_dump()),
                {"direction": "in", "source": f"{body.root}:{body.path}", "target": f"landing.{body.table}",
                 "mode": body.mode})


@router.post("/url")
def fetch_url(body: Url):
    host = ""
    try:
        host = dataio.check_url(body.url).hostname or ""
    except tabular.TabularError as exc:
        raise HTTPException(422, str(exc))
    return _job("ingress", "url", f"Download from {host}", lambda: dataio.fetch_url_job(body.url),
                {"direction": "in", "source": host, "target": "lake:landing"})


@router.get("/bigquery/datasets")
async def bq_datasets(request: Request, identity: str = "", project: str = "", exec_project: str = ""):
    return await _call(dataio.bigquery_datasets, principal(request, identity, project, exec_project))


@router.get("/bigquery/tables")
async def bq_tables(request: Request, dataset: str, identity: str = "", project: str = "", exec_project: str = ""):
    return await _call(dataio.bigquery_tables, dataset, principal(request, identity, project, exec_project))


@router.post("/bigquery/preview")
async def bq_preview(body: BqQuery, request: Request):
    return await _call(dataio.bigquery_preview, body.sql, body.dataset, body.source_table, min(body.limit, 200),
                       principal(request, body.identity, body.project, body.exec_project))


@router.post("/bigquery/load")
def bq_load(body: BqLoad, request: Request):
    pr = principal(request, body.identity, body.project, body.exec_project)
    if not pr["identity"] and not dataio.bigquery_status()["configured"]:
        raise HTTPException(409, "BigQuery isn't connected — add the GCP service-account key in Studio → Credentials")
    src = f"{body.dataset}.{body.source_table}" if body.source_table else "query"
    return _job("ingress", "bigquery", f"BigQuery {src} → landing.{body.table}",
                lambda: dataio.bigquery_job(body.sql, body.dataset, body.source_table, body.table, body.mode, pr),
                {"direction": "in", "source": f"bigquery:{src}", "target": f"landing.{body.table}", "mode": body.mode})


@router.get("/trino/catalogs")
async def trino_catalogs():
    return await _call(dataio.trino_catalogs)


@router.get("/trino/tables")
async def trino_tables(catalog: str, schema: str | None = None):
    return await _call(dataio.trino_tables, catalog, schema)


@router.post("/trino/preview")
async def trino_preview(body: Sql):
    return await _call(dataio.trino_preview, body.sql, min(body.limit, 200))


@router.post("/trino/load")
def trino_load(body: SqlLoad):
    if not dataio.trino_status()["configured"]:
        raise HTTPException(409, "Trino isn't running — start the Trino capability in Studio")
    return _job("ingress", "trino", f"Trino → landing.{body.table}",
                lambda: dataio.trino_job(body.sql, body.table, body.mode),
                {"direction": "in", "source": "trino", "target": f"landing.{body.table}", "mode": body.mode})


@router.get("/samples")
async def samples_list():
    return await run_in_threadpool(samples.list_datasets)


@router.post("/samples/load")
def samples_load(body: SampleLoad):
    label = samples.TARGET_LABELS.get(body.target, body.target)
    return _job("data", "hydrate", f"Sample data {', '.join(body.ids[:2])}{'…' if len(body.ids) > 2 else ''} → {label}",
                lambda: samples.hydrate_job(body.ids, body.target),
                {"direction": "in", "source": "samples", "target": body.target, "ids": body.ids})


# ── OUT ─────────────────────────────────────────────────────────────────
@router.post("/query")
async def query(body: Sql):
    return await _call(W.run_query, body.sql, body.limit)


@router.post("/export")
def export(body: Export):
    sql = body.sql
    if not sql:
        if not (body.schema_name and body.table):
            raise HTTPException(422, "give a query or pick a table")
        sql = W.table_sql(body.schema_name, body.table)
    label = body.table or "query"
    where = "Shared lake › exports" if body.destination == "lake" else "download"
    return _job("ingress", "export", f"Export {label} → {body.format.upper()} ({where})",
                lambda: dataio.export_job(sql, body.format, body.destination, body.filename or body.table,
                                          body.overwrite),
                {"direction": "out", "source": label, "target": where, "format": body.format})


@router.get("/export/{token}")
def export_download(token: str):
    try:
        p, name = dataio.export_file(token)
    except fsroots.PathError as exc:
        raise HTTPException(exc.status, str(exc))
    return FileResponse(p, media_type="application/octet-stream",
                        headers={"Content-Disposition": _disposition(name), "Cache-Control": "no-store"})


# ── warehouse catalogue ────────────────────────────────────────────────
@router.get("/warehouse/schemas")
async def wh_schemas():
    return {"schemas": await _call(W.schemas)}


@router.get("/warehouse/tables")
async def wh_tables(schema: str | None = None):
    return {"tables": await _call(W.tables, schema)}


@router.get("/warehouse/table")
async def wh_table(schema: str, table: str, rows: int = 50):
    return await _call(W.table_detail, schema, table, max(1, min(rows, 500)))


@router.delete("/warehouse/landing/{table}")
async def wh_drop(table: str):
    await _call(W.drop_landing_table, table)
    return {"dropped": f"{W.LANDING}.{table}"}
