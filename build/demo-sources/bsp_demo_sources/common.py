"""Shared pieces: dataset subsets, connections to the three demo systems, waiting."""

from __future__ import annotations

import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

BUCKET = "bsp-demo"
PREFIX = "billing"
MINIO_ENDPOINT = os.environ.get("DEMO_MINIO_ENDPOINT", "http://minio:9000")
MSSQL_HOST = os.environ.get("DEMO_MSSQL_HOST", "mssql")
ORACLE_DSN = os.environ.get("DEMO_ORACLE_DSN", "oracle:1521/FREEPDB1")

# initial loads (later months arrive through `more`)
MINIO_MONTHS = ["2026-03", "2026-04", "2026-05", "2026-06"]
ORACLE_MONTHS = ["2026-04", "2026-05", "2026-06"]
CARE_CUTOFF = pd.Timestamp("2026-08-01")
CARE_BATCH = 250
CARE_UPDATES = 40

# bump CACHE_VERSION whenever the derived frames change shape (a stale pickle would re-seed old columns)
CACHE_VERSION = 2
_CACHE = Path(os.environ.get("DEMO_CACHE", f"/tmp/bsp-demo-sources-v{CACHE_VERSION}.pkl"))


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc):%H:%M:%S} [demo-sources] {msg}", flush=True)


def need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise SystemExit(f"{name} is not set — start the Demo sources capability from Studio (it injects the vault secrets)")
    return v


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


# ── dataset ──────────────────────────────────────────────────────────────
def dataset() -> dict[str, pd.DataFrame]:
    """The subsets we need from bsp_seed.build(customers=20000, seed=7), cached.

    Keys match the warehouse: accounts by `ban` (10-digit billing account number),
    wire centers by `clli`, invoices `<ban>-<yymm>`, one `transactions` ledger."""
    if _CACHE.is_file():
        try:
            out = pickle.loads(_CACHE.read_bytes())
            if out.get("_version") == CACHE_VERSION:
                return out
        except Exception:  # noqa: BLE001 — stale/corrupt cache: rebuild
            pass
    from bsp_seed import build
    log("building the synthetic DataPlatform dataset (customers=20000, seed=7) …")
    ds = build(customers=20000, seed=7)
    inv = ds["invoices"].copy()
    for c in ("invoice_date", "due_date"):
        inv[c] = pd.to_datetime(inv[c])
    txn = ds["transactions"].copy()
    txn["txn_date"] = pd.to_datetime(txn["txn_date"])
    out = {"_version": CACHE_VERSION, "invoices": inv, "transactions": txn,
           "orders": _care_orders(ds), "agents": _agents()}
    try:
        _CACHE.write_bytes(pickle.dumps(out))
    except OSError:
        pass
    return out


def _agents() -> pd.DataFrame:
    rng = np.random.default_rng(23)
    first = ["Avery", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Quinn", "Jamie", "Drew", "Reese", "Parker", "Rowan"]
    last = ["Carter", "Nguyen", "Patel", "Brooks", "Rivera", "Coleman", "Hughes", "Foster", "Ward", "Price", "Hayes", "Ford"]
    states = ["NC", "OH", "VA", "PA", "MO", "TX", "FL", "AL", "SC", "TN", "IN"]
    n = 48
    return pd.DataFrame({
        "agent_id": [f"AG{i:03d}" for i in range(1, n + 1)],
        "agent_name": [f"{rng.choice(first)} {rng.choice(last)}" for _ in range(n)],
        "team": rng.choice(["Field Ops", "Install", "Repair", "Dispatch"], size=n, p=[0.35, 0.3, 0.25, 0.1]),
        "home_state": [states[i % len(states)] for i in range(n)],
        "hire_date": (pd.Timestamp("2016-01-01") + pd.to_timedelta(rng.integers(0, 3500, n), unit="D")).date,
        "active": rng.random(n) < 0.92,
    })


def _care_orders(ds) -> pd.DataFrame:
    """Service orders / truck rolls: one per field dispatch (repair or install), with its ticket."""
    d = ds["dispatches"].merge(ds["service_tickets"][["ticket_id", "opened_at", "category", "subcategory"]],
                               on="ticket_id", how="left")
    d = d.sort_values(["opened_at", "dispatch_id"])
    d = d.merge(ds["service_locations"][["ban", "technology"]].drop_duplicates("ban"), on="ban", how="left")
    d = d.merge(ds["customers"][["ban", "state", "city"]], on="ban", how="left").reset_index(drop=True)
    rng = np.random.default_rng(11)
    n = len(d)
    created = pd.to_datetime(d["opened_at"]).dt.floor("s")
    scheduled = pd.to_datetime(d["scheduled_at"]).dt.floor("s")
    completed = pd.to_datetime(d["completed_at"]).dt.floor("s")
    status = d["status"].to_numpy(dtype=object)
    # orders opened in the last week before the initial cutoff are still open
    open_ = ((created >= CARE_CUTOFF - pd.Timedelta(days=7)) & (created < CARE_CUTOFF)).to_numpy()
    status = np.where(open_, rng.choice(["scheduled", "dispatched"], n, p=[0.7, 0.3]), status)
    completed = completed.where(status == "completed")
    updated = completed.fillna(created + pd.Timedelta(minutes=5))
    return pd.DataFrame({
        "order_id": np.arange(500001, 500001 + n),
        "dispatch_id": d["dispatch_id"],
        "ticket_id": d["ticket_id"],
        "ban": d["ban"],
        "order_type": np.where(d["dispatch_type"] == "install", "installation", "repair_truck_roll"),
        "category": d["category"], "subcategory": d["subcategory"],
        "priority": rng.choice(["P1", "P2", "P3"], n, p=[0.15, 0.5, 0.35]),
        "status": status,
        "agent_id": [f"AG{i:03d}" for i in rng.integers(1, 49, n)],
        "state": d["state"], "city": d["city"], "clli": d["clli"], "technology": d["technology"],
        "cause": d["cause"],
        "created_at": created, "scheduled_at": scheduled, "completed_at": completed, "updated_at": updated,
    })


def next_month(ym: str) -> str:
    return (pd.Period(ym, "M") + 1).strftime("%Y-%m")


def month_frames(ds: dict, ym: str) -> dict[str, pd.DataFrame]:
    """Invoices / transactions for bill cycle `ym`; months past the synthetic
    window are re-dated copies of its last month (invoice ids `<ban>-<yymm>`
    via bsp_seed.invoice_id, fresh transaction ids)."""
    from bsp_seed import invoice_id
    inv = ds["invoices"]
    last = inv["bill_cycle"].max()
    src = ym if ym <= last else last
    i = inv[inv["bill_cycle"] == src].copy()
    t = ds["transactions"][ds["transactions"]["invoice_id"].isin(i["invoice_id"])].copy()
    if src != ym:
        shift = pd.Period(ym, "M").ordinal - pd.Period(src, "M").ordinal
        month = pd.Timestamp(ym + "-01")
        new_id = {old: invoice_id(b, month) for old, b in zip(i["invoice_id"], i["ban"])}
        i["bill_cycle"] = ym
        for c in ("invoice_date", "due_date"):
            i[c] = i[c] + pd.DateOffset(months=shift)
        i["invoice_id"] = i["invoice_id"].map(new_id)
        t["txn_date"] = t["txn_date"] + pd.DateOffset(months=shift)
        t["invoice_id"] = t["invoice_id"].map(new_id)
        t["txn_id"] = "TX" + month.strftime("%y%m") + t["txn_id"].str[2:]
    return {"invoices": i.reset_index(drop=True), "transactions": t.reset_index(drop=True)}


# ── connections ──────────────────────────────────────────────────────────
def minio_fs():
    import s3fs
    return s3fs.S3FileSystem(key=need("MINIO_ACCESS_KEY"), secret=need("MINIO_SECRET_KEY"), skip_instance_cache=True,
                             client_kwargs={"endpoint_url": MINIO_ENDPOINT, "region_name": "us-east-1"},
                             config_kwargs={"s3": {"addressing_style": "path"}, "connect_timeout": 5, "read_timeout": 60})


def mssql_conn(database: str = "master", user: str = "sa", password: str | None = None):
    import pyodbc
    pw = password or need("DEMO_MSSQL_SA_PASSWORD")
    return pyodbc.connect(f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={MSSQL_HOST},1433;DATABASE={database};"
                          f"UID={user};PWD={{{pw}}};Encrypt=yes;TrustServerCertificate=yes;Connection Timeout=10",
                          autocommit=True)


def oracle_conn():
    import oracledb
    return oracledb.connect(user="billing", password=need("BILLING_ORA_PASSWORD"), dsn=ORACLE_DSN, tcp_connect_timeout=10)


def wait_for(name: str, probe: Callable[[], Any], timeout: float = 900) -> None:
    t0, last = time.time(), ""
    while True:
        try:
            probe()
            log(f"{name} is up ({int(time.time() - t0)}s)")
            return
        except Exception as e:  # noqa: BLE001 — keep polling until the deadline
            msg = f"{type(e).__name__}: {str(e).splitlines()[0][:160] if str(e) else ''}"
            if msg != last:
                log(f"waiting for {name} … {msg}")
                last = msg
            if time.time() - t0 > timeout:
                raise SystemExit(f"{name} did not become reachable in {int(timeout)}s: {msg}")
            time.sleep(5)


def _py(v: Any) -> Any:
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if isinstance(v, np.generic):
        v = v.item()
    if isinstance(v, float) and v != v:
        return None
    return v


def rows(df: pd.DataFrame) -> list[tuple]:
    """DataFrame → list of plain-python tuples (None for NaN/NaT, datetime for timestamps)."""
    return [tuple(_py(x) for x in r) for r in df.itertuples(index=False, name=None)]
