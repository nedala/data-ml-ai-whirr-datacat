"""Postgres access for schema `ccai` (warehouse `bsp_edw`, plan §3).

Contract tables: ingest_runs, calls, call_tags, taxonomy. Additive helpers:
source_files (per-item sync state → incremental + idempotent ingest) and
settings (portal-editable settings)."""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg2
import psycopg2.extras
import psycopg2.pool

from bsp_common import settings as bsp_settings

log = logging.getLogger("ccai.db")

DDL = """
create schema if not exists ccai;

create table if not exists ccai.taxonomy (
  leaf_id text not null, version text not null,
  l1 text not null, l1_name text not null, l2 text not null, l2_name text not null,
  name text not null, definition text, include text, exclude text,
  examples jsonb, keywords jsonb, mapping jsonb, churn_risk boolean default false,
  digest text, loaded_at timestamptz default now(),
  primary key (leaf_id, version));

create table if not exists ccai.ingest_runs (
  run_id bigserial primary key,
  trigger text not null,               -- manual | schedule | retry | api
  mode text not null default 'incremental',  -- incremental | full | retry_failed | retag
  status text not null default 'running',    -- running | succeeded | partial | failed | cancelled | interrupted
  phase text, started_at timestamptz default now(), finished_at timestamptz,
  source_mode text, site text, folder text,
  files_discovered int default 0, files_new int default 0, files_changed int default 0,
  files_skipped int default 0, files_downloaded int default 0, manifests int default 0,
  calls_parsed int default 0, calls_tagged int default 0, errors int default 0,
  bytes_downloaded bigint default 0,
  list_ms int, download_ms int, parse_ms int, tag_ms int, duration_ms int,
  route text, taxonomy_version text, message text, params jsonb);

create table if not exists ccai.source_files (
  item_id text primary key, site text, path text not null, name text not null,
  size bigint, last_modified timestamptz, etag text, web_url text,
  kind text,                           -- call | manifest | unsupported
  format text, status text not null,   -- downloaded | parsed | skipped | error
  error text, local_path text, sha256 text, call_id text, meta jsonb,
  first_run_id bigint, last_run_id bigint, updated_at timestamptz default now());

create table if not exists ccai.calls (
  call_id text primary key, item_id text, file_name text, source_path text, format text,
  run_id bigint, started_at timestamptz, duration_sec double precision,
  agent_id text, queue text, ani_masked text, ban text, language text,
  turn_count int, agent_talk_sec double precision, customer_talk_sec double precision,
  agent_talk_pct double precision, silence_sec double precision, silence_pct double precision,
  avg_customer_sentiment double precision, sentiment_start double precision, sentiment_end double precision,
  turns jsonb, meta jsonb, transcript_text text,
  status text not null default 'parsed',    -- parsed | tagged | tag_error
  error text, ingested_at timestamptz default now(), tagged_at timestamptz);
alter table ccai.calls add column if not exists ban text;   -- BAN (10 digits), see plan §3 data model
create index if not exists calls_started_idx on ccai.calls (started_at);
create index if not exists calls_ban_idx on ccai.calls (ban);
create index if not exists calls_status_idx on ccai.calls (status);

create table if not exists ccai.call_tags (
  tag_id bigserial primary key, call_id text not null references ccai.calls(call_id) on delete cascade,
  run_id bigint, is_current boolean not null default true,
  primary_leaf text not null, secondary jsonb default '[]', candidates jsonb default '[]',
  confidence double precision, confidence_band text, signals jsonb,
  rationale text, evidence_turns jsonb default '[]', summary text,
  sentiment_trajectory text, sentiment_series jsonb, resolution_status text,
  churn_risk boolean default false, churn_reasons jsonb default '[]',
  model text, route text, taxonomy_version text, latency_ms int,
  created_at timestamptz default now(),
  reviewed_by text, reviewed_at timestamptz, override_leaf text, review_note text);
create index if not exists call_tags_current_idx on ccai.call_tags (call_id) where is_current;
create index if not exists call_tags_leaf_idx on ccai.call_tags (primary_leaf) where is_current;

create table if not exists ccai.settings (key text primary key, value jsonb, updated_at timestamptz default now());
"""

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
_pool_lock = threading.Lock()


def pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(1, 12, bsp_settings.get().pg_dsn, connect_timeout=5)
        return _pool


@contextmanager
def conn() -> Iterator[Any]:
    p = pool()
    c = p.getconn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        p.putconn(c, close=bool(c.closed))


def q(sql: str, params: Any = None) -> list[dict]:
    with conn() as c, c.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def one(sql: str, params: Any = None) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def ex(sql: str, params: Any = None) -> int:
    with conn() as c, c.cursor() as cur:
        cur.execute(sql, params)
        return cur.rowcount


def J(v: Any) -> psycopg2.extras.Json:
    return psycopg2.extras.Json(v, dumps=lambda o: json.dumps(o, default=str))


def init_schema(wait_s: float = 90) -> None:
    """Create schema/tables; wait for Postgres to come up (compose start order is not guaranteed)."""
    deadline = time.time() + wait_s
    while True:
        try:
            with conn() as c, c.cursor() as cur:
                cur.execute("select pg_advisory_lock(820100)")
                try:
                    cur.execute(DDL)
                finally:
                    cur.execute("select pg_advisory_unlock(820100)")
            return
        except psycopg2.OperationalError as e:
            global _pool
            _pool = None
            if time.time() > deadline:
                raise
            log.warning("waiting for postgres: %s", str(e).strip().splitlines()[0] if str(e).strip() else e)
            time.sleep(3)


def healthy() -> bool:
    try:
        one("select 1 as ok")
        return True
    except Exception:
        return False


# ── settings ─────────────────────────────────────────────────────────────
def get_settings(defaults: dict) -> dict:
    out = dict(defaults)
    try:
        for r in q("select key, value from ccai.settings"):
            if r["key"] in out:
                out[r["key"]] = r["value"]
    except Exception:
        pass
    return out


def put_settings(values: dict) -> None:
    for k, v in values.items():
        ex("insert into ccai.settings(key, value, updated_at) values (%s, %s, now()) "
           "on conflict (key) do update set value = excluded.value, updated_at = now()", (k, J(v)))
