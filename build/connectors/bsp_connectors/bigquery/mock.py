"""BigQuery mock: DuckDB presenting project GCP_PROJECT_ID (default
`to-be-named-soon`) with datasets backed by local data —

    raw      parquet in $LAKE_DIR/samples/bsp (else Postgres schema raw)
    marts / landing / staging / intermediate / features   Postgres schemas of the same name

Queries accept BigQuery-style table refs (`project.dataset.table`, backticks)
and a few BigQuery functions (SAFE_DIVIDE, COUNTIF, IFNULL, FORMAT_DATE …).
Postgres tables are pulled into DuckDB lazily (only those a query names).
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
import threading
import time
from pathlib import Path
from typing import Any

from .._common import ConnectorError, NotFound, env

PG_DATASETS = ("raw", "landing", "staging", "intermediate", "marts", "features")
TTL = 120.0
_lock = threading.RLock()
_state: dict[str, Any] = {"con": None, "at": 0.0, "loaded": set()}

BQ_TYPES = {"BIGINT": "INT64", "INTEGER": "INT64", "SMALLINT": "INT64", "HUGEINT": "INT64", "TINYINT": "INT64",
            "DOUBLE": "FLOAT64", "FLOAT": "FLOAT64", "REAL": "FLOAT64", "VARCHAR": "STRING", "BOOLEAN": "BOOL",
            "DATE": "DATE", "TIMESTAMP": "TIMESTAMP", "TIMESTAMP WITH TIME ZONE": "TIMESTAMP", "BLOB": "BYTES",
            "TIME": "TIME", "JSON": "JSON", "UUID": "STRING"}
PG_BQ = {"bigint": "INT64", "integer": "INT64", "smallint": "INT64", "double precision": "FLOAT64", "real": "FLOAT64",
         "numeric": "NUMERIC", "text": "STRING", "character varying": "STRING", "boolean": "BOOL", "date": "DATE",
         "timestamp without time zone": "TIMESTAMP", "timestamp with time zone": "TIMESTAMP", "jsonb": "JSON",
         "json": "JSON"}

MACROS = [
    "create or replace macro safe_divide(a, b) as case when b is null or b = 0 then null else a / b end",
    "create or replace macro countif(x) as count_if(x)",
    "create or replace macro format_date(f, d) as strftime(d, f)",
    "create or replace macro format_timestamp(f, d) as strftime(d, f)",
    "create or replace macro parse_date(f, s) as cast(strptime(s, f) as date)",
    "create or replace macro safe_cast_double(x) as try_cast(x as double)",
]


def project() -> str:
    return env("GCP_PROJECT_ID", "to-be-named-soon")


def _parquet_dir() -> Path:
    return Path(env("LAKE_DIR", "/data/lake")) / "samples" / "bsp"


def _pg():
    import psycopg2

    from bsp_common import settings
    return psycopg2.connect(settings.get().pg_dsn, connect_timeout=3)


def _pg_tables() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """{schema: {table: [(column, pg_type)]}} — empty when Postgres is unreachable."""
    if env("BIGQUERY_MOCK_SOURCE") == "parquet":
        return {}
    try:
        conn = _pg()
    except Exception:
        return {}
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""select table_schema, table_name, column_name, data_type from information_schema.columns
                           where table_schema = any(%s) order by 1, 2, ordinal_position""", (list(PG_DATASETS),))
            for s, t, c, d in cur.fetchall():
                out.setdefault(s, {}).setdefault(t, []).append((c, d))
    finally:
        conn.close()
    return out


def _con():
    import duckdb

    with _lock:
        if _state["con"] is None or time.time() - _state["at"] > TTL:
            if _state["con"] is not None:
                try:
                    _state["con"].close()
                except Exception:
                    pass
            con = duckdb.connect(":memory:")
            for m in MACROS:
                try:
                    con.execute(m)
                except Exception:
                    pass
            for ds in PG_DATASETS:
                con.execute(f'create schema if not exists "{ds}"')
            pq = _parquet_dir()
            if pq.is_dir():
                for f in sorted(pq.glob("*.parquet")):
                    con.execute(f"create or replace view raw.\"{f.stem}\" as select * from read_parquet('{f.as_posix()}')")
            _state.update(con=con, at=time.time(), loaded=set(), pg=_pg_tables())
        return _state["con"]


def catalog() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """{dataset: {table: [(column, BQ type)]}}."""
    con = _con()
    out: dict[str, dict[str, list[tuple[str, str]]]] = {}
    for ds, tables in _state["pg"].items():
        for t, cols in tables.items():
            out.setdefault(ds, {})[t] = [(c, PG_BQ.get(d, "STRING")) for c, d in cols]
    rows = con.execute("select table_schema, table_name from information_schema.tables "
                       "where table_schema = 'raw' and table_type = 'VIEW'").fetchall()
    for ds, t in rows:  # parquet-backed raw views win over Postgres raw
        cols = con.execute(f'describe "{ds}"."{t}"').fetchall()
        out.setdefault(ds, {})[t] = [(c[0], BQ_TYPES.get(str(c[1]).upper().split("(")[0], "STRING")) for c in cols]
    return out


_REF = re.compile(r"`([^`]+)`")


def translate(sql: str) -> str:
    """BigQuery SQL → DuckDB: strip the project from table refs, unquote backticks."""
    proj = re.escape(project())

    def bt(m: re.Match) -> str:
        parts = m.group(1).split(".")
        if len(parts) == 3:
            parts = parts[1:]
        return ".".join(f'"{p}"' for p in parts)

    sql = _REF.sub(bt, sql)
    sql = re.sub(rf'"{proj}"\.', "", sql)
    sql = re.sub(rf"\b{proj}\.", "", sql)
    sql = re.sub(r"\bSAFE_CAST\s*\(", "TRY_CAST(", sql, flags=re.I)
    sql = re.sub(r"\bCURRENT_DATE\s*\(\s*\)", "CURRENT_DATE", sql, flags=re.I)
    sql = re.sub(r"\bINT64\b", "BIGINT", sql)
    sql = re.sub(r"\bFLOAT64\b", "DOUBLE", sql)
    sql = re.sub(r"\bSTRING\b(?!\s*\()", "VARCHAR", sql)
    sql = re.sub(r"\bBOOL\b", "BOOLEAN", sql)
    return sql


def _ensure_loaded(sql: str) -> None:
    con = _con()
    pg = _state["pg"]
    raw_views = {r[0] for r in con.execute("select table_name from information_schema.tables "
                                           "where table_schema='raw'").fetchall()}
    wanted = set()
    for m in re.finditer(r'"?\b(' + "|".join(PG_DATASETS) + r')\b"?\s*\.\s*"?([A-Za-z_][A-Za-z0-9_]*)"?', sql):
        ds, t = m.group(1), m.group(2)
        if (ds, t) in _state["loaded"] or (ds == "raw" and t in raw_views):
            continue
        if t in pg.get(ds, {}):
            wanted.add((ds, t))
    if not wanted:
        return
    import pandas as pd
    conn = _pg()
    try:
        for ds, t in wanted:
            df = pd.read_sql_query(f'select * from "{ds}"."{t}"', conn)
            con.register("_tmp_df", df)
            con.execute(f'create or replace table "{ds}"."{t}" as select * from _tmp_df')
            con.unregister("_tmp_df")
            _state["loaded"].add((ds, t))
    finally:
        conn.close()


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    return v


def query(sql: str, max_rows: int = 1000) -> list[dict]:
    q = translate(sql)
    with _lock:
        try:
            _ensure_loaded(q)
            cur = _con().execute(q)
        except Exception as e:  # duckdb errors → one connector error type
            raise ConnectorError(f"query failed (BigQuery mock / DuckDB): {e}", status=400, system="bigquery") from e
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows) if max_rows else cur.fetchall()
    return [dict(zip(cols, (_jsonable(v) for v in r))) for r in rows]


def list_datasets() -> list[dict]:
    return [{"dataset_id": ds, "project": project(), "full_id": f"{project()}:{ds}", "location": env("BQ_LOCATION", "US")}
            for ds in sorted(catalog())]


def list_tables(dataset: str) -> list[dict]:
    cat = catalog()
    if dataset not in cat:
        raise NotFound(f"dataset {project()}:{dataset} not found", status=404, system="bigquery")
    return [{"table_id": t, "dataset_id": dataset, "project": project(), "type": "TABLE",
             "full_id": f"{project()}.{dataset}.{t}"} for t in sorted(cat[dataset])]


def table_schema(dataset: str, table: str) -> list[dict]:
    cols = catalog().get(dataset, {}).get(table)
    if cols is None:
        raise NotFound(f"table {project()}.{dataset}.{table} not found", status=404, system="bigquery")
    return [{"name": c, "type": t, "mode": "NULLABLE", "description": ""} for c, t in cols]


def reset() -> None:
    with _lock:
        _state["con"] = None
