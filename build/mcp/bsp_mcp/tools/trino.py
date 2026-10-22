"""Trino: federated read-only SQL across catalogs (warehouse Postgres, Oracle billing, SQL Server care,
BigQuery, lake). One statement can join tables from any number of catalogs."""
from __future__ import annotations

import os
import re
from typing import Annotated

from pydantic import Field

from ..common import MAX_ROWS, UserError, check_select, clamp, ident, md_table, result, tool

G = "trino"


def _run(sql: str, limit: int = 200) -> tuple[list[dict], bool]:
    try:
        import trino  # noqa: PLC0415
    except ImportError:
        raise UserError("the trino client is not installed in this container") from None
    try:
        conn = trino.dbapi.connect(host=os.environ.get("TRINO_HOST", "trino"), port=int(os.environ.get("TRINO_PORT", "8080")),
                                   user=os.environ.get("TRINO_USER", os.environ.get("USER_HANDLE", "analyst")),
                                   http_scheme="http", request_timeout=float(os.environ.get("MCP_TRINO_TIMEOUT", "60")),
                                   session_properties={"query_max_execution_time": os.environ.get("MCP_TRINO_MAX_TIME", "60s")})
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(limit + 1)
        cols = [d[0] for d in cur.description or []]
        cur.cancel() if len(rows) > limit else None
        conn.close()
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001
        msg = str(e).splitlines()[0][:300]
        if "readsessions.create" in str(e):
            raise UserError("BigQuery refused a table read: the shared Trino identity may not create Storage Read "
                            "sessions in the execution project. What still works on bq_* catalogs: metadata "
                            "(information_schema, trino_describe, catalog_describe) and COUNT(*) — with simple WHERE "
                            "filters, which BigQuery evaluates. Rewrite as counts, or say this data is not readable.") from None
        if "Connection" in type(e).__name__ or "connect" in msg.lower():
            raise UserError(f"Trino not reachable at {os.environ.get('TRINO_HOST', 'trino')} — start the Trino capability") from None
        raise UserError(f"Trino error: {msg}") from None
    return [dict(zip(cols, r)) for r in rows[:limit]], len(rows) > limit


@tool(G, "Run a read-only federated query across systems (Trino)")
def trino_query(
    sql: Annotated[str, Field(description="One SELECT/WITH statement (Trino SQL). It may join any number of tables "
                                          "across catalogs; fully qualify them as catalog.schema.table and join on ban")],
    max_rows: Annotated[int, Field(description="Row cap (1–500)")] = 100,
) -> dict:
    """Run one read-only query that joins data across systems without copying it. Catalogs: warehouse (Postgres
    bsp_edw: marts.dim_customer, marts.fct_invoice, ...), billing_oracle (Oracle billing.invoices, billing.transactions),
    care_mssql (SQL Server dbo.care_orders, dbo.care_agents), BigQuery projects as bq_<project> (bq_rpt_enterprise_dv,
    bq_tsa_dv, ...; a single `bigquery` catalog only on single-project setups), lake, tpch. Every local customer table carries ban (varchar).
    Use catalog_search (business terms) or trino_sources first to see what is available. Writes are rejected;
    query time is capped. On BigQuery, aggregate (COUNT(*)) rather than reading rows."""
    s = check_select(sql)
    cap = clamp(max_rows, 1, MAX_ROWS)
    if re.search(r"\bdistinct\b", s, re.I):
        cap = max(cap, 50)    # a value lookup capped at 1 row reads as "only one value exists"
    rows, more = _run(s, cap)
    note = ""
    if more:
        note = f"\n\nTruncated: more than {cap} rows exist; this is not the full result. Aggregate in SQL or raise max_rows."
    if not rows and "'" in s:
        note += ("\n\nNo rows. Before concluding there is no data, check that each text value you filter on really "
                "exists (codes are usually lower case): run SELECT DISTINCT <column> FROM <table> LIMIT 20, or read "
                "the 'codes' column from trino_sources, then rerun with the real values.")
    return result(f"{len(rows)} row(s){' (truncated)' if more else ''}\n\n" + md_table(rows) + note, rows=rows, truncated=more)


@tool(G, "List Trino catalogs, schemas or tables")
def trino_list(
    catalog: Annotated[str | None, Field(description="Catalog; omit to list catalogs")] = None,
    schema: Annotated[str | None, Field(description="Schema within the catalog; omit to list schemas")] = None,
) -> dict:
    """Browse Trino: catalogs → schemas → tables."""
    if not catalog:
        sql = "SHOW CATALOGS"
    elif not schema:
        sql = f'SHOW SCHEMAS FROM "{ident(catalog)}"'
    else:
        sql = f'SHOW TABLES FROM "{ident(catalog)}"."{ident(schema)}"'
    rows, _ = _run(sql, 1000)
    return result(f"{len(rows)} item(s)\n\n" + md_table(rows, 50), items=rows)


@tool(G, "Describe a Trino table")
def trino_describe(table: Annotated[str, Field(description="catalog.schema.table")]) -> dict:
    """Columns and types of a table in any Trino catalog."""
    parts = table.split(".")
    if len(parts) != 3:
        raise UserError("use catalog.schema.table")
    rows, _ = _run("DESCRIBE " + ".".join(f'"{ident(p)}"' for p in parts), 1000)
    return result(f"{table}: {len(rows)} columns\n\n" + md_table(rows, 60), columns=rows)


# catalogs that are slow or remote to crawl column-by-column; listed but not scanned for join keys
_NO_CRAWL = {"system", "tpch", "bigquery"}


def _remote(catalog: str) -> bool:
    """BigQuery catalogs (bigquery, bq_<project>): thousands of tables, billed metadata calls, real data —
    never crawled column-by-column and never value-sampled; the data catalog describes them instead."""
    return catalog in _NO_CRAWL or catalog.startswith("bq_")


_desc_cache: dict[str, tuple[float, dict]] = {}
_up_cache: list[float] = [0.0, 0.0]     # [checked until, up?]


def _catalog_up(dc) -> bool:
    """One cheap reachability probe per minute, so a stopped catalog never slows trino_sources down."""
    import time  # noqa: PLC0415
    if _up_cache[0] < time.time():
        _up_cache[:] = [time.time() + 60, 1.0 if dc.available() else 0.0]
    return bool(_up_cache[1])


def _catalog_tables(catalog: str) -> dict[str, dict]:
    """{schema.table: {description, has_ban, terms}} from the data catalog (DataHub), cached 5 min; {} when absent."""
    import time  # noqa: PLC0415
    hit = _desc_cache.get(catalog)
    if hit and hit[0] > time.time():
        return hit[1]
    out: dict[str, dict] = {}
    try:
        from bsp_common import catalog as dc  # noqa: PLC0415
        if dc.backend() == "datahub" and _catalog_up(dc):
            out = {t["name"]: t for t in dc.tables(catalog)}
    except Exception:  # noqa: BLE001 — enrichment only
        out = {}
    _desc_cache[catalog] = (time.time() + 300, out)
    return out
# low-cardinality code columns whose real values the data map shows (so text-to-SQL filters on real codes)
CODE_COLS = {"status", "technology", "category", "subcategory", "order_type", "segment", "priority", "region",
             "plan_code", "reason", "reason_code", "channel", "disposition", "dispute_reason", "txn_type", "type"}
_CODE_SCHEMAS = {"warehouse": {"marts"}}     # big catalogs: only these schemas get value samples
_codes_cache: dict[str, tuple[float, str]] = {}


def _codes(catalog: str, schema: str, table: str, cols: list[str]) -> str:
    import time  # noqa: PLC0415
    key = f"{catalog}.{schema}.{table}"
    hit = _codes_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    parts = []
    for c in [c for c in cols if c in CODE_COLS][:4]:
        try:
            vals, more = _run(f'SELECT DISTINCT "{ident(c)}" AS v FROM "{ident(catalog)}"."{ident(schema)}"."{ident(table)}" '
                              f'WHERE "{ident(c)}" IS NOT NULL LIMIT 13', 12)
        except UserError:
            continue
        if vals and not more:
            parts.append(f"{c}: " + "|".join(sorted(str(v["v"]) for v in vals)))
    out = "; ".join(parts)
    _codes_cache[key] = (time.time() + 600, out)
    return out


@tool(G, "Map every connected system: catalogs, tables and BAN join keys")
def trino_sources(
    catalog: Annotated[str | None, Field(description="Only this catalog; omit for all")] = None,
) -> dict:
    """The data map for text-to-SQL: each Trino catalog (warehouse, billing_oracle, care_mssql, lake, bq_*, ...),
    its tables with their column names and catalog descriptions, which tables carry the ban join key, and the real
    values of code columns (status, technology, category, ...) for local systems. BigQuery catalogs (bq_*) are too
    big to crawl: pass catalog='bq_...' to list their catalogued tables, or use catalog_search. Call this before
    writing a cross-system query, and use only the column names and code values it lists."""
    cats = [r["Catalog"] for r in _run("SHOW CATALOGS", 100)[0]]
    if catalog:
        if catalog not in cats:
            raise UserError(f"no catalog {catalog!r}; connected: {', '.join(cats)}")
        cats = [catalog]
    out = []
    for c in cats:
        if _remote(c) and not (c in ("system", "tpch") and c == catalog):
            known = _catalog_tables(c) if c not in ("system", "tpch") else {}
            if not known and c == catalog and c not in ("system", "tpch"):
                pass        # asked for explicitly and not catalogued: crawl it below (no value samples)
            elif known and c == catalog:
                for k, t in sorted(known.items()):
                    sch, tab = k.split(".", 1)
                    out.append({"catalog": c, "schema": sch, "table": tab, "has_ban": t.get("has_ban"), "columns": "",
                                "codes": "", "description": (t.get("description") or "")[:160]})
            else:
                hint = (f"({len(known)} catalogued tables: catalog_search / trino_sources catalog={c!r})" if known
                        else "(browse with trino_list, or trino_sources catalog=" + repr(c) + ")")
                out.append({"catalog": c, "schema": "*", "table": hint, "has_ban": None, "columns": "", "codes": "",
                            "description": ""})
            if known or c != catalog or c in ("system", "tpch"):
                continue
        try:
            rows, _ = _run(f"SELECT table_schema, table_name, max(CASE WHEN column_name = 'ban' THEN 1 ELSE 0 END) AS has_ban, "
                           "array_join(array_agg(column_name ORDER BY ordinal_position), ', ') AS cols "
                           f'FROM "{ident(c)}".information_schema.columns '
                           "WHERE table_schema NOT IN ('information_schema', 'sys', 'guest', 'pg_catalog') GROUP BY 1, 2 ORDER BY 1, 2", 2000)
            rows = [r for r in rows if not r["table_schema"].startswith("db_")]
        except UserError as e:
            out.append({"catalog": c, "schema": "*", "table": f"(unavailable: {str(e)[:80]})", "has_ban": None, "columns": "", "codes": ""})
            continue
        allowed = _CODE_SCHEMAS.get(c)
        descs = _catalog_tables(c)
        for r in rows:
            cols = r["cols"].split(", ")
            sample = bool(r["has_ban"]) and (allowed is None or r["table_schema"] in allowed) and not _remote(c)
            out.append({"catalog": c, "schema": r["table_schema"], "table": r["table_name"], "has_ban": bool(r["has_ban"]),
                        "columns": (r["cols"] if len(r["cols"]) <= 400 else r["cols"][:400] + ", ..."),
                        "codes": _codes(c, r["table_schema"], r["table_name"], cols) if sample else "",
                        "description": ((descs.get(f"{r['table_schema']}.{r['table_name']}") or {}).get("description") or "")[:160]})
    keyed = sum(1 for r in out if r["has_ban"])
    head = (f"{len(cats)} catalog(s), {len(out)} table(s); {keyed} carry the ban key and can be joined across systems. "
            "Qualify tables as catalog.schema.table.")
    return result(head + "\n\n" + md_table(out, 120), tables=out)


# every scan node: TableScan, ScanFilter, ScanProject, ScanFilterProject (… [table = catalog:schema.table …)
_SCAN = re.compile(r"\b(?:TableScan|Scan\w*)\[table = ([a-z0-9_]+):(\S+)")
_REMOTE = re.compile(r"ScanFilterProject|ScanProject|TableScan")


@tool(G, "Show how Trino will run a federated query")
def trino_explain(
    sql: Annotated[str, Field(description="The SELECT/WITH statement to plan")],
) -> dict:
    """Plan a query without running it: which systems each table is read from, what is pushed down to the source
    (filters, joins, aggregates) and how many distributed stages run. Good for showing how data virtualization works."""
    s = check_select(sql)
    rows, _ = _run("EXPLAIN (TYPE DISTRIBUTED) " + s, 5000)
    plan = "\n".join(str(next(iter(r.values()))) for r in rows)
    scans = sorted({f"{m.group(1)}: {m.group(2).strip().rstrip(',]')}" for m in _SCAN.finditer(plan)})  # whole plan, before truncation
    fragments = len(re.findall(r"^Fragment \d+", plan, re.M))
    systems = sorted({x.split(":")[0] for x in scans})
    pushed = [ln.strip() for ln in plan.splitlines() if "constraint on" in ln or "pushdown" in ln.lower()][:10]
    summary = (f"Reads from {len(systems)} system(s): {', '.join(systems) or 'none'}; {fragments} distributed stage(s).\n\n"
               + "\n".join(f"- {x}" for x in scans))
    if pushed:
        summary += "\n\nPushed down to the source:\n" + "\n".join(f"- {x[:160]}" for x in pushed)
    return result(summary + "\n\nFull plan (truncated):\n```\n" + plan[:6000] + "\n```",
                  systems=systems, scans=scans, stages=fragments)
