"""BigQuery REST under /connectors/bigquery/ (mock or live, same as the SDK)."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException

from .._common import ConnectorError, NotFound
from . import list_datasets, list_tables, mode, query, table_schema

router = APIRouter(tags=["bigquery"])


@router.get("/datasets")
def datasets() -> dict:
    return {"mode": mode(), "datasets": list_datasets()}


@router.get("/datasets/{dataset}/tables")
def tables(dataset: str) -> dict:
    try:
        return {"tables": list_tables(dataset)}
    except NotFound as e:
        raise HTTPException(404, str(e))


@router.get("/datasets/{dataset}/tables/{table}/schema")
def schema(dataset: str, table: str) -> dict:
    try:
        return {"fields": table_schema(dataset, table)}
    except NotFound as e:
        raise HTTPException(404, str(e))


@router.post("/query", summary="Run SQL (BigQuery dialect; mock runs on DuckDB)")
def run_query(body: dict = Body(..., examples=[{"sql": "select state, count(*) n from `to-be-named-soon.raw.customers` "
                                                       "group by 1 order by 2 desc", "max_rows": 100}])) -> dict:
    sql = (body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(400, "sql is required")
    try:
        rows = query(sql, max_rows=int(body.get("max_rows", 1000)))
    except ConnectorError as e:
        raise HTTPException(e.status or 400, str(e))
    return {"rows": rows, "row_count": len(rows), "mode": mode()}
