"""Initial (idempotent) loads and incremental `more` batches for each demo system."""

from __future__ import annotations

import io

import pandas as pd

from .common import (BUCKET, CARE_BATCH, CARE_CUTOFF, CARE_UPDATES, MINIO_MONTHS, ORACLE_MONTHS, log, minio_fs,
                     month_frames, mssql_conn, need, next_month, oracle_conn, rows, utcnow)

# ── MinIO: s3://bsp-demo/billing/YYYY-MM.parquet ─────────────────────────
INVOICE_COLS = ["invoice_id", "ban", "bill_cycle", "invoice_date", "due_date", "recurring_charges", "one_time_charges",
                "taxes_fees", "credits", "late_fee", "amount_due", "status"]


def _minio_months(fs) -> list[str]:
    try:
        names = fs.ls(f"{BUCKET}/billing", refresh=True)
    except FileNotFoundError:
        return []
    return sorted(n.rsplit("/", 1)[-1][:-8] for n in names if n.endswith(".parquet"))


def _put_month(fs, ds: dict, ym: str) -> int:
    df = month_frames(ds, ym)["invoices"][INVOICE_COLS].copy()
    for c in ("invoice_date", "due_date"):
        df[c] = df[c].dt.date
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    with fs.open(f"{BUCKET}/billing/{ym}.parquet", "wb") as f:
        f.write(buf.getvalue())
    log(f"minio: wrote s3://{BUCKET}/billing/{ym}.parquet ({len(df):,} invoices)")
    return len(df)


def minio_probe() -> None:
    minio_fs().ls("")


def _minio_stale(fs, have: list[str]) -> bool:
    """True when the bucket holds files from the old data model (account_id / tax / discounts columns)."""
    if not have:
        return False
    import pyarrow.parquet as pq
    with fs.open(f"{BUCKET}/billing/{have[0]}.parquet", "rb") as f:
        return pq.read_schema(f).names != INVOICE_COLS


def seed_minio(ds: dict) -> None:
    fs = minio_fs()
    if not fs.exists(BUCKET):
        fs.mkdir(BUCKET)
        log(f"minio: created bucket {BUCKET}")
    if _minio_stale(fs, _minio_months(fs)):
        for name in fs.ls(f"{BUCKET}/billing", refresh=True):
            fs.rm(name)
        log(f"minio: removed s3://{BUCKET}/billing/* (old data model) — re-seeding")
    have = set(_minio_months(fs))
    todo = [m for m in MINIO_MONTHS if m not in have]
    if not todo:
        log(f"minio: already seeded ({len(have)} monthly file(s))")
    for ym in todo:
        _put_month(fs, ds, ym)


def more_minio(ds: dict) -> str:
    fs = minio_fs()
    have = _minio_months(fs)
    ym = next_month(max(have)) if have else MINIO_MONTHS[0]
    n = _put_month(fs, ds, ym)
    return f"s3://{BUCKET}/billing/{ym}.parquet ({n:,} invoices)"


# ── SQL Server: database care · dbo.care_orders · dbo.care_agents ────────
ORDER_COLS = ["order_id", "dispatch_id", "ticket_id", "ban", "order_type", "category", "subcategory", "priority",
              "status", "agent_id", "state", "city", "clli", "technology", "cause", "created_at", "scheduled_at",
              "completed_at", "updated_at"]
# care_orders from the old data model (account_id / wire_center) is dropped and re-seeded
MIGRATE_MSSQL = ("IF COL_LENGTH('dbo.care_orders', 'account_id') IS NOT NULL "
                 "OR COL_LENGTH('dbo.care_orders', 'ban') IS NULL DROP TABLE IF EXISTS dbo.care_orders")
DDL_MSSQL = [
    """IF OBJECT_ID('dbo.care_agents') IS NULL CREATE TABLE dbo.care_agents (
         agent_id VARCHAR(8) NOT NULL PRIMARY KEY, agent_name NVARCHAR(80) NOT NULL, team VARCHAR(20) NOT NULL,
         home_state CHAR(2) NOT NULL, hire_date DATE NOT NULL, active BIT NOT NULL)""",
    """IF OBJECT_ID('dbo.care_orders') IS NULL CREATE TABLE dbo.care_orders (
         order_id INT NOT NULL PRIMARY KEY, dispatch_id VARCHAR(12), ticket_id VARCHAR(16) NOT NULL,
         ban VARCHAR(10) NOT NULL, order_type VARCHAR(24) NOT NULL, category VARCHAR(20), subcategory VARCHAR(32),
         priority CHAR(2), status VARCHAR(16) NOT NULL, agent_id VARCHAR(8), state CHAR(2), city NVARCHAR(40),
         clli CHAR(8), technology VARCHAR(8), cause VARCHAR(32), created_at DATETIME2(0) NOT NULL, scheduled_at DATETIME2(0),
         completed_at DATETIME2(0), updated_at DATETIME2(0) NOT NULL)""",
    "IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'ix_care_orders_updated_at') "
    "CREATE INDEX ix_care_orders_updated_at ON dbo.care_orders(updated_at)",
]


def mssql_probe() -> None:
    with mssql_conn() as c:
        c.execute("SELECT 1").fetchone()


def _insert(cur, table: str, cols: list[str], data: list[tuple]) -> None:
    """One transaction (autocommit would flush the log per row: minutes instead of seconds)."""
    conn = cur.connection
    conn.autocommit = False
    try:
        cur.fast_executemany = True
        cur.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", data)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True


def seed_mssql(ds: dict) -> None:
    app_pw = need("CARE_MSSQL_PASSWORD")
    with mssql_conn() as c:
        cur = c.cursor()
        cur.execute("IF DB_ID('care') IS NULL CREATE DATABASE care")
        # login follows the vault value (rotations in Studio are picked up on the next seed run)
        cur.execute("""DECLARE @pw NVARCHAR(128) = ?; DECLARE @q NVARCHAR(400);
                       IF SUSER_ID('canvas_reader') IS NULL
                         SET @q = N'CREATE LOGIN canvas_reader WITH PASSWORD = ' + QUOTENAME(@pw, '''')
                                  + N', CHECK_POLICY = OFF, DEFAULT_DATABASE = care';
                       ELSE
                         SET @q = N'ALTER LOGIN canvas_reader WITH PASSWORD = ' + QUOTENAME(@pw, '''');
                       EXEC(@q);""", app_pw)
    with mssql_conn("care") as c:
        cur = c.cursor()
        cur.execute("IF USER_ID('canvas_reader') IS NULL CREATE USER canvas_reader FOR LOGIN canvas_reader")
        cur.execute("ALTER ROLE db_datareader ADD MEMBER canvas_reader")
        if cur.execute("SELECT COL_LENGTH('dbo.care_orders', 'account_id')").fetchone()[0] is not None:
            log("mssql: care.dbo.care_orders has the old data model (account_id) — dropping and re-seeding")
        cur.execute(MIGRATE_MSSQL)
        for ddl in DDL_MSSQL:
            cur.execute(ddl)
        if cur.execute("SELECT COUNT(*) FROM dbo.care_agents").fetchone()[0] == 0:
            ag = ds["agents"]
            _insert(cur, "dbo.care_agents", list(ag.columns), rows(ag))
            log(f"mssql: care.dbo.care_agents ← {len(ag)} rows")
        n = cur.execute("SELECT COUNT(*) FROM dbo.care_orders").fetchone()[0]
        if n:
            log(f"mssql: care.dbo.care_orders already seeded ({n:,} rows)")
            return
        o = ds["orders"]
        o = o[o["created_at"] < CARE_CUTOFF][ORDER_COLS]
        _insert(cur, "dbo.care_orders", ORDER_COLS, rows(o))
        log(f"mssql: care.dbo.care_orders ← {len(o):,} rows (created before {CARE_CUTOFF:%Y-%m-%d})")


def more_mssql(ds: dict) -> str:
    now = utcnow()
    with mssql_conn("care") as c:
        cur = c.cursor()
        top = cur.execute("SELECT COALESCE(MAX(order_id), 500000) FROM dbo.care_orders").fetchone()[0]
        o = ds["orders"]
        new = o[o["order_id"] > top].head(CARE_BATCH).copy()
        if len(new) < CARE_BATCH:  # past the synthetic window: re-use random history under fresh ids
            extra = o.sample(CARE_BATCH - len(new), random_state=int(top) % 100000).copy()
            start = int(max(top, new["order_id"].max() if len(new) else top)) + 1
            extra["order_id"] = range(start, start + len(extra))
            extra["ticket_id"] = [f"TKS{i:08d}" for i in extra["order_id"]]
            extra["dispatch_id"] = [f"DSS{i:08d}" for i in extra["order_id"]]
            extra["created_at"] = now - pd.to_timedelta(range(len(extra)), unit="min")
            extra["scheduled_at"] = (now + pd.Timedelta(hours=24)).replace(minute=0, second=0)
            extra["status"], extra["completed_at"] = "scheduled", pd.NaT
            new = pd.concat([new, extra], ignore_index=True)
        new["updated_at"] = now
        _insert(cur, "dbo.care_orders", ORDER_COLS, rows(new[ORDER_COLS]))
        cur.execute(f"""UPDATE TOP ({CARE_UPDATES}) dbo.care_orders
                        SET status = 'completed', completed_at = ?, updated_at = ?
                        WHERE status IN ('scheduled', 'dispatched') AND order_id <= ?""", now, now, int(top))
        upd = cur.rowcount
    return f"care.dbo.care_orders: +{len(new)} new order(s), {upd} existing order(s) completed (updated_at {now:%Y-%m-%d %H:%M:%S})"


# ── Oracle: schema billing · INVOICES · TRANSACTIONS ────────────────────
DDL_ORA = {
    "INVOICES": """CREATE TABLE invoices (invoice_id VARCHAR2(20) PRIMARY KEY, ban VARCHAR2(10) NOT NULL,
                   bill_cycle VARCHAR2(7) NOT NULL, invoice_date DATE, due_date DATE, recurring_charges NUMBER(10,2),
                   one_time_charges NUMBER(10,2), taxes_fees NUMBER(10,2), credits NUMBER(10,2), late_fee NUMBER(10,2),
                   amount_due NUMBER(10,2), status VARCHAR2(16), updated_at TIMESTAMP(0) NOT NULL)""",
    "TRANSACTIONS": """CREATE TABLE transactions (txn_id VARCHAR2(20) PRIMARY KEY, ban VARCHAR2(10) NOT NULL,
                   invoice_id VARCHAR2(20), txn_date DATE, txn_type VARCHAR2(16), amount NUMBER(10,2),
                   payment_method VARCHAR2(16), reason VARCHAR2(200), updated_at TIMESTAMP(0) NOT NULL)""",
}
ORA_COLS = {
    "INVOICES": INVOICE_COLS + ["updated_at"],
    "TRANSACTIONS": ["txn_id", "ban", "invoice_id", "txn_date", "txn_type", "amount", "payment_method", "reason",
                     "updated_at"],
}
ORA_LEGACY = ("PAYMENTS", "ADJUSTMENTS")  # replaced by TRANSACTIONS


def oracle_probe() -> None:
    with oracle_conn() as c:
        c.cursor().execute("SELECT 1 FROM dual").fetchone()


def _ora_load(cur, frames: dict[str, pd.DataFrame], stamp=None) -> dict[str, int]:
    inv, txn = frames["invoices"].copy(), frames["transactions"].copy()
    if stamp is None:  # initial history: updated when issued / posted
        inv["updated_at"] = inv["invoice_date"] + pd.Timedelta(hours=2)
        txn["updated_at"] = txn["txn_date"].fillna(txn["txn_date"].max()) + pd.Timedelta(hours=20)
    else:
        inv["updated_at"] = txn["updated_at"] = pd.Timestamp(stamp)
    out = {}
    for table, df in (("INVOICES", inv), ("TRANSACTIONS", txn)):
        cols = ORA_COLS[table]
        data = rows(df[cols])
        cur.executemany(f"INSERT INTO {table.lower()} ({', '.join(cols)}) VALUES ({', '.join(f':{i + 1}' for i in range(len(cols)))})",
                        data, batcherrors=False)
        out[table] = len(data)
    return out


def seed_oracle(ds: dict) -> None:
    with oracle_conn() as c:
        cur = c.cursor()
        have = {r[0] for r in cur.execute("SELECT table_name FROM user_tables")}
        inv_cols = {r[0] for r in cur.execute("SELECT column_name FROM user_tab_columns WHERE table_name = 'INVOICES'")}
        if (have & set(ORA_LEGACY)) or (inv_cols and "BAN" not in inv_cols):
            for t in [*ORA_LEGACY, *DDL_ORA]:
                if t in have:
                    cur.execute(f"DROP TABLE {t.lower()} PURGE")
            log("oracle: dropped billing tables from the old data model (account_id, PAYMENTS, ADJUSTMENTS) — re-seeding")
            have = set()
        for t, ddl in DDL_ORA.items():
            if t not in have:
                cur.execute(ddl)
        cur.execute("BEGIN EXECUTE IMMEDIATE 'CREATE INDEX ix_invoices_updated_at ON invoices(updated_at)'; "
                    "EXCEPTION WHEN OTHERS THEN IF SQLCODE NOT IN (-955, -1408) THEN RAISE; END IF; END;")
        n = cur.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        if n:
            log(f"oracle: billing.INVOICES already seeded ({n:,} rows)")
            return
        tot = {t: 0 for t in DDL_ORA}
        for ym in ORACLE_MONTHS:
            for k, v in _ora_load(cur, month_frames(ds, ym)).items():
                tot[k] += v
        c.commit()
        log("oracle: billing ← " + ", ".join(f"{k} {v:,}" for k, v in tot.items()) + f" (bill cycles {', '.join(ORACLE_MONTHS)})")


def more_oracle(ds: dict) -> str:
    now = utcnow()
    with oracle_conn() as c:
        cur = c.cursor()
        last = cur.execute("SELECT MAX(bill_cycle) FROM invoices").fetchone()[0]
        ym = next_month(last) if last else ORACLE_MONTHS[0]
        got = _ora_load(cur, month_frames(ds, ym), stamp=now)
        c.commit()
    return f"billing bill cycle {ym}: " + ", ".join(f"{k} +{v:,}" for k, v in got.items()) + f" (updated_at {now:%Y-%m-%d %H:%M:%S})"


SEEDERS = {"minio": (minio_probe, seed_minio), "mssql": (mssql_probe, seed_mssql), "oracle": (oracle_probe, seed_oracle)}
MORE = {"minio": more_minio, "mssql": more_mssql, "oracle": more_oracle}
