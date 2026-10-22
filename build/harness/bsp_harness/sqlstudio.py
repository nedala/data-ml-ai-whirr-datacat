"""Federated SQL tab: catalog browser, SQL run / explain, text-to-SQL, saved queries and collections.

* Catalog definitions come from the shared ``bsp_common.catalog`` library (DataHub). When DataHub is
  not configured or not answering, the panel is built from Trino itself (MCP ``trino_list`` /
  ``trino_query`` on information_schema) with name-based PII and glossary hints.
* SQL runs through the MCP session's ``trino_query`` / ``trino_explain`` tools, so read-only checks and
  the caller identity live in one place (agent.client(caller=...)).
* "Ask in English" reuses agent.run (tool groups trino + catalog, model ``sql-default``).
* Saved queries live on the appliance disk, per user, like history.py:
    $HARNESS_DATA_DIR/sqlstudio/users/<user-slug>/library.json   own queries, collections, run stats
    $HARNESS_DATA_DIR/sqlstudio/shared/<collection-id>.json      shared collections (owner edits)
    $HARNESS_DATA_DIR/sqlstudio/catalog_cache.json               Trino structure crawl (metadata only)
  Result rows are never written to disk: only SQL, notes, timings and row counts.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import agent, identity
from .config import data_dir
from .identity import user_of

log = logging.getLogger("bsp_harness.sqlstudio")
api = APIRouter()

ANON = "playground"
MAX_QUERIES = 2000
MAX_COLLECTIONS = 200
MAX_IMPORT = 500
MAX_SQL = 100_000
ASK_MODEL = "sql-default"
_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
_IDENT = re.compile(r"^[A-Za-z0-9_$-]{1,128}$")
_LOCK = threading.RLock()


# ══ storage ═══════════════════════════════════════════════════════════════════
def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _root() -> Path:
    d = data_dir() / "sqlstudio"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(user: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "_", user)[:80] + "-" + hashlib.sha256(user.encode()).hexdigest()[:8]


def _user_file(user: str) -> Path:
    d = _root() / "users" / _slug(user)
    d.mkdir(parents=True, exist_ok=True)
    return d / "library.json"


def _shared_dir() -> Path:
    d = _root() / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write(path: Path, doc: Any) -> None:
    """Atomic write: unique temp file in the same directory, then rename over the target."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(doc, indent=1, default=str))
    os.replace(tmp, path)


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _new_id(prefix: str) -> str:
    return prefix + secrets.token_hex(6)


def _s(v: Any, n: int) -> str:
    return str(v if v is not None else "").strip()[:n]


def _check_id(x: str, what: str = "id") -> str:
    if not isinstance(x, str) or not _ID.match(x):
        raise HTTPException(400, f"bad {what}")
    return x


def load_library(user: str) -> dict:
    doc = _read(_user_file(user), {})
    doc.setdefault("version", 1)
    doc.setdefault("queries", [])
    doc.setdefault("collections", [])
    doc.setdefault("runs", {})
    return doc


def save_library(user: str, doc: dict) -> None:
    _write(_user_file(user), doc)


def _query_fields(body: dict, base: dict | None = None) -> dict:
    q = dict(base or {})
    if "title" in body or not base:
        q["title"] = _s(body.get("title"), 200) or "Untitled query"
    if "sql" in body or not base:
        sql = str(body.get("sql") or "")
        if len(sql) > MAX_SQL:
            raise HTTPException(413, "query text is too long")
        q["sql"] = sql
    for k, n in (("note", 4000), ("question", 2000)):
        if k in body:
            q[k] = _s(body.get(k), n)
    for k in ("starred", "pinned"):
        if k in body:
            q[k] = bool(body.get(k))
    q.setdefault("note", "")
    q.setdefault("question", "")
    q.setdefault("starred", False)
    q.setdefault("pinned", False)
    return q


def _find(items: list[dict], iid: str) -> dict | None:
    return next((x for x in items if x.get("id") == iid), None)


# ── own queries ──────────────────────────────────────────────────────────────
def create_query(user: str, body: dict) -> dict:
    with _LOCK:
        lib = load_library(user)
        if len(lib["queries"]) >= MAX_QUERIES:
            raise HTTPException(409, f"saved query limit reached ({MAX_QUERIES})")
        q = _query_fields(body)
        q.update(id=_new_id("q"), created=_now(), updated=_now(), last_run=None, row_count=None, ms=None)
        lib["queries"].insert(0, q)
        _set_membership(lib, q["id"], body.get("collection_ids"))
        save_library(user, lib)
        return q


def _set_membership(lib: dict, qid: str, cids: Any) -> None:
    if cids is None:
        return
    want = {c for c in cids if isinstance(c, str)}
    for c in lib["collections"]:
        ids = c.setdefault("query_ids", [])
        if c["id"] in want and qid not in ids:
            ids.append(qid)
            c["updated"] = _now()
        elif c["id"] not in want and qid in ids:
            ids.remove(qid)
            c["updated"] = _now()


def update_query(user: str, qid: str, body: dict) -> dict:
    with _LOCK:
        lib = load_library(user)
        q = _find(lib["queries"], qid)
        if not q:
            raise HTTPException(404, "query not found")
        q.update(_query_fields(body, q))
        q["updated"] = _now()
        _set_membership(lib, qid, body.get("collection_ids"))
        save_library(user, lib)
        return q


def delete_query(user: str, qid: str) -> None:
    with _LOCK:
        lib = load_library(user)
        if not _find(lib["queries"], qid):
            raise HTTPException(404, "query not found")
        lib["queries"] = [q for q in lib["queries"] if q["id"] != qid]
        for c in lib["collections"]:
            if qid in c.get("query_ids", []):
                c["query_ids"].remove(qid)
        save_library(user, lib)


def duplicate_query(user: str, qid: str) -> dict:
    lib = load_library(user)
    q = _find(lib["queries"], qid)
    if not q:
        raise HTTPException(404, "query not found")
    cids = [c["id"] for c in lib["collections"] if qid in c.get("query_ids", [])]
    return create_query(user, {**q, "title": (q.get("title") or "Query") + " (copy)", "collection_ids": cids})


def _reorder(items: list[dict], ids: list[str]) -> list[dict]:
    pos = {i: n for n, i in enumerate(ids) if isinstance(i, str)}
    return sorted(items, key=lambda x: pos.get(x["id"], len(pos) + items.index(x)))


def reorder_queries(user: str, ids: list[str]) -> None:
    with _LOCK:
        lib = load_library(user)
        lib["queries"] = _reorder(lib["queries"], ids)
        save_library(user, lib)


# ── collections (own and shared) ─────────────────────────────────────────────
def _shared_path(cid: str) -> Path:
    return _shared_dir() / f"{_check_id(cid, 'collection id')}.json"


def load_shared(cid: str) -> dict | None:
    p = _shared_path(cid)
    return _read(p, None) if p.is_file() else None


def list_shared() -> list[dict]:
    ensure_seed()
    out = []
    for f in _shared_dir().glob("*.json"):
        d = _read(f, None)
        if isinstance(d, dict) and d.get("id"):
            out.append(d)
    out.sort(key=lambda d: (not d.get("seed"), (d.get("name") or "").lower()))
    return out


def _require_owner(doc: dict, user: str) -> None:
    if user == ANON or doc.get("owner") != user:
        raise HTTPException(403, "only the owner can change a shared collection")


def create_collection(user: str, body: dict) -> dict:
    name = _s(body.get("name"), 120)
    if not name:
        raise HTTPException(400, "name is required")
    if body.get("shared"):
        if user == ANON:
            raise HTTPException(403, "sign in to share a collection")
        doc = {"id": _new_id("s"), "name": name, "note": _s(body.get("note"), 4000), "owner": user, "shared": True,
               "queries": [], "created": _now(), "updated": _now()}
        with _LOCK:
            _write(_shared_path(doc["id"]), doc)
        return doc
    with _LOCK:
        lib = load_library(user)
        if len(lib["collections"]) >= MAX_COLLECTIONS:
            raise HTTPException(409, f"collection limit reached ({MAX_COLLECTIONS})")
        c = {"id": _new_id("c"), "name": name, "note": _s(body.get("note"), 4000), "query_ids": [],
             "created": _now(), "updated": _now()}
        lib["collections"].append(c)
        save_library(user, lib)
        return c


def update_collection(user: str, cid: str, body: dict) -> dict:
    with _LOCK:
        lib = load_library(user)
        c = _find(lib["collections"], cid)
        if c:
            if "name" in body:
                c["name"] = _s(body["name"], 120) or c["name"]
            if "note" in body:
                c["note"] = _s(body["note"], 4000)
            if isinstance(body.get("query_ids"), list):
                own = {q["id"] for q in lib["queries"]}
                c["query_ids"] = list(dict.fromkeys(i for i in body["query_ids"] if i in own))
            c["updated"] = _now()
            save_library(user, lib)
            return c
        doc = load_shared(cid)
        if not doc:
            raise HTTPException(404, "collection not found")
        _require_owner(doc, user)
        if "name" in body:
            doc["name"] = _s(body["name"], 120) or doc["name"]
        if "note" in body:
            doc["note"] = _s(body["note"], 4000)
        if isinstance(body.get("query_ids"), list):
            doc["queries"] = _reorder(doc["queries"], body["query_ids"])
        doc["updated"] = _now()
        _write(_shared_path(cid), doc)
        return doc


def delete_collection(user: str, cid: str) -> None:
    with _LOCK:
        lib = load_library(user)
        if _find(lib["collections"], cid):
            lib["collections"] = [c for c in lib["collections"] if c["id"] != cid]
            save_library(user, lib)
            return
        doc = load_shared(cid)
        if not doc:
            raise HTTPException(404, "collection not found")
        _require_owner(doc, user)
        _shared_path(cid).unlink(missing_ok=True)


def reorder_collections(user: str, ids: list[str]) -> None:
    with _LOCK:
        lib = load_library(user)
        lib["collections"] = _reorder(lib["collections"], ids)
        save_library(user, lib)


def collection_queries(user: str, cid: str) -> tuple[dict, list[dict], bool]:
    """(collection, its queries in order, shared?)"""
    lib = load_library(user)
    c = _find(lib["collections"], cid)
    if c:
        by = {q["id"]: q for q in lib["queries"]}
        return c, [by[i] for i in c.get("query_ids", []) if i in by], False
    doc = load_shared(cid)
    if not doc:
        raise HTTPException(404, "collection not found")
    return doc, list(doc.get("queries") or []), True


def add_to_collection(user: str, cid: str, body: dict) -> dict:
    """Own collection: link an own query. Shared collection (owner only): add a copy of an own query
    (body.query_id) or a new one (body.title / body.sql)."""
    with _LOCK:
        lib = load_library(user)
        c = _find(lib["collections"], cid)
        if c:
            qid = body.get("query_id")
            if not _find(lib["queries"], qid or ""):
                raise HTTPException(404, "query not found")
            if qid not in c.setdefault("query_ids", []):
                c["query_ids"].append(qid)
            c["updated"] = _now()
            save_library(user, lib)
            return c
        doc = load_shared(cid)
        if not doc:
            raise HTTPException(404, "collection not found")
        _require_owner(doc, user)
        src = _find(lib["queries"], body.get("query_id") or "") if body.get("query_id") else body
        if src is None:
            raise HTTPException(404, "query not found")
        q = _query_fields(src)
        q.update(id=_new_id("q"), created=_now(), updated=_now())
        doc.setdefault("queries", []).append(q)
        doc["updated"] = _now()
        _write(_shared_path(cid), doc)
        return doc


def update_shared_query(user: str, cid: str, qid: str, body: dict) -> dict:
    with _LOCK:
        doc = load_shared(cid)
        if not doc:
            raise HTTPException(404, "collection not found")
        _require_owner(doc, user)
        q = _find(doc.get("queries") or [], qid)
        if not q:
            raise HTTPException(404, "query not found")
        q.update(_query_fields(body, q))
        q["updated"] = _now()
        doc["updated"] = _now()
        _write(_shared_path(cid), doc)
        return q


def remove_from_collection(user: str, cid: str, qid: str) -> None:
    with _LOCK:
        lib = load_library(user)
        c = _find(lib["collections"], cid)
        if c:
            if qid in c.get("query_ids", []):
                c["query_ids"].remove(qid)
                c["updated"] = _now()
                save_library(user, lib)
            return
        doc = load_shared(cid)
        if not doc:
            raise HTTPException(404, "collection not found")
        _require_owner(doc, user)
        doc["queries"] = [q for q in doc.get("queries") or [] if q.get("id") != qid]
        doc["updated"] = _now()
        _write(_shared_path(cid), doc)


def _portable(q: dict) -> dict:
    return {k: q.get(k) for k in ("title", "sql", "note", "question", "starred", "pinned")}


def export_collection(user: str, cid: str) -> dict:
    c, qs, shared = collection_queries(user, cid)
    return {"format": "bsp-sql-collection", "version": 1, "name": c.get("name"), "note": c.get("note") or "",
            "exported": _now(), "queries": [_portable(q) for q in qs]}


def import_collection(user: str, doc: Any, shared: bool = False, name: str | None = None) -> dict:
    if isinstance(doc, list):
        doc = {"queries": doc}
    if not isinstance(doc, dict) or not isinstance(doc.get("queries"), list):
        raise HTTPException(400, "expected a collection export: {name, queries: [{title, sql, ...}]}")
    if doc.get("format") not in (None, "bsp-sql-collection"):
        raise HTTPException(400, f"unknown format {doc.get('format')!r}")
    items = [q for q in doc["queries"] if isinstance(q, dict) and str(q.get("sql") or "").strip()]
    if not items:
        raise HTTPException(400, "the file has no queries with SQL")
    if len(items) > MAX_IMPORT:
        raise HTTPException(413, f"at most {MAX_IMPORT} queries per import")
    cname = _s(name or doc.get("name"), 120) or "Imported queries"
    c = create_collection(user, {"name": cname, "note": doc.get("note") or "", "shared": shared})
    if shared:
        with _LOCK:
            d = load_shared(c["id"])
            for q in items:
                nq = _query_fields(q)
                nq.update(id=_new_id("q"), created=_now(), updated=_now())
                d["queries"].append(nq)
            _write(_shared_path(c["id"]), d)
            return d
    with _LOCK:
        lib = load_library(user)
        if len(lib["queries"]) + len(items) > MAX_QUERIES:
            raise HTTPException(409, f"saved query limit reached ({MAX_QUERIES})")
        col = _find(lib["collections"], c["id"])
        for q in items:
            nq = _query_fields(q)
            nq.update(id=_new_id("q"), created=_now(), updated=_now(), last_run=None, row_count=None, ms=None)
            lib["queries"].append(nq)
            col["query_ids"].append(nq["id"])
        save_library(user, lib)
        return col


def duplicate_collection(user: str, cid: str) -> dict:
    """Copy any visible collection (own or shared) into the caller's own library."""
    exp = export_collection(user, cid)
    return import_collection(user, exp, shared=False, name=(exp.get("name") or "Collection") + " (copy)")


def share_collection(user: str, cid: str) -> dict:
    if user == ANON:
        raise HTTPException(403, "sign in to share a collection")
    lib = load_library(user)
    if not _find(lib["collections"], cid):
        raise HTTPException(404, "collection not found")
    return import_collection(user, export_collection(user, cid), shared=True)


def record_run(user: str, ref: dict | None, row_count: int | None, ms: int, ok: bool) -> None:
    if not isinstance(ref, dict) or not ref.get("qid"):
        return
    stat = {"last_run": _now(), "row_count": row_count, "ms": ms, "ok": ok}
    with _LOCK:
        lib = load_library(user)
        if ref.get("cid") and not _find(lib["collections"], ref["cid"]) and load_shared(str(ref["cid"])):
            lib["runs"][f"{ref['cid']}/{ref['qid']}"] = stat
            lib["runs"] = dict(list(lib["runs"].items())[-500:])
        else:
            q = _find(lib["queries"], str(ref["qid"]))
            if not q:
                return
            q.update(stat)
        save_library(user, lib)


def library_view(user: str) -> dict:
    lib = load_library(user)
    shared = []
    for d in list_shared():
        qs = []
        for q in d.get("queries") or []:
            st = lib["runs"].get(f"{d['id']}/{q.get('id')}") or {}
            qs.append({**q, **{k: st.get(k) for k in ("last_run", "row_count", "ms")}})
        shared.append({**d, "queries": qs, "editable": user != ANON and d.get("owner") == user})
    return {"user": user, "signed_in": user != ANON, "queries": lib["queries"], "collections": lib["collections"],
            "shared": shared}


def search_library(user: str, q: str) -> list[dict]:
    terms = [t for t in q.lower().split() if t]
    view = library_view(user)
    rows = [{"kind": "own", "collection": None, **x} for x in view["queries"]]
    for d in view["shared"]:
        rows += [{"kind": "shared", "collection": {"id": d["id"], "name": d["name"]}, **x} for x in d["queries"]]

    def hay(x: dict) -> str:
        return " ".join(str(x.get(k) or "") for k in ("title", "sql", "note", "question")).lower()

    return [x for x in rows if all(t in hay(x) for t in terms)][:200]


# ── seeded "Getting started" shared collection ───────────────────────────────
SEED_ID = "getting-started"
SEED_VERSION = 4
SEED_QUERIES = [
    {"title": "Open care orders on unpaid invoices, by technology",
     "note": "Three systems in one query: warehouse (Postgres) customers, Oracle billing invoices and SQL Server care "
             "orders, joined on ban. Each source is aggregated per ban first so the totals do not fan out.",
     "sql": """WITH unpaid AS (
  SELECT ban, count(*) AS unpaid_invoices, sum(amount_due) AS unpaid_amount
  FROM billing_oracle.billing.invoices
  WHERE status <> 'paid'
  GROUP BY ban
), open_orders AS (
  SELECT ban, count(*) AS open_orders
  FROM care_mssql.dbo.care_orders
  WHERE status NOT IN ('completed', 'cancelled')
  GROUP BY ban
)
SELECT c.technology,
       count(*) AS customers,
       sum(o.open_orders) AS open_care_orders,
       sum(u.unpaid_invoices) AS unpaid_invoices,
       round(sum(u.unpaid_amount), 2) AS unpaid_amount
FROM warehouse.marts.dim_customer c
JOIN unpaid u ON u.ban = c.ban
JOIN open_orders o ON o.ban = c.ban
GROUP BY c.technology
ORDER BY open_care_orders DESC"""},
    {"title": "Explain demo: what gets pushed down to each system",
     "note": "Press Explain (not Run). The plan shows the tables read from Postgres, Oracle and SQL Server, the filters "
             "pushed into each source and the dynamic filter Trino builds on ban.",
     "sql": """SELECT c.region,
       count(DISTINCT o.order_id) AS p1_orders,
       count(DISTINCT i.invoice_id) AS late_invoices
FROM warehouse.marts.dim_customer c
JOIN care_mssql.dbo.care_orders o ON o.ban = c.ban AND o.priority = 'P1'
JOIN billing_oracle.billing.invoices i ON i.ban = c.ban AND i.status = 'late'
WHERE c.technology = 'fiber'
GROUP BY c.region
ORDER BY p1_orders DESC"""},
    {"title": "Wire centers: repeat dispatches vs overdue balance in Oracle",
     "note": "Warehouse dispatches by wire center (CLLI) next to the Oracle late and written-off balance of the same "
             "customers. Top 10 wire centers by repeat dispatches.",
     "sql": """WITH dispatches AS (
  SELECT clli,
         max(wire_center_name) AS wire_center,
         count(*) AS dispatches,
         count(*) FILTER (WHERE repeat_dispatch_30d) AS repeat_dispatches
  FROM warehouse.marts.fct_dispatch
  GROUP BY clli
), overdue AS (
  SELECT ban, sum(amount_due) AS overdue_amount
  FROM billing_oracle.billing.invoices
  WHERE status IN ('late', 'written_off')
  GROUP BY ban
), balance AS (
  SELECT c.clli, count(*) AS overdue_customers, sum(o.overdue_amount) AS overdue_amount
  FROM warehouse.marts.dim_customer c
  JOIN overdue o ON o.ban = c.ban
  GROUP BY c.clli
)
SELECT d.clli,
       d.wire_center,
       d.dispatches,
       d.repeat_dispatches,
       coalesce(b.overdue_customers, 0) AS overdue_customers,
       round(coalesce(b.overdue_amount, 0), 2) AS overdue_amount
FROM dispatches d
LEFT JOIN balance b ON b.clli = d.clli
ORDER BY d.repeat_dispatches DESC
LIMIT 10"""},
    {"title": "Payment ledger by month and type (Oracle)",
     "note": "Payments, late fees and credits from the Oracle transaction ledger, per month. A single-system "
             "aggregate pushed down to Oracle.",
     "sql": """SELECT date_trunc('month', CAST(txn_date AS date)) AS month,
       txn_type,
       count(*) AS transactions,
       round(sum(amount), 2) AS amount
FROM billing_oracle.billing.transactions
GROUP BY 1, 2
ORDER BY 1 DESC, 2
LIMIT 36"""},
    {"title": "Connected systems and BigQuery catalogs",
     "note": "Lists every Trino catalog and its connector. BigQuery shows up as bigquery and bq_<project> catalogs; with "
             "none attached this still runs and the other examples work. On BigQuery, counts work "
             "(SELECT count(*) FROM bq_<project>.<dataset>.<table>); column reads need a read-session grant on the "
             "execution project (see the Trino docs). Aggregate only: BigQuery tables can hold personal data.",
     "sql": """SELECT catalog_name,
       connector_name,
       CASE WHEN connector_name = 'bigquery' THEN 'BigQuery'
            WHEN connector_name = 'oracle' THEN 'Oracle'
            WHEN connector_name = 'sqlserver' THEN 'SQL Server'
            WHEN connector_name = 'postgresql' THEN 'Postgres'
            WHEN connector_name = 'hive' THEN 'Object storage (lake)'
            ELSE connector_name END AS system
FROM system.metadata.catalogs
ORDER BY connector_name = 'bigquery' DESC, catalog_name"""},
]


def _seed_doc() -> dict:
    return {"id": SEED_ID, "name": "Getting started", "owner": "appliance", "shared": True, "seed": True,
            "seed_version": SEED_VERSION,
            "note": "Federation examples that run on this appliance today. Copy the collection to edit it.",
            "queries": [{**_query_fields(q), "id": f"gs-{i + 1}", "created": _now(), "updated": _now()}
                        for i, q in enumerate(SEED_QUERIES)],
            "created": _now(), "updated": _now()}


def ensure_seed() -> None:
    p = _shared_dir() / f"{SEED_ID}.json"
    cur = _read(p, None) if p.is_file() else None
    if cur and cur.get("seed_version", 0) >= SEED_VERSION:
        return
    with _LOCK:
        _write(p, _seed_doc())


# ══ MCP tool calls ═════════════════════════════════════════════════════════════
async def call_tool(name: str, args: dict, caller: str, timeout: float = 120.0) -> tuple[dict, str, bool]:
    """(structured result, text, is_error) of one MCP tool call made as `caller`."""
    try:
        async with agent.client(timeout, caller=caller) as c:
            r = await c.call_tool(name, args, raise_on_error=False)
    except Exception as e:  # noqa: BLE001
        return {}, f"MCP server not reachable ({type(e).__name__}): start AI Tools in Studio", True
    text, _imgs, err = agent._result_parts(r)  # noqa: SLF001 (same parsing as the agent loop)
    sc = getattr(r, "structured_content", None) or {}
    if isinstance(sc, dict) and "result" in sc and len(sc) == 1 and isinstance(sc["result"], dict):
        sc = sc["result"]
    return (sc if isinstance(sc, dict) else {}), text, err


async def tquery(sql: str, caller: str, max_rows: int = 500) -> list[dict]:
    sc, text, err = await call_tool("trino_query", {"sql": sql, "max_rows": max_rows}, caller)
    if err:
        raise RuntimeError(text.strip()[:400] or "query failed")
    return list(sc.get("rows") or [])


_cats_cache: dict[str, Any] = {"at": 0.0, "rows": []}


async def trino_catalogs(caller: str) -> list[dict]:
    if time.time() - _cats_cache["at"] < 120 and _cats_cache["rows"]:
        return _cats_cache["rows"]
    rows = await tquery("SELECT catalog_name, connector_name FROM system.metadata.catalogs ORDER BY 1", caller, 200)
    _cats_cache.update(at=time.time(), rows=rows)
    return rows


_REF = re.compile(r'(?<![\w."])"?([A-Za-z_][\w$-]*)"?\s*\.\s*"?([A-Za-z_][\w$-]*)"?\s*\.\s*"?([A-Za-z_][\w$-]*)"?')


def referenced_tables(sql: str, catalogs: set[str] | None = None) -> list[str]:
    """catalog.schema.table names written in a query (string literals and comments ignored)."""
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql or "", flags=re.S)
    s = re.sub(r"'(?:[^']|'')*'", "''", s)
    out = []
    for m in _REF.finditer(s):
        cat = m.group(1).lower()
        if catalogs is None or cat in catalogs:
            out.append(f"{cat}.{m.group(2).lower()}.{m.group(3).lower()}")
    return list(dict.fromkeys(out))


_SCAN = re.compile(r"\b(TableScan|ScanFilterProject|ScanFilter|ScanProject)\[table = ([a-z0-9_]+):(\S+)")


def plan_summary(text: str, sc: dict) -> dict:
    """Systems read, stages, scans and pushdowns from trino_explain's result (structured + plan text)."""
    plan = text.split("```", 2)[1] if "```" in text else text
    scans: dict[str, dict] = {}
    for m in _SCAN.finditer(plan):
        system, raw = m.group(2), m.group(3)
        handle = raw if raw.startswith("Query[") else re.match(r"[^\],\\\s]*", raw).group(0)
        end = plan.find("\n", m.start())
        line = plan[m.start(): end if end > 0 else len(plan)]
        pushed_query = handle.startswith("Query[")
        name = handle if not pushed_query else "pushed-down query"
        key = f"{system}:{name}"
        s = scans.setdefault(key, {"system": system, "table": name, "operator": m.group(1), "pushdowns": []})
        if pushed_query:
            s["pushdowns"].append("join or aggregate pushed down as one SQL query")
        if "constraint on" in line:
            s["pushdowns"].append("filter: " + line.split("constraint on", 1)[1].split("]", 1)[0].strip(" [")[:120])
        if re.search(r"\blimit=\d+", line):
            s["pushdowns"].append("limit")
        if "dynamicFilters" in line:
            s["pushdowns"].append("dynamic filter (" + ", ".join(sorted(set(re.findall(r"#(df_\d+)", line)))) + ")")
        if m.group(1) in ("ScanFilterProject", "ScanFilter") and "filterPredicate" in line:
            s["pushdowns"].append("filter evaluated in Trino after the read")
    for raw in sc.get("scans") or []:   # TableScan nodes the tool already extracted (may include truncated plan parts)
        system, _, rest = str(raw).partition(":")
        name = rest.strip().split(" ")[0]
        scans.setdefault(f"{system}:{name}", {"system": system, "table": name, "operator": "TableScan", "pushdowns": []})
    systems = sorted({s["system"] for s in scans.values()} | set(sc.get("systems") or []))
    stages = sc.get("stages") or len(re.findall(r"^Fragment \d+", plan, re.M))
    pushdowns = [f"{s['system']}.{s['table']}: {p}" for s in scans.values() for p in dict.fromkeys(s["pushdowns"])]
    return {"systems": systems, "stages": stages, "scans": list(scans.values()), "pushdowns": pushdowns,
            "truncated": "Full plan (truncated)" in text and len(plan) >= 5900, "plan": plan.strip()[:20000]}


# ══ SQL endpoints ═════════════════════════════════════════════════════════════
def _no_store(payload: dict, status: int = 200) -> JSONResponse:
    """Result rows may hold personal data: tell the browser not to keep them on disk."""
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


@api.post("/sql/run")
async def sql_run(request: Request):
    user = user_of(request)
    body = await request.json()
    sql = str(body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(400, "sql is required")
    cap = max(1, min(int(body.get("max_rows") or 200), 500))
    t0 = time.monotonic()
    sc, text, err = await call_tool("trino_query", {"sql": sql, "max_rows": cap}, user)
    ms = int((time.monotonic() - t0) * 1000)
    try:
        cats = {r["catalog_name"] for r in await trino_catalogs(user)}
    except Exception:  # noqa: BLE001
        cats = None
    tables = referenced_tables(sql, cats)
    systems = sorted({t.split(".")[0] for t in tables})
    if err:
        record_run(user, body.get("ref"), None, ms, False)
        return _no_store({"ok": False, "error": text.strip()[:2000] or "query failed", "ms": ms, "systems": systems,
                          "tables": tables})
    rows = list(sc.get("rows") or [])
    cols = list(rows[0].keys()) if rows else []
    if not cols:   # zero rows: recover the header from the markdown table, if any
        m = re.search(r"^\| (.+) \|$", text, re.M)
        cols = [c.strip() for c in m.group(1).split("|")] if m else []
    record_run(user, body.get("ref"), len(rows), ms, True)
    log.info("sql run by %s: %d rows, %d ms, systems=%s", user, len(rows), ms, ",".join(systems))
    return _no_store({"ok": True, "columns": cols, "rows": rows, "row_count": len(rows), "truncated": bool(sc.get("truncated")),
                      "cap": cap, "ms": ms, "systems": systems, "tables": tables})


@api.post("/sql/explain")
async def sql_explain(request: Request):
    user = user_of(request)
    body = await request.json()
    sql = str(body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(400, "sql is required")
    t0 = time.monotonic()
    sc, text, err = await call_tool("trino_explain", {"sql": sql}, user)
    ms = int((time.monotonic() - t0) * 1000)
    if err:
        return {"ok": False, "error": text.strip()[:2000] or "explain failed", "ms": ms}
    out = plan_summary(text, sc)
    try:
        cats = {r["catalog_name"] for r in await trino_catalogs(user)}
    except Exception:  # noqa: BLE001
        cats = None
    in_sql = sorted({t.split(".")[0] for t in referenced_tables(sql, cats)})
    out["systems_in_sql"] = in_sql
    # the tool cuts long plans: systems named in the query but not seen in the plan text are reported apart
    out["systems_not_in_plan_text"] = [x for x in in_sql if x not in out["systems"]] if out["truncated"] else []
    return {"ok": True, "ms": ms, **out}


# ── Trino monitor: find the query id of a statement the MCP tool ran (for the embedded Trino UI) ──
def trino_ui_url() -> str:
    return f"http://{os.environ.get('TRINO_HOST', 'trino')}:{os.environ.get('TRINO_PORT', '8080')}"


def _norm(q: str) -> str:
    q = re.sub(r"--[^\n]*|/\*.*?\*/", " ", q or "", flags=re.S)
    return re.sub(r"\s+", " ", q).strip().rstrip(";").strip().lower()


def match_query(items: list[dict], sql: str, since_ms: int, explain: bool = False) -> dict | None:
    """Newest Trino query whose text is this statement (as trino_query / trino_explain send it), started after since."""
    want = _norm(("EXPLAIN (TYPE DISTRIBUTED) " if explain else "") + sql)
    best = None
    for q in items:
        text = _norm(q.get("queryTextPreview") or q.get("query") or "")
        if not text or not (text == want or (len(text) >= 60 and want.startswith(text[:-3].rstrip())) or text.startswith(want)):
            continue
        ct = str((q.get("queryStats") or {}).get("createTime") or "")
        try:   # e.g. 2026-10-19T00:39:56.551004622Z (UTC, nanoseconds)
            ms = int(dt.datetime.fromisoformat(ct[:19]).replace(tzinfo=dt.timezone.utc).timestamp() * 1000) if ct else 0
        except ValueError:
            ms = 0
        if ms and ms < since_ms - 2000:
            continue
        if best is None or ms > best[0]:
            best = (ms, q)
    return best[1] if best else None


@api.post("/sql/trino/find")
async def trino_find(request: Request):
    """{sql, since (epoch ms), explain} -> the Trino query id and state, so the UI can open its live page."""
    import httpx  # noqa: PLC0415

    body = await request.json()
    sql = str(body.get("sql") or "")
    if not sql.strip():
        raise HTTPException(400, "sql is required")
    since = int(body.get("since") or 0)
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            r = await c.get(trino_ui_url() + "/ui/api/query")
            r.raise_for_status()
            items = r.json()
    except Exception as e:  # noqa: BLE001
        return {"found": False, "error": f"Trino not reachable ({type(e).__name__})", "ui": "/trino/ui/"}
    q = match_query(items if isinstance(items, list) else [], sql, since, bool(body.get("explain")))
    if not q:
        return {"found": False, "ui": "/trino/ui/"}
    qid = q.get("queryId", "")
    return {"found": True, "query_id": qid, "state": q.get("state"), "ui": f"/trino/ui/#/queries/{qid}"}


def _sse(events) -> StreamingResponse:
    async def gen():
        try:
            async for ev in events:
                yield "data: " + json.dumps(ev, default=str) + "\n\n"
        except Exception as e:  # noqa: BLE001
            log.exception("sql ask failed")
            yield "data: " + json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n\n"
        yield "data: " + json.dumps({"type": "end"}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no"})


ASK_SYSTEM = ("You write Trino SQL for DataPlatform analysts in the Federated SQL editor. Produce ONE read-only query "
              "that answers the question. Fully qualify tables as catalog.schema.table and join customer data on ban. "
              "Prefer aggregates over raw rows (personal data stays in the source). Check the query works by running "
              "it with trino_query (small max_rows). Finish with the final query in one ```sql block and one short "
              "sentence on what it returns. Discover narrowly: when you need the data map, call trino_sources WITH a "
              "catalog argument (one of: {catalogs}), never without one; never crawl bigquery or bq_* catalogs (slow and billed), and "
              "prefer the catalog definitions below when they cover the question.")
_SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)


def extract_sql(final: str, executed: list[str]) -> tuple[str, str]:
    blocks = [b.strip() for b in _SQL_BLOCK.findall(final or "") if b.strip()]
    if blocks:
        return blocks[-1], "answer"
    if executed:
        return executed[-1].strip(), "last query run"
    return "", ""


def _summary_line(text: str) -> str:
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            text = str(d.get("summary") or "")
    except ValueError:
        pass
    return text.strip().split("\n", 1)[0][:200]


@api.post("/sql/ask")
async def sql_ask(request: Request):
    """English → SQL with the harness agent (trino + catalog tools). Streams agent events, then {type: sql}."""
    user = user_of(request)
    body = await request.json()
    question = _s(body.get("question"), 2000)
    if not question:
        raise HTTPException(400, "question is required")
    model = _s(body.get("model"), 120) or ASK_MODEL
    tables = [t for t in (body.get("tables") or []) if isinstance(t, str)][:8]
    tables += [t for t in referenced_tables(body.get("sql") or "") if t not in tables][: max(0, 8 - len(tables))]
    terms = [t for t in (body.get("terms") or []) if isinstance(t, str)][:12]
    if terms and len(tables) < 8:   # terms without tables: use the tables the glossary links to them
        for g in await glossary_items(user):
            if g["name"] in terms:
                tables += [t for t in g.get("related_tables") or [] if t not in tables][: max(0, 8 - len(tables))]
    defs, refreshed = await definitions_context(tables, terms, user)
    try:
        cats = [r["catalog_name"] for r in await trino_catalogs(user)
                if r["connector_name"] != "bigquery" and r["catalog_name"] not in HIDDEN_CATALOGS | {"tpch"}]
    except Exception:  # noqa: BLE001
        cats = ["warehouse", "billing_oracle", "care_mssql", "lake"]
    picked = [g for g in (body.get("glossary") or []) if isinstance(g, dict) and g.get("name")][:12]
    if picked:   # terms the user added from the Glossary tab ("Use in question")
        defs = (defs + "\n\n" if defs else "") + "Glossary terms the user wants used:\n" + "\n".join(
            f"- {_s(g['name'], 120)}: {_s(g.get('definition'), 600)}" for g in picked)
    system = ASK_SYSTEM.replace("{catalogs}", ", ".join(cats)) + ("\n\n" + defs if defs else "")
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": question}]

    async def events():
        yield {"type": "context", "tables": tables, "definitions": bool(defs), "refreshed": refreshed}
        executed: list[str] = []
        final = ""
        async for ev in agent.run(msgs, model, ["trino", "catalog"], max(2, min(int(body.get("max_steps") or 8), 12)), 0, caller=user):
            t = ev.get("type")
            if t == "tool_call":
                if ev.get("name") == "trino_query" and isinstance((ev.get("args") or {}).get("sql"), str):
                    executed.append(ev["args"]["sql"])
                yield {"type": "tool_call", "id": ev.get("id"), "name": ev.get("name"), "args": ev.get("args")}
            elif t == "tool_result":   # summary line only: rows stay out of the event stream
                yield {"type": "tool_result", "id": ev.get("id"), "name": ev.get("name"), "ok": ev.get("ok"),
                       "ms": ev.get("ms"), "text": _summary_line(ev.get("text") or "")}
            elif t == "message":
                final = ev.get("content") or ""
                yield {"type": "message", "content": final}
            elif t in ("route", "error", "step", "thought"):
                yield {k: v for k, v in ev.items() if k != "messages"}
        sql, src = extract_sql(final, executed)
        yield {"type": "sql", "sql": sql, "source": src}

    return _sse(events())


# ══ catalog ═══════════════════════════════════════════════════════════════════
SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "sys", "guest", "lbacsys", "gsmadmin_internal", "system", "ctxsys",
                  "mdsys", "xdb", "outln", "dbsnmp", "appqossys", "audsys", "dvsys", "ojvmsys", "olapsys", "ordsys",
                  "wmsys", "lbacsys", "dbsfwuser", "gsmcatuser", "remote_scheduler_agent", "anonymous", "public_sync"}
HIDDEN_CATALOGS = {"system"}
_SYS_SCHEMA = re.compile(r"^(sys|gg|gsm|apex_|db_|dgpdb|dvf$|dip$|pdbadmin$|vecsys$|baassys$|ojvm|ordsys|ordplugins|olap|wmsys|"
                         r"xdb|lbac|ctx|mdsys|mddata|outln$|audsys$|appqossys$|dbsnmp$|remote_scheduler|anonymous$|dbsfwuser$)", re.I)


def is_system_schema(name: str) -> bool:
    return name.lower() in SYSTEM_SCHEMAS or bool(_SYS_SCHEMA.match(name))
CONNECTOR_LABEL = {"postgresql": "Postgres", "oracle": "Oracle", "sqlserver": "SQL Server", "bigquery": "BigQuery",
                   "hive": "Object storage", "iceberg": "Iceberg", "tpch": "Sample data", "mysql": "MySQL",
                   "delta_lake": "Delta Lake"}
FALLBACK_DEFINITIONS = {
    "BAN": "Billing account number: the 10-digit customer key shared by billing, care, tickets and churn data.",
    "CLLI": "Wire center code (Common Language Location Identifier).",
    "Invoice": "A monthly bill for a BAN; invoice ids are <ban>-<yymm>.",
    "Churn": "A customer disconnecting service (voluntary or not).",
    "Dispute": "A customer challenge to a charge on an invoice.",
    "Wire center": "The central office that serves an area; identified by CLLI.",
    "Copper": "DSL service over copper loops.",
    "Fiber": "Fiber-to-the-home service (ONT at the premises).",
}


def _catlib():
    """The shared catalog library, once its interface is marked stable."""
    try:
        from bsp_common import catalog as lib  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    return lib if "INTERFACE STABLE" in (lib.__doc__ or "") else None


_dh_probe: dict[str, Any] = {"at": 0.0, "ok": False}


async def datahub():
    """The catalog library when DataHub is configured and answering (probe cached 30 s), else None."""
    lib = _catlib()
    if not lib or lib.backend() != "datahub":
        return None
    if time.time() - _dh_probe["at"] > 30:
        _dh_probe.update(at=time.time(), ok=await asyncio.to_thread(lib.available))
    return lib if _dh_probe["ok"] else None


# ── Trino structure crawl (fallback and column source), metadata only ───────
def _cache_file() -> Path:
    return _root() / "catalog_cache.json"


_cache: dict[str, Any] = {}
_cache_loaded = False


def _cache_get() -> dict:
    global _cache_loaded
    if not _cache_loaded:
        _cache.update(_read(_cache_file(), {}))
        _cache.setdefault("schemas", {})
        _cache.setdefault("refreshed", {})
        _cache_loaded = True
    return _cache


def _cache_save() -> None:
    with _LOCK:
        _write(_cache_file(), _cache_get())


def _q(ident: str) -> str:
    if not _IDENT.match(ident or ""):
        raise HTTPException(400, f"bad identifier {ident!r}")
    return '"' + ident + '"'


async def crawl_schema(catalog: str, schema: str, caller: str) -> dict:
    """Tables + columns of one schema from Trino information_schema (+ table comments). Cached on disk."""
    rows = await tquery(
        "SELECT table_name, array_join(array_agg(column_name || ':' || data_type ORDER BY ordinal_position), '|') AS cols "
        f"FROM {_q(catalog)}.information_schema.columns WHERE table_schema = '{schema}' GROUP BY 1 ORDER BY 1",
        caller, 500) if _IDENT.match(schema) else []
    comments: dict[str, str] = {}
    try:
        for r in await tquery('SELECT table_name, "comment" AS c FROM system.metadata.table_comments '
                              f"WHERE catalog_name = '{catalog}' AND schema_name = '{schema}' AND \"comment\" IS NOT NULL",
                              caller, 500):
            comments[r["table_name"]] = r["c"] or ""
    except Exception:  # noqa: BLE001 (comments are optional)
        pass
    tables = []
    for r in rows:
        cols = [c.split(":", 1) for c in (r.get("cols") or "").split("|") if c]
        tables.append({"name": r["table_name"], "columns": [[c[0], c[1] if len(c) > 1 else ""] for c in cols],
                       "comment": comments.get(r["table_name"], "")})
    entry = {"at": _now(), "tables": tables}
    c = _cache_get()
    c["schemas"][f"{catalog}.{schema}"] = entry
    _cache_save()
    return entry


async def cached_schema(catalog: str, schema: str, caller: str, max_age: float = 6 * 3600) -> dict:
    e = _cache_get()["schemas"].get(f"{catalog}.{schema}")
    if e:
        try:
            age = time.time() - dt.datetime.fromisoformat(e["at"]).timestamp()
        except (KeyError, ValueError):
            age = max_age + 1
        if age < max_age:
            return e
    return await crawl_schema(catalog, schema, caller)


def _refreshed_at(catalog: str, schema: str | None = None, table: str | None = None) -> str | None:
    r = _cache_get()["refreshed"]
    keys = [catalog] + ([f"{catalog}.{schema}"] if schema else []) + ([f"{catalog}.{schema}.{table}"] if table else [])
    vals = [r[k] for k in keys if r.get(k)]
    return max(vals) if vals else None


def _mark_refreshed(scope: dict) -> None:
    cat, sch = scope.get("catalog"), scope.get("schema")
    keys = [f"{cat}.{sch}.{t}" for t in scope.get("tables") or []] or ([f"{cat}.{sch}"] if sch else [cat])
    c = _cache_get()
    for k in keys:
        c["refreshed"][k] = _now()
    _cache_save()


def _lib_or_none():
    return _catlib()


def _hint_table(catalog: str, schema: str, t: dict) -> dict:
    """A table entry from the Trino crawl, with name-based hints (PII, BAN, suggested glossary terms)."""
    lib = _lib_or_none()
    cols = [c[0] for c in t.get("columns") or []]
    pii = [c for c in cols if lib and lib.is_pii_name(c)]
    has_ban = any((lib.is_ban_name(c) if lib else c.lower() == "ban") for c in cols)
    terms = lib.suggested_terms(t["name"], cols) if lib else []
    tags = (["PII (suspected)"] if pii else [])
    return {"name": t["name"], "fqn": f"{catalog}.{schema}.{t['name']}", "urn": None, "description": t.get("comment") or "",
            "tags": tags, "terms": terms, "terms_suggested": True, "has_ban": has_ban, "ai_annotated": False,
            "pii_columns": pii, "column_count": len(cols), "in_catalog": False,
            "crawled": None, "refreshed": None}


# ── catalog endpoints ────────────────────────────────────────────────────────
@api.get("/sql/catalog/status")
async def catalog_status():
    lib = _catlib()
    dh = await datahub()
    configured = lib.backend() if lib else "none"
    if dh:
        msg = "Catalog definitions from DataHub."
    elif configured == "datahub":
        msg = "DataHub is configured but not answering. Showing structure from Trino. Catalog definitions come from DataHub when it is running."
    else:
        msg = "Catalog definitions come from DataHub when it is running. Showing structure from Trino with name-based hints."
    return {"backend": "datahub" if dh else "trino", "configured": configured, "library": bool(lib),
            "can_annotate": bool(dh), "message": msg}


@api.get("/sql/catalog/systems")
async def catalog_systems(request: Request):
    user = user_of(request)
    out: dict[str, dict] = {}
    err = None
    try:
        for r in await trino_catalogs(user):
            n = r["catalog_name"]
            if n in HIDDEN_CATALOGS:
                continue
            out[n] = {"name": n, "connector": r["connector_name"], "label": CONNECTOR_LABEL.get(r["connector_name"], r["connector_name"]),
                      "datasets": None, "in_trino": True}
    except Exception as e:  # noqa: BLE001
        err = str(e)[:300]
    dh = await datahub()
    if dh:
        try:
            for c in (await asyncio.to_thread(dh.tree)).get("catalogs") or []:
                out.setdefault(c["name"], {"name": c["name"], "connector": "", "label": "", "in_trino": False})["datasets"] = c.get("datasets")
        except Exception as e:  # noqa: BLE001
            err = err or f"DataHub: {str(e)[:200]}"
    for v in out.values():
        v["refreshed"] = _refreshed_at(v["name"])
    rank = {"postgresql": 0, "oracle": 1, "sqlserver": 2, "bigquery": 3, "hive": 4}
    items = sorted(out.values(), key=lambda x: (rank.get(x["connector"], 9), x["name"]))
    return {"systems": items, "error": err}


@api.get("/sql/catalog/schemas")
async def catalog_schemas(catalog: str, request: Request):
    user = user_of(request)
    _q(catalog)
    out: dict[str, dict] = {}
    err = None
    try:
        sc, text, e = await call_tool("trino_list", {"catalog": catalog}, user)
        if e:
            raise RuntimeError(text.strip()[:300])
        for r in sc.get("items") or []:
            n = next(iter(r.values()))
            if is_system_schema(n):
                continue
            out[n] = {"name": n, "datasets": None, "in_trino": True}
    except Exception as ex:  # noqa: BLE001
        err = str(ex)[:300]
    dh = await datahub()
    if dh:
        try:
            for s in (await asyncio.to_thread(dh.tree, catalog)).get("schemas") or []:
                out.setdefault(s["name"], {"name": s["name"], "in_trino": False})["datasets"] = s.get("datasets")
        except Exception as ex:  # noqa: BLE001
            err = err or f"DataHub: {str(ex)[:200]}"
    for k, v in out.items():
        v["refreshed"] = _refreshed_at(catalog, k)
        v["crawled"] = (_cache_get()["schemas"].get(f"{catalog}.{k}") or {}).get("at")
    pref = ["marts", "billing", "dbo"]
    items = sorted(out.values(), key=lambda x: (x["name"] not in pref, x["name"]))
    return {"catalog": catalog, "schemas": items, "error": err}


async def schema_tables(catalog: str, schema: str, user: str) -> tuple[list[dict], str | None]:
    _q(catalog)
    _q(schema)
    err = None
    tables: dict[str, dict] = {}
    crawled = None
    try:
        e = await cached_schema(catalog, schema, user)
        crawled = e.get("at")
        for t in e["tables"]:
            tables[t["name"]] = _hint_table(catalog, schema, t) | {"crawled": crawled}
    except Exception as ex:  # noqa: BLE001
        err = str(ex)[:300]
    dh = await datahub()
    if dh:
        try:
            for t in (await asyncio.to_thread(dh.tree, catalog, schema)).get("tables") or []:
                base = tables.get(t["name"]) or {"name": t["name"], "fqn": f"{catalog}.{schema}.{t['name']}",
                                                 "pii_columns": [], "column_count": None, "crawled": None}
                tags = list(t.get("tags") or [])
                if base.get("pii_columns") and "PII (suspected)" not in tags:
                    tags.append("PII (suspected)")
                if t.get("ai_annotated") and "ai-annotated" not in tags:
                    tags.append("ai-annotated")
                dh_terms = t.get("terms") or []
                tables[t["name"]] = {**base, "urn": t.get("urn"), "description": t.get("description") or base.get("description", ""),
                                     "tags": tags, "terms": dh_terms or base.get("terms") or [], "terms_suggested": not dh_terms and bool(base.get("terms")),
                                     "has_ban": bool(t.get("has_ban") or base.get("has_ban")),
                                     "ai_annotated": bool(t.get("ai_annotated")), "in_catalog": True,
                                     "dh_refreshed": t.get("last_refreshed")}
        except Exception as ex:  # noqa: BLE001
            err = err or f"DataHub: {str(ex)[:200]}"
    for t in tables.values():
        own = _refreshed_at(catalog, schema, t["name"])
        dhr = (t.get("dh_refreshed") or "").replace("Z", "+00:00") or None
        t["refreshed"] = max(filter(None, [own, dhr])) if (own or dhr) else None
    return sorted(tables.values(), key=lambda t: t["name"]), err


@api.get("/sql/catalog/tables")
async def catalog_tables(catalog: str, schema: str, request: Request, terms: str = ""):
    tables, err = await schema_tables(catalog, schema, user_of(request))
    want = [t for t in terms.split(",") if t]
    if want:
        gl = {g["name"]: g for g in (await glossary_items(user_of(request)))}
        def linked(t: dict) -> bool:
            return any(w in (t.get("terms") or []) or t["fqn"] in (gl.get(w, {}).get("related_tables") or []) for w in want)
        tables = [t for t in tables if linked(t)]
    return {"catalog": catalog, "schema": schema, "tables": tables, "error": err}


async def glossary_items(user: str) -> list[dict]:
    dh = await datahub()
    if dh:
        try:
            return [{**g, "source": "datahub"} for g in await asyncio.to_thread(dh.glossary)]
        except Exception as e:  # noqa: BLE001
            log.info("glossary from DataHub failed: %s", e)
    lib = _catlib()
    names = list(lib.TERM_HINTS) if lib else list(FALLBACK_DEFINITIONS)
    related: dict[str, list[str]] = {n: [] for n in names}
    for key, e in _cache_get()["schemas"].items():
        cat, sch = key.split(".", 1)
        for t in e.get("tables") or []:
            for term in _hint_table(cat, sch, t)["terms"]:
                related.setdefault(term, []).append(f"{key}.{t['name']}")
    return [{"urn": None, "name": n, "definition": FALLBACK_DEFINITIONS.get(n, ""), "related_tables": related.get(n, [])[:200],
             "source": "name hints"} for n in names]


@api.get("/sql/catalog/glossary")
async def catalog_glossary(request: Request):
    items = await glossary_items(user_of(request))
    return {"terms": items, "source": items[0]["source"] if items else "none"}


@api.get("/sql/catalog/columns")
async def catalog_columns(table: str, request: Request):
    user = user_of(request)
    parts = table.split(".")
    if len(parts) != 3:
        raise HTTPException(400, "use catalog.schema.table")
    for p in parts:
        _q(p)
    lib = _catlib()
    dh = await datahub()
    if dh:
        try:
            d = await asyncio.to_thread(dh.describe, table)
            if d and d.get("columns"):
                return {"table": table, "source": "datahub", "description": d.get("description", ""), "columns": [
                    {**c, "pii": "PII (suspected)" in (c.get("tags") or []) or bool(lib and lib.is_pii_name(c["name"]))}
                    for c in d["columns"]]}
        except Exception as e:  # noqa: BLE001
            log.info("describe from DataHub failed: %s", e)
    sc, text, err = await call_tool("trino_describe", {"table": table}, user)
    if err:
        return {"table": table, "source": "trino", "columns": [], "error": text.strip()[:300]}
    cols = []
    for r in sc.get("columns") or []:
        n = r.get("Column") or r.get("column") or ""
        pii = bool(lib and lib.is_pii_name(n))
        cols.append({"name": n, "type": r.get("Type") or "", "description": r.get("Comment") or "",
                     "tags": ["PII (suspected)"] if pii else [], "terms": [], "pii": pii})
    return {"table": table, "source": "trino", "columns": cols}


@api.get("/sql/catalog/search")
async def catalog_search(q: str, request: Request, limit: int = 30):
    q = q.strip()
    if not q:
        return {"results": []}
    dh = await datahub()
    if dh:
        try:
            hits = await asyncio.to_thread(dh.search, q, None, min(limit, 100), "trino")
            return {"source": "datahub", "results": [{**h, "fqn": h.get("name")} for h in hits]}
        except Exception as e:  # noqa: BLE001
            log.info("catalog search failed: %s", e)
    terms = q.lower().split()
    out = []
    for key, e in _cache_get()["schemas"].items():
        cat, sch = key.split(".", 1)
        for t in e.get("tables") or []:
            hay = f"{key}.{t['name']} {t.get('comment', '')} " + " ".join(c[0] for c in t.get("columns") or [])
            if all(x in hay.lower() for x in terms):
                out.append(_hint_table(cat, sch, t))
    return {"source": "trino cache", "results": out[:limit]}


# ── refresh jobs (persisted under sqlstudio/jobs/, survive reloads and restarts) ──
MAX_JOBS = 100
TERMINAL = {"succeeded", "failed", "cancelled", "interrupted"}
_jobs: dict[str, dict] = {}
_jobs_loaded = False
_job_lock = threading.Lock()


def _jobs_dir() -> Path:
    d = _root() / "jobs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _job_public(j: dict, full: bool = False) -> dict:
    log_lines = list(j.get("log") or [])
    tail = "\n".join(log_lines[-40:])
    if j.get("dh_tail"):
        tail = (tail + "\n--- DataHub report (tail) ---\n" + j["dh_tail"][-2500:]).strip()
    out = {k: j.get(k) for k in ("job_id", "backend", "scope", "scopes", "reannotate", "force", "state", "phase", "counts",
                                 "user", "started", "finished", "retry_of")}
    out["counts"] = dict(j.get("counts") or {})
    _add_counts(out["counts"], j.get("scope_counts") or {})   # live counts of the scope in progress
    out["log_tail"] = tail
    out["cancellable"] = j.get("state") in ("queued", "running") and bool(j.get("can_cancel"))
    if full:
        out["log"] = "\n".join(log_lines) + (("\n--- DataHub report (tail) ---\n" + j["dh_tail"]) if j.get("dh_tail") else "")
    return out


def job_save(j: dict) -> None:
    doc = {k: v for k, v in j.items() if k not in ("task",)}
    with _job_lock:
        _write(_jobs_dir() / f"{j['job_id']}.json", doc)


def _prune_jobs() -> None:
    files = sorted(_jobs_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for f in files[MAX_JOBS:]:
        _jobs.pop(f.stem, None)
        f.unlink(missing_ok=True)


def jobs_init() -> None:
    """Load persisted jobs once per process. Jobs that were queued or running belong to a previous
    process (their poller is gone): mark them interrupted so they can be retried."""
    global _jobs_loaded
    if _jobs_loaded:
        return
    _jobs_loaded = True
    for f in _jobs_dir().glob("*.json"):
        j = _read(f, None)
        if not isinstance(j, dict) or not j.get("job_id") or j["job_id"] in _jobs:
            continue
        if j.get("state") not in TERMINAL:
            j["state"] = "interrupted"
            j["finished"] = j.get("finished") or time.time()
            j.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} interrupted: the harness restarted while this job ran")
            job_save(j)
        _jobs[j["job_id"]] = j


def _job_log(j: dict, line: str) -> None:
    j.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {line}")
    j["log"] = j["log"][-2000:]


def _scope(body: dict) -> dict:
    cat = _s(body.get("catalog"), 128)
    sch = _s(body.get("schema"), 128) or None
    tables = [t for t in (body.get("tables") or []) if isinstance(t, str) and t][:200]
    if not cat or not _IDENT.match(cat) or (sch and not _IDENT.match(sch)) or any(not _IDENT.match(t) for t in tables):
        raise HTTPException(400, "scope needs a catalog (and optional schema, tables)")
    if tables and not sch:
        raise HTTPException(400, "tables need a schema")
    return {"catalog": cat, "schema": sch, "tables": tables}


def default_scope(sel: dict) -> dict:
    """What "Refresh definitions" refreshes for a selection: chosen tables, else the schema, else the system,
    else every system ({"all": True})."""
    sel = sel or {}
    cat, sch = sel.get("catalog") or None, sel.get("schema") or None
    tables = [t for t in (sel.get("tables") or []) if t]
    if cat and sch and tables:
        return {"catalog": cat, "schema": sch, "tables": tables}
    if cat and sch:
        return {"catalog": cat, "schema": sch, "tables": []}
    if cat:
        return {"catalog": cat, "schema": None, "tables": []}
    return {"all": True}


def scope_label(sc: dict) -> str:
    if sc.get("all"):
        return "all systems"
    base = ".".join(x for x in (sc.get("catalog"), sc.get("schema")) if x)
    return base + (f" ({len(sc['tables'])} tables)" if sc.get("tables") else "")


def _add_counts(total: dict, part: dict) -> None:
    for k, v in (part or {}).items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            total[k] = total.get(k, 0) + v


async def _crawl_scope(j: dict, sc: dict, user: str) -> dict:
    """Trino structure re-read for one scope; returns counts."""
    counts: dict = {}
    schemas = [sc["schema"]] if sc.get("schema") else []
    if not schemas:
        _job_log(j, f"listing schemas in {sc['catalog']}")
        res, text, err = await call_tool("trino_list", {"catalog": sc["catalog"]}, user)
        if err:
            raise RuntimeError(text.strip()[:300])
        schemas = [n for n in (next(iter(r.values())) for r in res.get("items") or []) if not is_system_schema(n)]
    counts["schemas"] = len(schemas)
    for s in schemas:
        if j.get("cancel"):
            break
        e = await crawl_schema(sc["catalog"], s, user)
        want = set(sc.get("tables") or [])
        seen = [t for t in e["tables"] if not want or t["name"] in want]
        pii = sum(1 for t in seen if _hint_table(sc["catalog"], s, t)["pii_columns"])
        counts["tables_scanned"] = counts.get("tables_scanned", 0) + len(seen)
        counts["tables_pii_suspected"] = counts.get("tables_pii_suspected", 0) + pii
        _job_log(j, f"{sc['catalog']}.{s}: {len(seen)} table(s) read from Trino, {pii} with suspected personal data")
        job_save(j)
    return counts


async def _datahub_scope(j: dict, lib, sc: dict) -> tuple[str, dict]:
    """One scoped DataHub refresh (ingest, then optional annotation), polled until it ends."""
    r = await asyncio.to_thread(lib.refresh, {k: v for k, v in sc.items() if v}, j["reannotate"], j["force"])
    j["dh_job"] = r["job_id"]
    _job_log(j, f"DataHub ingestion started for {scope_label(sc)}")
    job_save(j)
    phase = None
    while True:
        await asyncio.sleep(3)
        if j.get("cancel"):
            try:
                await asyncio.to_thread(lib.cancel, j["dh_job"])
            except Exception as e:  # noqa: BLE001
                _job_log(j, f"cancel: {e}")
            return "cancelled", {}
        d = await asyncio.to_thread(lib.job, j["dh_job"])
        j["dh_tail"] = (d.get("log_tail") or "")[-6000:]
        if d.get("phase") != phase:
            phase = d.get("phase")
            j["phase"] = phase
            _job_log(j, f"{scope_label(sc)}: {phase}")
        j["scope_counts"] = d.get("counts") or {}
        job_save(j)
        if d.get("state") in ("succeeded", "failed", "cancelled"):
            return d["state"], d.get("counts") or {}


async def run_job(j: dict) -> None:
    user = j["user"]
    lib = _catlib() if j["backend"] == "datahub" else None
    j["state"] = "running"
    states = []
    try:
        for sc in j["scopes"]:
            if j.get("cancel"):
                states.append("cancelled")
                break
            if lib:
                st, counts = await _datahub_scope(j, lib, sc)
                if st == "succeeded" and sc.get("schema"):
                    try:   # columns for the panel come from Trino: re-read the schema too
                        await crawl_schema(sc["catalog"], sc["schema"], user)
                    except Exception:  # noqa: BLE001
                        pass
            else:
                counts = await _crawl_scope(j, sc, user)
                st = "cancelled" if j.get("cancel") else "succeeded"
                if j["reannotate"]:
                    _job_log(j, "AI annotation needs DataHub; skipped")
            j.pop("scope_counts", None)
            _add_counts(j["counts"], counts)
            states.append(st)
            _job_log(j, f"{scope_label(sc)}: {st}" + (f" ({', '.join(f'{k} {v}' for k, v in counts.items() if v)})" if counts else ""))
            if st == "succeeded":
                _mark_refreshed(sc)
            job_save(j)
        j["state"] = ("cancelled" if "cancelled" in states else "failed" if "failed" in states
                      else "succeeded" if states else "cancelled")
    except Exception as e:  # noqa: BLE001
        _job_log(j, f"failed: {type(e).__name__}: {e}")
        j["state"] = "failed"
    j["finished"] = time.time()
    job_save(j)


async def start_job(user: str, scopes: list[dict], reannotate: bool, force: bool, retry_of: str | None = None,
                    all_systems: bool = False) -> dict:
    jobs_init()
    dh = await datahub()
    jid = secrets.token_hex(6)
    j = {"job_id": jid, "backend": "datahub" if dh else "trino", "scopes": scopes,
         "scope": {"all": True} if all_systems else scopes[0], "reannotate": bool(reannotate and dh), "force": bool(force and dh),
         "state": "queued", "phase": "ingest" if dh else "trino", "log": [], "counts": {}, "user": user,
         "started": time.time(), "finished": None, "retry_of": retry_of, "can_cancel": True}
    _job_log(j, ("DataHub refresh" if dh else "DataHub is not running: re-reading structure from Trino")
             + f" for {scope_label(j['scope'])}" + (" with AI annotation" if j["reannotate"] else "")
             + (" (may overwrite AI suggestions)" if j["force"] else ""))
    if reannotate and not dh:
        _job_log(j, "AI annotation needs DataHub; it will be skipped")
    _jobs[jid] = j
    job_save(j)
    _prune_jobs()
    j["task"] = asyncio.create_task(run_job(j))
    log.info("catalog refresh %s by %s: %s reannotate=%s force=%s backend=%s", jid, user, scope_label(j["scope"]),
             j["reannotate"], j["force"], j["backend"])
    return j


def is_admin(request: Request) -> bool:
    return identity._from_facade(request) and (request.headers.get("x-auth-role") or "").strip().lower() == "admin"


def _visible(j: dict, user: str, admin: bool) -> bool:
    return admin or j.get("user") == user


def _get_job(jid: str, request: Request) -> dict:
    jobs_init()
    j = _jobs.get(jid)
    if not j or not _visible(j, user_of(request), is_admin(request)):
        raise HTTPException(404, "unknown job")
    return j


@api.post("/sql/catalog/refresh")
async def catalog_refresh(request: Request):
    user = user_of(request)
    if user == ANON:
        raise HTTPException(403, "sign in to refresh catalog definitions")
    body = await request.json()
    if not body.get("scope") and isinstance(body.get("selection"), dict):
        d = default_scope(body["selection"])
        if d.get("all"):
            body["all"] = True
        else:
            body["scope"] = d
    if body.get("all"):
        cats = [r["catalog_name"] for r in await trino_catalogs(user) if r["catalog_name"] not in HIDDEN_CATALOGS | {"tpch"}]
        if not cats:
            raise HTTPException(400, "no systems to refresh")
        j = await start_job(user, [{"catalog": c, "schema": None, "tables": []} for c in cats], False, False, all_systems=True)
    else:
        j = await start_job(user, [_scope(body.get("scope") or body)], bool(body.get("reannotate")), bool(body.get("force")))
    return {"job_id": j["job_id"], "backend": j["backend"], "job": _job_public(j)}


@api.get("/sql/catalog/jobs")
def catalog_jobs(request: Request, active: bool = False, limit: int = 100):
    jobs_init()
    user, admin = user_of(request), is_admin(request)
    items = [j for j in _jobs.values() if _visible(j, user, admin) and (not active or j.get("state") not in TERMINAL)]
    items.sort(key=lambda j: j.get("started") or 0, reverse=True)
    return {"jobs": [_job_public(j) for j in items[:max(1, min(limit, MAX_JOBS))]], "admin": admin, "user": user}


@api.get("/sql/catalog/jobs/{jid}")
def catalog_job(jid: str, request: Request, full: bool = False):
    return _job_public(_get_job(jid, request), full=full)


@api.post("/sql/catalog/jobs/{jid}/cancel")
def catalog_job_cancel(jid: str, request: Request):
    j = _get_job(jid, request)
    if j.get("state") in TERMINAL:
        return _job_public(j)
    j["cancel"] = True
    _job_log(j, f"cancel requested by {user_of(request)}")
    if not j.get("task"):   # nothing drives it (should not happen): settle it now
        j.update(state="cancelled", finished=time.time())
    job_save(j)
    return _job_public(j)


@api.post("/sql/catalog/jobs/{jid}/retry")
async def catalog_job_retry(jid: str, request: Request):
    j = _get_job(jid, request)
    user = user_of(request)
    if user == ANON:
        raise HTTPException(403, "sign in to refresh catalog definitions")
    if j.get("state") not in TERMINAL:
        raise HTTPException(409, "the job is still running")
    nj = await start_job(user, j["scopes"], j.get("reannotate"), j.get("force"), retry_of=jid,
                         all_systems=bool((j.get("scope") or {}).get("all")))
    return {"job_id": nj["job_id"], "backend": nj["backend"], "job": _job_public(nj)}


# ── glossary view (terms grouped by node, linked tables with provenance) ─────
GLOSSARY_TTL = 60
_gl_cache: dict[str, Any] = {"at": 0.0, "data": None}


def datahub_ui() -> str:
    return os.environ.get("DATAHUB_UI_URL", "https://localhost:38081").rstrip("/")


def term_source(props: dict, nodes: list[dict] | None = None) -> str:
    """imported (BigQuery) | ai | curated, from the term's properties and its parent glossary nodes.
    Imported: termSource EXTERNAL, BigQuery named in sourceRef / sourceUrl / custom properties, a parent node id
    starting with vco-cx-out, or a parent node description that mentions BigQuery."""
    lib = _catlib()
    cps = {k.get("key", ""): str(k.get("value", "")) for k in props.get("customProperties") or []}
    hay = " ".join([str(props.get("sourceRef") or ""), str(props.get("sourceUrl") or "")] + list(cps.keys()) + list(cps.values())).lower()
    for n in nodes or []:
        nid = str(n.get("urn") or "").split("urn:li:glossaryNode:")[-1]
        if nid.startswith("vco-cx-out") or "bigquery" in str((n.get("properties") or {}).get("description") or "").lower():
            return "imported"
    if props.get("termSource") == "EXTERNAL" or "bigquery" in hay or "vco_cx_out" in hay:
        return "imported"
    if (lib and lib.is_ai_text(props.get("definition"))) or "ai-suggested" in hay or cps.get("ai_suggested") in ("true", "1"):
        return "ai"
    return "curated"


def trino_name(platform: str, name: str) -> str | None:
    """Map a DataHub dataset name to the catalog.schema.table a query would use."""
    parts = name.split(".")
    if platform == "trino" and len(parts) == 3:
        return name
    if platform in ("postgres", "dbt") and len(parts) == 3 and parts[0] == "bsp_edw":
        return "warehouse." + ".".join(parts[1:])
    if platform == "bigquery" and len(parts) == 3:
        proj = parts[0][3:] if parts[0].startswith("da-") else parts[0]
        return f"bq_{proj.replace('-', '_')}.{parts[1]}.{parts[2]}"
    return None


_GL_TERMS = """query g($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) { total
  searchResults { entity { urn ... on GlossaryTerm { name
    properties { name definition termSource sourceRef sourceUrl customProperties { key value } }
    parentNodes { nodes { urn properties { name description } } } } } } } }"""
_GL_LINKS = """query s($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) { total
  searchResults { entity { urn ... on Dataset { platform { name }
    tags { tags { tag { urn } } }
    glossaryTerms { terms { term { urn } } }
    schemaMetadata { fields { glossaryTerms { terms { term { urn } } } } }
    editableSchemaMetadata { editableSchemaFieldInfo { glossaryTerms { terms { term { urn } } } } } } } } } }"""


def _gl_scroll(lib, q: str, inp: dict, cap: int = 5000) -> list[dict]:
    out, start = [], 0
    while start < cap:
        d = (lib._gql(q, {"input": {**inp, "start": start, "count": 500}}) or {}).get("searchAcrossEntities") or {}  # noqa: SLF001
        rows = d.get("searchResults") or []
        out += [r["entity"] for r in rows]
        start += len(rows)
        if not rows or start >= (d.get("total") or 0):
            break
    return out


def glossary_view_datahub(lib) -> dict:
    """Terms with group path and source, and linked tables with provenance:
    'ai' = a table-level link on a dataset the annotator tagged ai-annotated (the annotator writes table-level
    terms only); 'curated' = column-level links (ingestion rules, imports) and table links on non-annotated datasets."""
    terms = []
    for e in _gl_scroll(lib, _GL_TERMS, {"types": ["GLOSSARY_TERM"], "query": "*"}):
        p = e.get("properties") or {}
        pnodes = (e.get("parentNodes") or {}).get("nodes") or []
        nodes = [n["properties"]["name"] for n in pnodes if n.get("properties")]
        lib_ai = bool(getattr(lib, "is_ai_text", None) and lib.is_ai_text(p.get("definition")))
        terms.append({"urn": e["urn"], "name": p.get("name") or e.get("name") or e["urn"].split(":")[-1],
                      "definition": p.get("definition") or "", "group": " / ".join(reversed(nodes)) or "Ungrouped",
                      "source": term_source(p, pnodes), "ai_definition": lib_ai, "tables": []})
    by = {t["urn"]: t for t in terms}
    links: dict[str, dict[str, dict]] = {u: {} for u in by}
    urns = list(by)
    for i in range(0, len(urns), 200):   # one search per field type and batch of terms (not one per term)
        chunk = urns[i:i + 200]
        for field in ("glossaryTerms", "fieldGlossaryTerms"):
            for ds in _gl_scroll(lib, _GL_LINKS, {"types": ["DATASET"], "query": "*", "orFilters": [{"and": [{"field": field, "values": chunk}]}]}):
                platform = ((ds.get("platform") or {}).get("name")) or lib.platform_from_urn(ds["urn"])
                name = lib.name_from_urn(ds["urn"])
                ai_ds = any(t["tag"]["urn"].endswith(":ai-annotated") for t in (ds.get("tags") or {}).get("tags") or [])
                table_terms = {t["term"]["urn"] for t in (ds.get("glossaryTerms") or {}).get("terms") or []}
                field_terms = {t["term"]["urn"] for f in ((ds.get("schemaMetadata") or {}).get("fields") or [])
                               for t in ((f.get("glossaryTerms") or {}).get("terms") or [])}
                field_terms |= {t["term"]["urn"] for f in ((ds.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo") or [])
                                for t in ((f.get("glossaryTerms") or {}).get("terms") or [])}
                for tu in (table_terms | field_terms) & set(chunk):
                    prov = "curated" if tu in field_terms or not ai_ds else "ai"
                    cur = links[tu].get(ds["urn"])
                    if cur and cur["provenance"] == "curated":
                        continue
                    links[tu][ds["urn"]] = {"name": name, "platform": platform, "trino": trino_name(platform, name),
                                            "level": "column" if tu in field_terms else "table", "provenance": prov}
    for u, t in by.items():
        merged: dict[str, dict] = {}   # the same table via postgres (column rules) and trino (annotator): one row
        for x in links[u].values():
            k = x["trino"] or f"{x['platform']}:{x['name']}"
            m = merged.get(k)
            if not m:
                merged[k] = {**x, "platforms": [x["platform"]]}
                continue
            m["platforms"] = sorted(set(m["platforms"]) | {x["platform"]})
            m["also_ai"] = bool(m.get("also_ai") or "ai" in (m["provenance"], x["provenance"]))
            if x["provenance"] == "curated":
                m.update(provenance="curated", level=x["level"])
        t["tables"] = sorted(merged.values(), key=lambda x: (x["trino"] is None, x["trino"] or x["name"]))
        t["datahub_url"] = f"{datahub_ui()}/glossaryTerm/{u}"
    return {"source": "datahub", "terms": sorted(terms, key=lambda t: (t["group"].lower(), t["name"].lower()))}


@api.get("/sql/glossary")
async def glossary_view(request: Request, refresh: bool = False):
    if not refresh and _gl_cache["data"] and time.time() - _gl_cache["at"] < GLOSSARY_TTL:
        return {**_gl_cache["data"], "cached_at": _gl_cache["at"], "cached": True}
    dh = await datahub()
    data = None
    if dh:
        try:
            data = await asyncio.to_thread(glossary_view_datahub, dh)
        except Exception as e:  # noqa: BLE001
            log.info("glossary view from DataHub failed: %s", e)
    if data is None:   # no DataHub: name-hint glossary over the Trino structure crawl
        items = await glossary_items(user_of(request))
        data = {"source": "name hints", "terms": [
            {"urn": None, "name": g["name"], "definition": g.get("definition") or "", "group": "Name hints", "source": "hint",
             "datahub_url": None, "tables": [{"name": t, "platform": "trino", "trino": t, "level": "table", "provenance": "hint"}
                                             for t in g.get("related_tables") or []]} for g in items]}
    groups: dict[str, int] = {}
    for t in data["terms"]:
        t["table_count"] = len(t["tables"])
        groups[t["group"]] = groups.get(t["group"], 0) + 1
    data["groups"] = [{"name": k, "terms": v} for k, v in groups.items()]
    _gl_cache.update(at=time.time(), data=data)
    return {**data, "cached_at": _gl_cache["at"], "cached": False}


async def definitions_context(tables: list[str], terms: list[str], user: str) -> tuple[str, str | None]:
    """Text block of catalog definitions for text-to-SQL (refreshed descriptions, columns, terms)."""
    if not tables and not terms:
        return "", None
    lines, newest = [], None
    lib = _catlib()
    for fqn in tables:
        parts = fqn.split(".")
        if len(parts) != 3 or not all(_IDENT.match(p) for p in parts):
            continue
        cols: list[str] = []
        desc = ""
        tags: list[str] = []
        dh = await datahub()
        if dh:
            try:
                d = await asyncio.to_thread(dh.describe, fqn)
                if d:
                    desc, tags = d.get("description") or "", d.get("tags") or []
                    cols = [f"{c['name']} {c.get('type', '')}" + (f" -- {c['description']}" if c.get("description") else "")
                            for c in d.get("columns") or []]
            except Exception:  # noqa: BLE001
                pass
        if not cols:
            try:
                e = await cached_schema(parts[0], parts[1], user)
                t = next((x for x in e["tables"] if x["name"] == parts[2]), None)
                if t:
                    desc = desc or t.get("comment") or ""
                    cols = [f"{c[0]} {c[1]}" for c in t["columns"]]
                    if lib and any(lib.is_pii_name(c[0]) for c in t["columns"]):
                        tags = tags + ["PII (suspected)"]
            except Exception:  # noqa: BLE001
                pass
        r = _refreshed_at(*parts)
        newest = max(filter(None, [newest, r])) if (newest or r) else None
        lines.append(f"- {fqn}: {desc or '(no description)'}" + (f" [tags: {', '.join(tags)}]" if tags else "")
                     + ("\n    columns: " + "; ".join(cols[:80]) if cols else ""))
    if terms:
        gl = {g["name"]: g for g in await glossary_items(user)}
        for t in terms[:12]:
            g = gl.get(t)
            if g:
                lines.append(f"- glossary {t}: {g.get('definition') or ''}"
                             + (f" (tables: {', '.join(g['related_tables'][:8])})" if g.get("related_tables") else ""))
    if not lines:
        return "", newest
    return ("Catalog definitions for the tables in scope (use these names and meanings; columns tagged "
            "PII (suspected) should be aggregated, not listed):\n" + "\n".join(lines)), newest


# ══ library endpoints ═════════════════════════════════════════════════════════
@api.get("/sql/library")
def lib_view(request: Request):
    return library_view(user_of(request))


@api.get("/sql/search")
def lib_search(q: str, request: Request):
    return {"results": search_library(user_of(request), q)}


@api.post("/sql/queries")
async def q_create(request: Request):
    return create_query(user_of(request), await request.json())


@api.patch("/sql/queries/{qid}")
async def q_update(qid: str, request: Request):
    return update_query(user_of(request), _check_id(qid), await request.json())


@api.delete("/sql/queries/{qid}")
def q_delete(qid: str, request: Request):
    delete_query(user_of(request), _check_id(qid))
    return {"deleted": qid}


@api.post("/sql/queries/{qid}/duplicate")
def q_duplicate(qid: str, request: Request):
    return duplicate_query(user_of(request), _check_id(qid))


@api.put("/sql/queries/order")
async def q_order(request: Request):
    reorder_queries(user_of(request), (await request.json()).get("ids") or [])
    return {"ok": True}


@api.post("/sql/collections")
async def c_create(request: Request):
    return create_collection(user_of(request), await request.json())


@api.put("/sql/collections/order")
async def c_order(request: Request):
    reorder_collections(user_of(request), (await request.json()).get("ids") or [])
    return {"ok": True}


@api.post("/sql/collections/import")
async def c_import(request: Request):
    body = await request.json()
    doc = body.get("doc") if isinstance(body, dict) and "doc" in body else body
    return import_collection(user_of(request), doc, shared=bool(isinstance(body, dict) and body.get("shared")),
                             name=body.get("name") if isinstance(body, dict) else None)


@api.patch("/sql/collections/{cid}")
async def c_update(cid: str, request: Request):
    return update_collection(user_of(request), _check_id(cid), await request.json())


@api.delete("/sql/collections/{cid}")
def c_delete(cid: str, request: Request):
    delete_collection(user_of(request), _check_id(cid))
    return {"deleted": cid}


@api.post("/sql/collections/{cid}/duplicate")
def c_duplicate(cid: str, request: Request):
    return duplicate_collection(user_of(request), _check_id(cid))


@api.post("/sql/collections/{cid}/share")
def c_share(cid: str, request: Request):
    return share_collection(user_of(request), _check_id(cid))


@api.get("/sql/collections/{cid}/export")
def c_export(cid: str, request: Request):
    exp = export_collection(user_of(request), _check_id(cid))
    fname = re.sub(r"[^A-Za-z0-9_-]+", "-", exp.get("name") or "collection").strip("-").lower() or "collection"
    return JSONResponse(exp, headers={"Content-Disposition": f'attachment; filename="{fname}.json"'})


@api.post("/sql/collections/{cid}/queries")
async def c_add(cid: str, request: Request):
    return add_to_collection(user_of(request), _check_id(cid), await request.json())


@api.patch("/sql/collections/{cid}/queries/{qid}")
async def c_query_update(cid: str, qid: str, request: Request):
    return update_shared_query(user_of(request), _check_id(cid), _check_id(qid), await request.json())


@api.delete("/sql/collections/{cid}/queries/{qid}")
def c_remove(cid: str, qid: str, request: Request):
    remove_from_collection(user_of(request), _check_id(cid), _check_id(qid))
    return {"removed": qid}
