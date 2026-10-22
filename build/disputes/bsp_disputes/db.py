"""Schema `disputes` (inbox_messages, cases, brim_submissions) + helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

DDL = """
create schema if not exists disputes;
create table if not exists disputes.inbox_messages (
  id               bigserial primary key,
  mailbox          text not null,
  graph_id         text not null,
  internet_message_id text,
  conversation_id  text,
  subject          text,
  sender_name      text,
  sender_email     text,
  received_at      timestamptz,
  body_text        text,
  body_html        text,
  headers          jsonb default '{}'::jsonb,
  attachments      jsonb default '[]'::jsonb,
  triage           text,
  moved_to         text,
  ingested_at      timestamptz default now(),
  unique (mailbox, graph_id)
);
create table if not exists disputes.cases (
  id               bigserial primary key,
  case_ref         text unique,
  message_id       bigint references disputes.inbox_messages(id),
  status           text not null,
  fields           jsonb not null default '{}'::jsonb,
  original_fields  jsonb not null default '{}'::jsonb,
  confidence       jsonb not null default '{}'::jsonb,
  evidence         jsonb not null default '{}'::jsonb,
  validation       jsonb not null default '{}'::jsonb,
  overall_confidence double precision,
  extraction_method text,
  auto_approved    boolean default false,
  assignee         text,
  approved_by      text,
  approved_at      timestamptz,
  rejected_reason  text,
  sap_dispute_id   text,
  sap_url          text,
  sla_due_at       timestamptz,
  submitted_at     timestamptz,
  history          jsonb not null default '[]'::jsonb,
  created_at       timestamptz default now(),
  updated_at       timestamptz default now()
);
create index if not exists cases_status_idx on disputes.cases(status);
create table if not exists disputes.brim_submissions (
  id               bigserial primary key,
  case_id          bigint references disputes.cases(id),
  batch_id         text,
  mode             text,
  adapter          text,
  status           text,
  sap_dispute_id   text,
  url              text,
  worker           int,
  attempts         int,
  started_at       timestamptz,
  finished_at      timestamptz,
  duration_ms      int,
  error            text,
  steps            jsonb default '[]'::jsonb,
  screenshots      jsonb default '[]'::jsonb,
  request          jsonb,
  response         jsonb
);
create index if not exists brim_sub_case_idx on disputes.brim_submissions(case_id);
"""


@contextmanager
def cursor() -> Iterator[Any]:
    import psycopg2.extras
    from bsp_common.db import connect

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            yield cur


def J(v: Any):
    import json

    import psycopg2.extras
    return psycopg2.extras.Json(v, dumps=lambda o: json.dumps(o, default=str))


def init() -> None:
    with cursor() as cur:
        cur.execute("select pg_advisory_xact_lock(84099)")
        cur.execute(DDL)


def rows(sql: str, params: Any = None) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()] if cur.description else []


def one(sql: str, params: Any = None) -> dict | None:
    r = rows(sql, params)
    return r[0] if r else None
