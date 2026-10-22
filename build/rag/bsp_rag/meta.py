"""Metadata store — Postgres schema `rag` (plan §3) or SQLite (tests/notebooks).

Tables: collections, documents, chunks (+ `vector` via pgvector, + a generated
`tsv` for full-text search on Postgres), queries, eval_runs. Chunks are the
source of truth for text; the vector backend holds vectors + payload.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

log = logging.getLogger("bsp_rag.meta")

JSON_COLS = {"settings", "meta", "filters", "top_ids", "scores", "model_route", "config", "metrics", "per_question", "tags"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


PG_DDL = """
CREATE SCHEMA IF NOT EXISTS rag;
CREATE TABLE IF NOT EXISTS rag.collections (
  id SERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '',
  backend TEXT NOT NULL, embed_model TEXT NOT NULL, embed_dim INT NOT NULL,
  chunker TEXT NOT NULL DEFAULT 'structure', chunk_size INT NOT NULL DEFAULT 380,
  settings JSONB DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS rag.documents (
  id SERIAL PRIMARY KEY, collection_id INT NOT NULL REFERENCES rag.collections(id) ON DELETE CASCADE,
  doc_key TEXT, title TEXT, source TEXT, uri TEXT NOT NULL, content_hash TEXT, etag TEXT, last_modified TEXT,
  doc_type TEXT, system TEXT, owner TEXT, version TEXT, effective_date TEXT, acl_group TEXT,
  meta JSONB DEFAULT '{}'::jsonb, n_chunks INT DEFAULT 0, status TEXT DEFAULT 'indexed', error TEXT,
  ingested_at TIMESTAMPTZ DEFAULT now(), updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (collection_id, uri));
CREATE TABLE IF NOT EXISTS rag.chunks (
  id TEXT PRIMARY KEY, collection_id INT NOT NULL REFERENCES rag.collections(id) ON DELETE CASCADE,
  document_id INT NOT NULL REFERENCES rag.documents(id) ON DELETE CASCADE,
  ordinal INT, section TEXT, kind TEXT, text TEXT, body TEXT, tokens INT, content_hash TEXT,
  meta JSONB DEFAULT '{}'::jsonb, embedding REAL[],
  tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(text, ''))) STORED);
CREATE INDEX IF NOT EXISTS chunks_coll_idx ON rag.chunks (collection_id);
CREATE INDEX IF NOT EXISTS chunks_doc_idx ON rag.chunks (document_id);
CREATE INDEX IF NOT EXISTS chunks_tsv_idx ON rag.chunks USING gin (tsv);
CREATE TABLE IF NOT EXISTS rag.queries (
  id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), collection TEXT, username TEXT, mode TEXT,
  query TEXT, rewritten TEXT, filters JSONB, top_ids JSONB, scores JSONB, latency_ms INT,
  model_route JSONB, answer TEXT, n_citations INT, status TEXT);
CREATE TABLE IF NOT EXISTS rag.eval_runs (
  id SERIAL PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now(), collection TEXT, label TEXT, config JSONB,
  metrics JSONB, per_question JSONB, n INT, judge TEXT);
"""

SQLITE_DDL = """
CREATE TABLE IF NOT EXISTS collections (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, description TEXT DEFAULT '',
  backend TEXT NOT NULL, embed_model TEXT NOT NULL, embed_dim INT NOT NULL,
  chunker TEXT NOT NULL DEFAULT 'structure', chunk_size INT NOT NULL DEFAULT 380,
  settings TEXT DEFAULT '{}', created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY AUTOINCREMENT, collection_id INT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  doc_key TEXT, title TEXT, source TEXT, uri TEXT NOT NULL, content_hash TEXT, etag TEXT, last_modified TEXT,
  doc_type TEXT, system TEXT, owner TEXT, version TEXT, effective_date TEXT, acl_group TEXT,
  meta TEXT DEFAULT '{}', n_chunks INT DEFAULT 0, status TEXT DEFAULT 'indexed', error TEXT,
  ingested_at TEXT, updated_at TEXT, UNIQUE (collection_id, uri));
CREATE TABLE IF NOT EXISTS chunks (
  id TEXT PRIMARY KEY, collection_id INT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  document_id INT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  ordinal INT, section TEXT, kind TEXT, text TEXT, body TEXT, tokens INT, content_hash TEXT,
  meta TEXT DEFAULT '{}', embedding TEXT);
CREATE TABLE IF NOT EXISTS queries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, collection TEXT, username TEXT, mode TEXT,
  query TEXT, rewritten TEXT, filters TEXT, top_ids TEXT, scores TEXT, latency_ms INT,
  model_route TEXT, answer TEXT, n_citations INT, status TEXT);
CREATE TABLE IF NOT EXISTS eval_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, collection TEXT, label TEXT, config TEXT,
  metrics TEXT, per_question TEXT, n INT, judge TEXT);
"""


def _jsonable(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


class Meta:
    def __init__(self, kind: str | None = None, sqlite_path: str | None = None):
        cfg = config.get()
        self.kind = (kind or cfg.meta_store)
        self._lock = threading.RLock()
        self.pgvector = False
        if self.kind == "sqlite":
            path = sqlite_path or cfg.sqlite_path
            if path != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._sq = sqlite3.connect(path, check_same_thread=False)
            self._sq.row_factory = sqlite3.Row
            self._sq.execute("PRAGMA foreign_keys=ON")
            self._sq.executescript(SQLITE_DDL)
            self.prefix = ""
        else:
            self.prefix = "rag."
            self._pg_init()

    # ── connection plumbing ─────────────────────────────────────────────
    def _pg_connect(self):
        import psycopg2

        from bsp_common import settings

        return psycopg2.connect(settings.get().pg_dsn)

    def _pg_init(self) -> None:
        conn = self._pg_connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(PG_DDL)
            # pgvector is optional: the platform enables it in the pgvector image
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as e:  # noqa: BLE001 — not superuser / extension missing
                log.info("CREATE EXTENSION vector failed (%s); checking whether it already exists", e)
            with conn, conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_extension WHERE extname='vector'")
                self.pgvector = cur.fetchone() is not None
                if self.pgvector:
                    cur.execute("ALTER TABLE rag.chunks ADD COLUMN IF NOT EXISTS vector vector")
        finally:
            conn.close()
        self._pg = self._pg_connect()

    @contextmanager
    def cursor(self) -> Iterator[Any]:
        with self._lock:
            if self.kind == "sqlite":
                cur = self._sq.cursor()
                try:
                    yield cur
                    self._sq.commit()
                except Exception:
                    self._sq.rollback()
                    raise
                return
            if self._pg.closed:
                self._pg = self._pg_connect()
            try:
                with self._pg.cursor() as cur:
                    yield cur
                self._pg.commit()
            except Exception as e:
                try:
                    self._pg.rollback()
                except Exception:  # noqa: BLE001
                    pass
                import psycopg2

                if isinstance(e, (psycopg2.OperationalError, psycopg2.InterfaceError)):
                    self._pg = self._pg_connect()
                raise

    def t(self, name: str) -> str:
        return self.prefix + name

    def q(self, sql: str) -> str:
        return sql.replace("%s", "?") if self.kind == "sqlite" else sql

    def _rows(self, cur) -> list[dict[str, Any]]:
        if cur.description is None:
            return []
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            for k, v in d.items():
                if k in JSON_COLS and isinstance(v, str):
                    try:
                        d[k] = json.loads(v)
                    except ValueError:
                        pass
                elif isinstance(v, (datetime, date)):
                    d[k] = v.isoformat()
            out.append(d)
        return out

    def all(self, sql: str, params: tuple | list = ()) -> list[dict[str, Any]]:
        with self.cursor() as cur:
            cur.execute(self.q(sql), tuple(params))
            return self._rows(cur)

    def one(self, sql: str, params: tuple | list = ()) -> dict[str, Any] | None:
        rows = self.all(sql, params)
        return rows[0] if rows else None

    def run(self, sql: str, params: tuple | list = ()) -> None:
        with self.cursor() as cur:
            cur.execute(self.q(sql), tuple(params))

    @staticmethod
    def j(v: Any) -> str:
        return json.dumps(v, default=_jsonable)

    # ── collections ─────────────────────────────────────────────────────
    def collections(self) -> list[dict[str, Any]]:
        rows = self.all(f"SELECT * FROM {self.t('collections')} ORDER BY name")
        stats = {r["collection_id"]: r for r in self.all(
            f"SELECT collection_id, COUNT(*) AS n_chunks, SUM(tokens) AS n_tokens FROM {self.t('chunks')} GROUP BY collection_id")}
        docs = {r["collection_id"]: r for r in self.all(
            f"SELECT collection_id, COUNT(*) AS n_docs, MAX(updated_at) AS last_ingest FROM {self.t('documents')} GROUP BY collection_id")}
        for r in rows:
            s, d = stats.get(r["id"], {}), docs.get(r["id"], {})
            r["n_chunks"] = int(s.get("n_chunks") or 0)
            r["n_tokens"] = int(s.get("n_tokens") or 0)
            r["n_docs"] = int(d.get("n_docs") or 0)
            r["last_ingest"] = d.get("last_ingest")
        return rows

    def collection(self, name: str) -> dict[str, Any] | None:
        return self.one(f"SELECT * FROM {self.t('collections')} WHERE name=%s", (name,))

    def create_collection(self, name: str, backend: str, embed_model: str, embed_dim: int, chunker: str,
                          chunk_size: int, description: str = "", settings: dict | None = None) -> dict[str, Any]:
        ts = now()
        self.run(f"INSERT INTO {self.t('collections')} (name, description, backend, embed_model, embed_dim, chunker, "
                 f"chunk_size, settings, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                 (name, description, backend, embed_model, embed_dim, chunker, chunk_size, self.j(settings or {}), ts, ts))
        return self.collection(name)  # type: ignore[return-value]

    def delete_collection(self, cid: int) -> None:
        self.run(f"DELETE FROM {self.t('chunks')} WHERE collection_id=%s", (cid,))
        self.run(f"DELETE FROM {self.t('documents')} WHERE collection_id=%s", (cid,))
        self.run(f"DELETE FROM {self.t('collections')} WHERE id=%s", (cid,))

    # ── documents ───────────────────────────────────────────────────────
    def documents(self, cid: int) -> list[dict[str, Any]]:
        return self.all(f"SELECT id, doc_key, title, source, uri, content_hash, etag, last_modified, doc_type, system, "
                        f"owner, version, effective_date, acl_group, n_chunks, status, error, updated_at "
                        f"FROM {self.t('documents')} WHERE collection_id=%s ORDER BY doc_type, title", (cid,))

    def document_by_uri(self, cid: int, uri: str) -> dict[str, Any] | None:
        return self.one(f"SELECT * FROM {self.t('documents')} WHERE collection_id=%s AND uri=%s", (cid, uri))

    def document(self, did: int) -> dict[str, Any] | None:
        return self.one(f"SELECT * FROM {self.t('documents')} WHERE id=%s", (did,))

    def upsert_document(self, cid: int, d: dict[str, Any]) -> int:
        ts = now()
        cols = ["doc_key", "title", "source", "uri", "content_hash", "etag", "last_modified", "doc_type", "system",
                "owner", "version", "effective_date", "acl_group", "meta", "n_chunks", "status", "error"]
        def val(c: str) -> Any:
            v = d.get(c)
            if c == "meta":
                return self.j(v or {})
            if c == "n_chunks":
                return int(v or 0)
            return None if v is None else _jsonable(v) if isinstance(v, (date, datetime)) else str(v)
        vals = [val(c) for c in cols]
        existing = self.document_by_uri(cid, d["uri"])
        if existing:
            sets = ", ".join(f"{c}=%s" for c in cols)
            self.run(f"UPDATE {self.t('documents')} SET {sets}, updated_at=%s WHERE id=%s", (*vals, ts, existing["id"]))
            return int(existing["id"])
        with self.cursor() as cur:
            cur.execute(self.q(f"INSERT INTO {self.t('documents')} (collection_id, {', '.join(cols)}, ingested_at, updated_at) "
                               f"VALUES (%s, {', '.join(['%s'] * len(cols))}, %s, %s) RETURNING id"), (cid, *vals, ts, ts))
            return int(cur.fetchone()[0])

    def delete_document(self, did: int) -> None:
        self.run(f"DELETE FROM {self.t('chunks')} WHERE document_id=%s", (did,))
        self.run(f"DELETE FROM {self.t('documents')} WHERE id=%s", (did,))

    # ── chunks ──────────────────────────────────────────────────────────
    def replace_chunks(self, cid: int, did: int, rows: list[dict[str, Any]]) -> None:
        with self.cursor() as cur:
            cur.execute(self.q(f"DELETE FROM {self.t('chunks')} WHERE document_id=%s"), (did,))
            sql = self.q(f"INSERT INTO {self.t('chunks')} (id, collection_id, document_id, ordinal, section, kind, text, "
                         f"body, tokens, content_hash, meta) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)")
            cur.executemany(sql, [(r["id"], cid, did, r["ordinal"], r["section"], r["kind"], r["text"], r["body"],
                                   r["tokens"], r["content_hash"], self.j(r.get("meta") or {})) for r in rows])

    def chunk_ids_for_doc(self, did: int) -> list[str]:
        return [r["id"] for r in self.all(f"SELECT id FROM {self.t('chunks')} WHERE document_id=%s", (did,))]

    def chunks(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        ph = ",".join(["%s"] * len(ids))
        rows = self.all(f"SELECT c.id, c.ordinal, c.section, c.kind, c.text, c.body, c.tokens, c.meta, c.document_id, "
                        f"d.title, d.doc_key, d.doc_type, d.system, d.uri, d.source, d.version, d.effective_date, d.owner, d.acl_group "
                        f"FROM {self.t('chunks')} c JOIN {self.t('documents')} d ON d.id=c.document_id WHERE c.id IN ({ph})", ids)
        return {r["id"]: r for r in rows}

    def doc_chunks(self, did: int) -> list[dict[str, Any]]:
        return self.all(f"SELECT id, ordinal, section, kind, body, tokens FROM {self.t('chunks')} WHERE document_id=%s ORDER BY ordinal", (did,))

    # ── logs ────────────────────────────────────────────────────────────
    def log_query(self, row: dict[str, Any]) -> None:
        try:
            self.run(f"INSERT INTO {self.t('queries')} (ts, collection, username, mode, query, rewritten, filters, top_ids, "
                     f"scores, latency_ms, model_route, answer, n_citations, status) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                     (now(), row.get("collection"), row.get("user"), row.get("mode"), row.get("query"), row.get("rewritten"),
                      self.j(row.get("filters") or {}), self.j(row.get("top_ids") or []), self.j(row.get("scores") or []),
                      int(row.get("latency_ms") or 0), self.j(row.get("model_route") or {}), row.get("answer"),
                      int(row.get("n_citations") or 0), row.get("status", "ok")))
        except Exception as e:  # noqa: BLE001 — logging must never break a query
            log.warning("query log failed: %s", e)

    def queries(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.all(f"SELECT * FROM {self.t('queries')} ORDER BY id DESC LIMIT %s", (limit,))

    def save_eval(self, row: dict[str, Any]) -> int:
        with self.cursor() as cur:
            cur.execute(self.q(f"INSERT INTO {self.t('eval_runs')} (ts, collection, label, config, metrics, per_question, n, judge) "
                               f"VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id"),
                        (now(), row["collection"], row.get("label", ""), self.j(row["config"]), self.j(row["metrics"]),
                         self.j(row.get("per_question") or []), row.get("n", 0), row.get("judge", "lexical")))
            return int(cur.fetchone()[0])

    def evals(self, limit: int = 200) -> list[dict[str, Any]]:
        return self.all(f"SELECT id, ts, collection, label, config, metrics, n, judge FROM {self.t('eval_runs')} "
                        f"ORDER BY id DESC LIMIT %s", (limit,))

    def eval_run(self, rid: int) -> dict[str, Any] | None:
        return self.one(f"SELECT * FROM {self.t('eval_runs')} WHERE id=%s", (rid,))


_meta: Meta | None = None
_meta_lock = threading.Lock()


def get() -> Meta:
    global _meta
    with _meta_lock:
        if _meta is None:
            _meta = Meta()
        return _meta


def set_instance(m: Meta | None) -> None:
    global _meta
    _meta = m
