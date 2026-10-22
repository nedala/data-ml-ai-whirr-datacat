"""Data integration for the churn use case.

Source of truth for the monthly customer snapshot:
  1. `marts.mart_churn_features` (dbt, data-pro appliance) when it exists and
     carries the columns we need, else
  2. `raw.customer_monthly` (seeded by bsp_seed).

Keys follow the warehouse contract (plan/architecture.md §3): every account
table is keyed by `ban` (config.ENTITY); technology ∈ {copper, fiber}. The
plant-quality signals are technology-specific — ONT optics for fiber
(`ont_rx_power_dbm`, `ont_los_events`, NaN on copper) and DSL line quality for
copper (`dsl_snr_margin_db`, `dsl_retrains`, NaN on fiber) — so integrate()
also derives one technology-neutral `line_quality_bad` flag.

`integrate()` joins that snapshot with two more warehouse tables — NPS
surveys (as-of join, latest survey strictly before the snapshot) and field
dispatches (repair truck rolls in the 90 days before the snapshot) — and writes the
wide, leakage-safe base table `features.churn_base` that the feature store,
EDA and feature selection all read.
"""

from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import db
from .config import ENTITY, LABEL, TS

MART = "marts.mart_churn_features"
RAW = "raw.customer_monthly"
BASE = "features.churn_base"

NUMERIC = ["monthly_charge", "tenure_months", "gb_down", "avg_latency_ms", "outage_minutes", "tickets_month",
           "tickets_decayed", "calls_30d", "calls_decayed", "dispatches_90d", "disputes_90d", "late_payments_decayed",
           "nps_last", "ont_rx_power_dbm", "ont_los_events", "dsl_snr_margin_db", "dsl_retrains"]
TECH_SPECIFIC = {"fiber": ["ont_rx_power_dbm", "ont_los_events"], "copper": ["dsl_snr_margin_db", "dsl_retrains"]}
TECHNOLOGIES = ("copper", "fiber")
BOOLEAN = ["autopay", "competitor_fiber_available", "recent_price_increase", "promo_expired"]
CATEGORICAL = ["technology", "plan_id", "credit_band", "segment", "state"]
CORE = NUMERIC + BOOLEAN + CATEGORICAL
REQUIRED = [ENTITY, TS, LABEL, "label_complete"] + CORE

# Engineered / integrated candidates added by integrate(). Some are useful,
# some are deliberately redundant or useless so feature sifting has real work:
DERIVED = {
    "truck_rolls_90d": "useful: repair truck rolls in the 90 days before the snapshot (raw.dispatches)",
    "line_quality_bad": "useful: technology-neutral plant flag — fiber ONT rx < -25 dBm, copper DSL SNR margin < 6 dB",
    "last_survey_score": "sparse: latest NPS survey score before the snapshot (nps_surveys) — mostly missing",
    "outage_hours": "redundant: outage_minutes / 60 (perfectly correlated)",
    "tenure_years": "redundant: tenure_months / 12",
    "is_fiber": "redundant: technology = fiber",
    "charge_x_increase": "interaction: monthly_charge × recent_price_increase",
    "log_gb_down": "monotone transform of gb_down",
    "probe_random": "random probe (seeded noise) — anything ranking below it is noise",
    "probe_constant": "near-constant column — variance filter must drop it",
}
CANDIDATES = CORE + list(DERIVED)


def source_table() -> str:
    """Prefer the dbt mart when present and complete; fall back to raw."""
    try:
        if db.table_exists(MART):
            cols = set(db.columns_of(MART))
            if set(REQUIRED) <= cols:
                return MART
    except Exception:
        pass
    return RAW


def integrate(verbose: bool = True, max_accounts: int | None = None) -> dict:
    """Build `features.churn_base` (idempotent). Returns a summary dict.

    max_accounts (or env CHURN_MAX_ACCOUNTS): keep a deterministic sample of
    that many accounts (ordered by md5(ban)) — a fast demo variant that
    leaves the warehouse untouched."""
    import os

    t0 = time.time()
    if max_accounts is None:
        max_accounts = int(os.environ.get("CHURN_MAX_ACCOUNTS", "0") or 0)
    src = source_table()
    db.execute("create schema if not exists features")
    has_nps = db.table_exists("raw.nps_surveys")
    has_tix = db.table_exists("raw.dispatches")
    nps_sql = (f"(select s.nps_score::double precision from raw.nps_surveys s where s.{ENTITY} = c.{ENTITY} "
               "and s.survey_date < c.snapshot_month order by s.survey_date desc limit 1)") if has_nps else "null::double precision"
    tix_sql = ("coalesce(t.truck_rolls_90d, 0)") if has_tix else "0"
    tix_join = (f"""left join lateral (
          select count(*)::int as truck_rolls_90d from raw.dispatches k
          where k.{ENTITY} = c.{ENTITY} and k.dispatch_type = 'repair'
            and k.scheduled_at >= c.{TS} - interval '90 days' and k.scheduled_at < c.{TS}) t on true"""
                if has_tix else "")
    if has_tix:
        db.execute(f"create index if not exists dispatches_key_ts on raw.dispatches ({ENTITY}, scheduled_at)")
    if has_nps:
        db.execute(f"create index if not exists nps_surveys_key_ts on raw.nps_surveys ({ENTITY}, survey_date)")
    cols = ", ".join(f"c.{x}" for x in REQUIRED)
    sample_sql = (f"where c.{ENTITY} in (select a from (select distinct {ENTITY} a from {src}) d "
                  f"order by md5(a) limit {int(max_accounts)})") if max_accounts and max_accounts > 0 else ""
    db.execute(f"drop table if exists {BASE} cascade")
    db.execute(f"""
    create table {BASE} as
    select {cols},
           {tix_sql} as truck_rolls_90d,
           case when c.technology = 'fiber' then (coalesce(c.ont_rx_power_dbm, -20) < -25)::int
                else (coalesce(c.dsl_snr_margin_db, 12) < 6)::int end as line_quality_bad,
           {nps_sql} as last_survey_score,
           c.outage_minutes / 60.0 as outage_hours,
           c.tenure_months / 12.0 as tenure_years,
           (c.technology = 'fiber')::int as is_fiber,
           c.monthly_charge * (c.recent_price_increase)::int as charge_x_increase,
           ln(1 + c.gb_down) as log_gb_down,
           ((('x' || substr(md5(c.{ENTITY}::text || c.{TS}::text), 1, 8))::bit(32)::bigint % 10000) / 10000.0)
               as probe_random,
           case when ('x' || substr(md5(c.{ENTITY}::text), 1, 6))::bit(24)::int % 997 = 0 then 1 else 0 end as probe_constant
    from {src} c
    {tix_join}
    {sample_sql}
    """)
    db.execute(f"create index on {BASE} ({ENTITY}, {TS})")
    db.execute(f"analyze {BASE}")
    s = db.fetchone(f"""select count(*) n, count(distinct {ENTITY}) accounts, min({TS}) first_month, max({TS}) last_month,
                        avg(({LABEL})::int) filter (where label_complete) churn_rate,
                        count(*) filter (where label_complete) labelled from {BASE}""")
    s.update(source=src, table=BASE, seconds=round(time.time() - t0, 1), max_accounts=int(max_accounts or 0),
             nps_joined=has_nps, dispatches_joined=has_tix)
    if verbose:
        print(f"  integrated {s['n']:,} snapshot rows · {s['accounts']:,} accounts · {s['first_month']} → "
              f"{s['last_month']} · source {src} · {s['seconds']}s")
    return s


def ensure_base() -> None:
    if not db.table_exists(BASE):
        integrate(verbose=False)


def load(months: tuple | None = None, labelled_only: bool = False, columns: list[str] | None = None) -> pd.DataFrame:
    """Read the integrated base (booleans → 0/1 ints, month as datetime)."""
    ensure_base()
    where = []
    params: list = []
    if labelled_only:
        where.append("label_complete")
    if months:
        where.append(f"{TS} between %s and %s")
        params += [months[0], months[1]]
    cols = "*" if not columns else ", ".join(dict.fromkeys([ENTITY, TS, LABEL, "label_complete"] + columns))
    df = db.query_df(f"select {cols} from {BASE}" + (" where " + " and ".join(where) if where else ""), params or None)
    return coerce(df)


def coerce(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if TS in df:
        df[TS] = pd.to_datetime(df[TS])
    for c in BOOLEAN + [LABEL, "label_complete"]:
        if c in df:
            df[c] = df[c].astype("float").fillna(0).astype(int)
    for c in df.columns:
        if c in NUMERIC or c in DERIVED:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    for c in CATEGORICAL:
        if c in df:
            df[c] = df[c].astype("string").astype(object)
    return df


def months() -> list[pd.Timestamp]:
    ensure_base()
    rows = db.fetchall(f"select distinct {TS} m from {BASE} order by 1")
    return [pd.Timestamp(r["m"]) for r in rows]


def windows() -> dict:
    """The demo's time windows, derived from the data (no hard-coded dates):

    * shift  = the last 4 months (price-increase wave + outage cluster)
    * labelled = months whose 90-day label is complete
    * champion training = labelled months before the shift, minus 4 months
      held out as validation (2) and test (2)
    * replay ('normal' traffic) = the 4 months right before the shift
    """
    ms = months()
    labelled = [pd.Timestamp(r["m"]) for r in db.fetchall(
        f"select distinct {TS} m from {BASE} where label_complete order by 1")]
    shift = ms[-4:]
    pre = [m for m in labelled if m < shift[0]]
    return {
        "all": ms, "labelled": labelled, "shift": shift,
        "train": (pre[0], pre[-5]), "valid": (pre[-4], pre[-3]), "test": (pre[-2], pre[-1]),
        "normal_replay": (ms[-8], ms[-5]), "shift_replay": (shift[0], shift[-1]),
        "retrain_train": (labelled[0], labelled[-2]), "retrain_test": (labelled[-1], labelled[-1]),
        "latest": ms[-1],
    }


def summary_table() -> pd.DataFrame:
    """Churn rate and volume by month (for EDA and the shift story)."""
    ensure_base()
    return db.query_df(f"""
        select {TS}, count(*) customers, avg(({LABEL})::int) filter (where label_complete) churn_rate,
               avg((recent_price_increase)::int) price_increase_share, avg(outage_minutes) avg_outage_min,
               avg(tickets_month) avg_tickets, bool_and(label_complete) label_complete
        from {BASE} group by 1 order by 1""")


def X_y(df: pd.DataFrame, features: list[str]) -> tuple[pd.DataFrame, np.ndarray]:
    return df[features].copy(), df[LABEL].to_numpy().astype(int)
