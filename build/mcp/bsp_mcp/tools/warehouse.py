"""Local warehouse (Postgres bsp_edw) — read-only SQL with statement timeout + row cap."""
from __future__ import annotations

import os
from typing import Annotated

from pydantic import Field

from ..common import MAX_ROWS, UserError, check_select, clamp, ident, md_table, result, tool

G = "warehouse"
TIMEOUT_MS = int(os.environ.get("MCP_SQL_TIMEOUT_MS", "20000"))
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def connect():
    import psycopg2  # noqa: PLC0415

    if not os.environ.get("PG_PASS") and not os.environ.get("PG_ALLOW_NOPASS"):
        raise UserError("the warehouse password (PG_PASS) is not set for the MCP server — start it from Studio")
    try:
        con = psycopg2.connect(host=os.environ.get("PG_HOST", "postgres"), port=int(os.environ.get("PG_PORT", "5432")),
                               dbname=os.environ.get("PG_DB", "bsp_edw"), user=os.environ.get("PG_USER", "analyst"),
                               password=os.environ.get("PG_PASS", ""), connect_timeout=5, application_name="bsp-mcp",
                               options=f"-c statement_timeout={TIMEOUT_MS} -c default_transaction_read_only=on")
    except psycopg2.OperationalError as e:
        raise UserError(f"warehouse not reachable at {os.environ.get('PG_HOST', 'postgres')}: "
                        f"{str(e).strip().splitlines()[0]}") from None
    con.set_session(readonly=True, autocommit=False)
    return con


def run(sql: str, params=None, limit: int = 200) -> tuple[list[dict], bool]:
    con = connect()
    try:
        with con.cursor() as cur:
            try:
                cur.execute(sql, params)
            except Exception as e:  # noqa: BLE001
                raise UserError(f"SQL error: {str(e).strip().splitlines()[0]}") from None
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(limit + 1)
        con.rollback()
    finally:
        con.close()
    out = [dict(zip(cols, r)) for r in rows[:limit]]
    return out, len(rows) > limit


@tool(G, "Run a read-only SQL query on the warehouse")
def warehouse_query(
    sql: Annotated[str, Field(description="One SELECT / WITH statement (Postgres dialect). Schemas: raw, staging, "
                                          "intermediate, marts, features, mlops, ai")],
    max_rows: Annotated[int, Field(description="Row cap (1–500)")] = 100,
) -> dict:
    """Run one read-only SELECT on the local warehouse (Postgres bsp_edw). One statement can join and aggregate any
    number of warehouse tables. To combine the warehouse with other systems (Oracle billing, SQL Server care,
    BigQuery, the lake) in one query, use trino_query instead. Writes and DDL are rejected; the session is read-only."""
    s = check_select(sql)
    rows, more = run(s, None, clamp(max_rows, 1, MAX_ROWS))
    return result(f"{len(rows)} row(s){' (truncated)' if more else ''}\n\n" + md_table(rows), rows=rows, truncated=more)


@tool(G, "List warehouse schemas and tables")
def warehouse_list_tables(
    schema: Annotated[str | None, Field(description="Only this schema (e.g. 'marts'); omit for all")] = None,
) -> dict:
    """List schemas and their tables/views with approximate row counts."""
    sql = ("SELECT t.table_schema AS schema, t.table_name AS name, t.table_type AS type, "
           "coalesce(c.reltuples, 0)::bigint AS approx_rows FROM information_schema.tables t "
           "LEFT JOIN pg_class c ON c.relname = t.table_name AND c.relnamespace = (SELECT oid FROM pg_namespace "
           "WHERE nspname = t.table_schema) WHERE t.table_schema NOT IN %s")
    params: list = [SYSTEM_SCHEMAS]
    if schema:
        sql += " AND t.table_schema = %s"
        params.append(ident(schema, "schema"))
    rows, _ = run(sql + " ORDER BY 1, 2", tuple(params), 2000)
    schemas = sorted({r["schema"] for r in rows})
    return result(f"{len(rows)} tables in {len(schemas)} schema(s): {', '.join(schemas)}\n\n" + md_table(rows, 40),
                  tables=rows)


@tool(G, "Describe a warehouse table")
def warehouse_describe(
    table: Annotated[str, Field(description="schema.table, e.g. 'raw.customers' (keyed by ban)")],
    sample_rows: Annotated[int, Field(description="Sample rows to include (0–20)")] = 5,
) -> dict:
    """Columns (name, type, nullable, comment) and a few sample rows of one table or view."""
    if "." not in table:
        raise UserError("use schema.table")
    schema, name = (ident(x) for x in table.split(".", 1))
    cols, _ = run("SELECT c.column_name AS name, c.data_type AS type, c.is_nullable AS nullable, "
                  "col_description((quote_ident(c.table_schema)||'.'||quote_ident(c.table_name))::regclass, "
                  "c.ordinal_position) AS comment FROM information_schema.columns c "
                  "WHERE c.table_schema=%s AND c.table_name=%s ORDER BY c.ordinal_position", (schema, name), 500)
    if not cols:
        raise UserError(f"table {table} not found")
    sample, _ = run(f'SELECT * FROM "{schema}"."{name}" LIMIT %s', (clamp(sample_rows, 0, 20),), 20) if sample_rows else ([], False)
    return result(f"{table}: {len(cols)} columns\n\n" + md_table(cols, 60) + ("\n\nSample:\n" + md_table(sample, 5) if sample else ""),
                  columns=cols, sample=sample)


def schema_markdown(max_tables: int = 200) -> str:
    rows, _ = run("SELECT table_schema, table_name, string_agg(column_name || ' ' || data_type, ', ' ORDER BY ordinal_position) "
                  "AS cols FROM information_schema.columns WHERE table_schema NOT IN %s GROUP BY 1,2 ORDER BY 1,2",
                  (SYSTEM_SCHEMAS,), max_tables)
    out = ["# Warehouse schema (Postgres bsp_edw)", ""]
    cur = None
    for r in rows:
        if r["table_schema"] != cur:
            cur = r["table_schema"]
            out += ["", f"## {cur}"]
        out.append(f"- **{r['table_name']}**({r['cols']})")
    return "\n".join(out)
