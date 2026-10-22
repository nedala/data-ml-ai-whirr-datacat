"""End-to-end test against real services (skipped unless BSP_INTEG=1).

Needs: a seeded Postgres (`python -m bsp_seed --target postgres --target parquet`),
the connectors app on CONNECTORS_URL and the canvas app on CANVAS_URL, plus
optional MinIO (alias minio), SQL Server (alias mssql) and Oracle Free
(alias oracle) for the object-storage and database connectors. A tiny
in-process HTTP server stands in for DataHub GMS (DATAHUB_GMS_URL).
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import pandas as pd
import psycopg2
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("BSP_INTEG") != "1", reason="integration only (BSP_INTEG=1)")
CANVAS = os.environ.get("CANVAS_URL", "http://127.0.0.1:8093/canvas")
CONN = os.environ.get("CONNECTORS_URL", "http://127.0.0.1:8092/connectors")
H = {"X-Studio": "1"}
c = httpx.Client(timeout=300)


def pg_exec(sql: str) -> None:
    with psycopg2.connect(host=os.environ.get("PG_HOST", "postgres"), dbname="bsp_edw", user="analyst",
                          password=os.environ["PG_PASS"]) as conn, conn.cursor() as cur:
        cur.execute(sql)


def pg_rows(sql: str):
    with psycopg2.connect(host=os.environ.get("PG_HOST", "postgres"), dbname="bsp_edw", user="analyst",
                          password=os.environ["PG_PASS"]) as conn, conn.cursor() as cur:
        cur.execute(sql)
        return cur.fetchall()


def run(pid: str) -> dict:
    r = c.post(f"{CANVAS}/api/pipelines/{pid}/run?wait=true", headers=H)
    assert r.status_code == 200, r.text
    rec = r.json()["run"]
    if rec["status"] != "success":
        log = c.get(f"{CANVAS}/api/runs/{pid}/{rec['run_id']}/log").json()["lines"]
        pytest.fail(f"{pid} failed: {rec['error']}\n" + "\n".join(log[-25:]))
    return rec


def loaded(rec: dict) -> int:
    return sum(x["rows"] for x in rec["loaded"])


def save(doc: dict) -> dict:
    r = c.post(f"{CANVAS}/api/pipelines", json=doc, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture(scope="module", autouse=True)
def fresh_landing():
    """Start from an empty landing schema (dlt keeps its own state tables there)."""
    pg_exec("drop schema if exists landing cascade")
    yield


# ── UI ───────────────────────────────────────────────────────────────────
def test_ui_and_assets():
    html = c.get(f"{CANVAS}/").text
    assert "Integration Canvas" in html
    for ref in ("ui-kit/bsp.css", "ui-kit/bsp.js", "static/canvas.css", "static/canvas.js"):
        assert f'"{ref}"' in html
        r = c.get(f"{CANVAS}/{ref}")
        assert r.status_code == 200 and len(r.content) > 500, ref
    assert c.get(f"{CONN}/").status_code == 200 and c.get(f"{CONN}/ui-kit/bsp.js").status_code == 200


# ── the four mock-mode examples ──────────────────────────────────────────
def test_example_sharepoint_finance():
    rec = run("sharepoint-finance-monthly")
    n = pg_rows("select count(*), count(distinct state), min(fiber_share) > 0 from landing.finance_monthly")[0]
    assert n[0] == loaded(rec) == 96 and n[1] == 8 and n[2] is True


def test_example_outlook_disputes():
    rec = run("outlook-dispute-emails")
    total, with_acct = pg_rows("select count(*), count(ban) from landing.dispute_emails")[0]
    assert total == loaded(rec) > 0 and with_acct > 0
    cols = {r[0] for r in pg_rows("select column_name from information_schema.columns where table_schema='landing' "
                                   "and table_name='dispute_emails'")}
    assert "body" not in cols and {"message_id", "subject", "from_address", "received_at"} <= cols
    rec2 = run("outlook-dispute-emails")  # merge: idempotent
    assert pg_rows("select count(*) from landing.dispute_emails")[0][0] == total and loaded(rec2) == total


def test_example_rest_atlan():
    rec = run("rest-atlan-tables")
    n, verified = pg_rows("select count(*), count(*) filter (where certificate = 'VERIFIED') from landing.atlan_tables")[0]
    assert n == loaded(rec) > 10 and verified > 0
    log = c.get(f"{CANVAS}/api/runs/rest-atlan-tables/{rec['run_id']}/log").json()["lines"]
    assert sum("page " in line for line in log) >= 2  # offset pagination really paged


def test_example_tickets_pii_lake():
    rec = run("tickets-pii-to-lake")
    lake = os.environ.get("LAKE_DIR", "/data/lake")
    root = os.path.join(lake, "exports", "canvas", "service_tickets_masked")
    parts = [d for d in os.listdir(root) if d.startswith("category=")]
    assert parts, os.listdir(root)
    df = pd.read_parquet(root)
    assert len(df) == loaded(rec) == pg_rows("select count(*) from raw.service_tickets")[0][0]
    assert df["ban"].str.len().eq(32).all() and not df["ban"].str.fullmatch(r"\d{10}").any()
    assert df["email"].dropna().str.contains(r"\*\*\*@").all()


def test_lineage_pushed_to_atlan_mock_and_datahub():
    lin = c.get(f"{CONN}/atlan/lineage", params={"qn": "default/postgres/bsp_edw/landing/finance_monthly",
                                                  "direction": "upstream"}).json()
    assert any("sharepoint" in q for q in lin["upstream"]), lin["upstream"]
    got = c.get(os.environ["DATAHUB_GMS_URL"] + "/_received").json()
    assert got["paths"] and set(got["paths"]) == {"/openapi/openlineage/api/v1/lineage"}
    assert {"START", "COMPLETE"} <= set(got["types"])


# ── object storage (MinIO, S3-compatible) ────────────────────────────────
def _s3():
    import s3fs
    return s3fs.S3FileSystem(key=os.environ["MINIO_ACCESS_KEY"], secret=os.environ["MINIO_SECRET_KEY"],
                             client_kwargs={"endpoint_url": "http://minio:9000", "region_name": "us-east-1"},
                             config_kwargs={"s3": {"addressing_style": "path"}})


def _put_parquet(fs, path: str, start: int, n: int) -> None:
    df = pd.DataFrame({"invoice_id": [f"{3100000000 + i * 7 + i % 5}-2610" for i in range(start, start + n)],
                       "ban": [f"{3100000000 + i * 7 + i % 5}" for i in range(start, start + n)],
                       "amount_due": [50.0 + i for i in range(n)]})
    with fs.open(path, "wb") as fh:
        df.to_parquet(fh, index=False)


@pytest.mark.skipif(not os.environ.get("MINIO_ACCESS_KEY"), reason="no MinIO")
def test_object_storage_incremental_and_destination():
    fs = _s3()
    if not fs.exists("bsp-demo"):
        fs.mkdir("bsp-demo")
    for old in fs.glob("bsp-demo/**"):
        if fs.isfile(old):
            fs.rm(old)
    c.delete(f"{CANVAS}/api/pipelines/bucket-parquet-to-landing/state", headers=H)
    _put_parquet(fs, "bsp-demo/billing/part-001.parquet", 0, 40)
    with fs.open("bsp-demo/raw/usage.csv.gz", "wb") as fh:
        import gzip
        fh.write(gzip.compress(b"ban,gb_down\n3100000008,120.5\n3100000016,98.1\n"))
    t = c.post(f"{CANVAS}/api/connections/test", json={"name": "minio-demo"}, headers=H).json()
    assert t["ok"], t
    assert "bsp-demo" in {e["name"] for e in c.get(f"{CANVAS}/api/connections/minio-demo/browse").json()["entries"]}
    g = c.get(f"{CANVAS}/api/connections/minio-demo/glob", params={"bucket": "bsp-demo", "pattern": "**/*.csv.gz"}).json()
    assert g["count"] == 1
    # preview a gzipped CSV through the storage source
    doc = {"id": "tmp-csv", "nodes": [{"id": "s", "type": "source.storage", "config": {
        "connection": "minio-demo", "bucket": "bsp-demo", "pattern": "raw/*.csv.gz"}}], "edges": []}
    pv = c.post(f"{CANVAS}/api/preview", json={"pipeline": doc, "node_id": "s"}, headers=H).json()
    assert pv["row_count"] == 2 and pv["columns"][0]["name"] == "ban"

    r1 = run("bucket-parquet-to-landing")
    assert loaded(r1) == 40
    r2 = run("bucket-parquet-to-landing")
    assert loaded(r2) == 0  # no new files
    time.sleep(1.1)
    _put_parquet(fs, "bsp-demo/billing/part-002.parquet", 40, 15)
    r3 = run("bucket-parquet-to-landing")
    assert loaded(r3) == 15
    assert pg_rows("select count(*), count(distinct _source_file) from landing.bucket_invoices")[0] == (55, 2)

    # destination: warehouse → MinIO parquet partitioned by load date
    save({"id": "plans-to-bucket", "name": "plans → bucket", "nodes": [
        {"id": "s", "type": "source.database", "config": {"connection": "warehouse", "schema": "raw", "table": "plans"}},
        {"id": "d", "type": "dest.storage", "config": {"connection": "minio-demo", "bucket": "bsp-demo", "prefix": "exports",
                                                       "table": "plans", "format": "parquet", "partition_by": "load_date",
                                                       "mode": "full_refresh"}}],
        "edges": [{"from": "s", "to": "d"}]})
    rec = run("plans-to-bucket")
    files = fs.glob("bsp-demo/exports/plans/load_date=*/*.parquet")
    assert files and loaded(rec) == 7
    with fs.open(files[0], "rb") as fh:
        assert len(pd.read_parquet(fh)) == 7


# ── SQL Server ───────────────────────────────────────────────────────────
def _mssql(sql: str, db: str = "master", many=None):
    import pymssql
    conn = pymssql.connect(server="mssql", user="sa", password=os.environ["MSSQL_SA_PASSWORD"], database=db,
                           autocommit=True)
    cur = conn.cursor()
    if many:
        cur.executemany(sql, many)
    else:
        cur.execute(sql)
    conn.close()


@pytest.mark.skipif(not os.environ.get("MSSQL_SA_PASSWORD"), reason="no SQL Server")
def test_mssql_incremental():
    _mssql("if db_id('care') is null create database care")
    _mssql("if not exists (select * from sys.sql_logins where name='canvas_reader') "
           f"create login canvas_reader with password='{os.environ['CARE_MSSQL_PASSWORD']}', check_policy=off")
    _mssql("if not exists (select * from sys.database_principals where name='canvas_reader') "
           "create user canvas_reader for login canvas_reader; alter role db_datareader add member canvas_reader", "care")
    _mssql("if object_id('dbo.care_orders') is not null drop table dbo.care_orders; "
           "create table dbo.care_orders (order_id int primary key, ban varchar(10), order_type varchar(30), "
           "amount decimal(10,2), updated_at datetime2 not null)", "care")
    t0 = datetime(2026, 10, 1, 8, 0)
    rows = [(i, f"{3100000000 + i * 7 + i % 5}", "install" if i % 2 else "repair", 100 + i, t0 + timedelta(hours=i)) for i in range(1, 6)]
    _mssql("insert into dbo.care_orders values (%s, %s, %s, %s, %s)", "care", many=rows)

    t = c.post(f"{CANVAS}/api/connections/test", json={"name": "care-mssql"}, headers=H).json()
    assert t["ok"] and "pyodbc" in t["driver"], t
    assert "dbo" in c.get(f"{CANVAS}/api/connections/care-mssql/schemas").json()["schemas"]
    tabs = c.get(f"{CANVAS}/api/connections/care-mssql/tables", params={"schema": "dbo", "q": "care"}).json()["tables"]
    assert [x["name"] for x in tabs] == ["care_orders"]
    cols = c.get(f"{CANVAS}/api/connections/care-mssql/columns", params={"schema": "dbo", "table": "care_orders"}).json()
    assert {x["name"]: x["primary_key"] for x in cols["columns"]}["order_id"] is True
    est = c.get(f"{CANVAS}/api/connections/care-mssql/estimate", params={"schema": "dbo", "table": "care_orders"}).json()
    assert est["rows"] == 5

    r1 = run("mssql-orders-incremental")
    assert loaded(r1) == 5
    st = c.get(f"{CANVAS}/api/pipelines/mssql-orders-incremental/state").json()["state"]
    assert st["nodes"]["src"]["cursor"].startswith("2026-10-01T13:00")
    r2 = run("mssql-orders-incremental")
    assert loaded(r2) == 0
    _mssql("insert into dbo.care_orders values (%s, %s, %s, %s, %s)", "care",
           many=[(6, "3100000043", "repair", 106, t0 + timedelta(days=2)), (7, "3100000051", "install", 107, t0 + timedelta(days=2))])
    _mssql(f"update dbo.care_orders set amount = 999, updated_at = '{(t0 + timedelta(days=3)).isoformat()}' where order_id = 2", "care")
    r3 = run("mssql-orders-incremental")
    assert loaded(r3) == 3  # 2 new + 1 updated, nothing else re-read
    assert pg_rows("select count(*), max(amount) from landing.care_orders")[0] == (7, 999)

    # pymssql fallback driver
    conn = {"name": "care-mssql-tds", "type": "mssql", "params": {"host": "mssql", "port": 1433, "database": "care",
            "username": "canvas_reader", "password_ref": "vault:CARE_MSSQL_PASSWORD", "driver": "pymssql"}}
    assert c.post(f"{CANVAS}/api/connections", json=conn, headers=H).status_code == 200
    t = c.post(f"{CANVAS}/api/connections/test", json={"name": "care-mssql-tds"}, headers=H).json()
    assert t["ok"] and "pymssql" in t["driver"], t


# ── Oracle ───────────────────────────────────────────────────────────────
@pytest.mark.skipif(not os.environ.get("BILLING_ORA_PASSWORD"), reason="no Oracle")
def test_oracle_incremental():
    import oracledb
    with oracledb.connect(user="billing", password=os.environ["BILLING_ORA_PASSWORD"], dsn="oracle:1521/FREEPDB1") as o:
        cur = o.cursor()
        try:
            cur.execute("drop table invoice_lines purge")
        except oracledb.DatabaseError:
            pass
        cur.execute("create table invoice_lines (line_id number(10) primary key, invoice_id varchar2(20), "
                    "charge_code varchar2(20), amount number(10,2), updated_at timestamp not null)")
        cur.executemany("insert into invoice_lines values (:1, :2, :3, :4, :5)",
                        [(i, f"{3100000000 + i * 7 + i % 5}-2610", "FB-1G", 65 + i, datetime(2026, 10, 1) + timedelta(minutes=i)) for i in range(1, 11)])
        o.commit()
    t = c.post(f"{CANVAS}/api/connections/test", json={"name": "billing-oracle"}, headers=H).json()
    assert t["ok"] and "Oracle" in t["detail"], t
    assert "BILLING" in [s.upper() for s in c.get(f"{CANVAS}/api/connections/billing-oracle/schemas").json()["schemas"]]
    save({"id": "oracle-invoice-lines", "name": "Oracle invoice lines → landing", "nodes": [
        {"id": "src", "type": "source.database", "config": {"connection": "billing-oracle", "schema": "billing",
                                                            "table": "invoice_lines", "cursor_column": "updated_at",
                                                            "backend": "sqlalchemy"}},
        {"id": "dst", "type": "dest.warehouse", "config": {"schema": "landing", "table": "ora_invoice_lines", "mode": "append"}}],
        "edges": [{"from": "src", "to": "dst"}]})
    assert loaded(run("oracle-invoice-lines")) == 10
    assert loaded(run("oracle-invoice-lines")) == 0
    with oracledb.connect(user="billing", password=os.environ["BILLING_ORA_PASSWORD"], dsn="oracle:1521/FREEPDB1") as o:
        o.cursor().executemany("insert into invoice_lines values (:1, :2, :3, :4, :5)",
                               [(i, f"{3100000000 + i * 7 + i % 5}-2610", "LATE-FEE", 20, datetime(2026, 10, 2) + timedelta(minutes=i)) for i in range(11, 15)])
        o.commit()
    assert loaded(run("oracle-invoice-lines")) == 4
    assert pg_rows("select count(*) from landing.ora_invoice_lines")[0][0] == 14
