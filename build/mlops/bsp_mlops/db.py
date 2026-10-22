"""Warehouse access + the `mlops` / `features` schema DDL (architecture §3).

Tables (schema `mlops`): inference_log, deployments, drift_reports,
retrain_runs, frozen_artifacts, scored_customers (+ view v_score_outcomes),
and the release / governance tables: promotions (gated Dev → Staging → Prod
moves, approvals, rollbacks), audit_log (who did what), alerts (incident
workflow), label_evaluations (delayed-label performance), experiments
(champion / challenger and business A/B results), reproductions, lifecycle_runs.
All DDL is idempotent; every service calls `ensure_schema()` on start."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import Any, Iterator

DDL = r"""
create schema if not exists mlops;
create schema if not exists features;

create table if not exists mlops.inference_log (
    id             bigserial primary key,
    request_id     text not null,
    ts             timestamptz not null default now(),
    deployment     text not null,
    mode           text not null,
    model_name     text,
    model_version  text,
    alias          text,
    colour         text,
    ban     text,
    snapshot_month date,
    features       jsonb,
    score          double precision,
    latency_ms     double precision,
    shadow         boolean not null default false,
    source         text
);
create index if not exists inference_log_ts on mlops.inference_log (ts);
create index if not exists inference_log_dep_ts on mlops.inference_log (deployment, ts);
create index if not exists inference_log_req on mlops.inference_log (request_id);

create table if not exists mlops.deployments (
    deployment     text primary key,
    model_name     text not null,
    mode           text not null default 'single',
    blue_version   text,
    green_version  text,
    live_color     text not null default 'blue',
    canary_weight  double precision not null default 0.1,
    shadow_enabled boolean not null default false,
    updated_at     timestamptz not null default now(),
    updated_by     text,
    history        jsonb not null default '[]'::jsonb
);

create table if not exists mlops.drift_reports (
    id                  bigserial primary key,
    created_at          timestamptz not null default now(),
    deployment          text,
    model_version       text,
    window_start        timestamptz,
    window_end          timestamptz,
    n_reference         integer,
    n_current           integer,
    feature_drift       jsonb,
    prediction_psi      double precision,
    max_psi             double precision,
    n_features_breached integer,
    auc_reference       double precision,
    auc_current         double precision,
    n_labeled           integer,
    breached            boolean not null default false,
    breach_reasons      jsonb,
    triggered_retrain_id bigint,
    source              text
);

create table if not exists mlops.retrain_runs (
    id               bigserial primary key,
    created_at       timestamptz not null default now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    status           text not null default 'queued',
    trigger          text not null,
    reason           text,
    drift_report_id  bigint,
    mlflow_run_id    text,
    model_version    text,
    champion_version text,
    challenger_auc   double precision,
    champion_auc     double precision,
    margin           double precision,
    promoted         boolean not null default false,
    decision         text,
    shadow_started   boolean not null default false,
    log_path         text,
    details          jsonb
);

create table if not exists mlops.frozen_artifacts (
    id            bigserial primary key,
    created_at    timestamptz not null default now(),
    model_name    text not null,
    model_version text not null,
    mlflow_run_id text,
    bundle_name   text not null,
    sha256        text not null,
    size_bytes    bigint,
    repository    text,
    uri           text,
    manifest      jsonb,
    created_by    text
);

create table if not exists mlops.scored_customers (
    ban     text not null,
    snapshot_month date not null,
    score          double precision not null,
    decile         integer not null,
    risk_band      text,
    model_name     text not null,
    model_version  text not null,
    run_id         text,
    reason_1       text,
    reason_2       text,
    reason_3       text,
    reason_codes   jsonb,
    batch_id       text,
    scored_at      timestamptz not null default now(),
    primary key (ban, snapshot_month, model_version)
);
create index if not exists scored_customers_month on mlops.scored_customers (snapshot_month);

alter table mlops.inference_log add column if not exists caller text;

create table if not exists mlops.promotions (
    id                   bigserial primary key,
    created_at           timestamptz not null default now(),
    updated_at           timestamptz not null default now(),
    model_name           text not null,
    model_version        text not null,
    action               text not null,           -- promote | rollback | retire
    from_stage           text,
    to_stage             text not null,           -- staging | production | archived
    status               text not null,           -- gates_failed | completed | pending_approval | approved | rejected | superseded
    requested_by         text,
    requested_role       text,
    requested_source     text,                    -- sso-gate | cli | notebook | automation
    approved_by          text,
    approved_role        text,
    approved_source      text,
    approved_at          timestamptz,
    decision_note        text,
    baseline_version     text,
    champion_before      text,
    champion_after       text,
    gates                jsonb,
    gates_passed         boolean,
    holdout              jsonb,
    git_sha              text,
    code_sha256          text,
    data_snapshot_sha256 text,
    env_lock_sha256      text,
    artifact_sha256      text,
    mlflow_run_id        text,
    evidence_run_id      text,
    parent_id            bigint,
    details              jsonb
);
create index if not exists promotions_version on mlops.promotions (model_name, model_version);

create table if not exists mlops.audit_log (
    id        bigserial primary key,
    ts        timestamptz not null default now(),
    actor     text,
    role      text,
    source    text,
    action    text not null,
    target    text,
    details   jsonb
);
create index if not exists audit_log_ts on mlops.audit_log (ts);

create table if not exists mlops.alerts (
    id          bigserial primary key,
    created_at  timestamptz not null default now(),
    kind        text not null,                    -- drift | performance | serving_divergence | gate_failure | slo
    severity    text not null default 'warning',  -- info | warning | critical
    title       text not null,
    details     jsonb,
    status      text not null default 'open',     -- open | acknowledged | resolved
    owner       text,
    acked_by    text,
    acked_at    timestamptz,
    resolved_by text,
    resolved_at timestamptz,
    resolution  text,
    retrain_id  bigint,
    dedupe_key  text
);
create index if not exists alerts_open on mlops.alerts (status, created_at);

create table if not exists mlops.label_evaluations (
    id              bigserial primary key,
    created_at      timestamptz not null default now(),
    deployment      text,
    n_scored        integer,
    n_labelled      integer,
    n_pending       integer,
    result          jsonb,
    breached        boolean not null default false,
    source          text
);

create table if not exists mlops.experiments (
    id                 bigserial primary key,
    created_at         timestamptz not null default now(),
    kind               text not null,             -- shadow_paired | canary | ab_retention_offer
    name               text,
    champion_version   text,
    challenger_version text,
    status             text,
    result             jsonb,
    created_by         text
);

create table if not exists mlops.reproductions (
    id               bigserial primary key,
    created_at       timestamptz not null default now(),
    finished_at      timestamptz,
    model_version    text not null,
    source_run_id    text,
    repro_run_id     text,
    status           text not null default 'running',
    parity           text,                        -- identical | within_tolerance | diverged
    data_hash_match  boolean,
    code_match       boolean,
    env_match        boolean,
    metrics          jsonb,
    env_diff         jsonb,
    details          jsonb,
    created_by       text
);

create table if not exists mlops.lifecycle_runs (
    id           bigserial primary key,
    created_at   timestamptz not null default now(),
    finished_at  timestamptz,
    status       text not null default 'running',
    requested_by text,
    args         jsonb,
    log_path     text,
    pid          integer,
    summary      jsonb
);
"""

_lock = threading.Lock()
_done = False


def dsn() -> str:
    try:
        from bsp_common import settings

        return settings.get().pg_dsn
    except Exception:  # pragma: no cover - bsp_common always on PYTHONPATH in the appliance
        e = os.environ.get
        return (f"host={e('PG_HOST', 'postgres')} port={e('PG_PORT', '5432')} dbname={e('PG_DB', 'bsp_edw')} "
                f"user={e('PG_USER', 'analyst')} password={e('PG_PASS', '')}")


def sa_url() -> str:
    e = os.environ.get
    return (f"postgresql+psycopg2://{e('PG_USER', 'analyst')}:{e('PG_PASS', '')}@{e('PG_HOST', 'postgres')}:"
            f"{e('PG_PORT', '5432')}/{e('PG_DB', 'bsp_edw')}")


@contextmanager
def connect() -> Iterator[Any]:
    import psycopg2

    conn = psycopg2.connect(dsn())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def execute(sql: str, params: Any = None) -> None:
    with connect() as c, c.cursor() as cur:
        cur.execute(sql, params)


def fetchall(sql: str, params: Any = None) -> list[dict]:
    import psycopg2.extras

    with connect() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def fetchone(sql: str, params: Any = None) -> dict | None:
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def query_df(sql: str, params: Any = None):
    import pandas as pd

    with connect() as c:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pandas prefers SQLAlchemy; psycopg2 is fine here
            return pd.read_sql_query(sql, c, params=params)


def table_exists(qualified: str) -> bool:
    row = fetchone("select to_regclass(%s) is not null as ok", (qualified,))
    return bool(row and row["ok"])


def columns_of(qualified: str) -> list[str]:
    schema, table = qualified.split(".", 1)
    rows = fetchall("select column_name from information_schema.columns where table_schema=%s and table_name=%s "
                    "order by ordinal_position", (schema, table))
    return [r["column_name"] for r in rows]


def ensure_schema(force: bool = False) -> None:
    global _done
    with _lock:
        if _done and not force:
            return
        migrate_key()
        execute(DDL)
        ensure_monitoring_view()
        _done = True


KEYED_TABLES = ("mlops.inference_log", "mlops.scored_customers", "features.online_features")


def migrate_key() -> None:
    """Warehouse contract change account_id → ban (plan/architecture.md §3):
    rename the key column in place (non-destructive, idempotent)."""
    for t in KEYED_TABLES:
        schema, table = t.split(".")
        cols = set(columns_of(t)) if table_exists(t) else set()
        if "account_id" in cols and "ban" not in cols:
            if t == "mlops.scored_customers":
                execute("drop view if exists mlops.v_score_outcomes")
            execute(f"alter table {t} rename column account_id to ban")


def ensure_monitoring_view() -> None:
    """`mlops.v_score_outcomes` — scores joined with realised churn once the
    90-day label window has closed (for monitoring and BI)."""
    src = "raw.customer_monthly"
    if not table_exists(src):
        return
    body = f"""
    select s.ban, s.snapshot_month, s.score, s.decile, s.risk_band, s.model_name, s.model_version,
           s.reason_1, s.reason_2, s.reason_3, s.scored_at,
           c.technology, c.segment, c.state,
           case when c.label_complete then c.churned_next_90d end as churned_next_90d,
           coalesce(c.label_complete, false) as outcome_known
    from mlops.scored_customers s
    left join {src} c on c.ban = s.ban and c.snapshot_month = s.snapshot_month
    """
    try:
        execute("create or replace view mlops.v_score_outcomes as " + body)
    except Exception:          # column set changed (e.g. account_id → ban): recreate
        execute("drop view if exists mlops.v_score_outcomes")
        execute("create view mlops.v_score_outcomes as " + body)


def audit(action: str, target: str = "", actor: str | None = None, role: str | None = None, source: str | None = None,
          **details) -> None:
    """Append-only audit trail (mlops.audit_log). Never raises."""
    try:
        execute("insert into mlops.audit_log (actor, role, source, action, target, details) values (%s,%s,%s,%s,%s,%s)",
                (actor, role, source, action, str(target), jdump(details)))
    except Exception:
        pass


def jdump(o: Any) -> str:
    def default(x: Any):
        if hasattr(x, "isoformat"):
            return x.isoformat()
        if hasattr(x, "item"):
            return x.item()
        return str(x)

    return json.dumps(o, default=default)
