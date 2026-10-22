"""INTERFACE STABLE — signatures and return shapes below are the contract.

Data catalog access for the appliance (harness, MCP, CLIs): one small API over
DataHub and Atlan side-by-side: DataHub for the local Trino/warehouse
assets and lineage, Atlan for the enterprise catalog, glossary and BigQuery.

    backend()                         "datahub" | "atlan" | "none"
    available()                       backend() != "none" and it answers
    search(query, types=None, limit)  [{urn, platform, name, description, tags,
                                        terms, has_ban, ai_annotated}]
    describe(name)                    one dataset: the search fields + columns
    tree(catalog=None, schema=None)   catalog → schema → table hierarchy
    tables(catalog)                   every catalogued table of one catalog
    glossary()                        [{urn, name, definition, related_tables}]
    refresh(scope, reannotate, force) {job_id}   scoped re-ingestion (+ annotate)
    job(job_id)                       {state, log_tail, counts}
    cancel(job_id)                    stop a refresh job → state "cancelled"
    annotate(urns, force=False)       metadata-only AI annotation → counts
    tag_pii(urns)                     rule-only "PII (suspected)" column tags

Names: Trino assets are addressed exactly as queries are written —
``catalog.schema.table`` (DataHub platform ``trino``). Other platforms keep
their native dotted name (dbt ``bsp_edw.marts.fct_x``, postgres ``db.schema.t``).

Backends (picked from env, no SDK dependency — plain httpx + GraphQL):
  * DataHub: ``DATAHUB_GMS_URL`` (default http://datahub-frontend:9002/api/gms,
    the frontend's GMS proxy — GMS itself stays on DataHub's own network) and
    ``DATAHUB_TOKEN`` (a DataHub personal access token, Studio secret). Both
    set → "datahub".
  * Atlan: ``ATLAN_BASE_URL`` + ``ATLAN_API_TOKEN`` → "atlan" (search / describe
    via bsp_connectors.atlan when importable; refresh / annotate are DataHub
    only for now and raise CatalogError). ``CATALOG_BACKEND`` forces one.
  * otherwise "none": every read returns empty results, refresh raises.

AI annotation (``annotate``) sends METADATA ONLY to the LLM gateway — table and
column names, types, existing descriptions, tags and glossary terms. It never
reads or sends data rows or value samples. Generated text carries the suffix
" (AI-suggested)" and the tag ``ai-annotated``; human-written descriptions are
never overwritten (``force=True`` may replace earlier AI-suggested text only).
Columns whose names look like personal data get the tag ``PII (suspected)``.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any

import httpx

from . import atlan_config

AI_SUFFIX = " (AI-suggested)"
AI_TAG = "ai-annotated"
PII_TAG = "PII (suspected)"          # display name; tag id below (ids cannot hold spaces)
PII_TAG_ID = "pii-suspected"
TRINO_PLATFORM = "trino"
DEFAULT_GMS_URL = "http://datahub-frontend:9002/api/gms"
ENV = os.environ.get("DATAHUB_ENV", "PROD")
TIMEOUT = float(os.environ.get("DATAHUB_TIMEOUT", "30"))
# business glossary terms the annotator may link (name → keywords in table / column names)
TERM_HINTS: dict[str, tuple[str, ...]] = {
    "BAN": ("ban", "billing_account", "billing_acct", "account_number", "acct_num", "acct_no"),
    "CLLI": ("clli",),
    "Invoice": ("invoice", "inv_", "bill_amt", "billed"),
    "Churn": ("churn", "disconnect", "attrition"),
    "Dispute": ("dispute", "adjustment", "credit_req"),
    "Wire center": ("wire_center", "wirecenter", "wc_", "central_office"),
    "Copper": ("copper", "dsl", "adsl", "vdsl", "pots"),
    "Fiber": ("fiber", "fibre", "ftth", "gpon", "xgs", "ont"),
}
# column-name heuristics for suspected personal data (names only, never values)
PII_PATTERNS = re.compile(
    r"(^|_)(first_?name|last_?name|full_?name|middle_?name|cust(omer)?_?name|contact_?name|name|e_?mail|email_?addr\w*|"
    r"phone|phone_?num\w*|mobile|cell|tel|fax|addr|address\w*|street|addr_?line\w*|zip|zip_?code|postal\w*|"
    r"ssn|social_?sec\w*|tax_?id|tin|dob|birth\w*|date_?of_?birth|account_?holder\w*|acct_?holder\w*|"
    r"driver\w*lic\w*|passport\w*|ip_?addr\w*|lat|latitude|lon|lng|longitude|geo\w*)($|_)", re.I)
BAN_COLUMNS = re.compile(r"^(ban|ban_id|ban_nbr|ban_num(ber)?|billing_account_(number|num|no|id)|bill_acct_(nbr|num|no))$", re.I)


class CatalogError(RuntimeError):
    """The catalog is not configured, unreachable, or refused the request."""


# ── backend selection ───────────────────────────────────────────────────────
def backend() -> str:
    """"datahub" | "atlan" | "both" | "none".

    Default is side-by-side when both are configured: DataHub for the local
    Trino/warehouse assets + lineage, Atlan for the enterprise catalog
    (BigQuery, glossary, certification). CATALOG_BACKEND forces one.
    """
    forced = os.environ.get("CATALOG_BACKEND", "").strip().lower()
    if forced in ("datahub", "atlan", "both", "none"):
        return forced
    dh = bool(os.environ.get("DATAHUB_TOKEN", "").strip())
    at = bool(os.environ.get("ATLAN_API_TOKEN", "").strip())
    if dh and at:
        return "both"
    if dh:
        return "datahub"
    if at:
        return "atlan"
    return "none"


def available() -> bool:
    """True when the configured backend answers (a cheap probe, 3 s timeout)."""
    b = backend()
    if b in ("datahub", "both"):
        try:
            _gql("{ me { corpUser { username } } }", timeout=3)
            return True
        except Exception:
            return b == "atlan" and _atlan() is not None
    if b == "atlan":
        return _atlan() is not None
    return False


# ── reads ───────────────────────────────────────────────────────────────────
def search(query: str, types: list[str] | None = None, limit: int = 20,
           platform: str | None = None) -> list[dict[str, Any]]:
    """Full-text search over DataHub and/or Atlan (side-by-side by default).

    Returns [{urn, platform, name, description, tags, terms, has_ban,
    ai_annotated, source, sources, certified, identity, project, exec_project}]
    — ``name`` is ``catalog.schema.table`` for Trino assets. Atlan BigQuery
    assets are mapped to Trino's ``bq_<project>.<dataset>.<table>`` and carry
    the configured query identity/execution project."""
    b = backend()
    dh_hits: list[dict[str, Any]] = []
    at_hits: list[dict[str, Any]] = []
    if b in ("datahub", "both"):
        n = min(200, max(limit * 2, 20))
        dh_hits = _dh_search(query, types or ["DATASET"], n, platform)
        if len(dh_hits) < n:
            # DataHub matches all words; widen with the significant words alone (then rank by name overlap)
            words = _words(query)
            for q in ([" ".join(words)] if len(words) > 1 else []) + words[:4]:
                if q != query:
                    dh_hits += _dh_search(q, types or ["DATASET"], n, platform)
            seen: set[str] = set()
            dh_hits = [h for h in dh_hits if not (h["urn"] in seen or seen.add(h["urn"]))]
        for h in dh_hits:
            h.setdefault("source", "datahub")
            h.setdefault("sources", ["datahub"])
    if b in ("atlan", "both"):
        at_hits = _atlan_search(query, limit)
    if b == "both":
        return rerank(query, _merge_hits(dh_hits, at_hits))[:limit]
    if b == "atlan":
        return rerank(query, at_hits)[:limit]
    return dh_hits[:limit]


_STOP = {"the", "and", "for", "per", "with", "table", "tables", "data", "that", "holds", "which", "from", "about",
         "account", "accounts", "customer", "customers", "bigquery", "how", "many", "what", "where"}


def _words(query: str) -> list[str]:
    return list(dict.fromkeys(w for w in re.split(r"[^a-z0-9]+", query.lower()) if len(w) > 2 and w not in _STOP))


def rerank(query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable re-order: hits whose table NAME carries more of the query words
    first (then description words), keeping the catalog's order otherwise —
    "days until churn" → ``…days_until_churn_eis`` before generic churn tables."""
    words = _words(query)
    if not words:
        return hits

    def score(h: dict[str, Any]) -> tuple[int, int]:
        name = h.get("name", "").lower().split(".")[-1]
        desc = (h.get("description") or "").lower()
        return (-sum(w in name for w in words), -sum(w in desc for w in words))
    return sorted(hits, key=score)


def describe(name: str, platform: str = TRINO_PLATFORM) -> dict[str, Any] | None:
    """One dataset by name (``catalog.schema.table`` for Trino) or URN: the
    search fields plus ``columns`` [{name, type, description, tags, terms}],
    ``domain`` and the BigQuery identity/project when known. With both
    catalogs configured, DataHub columns/lineage and Atlan business
    descriptions/terms/certification are merged."""
    b = backend()
    dh = at = None
    if b in ("datahub", "both"):
        dh = _dh_describe(name if name.startswith("urn:li:") else dataset_urn(name, platform))
    if b in ("atlan", "both"):
        at = _atlan_describe(name)
    if dh and at:
        return _merge_describe(dh, at)
    return dh or at


def tree(catalog: str | None = None, schema: str | None = None) -> dict[str, Any]:
    """Hierarchy for cascading pickers, Trino platform only:
      tree()                 → {"catalogs": [{name, datasets}]}
      tree(catalog)          → {"catalog", "schemas": [{name, datasets}]}
      tree(catalog, schema)  → {"catalog", "schema", "tables": [{name, urn,
                                description, tags, terms, has_ban, ai_annotated,
                                last_refreshed}]}
    ``last_refreshed`` = DataHub's last-ingested time (ISO-8601 UTC) or None.
    ``datasets`` are counts. Empty lists when no catalog backend is set."""
    if backend() not in ("datahub", "both"):
        return {"catalogs": []} if not catalog else {"catalog": catalog, "schemas": []} if not schema else \
            {"catalog": catalog, "schema": schema, "tables": []}
    return _dh_tree(catalog, schema)


def tables(catalog: str) -> list[dict[str, Any]]:
    """Every catalogued table of one Trino catalog in one call: [{name
    ("schema.table"), urn, description, tags, terms, has_ban, ai_annotated,
    last_refreshed}].
    [] when no DataHub backend."""
    if backend() not in ("datahub", "both"):
        return []
    out = []
    for e in _scroll_datasets(query=catalog, fields=_LIGHT):
        parts = name_from_urn(e["urn"]).split(".")
        if len(parts) >= 3 and parts[0] == catalog:
            cols = [{"fieldPath": f["fieldPath"]} for f in ((e.get("schemaMetadata") or {}).get("fields") or [])]
            sh = _shape(e | {"schemaMetadata": {"fields": cols}})
            out.append({"name": ".".join(parts[1:])} | {k: sh[k] for k in
                       ("urn", "description", "tags", "terms", "has_ban", "ai_annotated")}
                       | {"last_refreshed": _iso_ms(e.get("lastIngested"))})
    return sorted(out, key=lambda t: t["name"])


def glossary() -> list[dict[str, Any]]:
    """Business glossary, merged from DataHub and Atlan:
    [{urn, name, definition, related_tables, sources}]."""
    b = backend()
    out: list[dict[str, Any]] = []
    if b in ("datahub", "both"):
        out += [dict(t, sources=t.get("sources") or ["datahub"]) for t in _dh_glossary()]
    if b in ("atlan", "both"):
        out += _atlan_glossary()
    merged: dict[str, dict[str, Any]] = {}
    for t in out:
        key = (t.get("name") or "").strip().lower()
        if not key:
            continue
        if key in merged:
            base = merged[key]
            if not base.get("definition") and t.get("definition"):
                base["definition"] = t["definition"]
            base["related_tables"] = sorted(set((base.get("related_tables") or []) + (t.get("related_tables") or [])))
            base["sources"] = sorted(set((base.get("sources") or []) + (t.get("sources") or [])))
        else:
            merged[key] = dict(t)
    return list(merged.values())


# ── refresh (scoped re-ingestion) and jobs ──────────────────────────────────
def refresh(scope: dict[str, Any], reannotate: bool = False, force: bool = False) -> dict[str, str]:
    """Re-ingest one Trino scope into DataHub and optionally re-annotate it.
    scope = {"catalog": "bq_tsa_dv", "schema": "optional", "tables": ["optional", ...]}.
    Runs as DataHub managed ingestion (an ingestion source per catalog,
    executed by the datahub-actions executor, with allow patterns for the
    scope; schema + columns only, no profiling). With ``reannotate`` the
    annotator then runs over the refreshed tables (``force`` may overwrite
    AI-suggested text, never human-written text). Returns {"job_id"}
    immediately; poll ``job(job_id)``."""
    if backend() not in ("datahub", "both"):
        raise CatalogError("refresh needs the DataHub catalog (set DATAHUB_TOKEN)")
    return _dh_refresh(scope, reannotate, force)


def job(job_id: str) -> dict[str, Any]:
    """{"state": queued|running|succeeded|failed|cancelled, "log_tail": str,
    "counts": {...}, "scope": {...}} for a ``refresh`` job."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if not j:
        raise CatalogError(f"unknown job {job_id}")
    if j.get("execution_urn") and j["state"] in ("queued", "running") and j.get("phase") == "ingest" and not j.get("cancel"):
        _poll_execution(j)
    return {k: j.get(k) for k in ("job_id", "state", "phase", "log_tail", "counts", "scope", "started", "finished")}


def cancel(job_id: str) -> dict[str, Any]:
    """Cancel a queued / running ``refresh`` job: cancels the DataHub
    execution request (ingest phase) or stops before the next table
    (annotate phase). Returns ``job(job_id)`` with state "cancelled"; a job
    that already finished is returned unchanged."""
    with _jobs_lock:
        j = _jobs.get(job_id)
    if not j:
        raise CatalogError(f"unknown job {job_id}")
    if j["state"] in ("queued", "running"):
        if j.get("phase") == "ingest" and j.get("execution_urn"):
            try:
                _gql("""mutation c($input: CancelIngestionExecutionRequestInput!) {
                  cancelIngestionExecutionRequest(input: $input) }""",
                     {"input": {"ingestionSourceUrn": j["source_urn"], "executionRequestUrn": j["execution_urn"]}})
            except CatalogError as e:
                j["log_tail"] += f"\ncancel: {e}"
        j["cancel"] = True
        j.update(state="cancelled", finished=time.time())
    return job(job_id)


# ── annotation ──────────────────────────────────────────────────────────────
def annotate(urns: list[str], force: bool = False, model: str | None = None,
             max_tables: int | None = None, log: Any = None, stop: Any = None) -> dict[str, int]:
    """Metadata-only AI annotation of the given dataset URNs (see module doc).
    For each table lacking a human description: one-sentence table
    description, short column descriptions, glossary-term links where a term
    clearly applies, a domain, ``PII (suspected)`` tags on personal-looking
    column names, and the ``ai-annotated`` tag. Idempotent: skips tables that
    already carry AI text unless ``force``. Returns counts {tables_seen,
    tables_annotated, columns_described, pii_columns, terms_linked, skipped,
    errors}. ``stop`` (a callable) is checked before each table."""
    if backend() not in ("datahub", "both"):
        raise CatalogError("annotate needs the DataHub catalog (set DATAHUB_TOKEN)")
    return _annotate(urns, force=force, model=model or os.environ.get("CATALOG_ANNOTATE_MODEL", "chat-default"),
                     max_tables=max_tables or int(os.environ.get("CATALOG_ANNOTATE_MAX", "300")), log=log, stop=stop)


# ── helpers (public, small) ─────────────────────────────────────────────────
def dataset_urn(name: str, platform: str = TRINO_PLATFORM, env: str = ENV) -> str:
    return f"urn:li:dataset:(urn:li:dataPlatform:{platform},{name},{env})"


def name_from_urn(urn: str) -> str:
    m = re.match(r"urn:li:dataset:\(urn:li:dataPlatform:[^,]+,(.+),[A-Z]+\)$", urn)
    return m.group(1) if m else urn


def platform_from_urn(urn: str) -> str:
    m = re.match(r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),", urn)
    return m.group(1) if m else ""


def is_pii_name(column: str) -> bool:
    """Column NAME looks like personal data (names only — values never seen)."""
    return bool(PII_PATTERNS.search(column.strip().lower().replace(" ", "_")))


def is_ban_name(column: str) -> bool:
    return bool(BAN_COLUMNS.match(column.strip()))


def suggested_terms(table: str, columns: list[str]) -> list[str]:
    """Glossary terms whose keywords appear in the table or column names."""
    hay = " ".join([table.lower()] + [c.lower() for c in columns])
    out = []
    for term, keys in TERM_HINTS.items():
        if term == "BAN":
            if any(is_ban_name(c) for c in columns) or any(k in hay for k in keys[1:]):
                out.append(term)
        elif term in ("Copper", "Fiber"):
            if any(re.search(rf"(^|[_ .]){re.escape(k)}($|[_ .])", hay) for k in keys):
                out.append(term)
        elif any(k in hay for k in keys):
            out.append(term)
    return out


def is_ai_text(text: str | None) -> bool:
    return bool(text) and text.rstrip().endswith(AI_SUFFIX.strip())


# ═════════════════════════════ implementation ══════════════════════════════
def _gms() -> str:
    return (os.environ.get("DATAHUB_GMS_URL", "").strip() or DEFAULT_GMS_URL).rstrip("/")


def _headers() -> dict[str, str]:
    tok = os.environ.get("DATAHUB_TOKEN", "").strip()
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _gql(query: str, variables: dict[str, Any] | None = None, timeout: float | None = None) -> dict[str, Any]:
    try:
        # DataHub is on the appliance network: never through the egress proxy
        r = httpx.post(_gms() + "/api/graphql", json={"query": query, "variables": variables or {}},
                       headers=_headers(), timeout=timeout or TIMEOUT, trust_env=False)
    except httpx.HTTPError as e:
        raise CatalogError(f"DataHub unreachable at {_gms()}: {type(e).__name__}") from e
    if r.status_code in (401, 403):
        raise CatalogError("DataHub refused the token (DATAHUB_TOKEN)")
    if r.status_code >= 400:
        raise CatalogError(f"DataHub GraphQL HTTP {r.status_code}: {r.text[:200]}")
    body = r.json()
    if body.get("errors"):        # partial data with errors = a mutation that did not apply
        raise CatalogError("DataHub GraphQL: " + "; ".join(e.get("message", "?") for e in body["errors"])[:400])
    return body.get("data") or {}


_DS_FIELDS = """
  urn
  ... on Dataset {
    name platform { name }
    properties { name description qualifiedName }
    editableProperties { description }
    tags { tags { tag { urn properties { name } } } }
    glossaryTerms { terms { term { urn properties { name } } } }
    domain { domain { properties { name } } }
    schemaMetadata { fields { fieldPath type nativeDataType description
      globalTags { tags { tag { properties { name } } } }
      glossaryTerms { terms { term { properties { name } } } } } }
    editableSchemaMetadata { editableSchemaFieldInfo { fieldPath description
      globalTags { tags { tag { properties { name } } } }
      glossaryTerms { terms { term { properties { name } } } } } }
  }
"""


def _tagnames(block: dict[str, Any] | None) -> list[str]:
    return [((t.get("tag") or {}).get("properties") or {}).get("name") or (t.get("tag") or {}).get("urn", "").split(":")[-1]
            for t in ((block or {}).get("tags") or [])]


def _termnames(block: dict[str, Any] | None) -> list[str]:
    return [((t.get("term") or {}).get("properties") or {}).get("name") or (t.get("term") or {}).get("urn", "").split(":")[-1]
            for t in ((block or {}).get("terms") or [])]


def _columns(e: dict[str, Any]) -> list[dict[str, Any]]:
    edit = {f["fieldPath"]: f for f in (((e.get("editableSchemaMetadata") or {}).get("editableSchemaFieldInfo")) or [])}
    out = []
    for f in ((e.get("schemaMetadata") or {}).get("fields") or []):
        ed = edit.get(f["fieldPath"], {})
        out.append({"name": f["fieldPath"].split(".")[-1] if "[" in f["fieldPath"] else f["fieldPath"],
                    "field_path": f["fieldPath"], "type": f.get("nativeDataType") or f.get("type") or "",
                    "description": ed.get("description") or f.get("description") or "",
                    "tags": sorted(set(_tagnames(f.get("globalTags")) + _tagnames(ed.get("globalTags")))),
                    "terms": sorted(set(_termnames(f.get("glossaryTerms")) + _termnames(ed.get("glossaryTerms"))))})
    return out


def _shape(e: dict[str, Any], with_columns: bool = False) -> dict[str, Any]:
    urn = e.get("urn", "")
    props = e.get("properties") or {}
    desc = ((e.get("editableProperties") or {}).get("description") or props.get("description") or "").strip()
    cols = _columns(e)
    tags = _tagnames(e.get("tags"))
    out = {"urn": urn, "platform": ((e.get("platform") or {}).get("name") or platform_from_urn(urn)),
           "name": name_from_urn(urn) if urn.startswith("urn:li:dataset:") else (props.get("name") or e.get("name") or urn),
           "description": desc, "tags": tags, "terms": _termnames(e.get("glossaryTerms")),
           "has_ban": any(is_ban_name(c["name"]) for c in cols),
           "ai_annotated": AI_TAG in tags or is_ai_text(desc),
           "domain": (((e.get("domain") or {}).get("domain") or {}).get("properties") or {}).get("name", "")}
    if with_columns:
        out["columns"] = [{k: c[k] for k in ("name", "field_path", "type", "description", "tags", "terms")} for c in cols]
    return out


def _dh_search(query: str, types: list[str], limit: int, platform: str | None) -> list[dict[str, Any]]:
    filters = [{"field": "platform", "values": [f"urn:li:dataPlatform:{platform}"]}] if platform else []
    q = """query s($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
      total searchResults { entity { type %s
        ... on GlossaryTerm { properties { name definition } }
        ... on Domain { properties { name description } } } } } }""" % _DS_FIELDS
    inp: dict[str, Any] = {"types": types, "query": query or "*", "start": 0, "count": max(1, min(int(limit), 200))}
    if filters:
        inp["orFilters"] = [{"and": filters}]
    data = _gql(q, {"input": inp})
    out = []
    for r in (data.get("searchAcrossEntities") or {}).get("searchResults") or []:
        e = r.get("entity") or {}
        if e.get("type") == "DATASET":
            out.append(_shape(e))
        else:
            p = e.get("properties") or {}
            out.append({"urn": e.get("urn"), "platform": e.get("type", "").lower(), "name": p.get("name", ""),
                        "description": p.get("definition") or p.get("description") or "", "tags": [], "terms": [],
                        "has_ban": False, "ai_annotated": False})
    return out


def _dh_describe(urn: str) -> dict[str, Any] | None:
    data = _gql("query d($urn: String!) { dataset(urn: $urn) { %s } }" % _DS_FIELDS, {"urn": urn})
    e = data.get("dataset")
    if not e or not (e.get("properties") or e.get("schemaMetadata")):
        return None
    return _shape(e, with_columns=True)


def _scroll_datasets(query: str = "*", platform: str = TRINO_PLATFORM, fields: str = "", batch: int = 500,
                     cap: int = 20000) -> list[dict[str, Any]]:
    """Every dataset of a platform matching a query (scrollAcrossEntities)."""
    q = """query s($input: ScrollAcrossEntitiesInput!) { scrollAcrossEntities(input: $input) {
      nextScrollId searchResults { entity { urn %s } } } }""" % (fields or "")
    out, scroll = [], None
    while len(out) < cap:
        inp: dict[str, Any] = {"types": ["DATASET"], "query": query, "count": batch,
                               "orFilters": [{"and": [{"field": "platform", "values": [f"urn:li:dataPlatform:{platform}"]}]}]}
        if scroll:
            inp["scrollId"] = scroll
        d = (_gql(q, {"input": inp}).get("scrollAcrossEntities") or {})
        out += [r["entity"] for r in d.get("searchResults") or []]
        scroll = d.get("nextScrollId")
        if not scroll:
            break
    return out


_LIGHT = """... on Dataset { lastIngested properties { description } editableProperties { description }
  tags { tags { tag { urn properties { name } } } } glossaryTerms { terms { term { urn properties { name } } } }
  schemaMetadata { fields { fieldPath } } }"""


def _dh_tree(catalog: str | None, schema: str | None) -> dict[str, Any]:
    if not catalog:
        counts: dict[str, int] = {}
        for e in _scroll_datasets():
            counts[name_from_urn(e["urn"]).split(".")[0]] = counts.get(name_from_urn(e["urn"]).split(".")[0], 0) + 1
        return {"catalogs": [{"name": k, "datasets": v} for k, v in sorted(counts.items())]}
    ents = [e for e in _scroll_datasets(query=f"{catalog}.{schema}" if schema else catalog,
                                         fields=_LIGHT if schema else "")
            if name_from_urn(e["urn"]).split(".")[0] == catalog]
    if not schema:
        counts = {}
        for e in ents:
            parts = name_from_urn(e["urn"]).split(".")
            if len(parts) >= 3:
                counts[parts[1]] = counts.get(parts[1], 0) + 1
        return {"catalog": catalog, "schemas": [{"name": k, "datasets": v} for k, v in sorted(counts.items())]}
    tables = []
    for e in ents:
        parts = name_from_urn(e["urn"]).split(".")
        if len(parts) >= 3 and parts[1] == schema:
            cols = [{"fieldPath": f["fieldPath"]} for f in ((e.get("schemaMetadata") or {}).get("fields") or [])]
            s = _shape(e | {"schemaMetadata": {"fields": cols}})
            tables.append({"name": ".".join(parts[2:])} | {k: s[k] for k in
                          ("urn", "description", "tags", "terms", "has_ban", "ai_annotated")}
                          | {"last_refreshed": _iso_ms(e.get("lastIngested"))})
    return {"catalog": catalog, "schema": schema, "tables": sorted(tables, key=lambda t: t["name"])}


def _iso_ms(ms: Any) -> str | None:
    """DataHub epoch-millis → ISO-8601 UTC ('2026-10-19T00:43:46Z'); None when unknown."""
    if not ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(ms) / 1000))


def _dh_glossary() -> list[dict[str, Any]]:
    q = """query g($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
      searchResults { entity { urn ... on GlossaryTerm { name properties { name definition } } } } } }"""
    data = _gql(q, {"input": {"types": ["GLOSSARY_TERM"], "query": "*", "start": 0, "count": 500}})
    out = []
    for r in (data.get("searchAcrossEntities") or {}).get("searchResults") or []:
        e = r["entity"]
        p = e.get("properties") or {}
        out.append({"urn": e["urn"], "name": p.get("name") or e.get("name", ""), "definition": p.get("definition", ""),
                    "related_tables": _term_tables(e["urn"])})
    return sorted(out, key=lambda t: t["name"].lower())


def _term_tables(term_urn: str, limit: int = 50) -> list[str]:
    names: list[str] = []
    for field in ("glossaryTerms", "fieldGlossaryTerms"):
        q = """query s($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
          searchResults { entity { urn } } } }"""
        try:
            d = _gql(q, {"input": {"types": ["DATASET"], "query": "*", "start": 0, "count": limit,
                                   "orFilters": [{"and": [{"field": field, "values": [term_urn]}]}]}})
        except CatalogError:
            continue
        names += [name_from_urn(r["entity"]["urn"]) for r in (d.get("searchAcrossEntities") or {}).get("searchResults") or []]
    return list(dict.fromkeys(names))


# ── Atlan (via bsp_connectors when importable) ──────────────────────────────
def _atlan() -> Any:
    try:
        from bsp_connectors import atlan  # type: ignore
        return atlan
    except Exception:
        return None


_LAST_ATLAN_ERROR = ""


def last_atlan_error() -> str:
    """Last Atlan read error (e.g. 401 invalid token); "" when healthy."""
    return _LAST_ATLAN_ERROR


def _atlan_terms(r: dict[str, Any]) -> list[str]:
    vals = r.get("meanings") or (r.get("attributes") or {}).get("meanings") or []
    return [m.get("displayText", m) if isinstance(m, dict) else str(m) for m in vals if m]


def _atlan_tags(r: dict[str, Any]) -> list[str]:
    return list(r.get("classificationNames") or (r.get("attributes") or {}).get("classificationNames") or [])


def _atlan_columns(attrs: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in attrs.get("columns") or []:
        if isinstance(c, str):
            out.append({"name": c, "type": "", "description": "", "tags": [], "terms": []})
            continue
        if not isinstance(c, dict):
            continue
        ca = c.get("attributes") or c
        cname = ca.get("name") or c.get("name") or ""
        if not cname:
            continue
        out.append({"name": cname, "type": ca.get("dataType") or c.get("dataType") or "",
                    "description": ca.get("userDescription") or ca.get("description") or "",
                    "tags": list(ca.get("classificationNames") or c.get("classificationNames") or []),
                    "terms": [m.get("displayText", m) if isinstance(m, dict) else str(m)
                              for m in (ca.get("meanings") or c.get("meanings") or [])]})
    return out


def _atlan_hit(r: dict[str, Any]) -> dict[str, Any]:
    """Map an Atlan asset to the catalog hit shape + Trino name + identity."""
    attrs = r.get("attributes") or {}
    qn = attrs.get("qualifiedName") or r.get("qualifiedName") or ""
    connector = str(attrs.get("connectorName") or r.get("connectorName") or "").lower()
    if not connector and "/" in qn:
        parts = qn.split("/")
        connector = parts[1].lower() if len(parts) > 1 else ""
    db = str(attrs.get("databaseName") or "")
    schema = str(attrs.get("schemaName") or "")
    table = str(attrs.get("tableName") or attrs.get("name") or r.get("name") or "")
    if not (db and schema and table) and qn.count("/") >= 4:
        parts = qn.split("/")
        db, schema, table = parts[-3], parts[-2], parts[-1]
    data_project = db
    if connector == "bigquery" and db and schema and table:
        from .pick import bq_catalog  # noqa: PLC0415
        name = f"{bq_catalog(db)}.{schema}.{table}"
    elif connector == "trino" and db and schema and table:
        name = f"{db}.{schema}.{table}"
        data_project = ""
    else:
        name = qn or table
    mapping = atlan_config.lookup(connection=qn, project=data_project)
    cols = _atlan_columns(attrs)
    return {
        "urn": r.get("guid") or qn,
        "platform": "trino" if connector in ("bigquery", "trino") else (connector or "atlan"),
        "name": name,
        "description": attrs.get("userDescription") or attrs.get("description") or r.get("description") or "",
        "tags": _atlan_tags(r),
        "terms": _atlan_terms(r),
        "has_ban": (any(is_ban_name(c["name"]) for c in cols) if cols else None),
        "ai_annotated": False,
        "source": "atlan",
        "sources": ["atlan"],
        "certified": attrs.get("certificateStatus") or r.get("certificateStatus") or "",
        "owner": ", ".join(list(attrs.get("ownerUsers") or []) + list(attrs.get("ownerGroups") or [])),
        "atlan_name": qn,
        "identity": mapping["identity"],
        "project": mapping["project"] or data_project,
        "exec_project": mapping["exec_project"],
    }


def _merge_hits(dh: list[dict[str, Any]], at: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for h in dh:
        key = (h.get("name") or h.get("urn") or "").lower()
        merged[key] = dict(h, source=h.get("source", "datahub"),
                           sources=h.get("sources") or ["datahub"])
    for h in at:
        key = (h.get("name") or h.get("urn") or "").lower()
        if key not in merged:
            merged[key] = dict(h)
            continue
        base = merged[key]
        base["sources"] = sorted(set((base.get("sources") or []) + (h.get("sources") or ["atlan"])))
        base["source"] = "both"
        for f in ("description", "certified", "owner", "identity", "project", "exec_project", "atlan_name"):
            if h.get(f):
                base[f] = h[f]
        for f in ("tags", "terms"):
            base[f] = sorted(set((base.get(f) or []) + (h.get(f) or [])))
        if base.get("has_ban") is None and h.get("has_ban") is not None:
            base["has_ban"] = h["has_ban"]
    return list(merged.values())


def _atlan_describe(name: str) -> dict[str, Any] | None:
    global _LAST_ATLAN_ERROR
    a = _atlan()
    if a is None:
        return None
    try:
        rows = a.search_assets(name, type_names=["Table", "View"], limit=5)
        _LAST_ATLAN_ERROR = ""
    except Exception as e:  # noqa: BLE001
        _LAST_ATLAN_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
        return None
    if not rows:
        return None
    hit = None
    qn = ""
    for r in rows:
        h = _atlan_hit(r)
        if h["name"].lower().endswith(name.lower().split(".")[-1]) or h["atlan_name"].lower() == name.lower():
            hit, qn = h, h["atlan_name"]
            break
    if hit is None:
        hit, qn = _atlan_hit(rows[0]), _atlan_hit(rows[0])["atlan_name"]
    attrs: dict[str, Any] = rows[0].get("attributes") or {}
    try:
        asset = a.get_asset(qn)
        attrs = asset.get("attributes") or attrs
        hit = dict(hit, description=attrs.get("userDescription") or attrs.get("description") or hit["description"])
    except Exception:  # noqa: BLE001
        pass
    hit["columns"] = _atlan_columns(attrs)
    hit["domain"] = attrs.get("domain") or ""
    return hit


def _merge_describe(dh: dict[str, Any], at: dict[str, Any]) -> dict[str, Any]:
    base = dict(dh)
    base["sources"] = sorted(set((dh.get("sources") or ["datahub"]) + (at.get("sources") or ["atlan"])))
    base["source"] = "both"
    for f in ("description", "certified", "owner", "identity", "project", "exec_project", "atlan_name"):
        if at.get(f):
            base[f] = at[f]
    for f in ("tags", "terms"):
        base[f] = sorted(set((base.get(f) or []) + (at.get(f) or [])))
    if base.get("has_ban") is None and at.get("has_ban") is not None:
        base["has_ban"] = at["has_ban"]
    cols = {(c.get("name") or "").lower(): dict(c) for c in (base.get("columns") or [])}
    for c in at.get("columns") or []:
        key = (c.get("name") or "").lower()
        if key in cols:
            if c.get("description"):
                cols[key]["description"] = c["description"]
            for f in ("tags", "terms"):
                cols[key][f] = sorted(set((cols[key].get(f) or []) + (c.get(f) or [])))
        else:
            cols[key] = c
    base["columns"] = list(cols.values())
    return base


def _atlan_glossary() -> list[dict[str, Any]]:
    a = _atlan()
    if a is None:
        return []
    try:
        rows = a.search_assets("", type_names=["AtlasGlossaryTerm"], limit=200)
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        attrs = r.get("attributes") or {}
        tname = attrs.get("name") or r.get("name") or attrs.get("qualifiedName") or r.get("qualifiedName") or ""
        if not tname:
            continue
        related: list[str] = []
        for e in attrs.get("assignedEntities") or []:
            if isinstance(e, dict):
                rq = e.get("qualifiedName") or (e.get("uniqueAttributes") or {}).get("qualifiedName")
                if rq:
                    related.append(rq)
        out.append({"urn": r.get("guid") or tname, "name": tname,
                    "definition": attrs.get("userDescription") or attrs.get("description") or "",
                    "related_tables": related, "sources": ["atlan"]})
    return out


def _atlan_search(query: str, limit: int) -> list[dict[str, Any]]:
    global _LAST_ATLAN_ERROR
    a = _atlan()
    if a is None:
        return []
    try:
        rows = a.search_assets(query, type_names=["Table", "View", "DbtModel"], limit=limit)
        _LAST_ATLAN_ERROR = ""
    except Exception as e:  # noqa: BLE001 — an invalid token must not break DataHub search
        _LAST_ATLAN_ERROR = f"{type(e).__name__}: {str(e)[:200]}"
        return []
    return [_atlan_hit(r) for r in rows]


# ── refresh via DataHub managed ingestion ───────────────────────────────────
_jobs: dict[str, dict[str, Any]] = {}
_jobs_lock = threading.Lock()
SOURCE_PREFIX = "bsp-trino-"


def scope_recipe(scope: dict[str, Any]) -> dict[str, Any]:
    """DataHub trino recipe limited to one catalog / schema / table list
    (schema + columns only, no profiling, no usage)."""
    cat = (scope.get("catalog") or "").strip()
    if not re.fullmatch(r"[a-z0-9_]+", cat or "-"):
        raise CatalogError("scope.catalog must be a Trino catalog name")
    sch = (scope.get("schema") or "").strip()
    tables = [t for t in (scope.get("tables") or []) if t]
    cfg: dict[str, Any] = {
        "host_port": f"{os.environ.get('TRINO_HOST', 'trino')}:{os.environ.get('TRINO_PORT', '8080')}",
        "database": cat, "username": os.environ.get("DATAHUB_TRINO_USER", "datahub"),
        "include_views": True, "include_tables": True, "profiling": {"enabled": False},
        "env": ENV, "convert_urns_to_lowercase": False,
        "stateful_ingestion": {"enabled": False},
    }
    if sch:
        cfg["schema_pattern"] = {"allow": [f"^{re.escape(sch)}$"], "ignoreCase": True}
        if tables:
            cfg["table_pattern"] = {"allow": [f"^{re.escape(sch)}\\.{re.escape(t)}$" for t in tables], "ignoreCase": True}
    else:
        cfg["schema_pattern"] = {"deny": ["^information_schema$"]}
    return {"source": {"type": "trino", "config": cfg}}


def _source_urn(catalog: str) -> str:
    """Find or create the managed ingestion source for a catalog."""
    name = SOURCE_PREFIX + catalog
    d = _gql("""query l($input: ListIngestionSourcesInput!) { listIngestionSources(input: $input) {
      ingestionSources { urn name } } }""", {"input": {"start": 0, "count": 200, "query": name}})
    for s in (d.get("listIngestionSources") or {}).get("ingestionSources") or []:
        if s["name"] == name:
            return s["urn"]
    d = _gql("""mutation c($input: UpdateIngestionSourceInput!) { createIngestionSource(input: $input) }""",
             {"input": _source_input(catalog, scope_recipe({"catalog": catalog}))})
    return d["createIngestionSource"]


def _source_input(catalog: str, recipe: dict[str, Any]) -> dict[str, Any]:
    return {"name": SOURCE_PREFIX + catalog, "type": "trino",
            "description": f"Trino catalog {catalog} (schema + columns; appliance refresh, scope set per run)",
            "config": {"recipe": json.dumps(recipe), "executorId": "default", "debugMode": False,
                       # CLI version the executor pip-installs (server v1.7.0.1 has no 1.7.0.1 wheel on PyPI)
                       "version": os.environ.get("DATAHUB_INGEST_VERSION", "1.7.0.2")}}


def _dh_refresh(scope: dict[str, Any], reannotate: bool, force: bool) -> dict[str, str]:
    recipe = scope_recipe(scope)       # validates the scope
    cat = scope["catalog"].strip()
    src = _source_urn(cat)
    _gql("""mutation u($urn: String!, $input: UpdateIngestionSourceInput!) { updateIngestionSource(urn: $urn, input: $input) }""",
         {"urn": src, "input": _source_input(cat, recipe)})
    ex = _gql("""mutation r($input: CreateIngestionExecutionRequestInput!) { createIngestionExecutionRequest(input: $input) }""",
              {"input": {"ingestionSourceUrn": src}})["createIngestionExecutionRequest"]
    jid = uuid.uuid4().hex[:12]
    j = {"job_id": jid, "state": "queued", "phase": "ingest", "scope": {k: scope.get(k) for k in ("catalog", "schema", "tables")},
         "execution_urn": ex, "source_urn": src, "reannotate": reannotate, "force": force, "log_tail": "",
         "counts": {}, "started": time.time(), "finished": None}
    with _jobs_lock:
        _jobs[jid] = j
    return {"job_id": jid}


def _poll_execution(j: dict[str, Any]) -> None:
    d = _gql("""query e($urn: String!) { executionRequest(urn: $urn) { result { status report startTimeMs durationMs
      structuredReport { serializedValue } } } }""", {"urn": j["execution_urn"]})
    res = ((d.get("executionRequest") or {}).get("result")) or {}
    status = (res.get("status") or "").upper()
    j["log_tail"] = (res.get("report") or "")[-3000:]
    if status in ("", "PENDING"):
        j["state"] = "queued"
        return
    if status == "RUNNING":
        j["state"] = "running"
        return
    try:
        rep = json.loads(((res.get("structuredReport") or {}).get("serializedValue")) or "{}")
        src = (rep.get("source") or {}).get("report") or {}
        j["counts"]["tables_scanned"] = src.get("tables_scanned")
        j["counts"]["views_scanned"] = src.get("views_scanned")
        j["counts"]["events_produced"] = src.get("events_produced")
    except Exception:
        pass
    if status in ("CANCELLED", "ABORTED"):
        j.update(state="cancelled", finished=time.time())
        return
    if status != "SUCCESS":
        j.update(state="failed", finished=time.time())
        return
    if not j["reannotate"]:
        j.update(state="succeeded", finished=time.time())
        return
    j.update(state="running", phase="annotate")
    threading.Thread(target=_annotate_scope, args=(j,), daemon=True).start()


def _annotate_scope(j: dict[str, Any]) -> None:
    try:
        sc = j["scope"]
        prefix = sc["catalog"] + (f".{sc['schema']}." if sc.get("schema") else ".")
        urns = [e["urn"] for e in _scroll_datasets(query=prefix.rstrip("."))
                if name_from_urn(e["urn"]).lower().startswith(prefix.lower())]
        if sc.get("tables"):
            want = {f"{sc['catalog']}.{sc['schema']}.{t}".lower() for t in sc["tables"]}
            urns = [u for u in urns if name_from_urn(u).lower() in want]
        lines: list[str] = []
        counts = annotate(urns, force=j["force"], log=lines.append, stop=lambda: bool(j.get("cancel")))
        j["counts"].update(counts)
        j["log_tail"] = (j["log_tail"] + "\n" + "\n".join(lines))[-3000:]
        if not j.get("cancel"):
            j.update(state="succeeded", finished=time.time())
    except Exception as e:     # noqa: BLE001 — surfaced through job()
        j["log_tail"] += f"\nannotate failed: {type(e).__name__}: {e}"
        j.update(state="failed", finished=time.time())


# ── annotation ──────────────────────────────────────────────────────────────
# the appliance domains (stacks/datahub/recipes/domains.json); "Other" = leave unset
DOMAINS = ("Billing", "Customer Care", "Network", "Sales", "Other")
# annotator term name → glossary term names it may resolve to (business_glossary.yml)
TERM_ALIASES = {"BAN": ("ban",), "CLLI": ("clli",), "Invoice": ("invoice",), "Churn": ("churn",),
                "Dispute": ("billing dispute", "dispute"), "Wire center": ("wire center",),
                "Copper": ("copper",), "Fiber": ("fiber",)}
_PROMPT = """You document tables in a data catalog for a US internet service provider (copper and fiber
broadband; BAN = billing account number, the customer key). You see METADATA ONLY: the table name,
column names and types, and any existing descriptions. Do not invent values.
Return JSON: {"description": "<one sentence about the table>",
 "columns": {"<column>": "<short description, <= 12 words>", ...},
 "domain": "<one of: %s>"}
Describe every column listed. Be plain and specific; say "likely" when a name is ambiguous."""


def _llm_json(model: str, meta: dict[str, Any]) -> dict[str, Any]:
    from . import llm, settings

    body = {"model": model, "temperature": 0, "max_tokens": 4000, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": _PROMPT % ", ".join(DOMAINS)},
                         {"role": "user", "content": json.dumps(meta)}]}
    r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/chat/completions", json=body,
                   headers=llm._headers(), timeout=llm.TIMEOUT)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or "{}"
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


COLUMN_CHUNK = 40


def metadata_for_llm(d: dict[str, Any], max_columns: int = 120, columns: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """The ONLY payload the annotator sends to a model: names, types and the
    existing descriptions / tags / terms. No values, no samples, no stats.
    ``columns`` limits it to a subset (chunked annotation of wide tables)."""
    cols = d.get("columns", [])[:max_columns] if columns is None else columns
    return {"table": d["name"], "platform": d.get("platform", ""), "existing_description": d.get("description", ""),
            "tags": [t for t in d.get("tags", []) if t != AI_TAG], "terms": d.get("terms", []),
            "columns": [{"name": c["name"], "type": c["type"], **({"description": c["description"]} if c["description"] else {})}
                        for c in cols]}


_ensured: set[str] = set()


def _ensure_tag(name: str, description: str, tag_id: str | None = None) -> str:
    tag_id = tag_id or name
    urn = "urn:li:tag:" + tag_id
    if urn in _ensured:
        return urn
    d = _gql("query t($urn: String!) { tag(urn: $urn) { urn properties { name } } }", {"urn": urn})
    if not (d.get("tag") or {}).get("properties"):
        try:
            _gql("mutation c($input: CreateTagInput!) { createTag(input: $input) }",
                 {"input": {"id": tag_id, "name": name, "description": description}})
        except CatalogError as e:
            if "already exists" not in str(e):
                raise
    _ensured.add(urn)
    return urn


def _ensure_pii_tag() -> str:
    return _ensure_tag(PII_TAG, "Column name suggests personal data (name heuristics only; values never inspected)", PII_TAG_ID)


def tag_pii(urns: list[str], log: Any = None) -> dict[str, int]:
    """Rule-only pass (no model): tag every column whose NAME looks personal
    with ``PII (suspected)``. Idempotent. Returns {tables_seen, pii_columns,
    errors}."""
    if backend() not in ("datahub", "both"):
        raise CatalogError("tag_pii needs the DataHub catalog (set DATAHUB_TOKEN)")
    say = log or (lambda m: None)
    tag = _ensure_pii_tag()
    c = {"tables_seen": 0, "pii_columns": 0, "errors": 0}
    for urn in urns:
        c["tables_seen"] += 1
        try:
            d = _dh_describe(urn)
            for col in (d or {}).get("columns", []):
                if is_pii_name(col["name"]) and PII_TAG not in col["tags"]:
                    _add_tag(tag, urn, col["field_path"])
                    c["pii_columns"] += 1
        except Exception as e:     # noqa: BLE001
            c["errors"] += 1
            say(f"error on {name_from_urn(urn)}: {type(e).__name__}: {str(e)[:160]}")
    return c


def _term_urns() -> dict[str, str]:
    q = """query g($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
      searchResults { entity { urn ... on GlossaryTerm { name properties { name } } } } } }"""
    d = _gql(q, {"input": {"types": ["GLOSSARY_TERM"], "query": "*", "start": 0, "count": 500}})
    out = {}
    for r in (d.get("searchAcrossEntities") or {}).get("searchResults") or []:
        e = r["entity"]
        nm = ((e.get("properties") or {}).get("name") or e.get("name") or "").lower()
        out[nm] = e["urn"]
    return out


def _domain_urns() -> dict[str, str]:
    q = """query g($input: SearchAcrossEntitiesInput!) { searchAcrossEntities(input: $input) {
      searchResults { entity { urn ... on Domain { properties { name } } } } } }"""
    d = _gql(q, {"input": {"types": ["DOMAIN"], "query": "*", "start": 0, "count": 200}})
    return {((r["entity"].get("properties") or {}).get("name") or "").lower(): r["entity"]["urn"]
            for r in (d.get("searchAcrossEntities") or {}).get("searchResults") or []}


def _resolve_term(name: str, terms: dict[str, str]) -> str | None:
    for cand in TERM_ALIASES.get(name, (name.lower(),)):
        if cand in terms:
            return terms[cand]
    return None


def _annotate(urns: list[str], force: bool, model: str, max_tables: int, log: Any, stop: Any = None) -> dict[str, int]:
    say = log or (lambda m: None)
    c = {"tables_seen": 0, "tables_annotated": 0, "columns_described": 0, "pii_columns": 0, "terms_linked": 0,
         "skipped": 0, "errors": 0}
    lock = threading.Lock()
    ai_tag = _ensure_tag(AI_TAG, "Description or links written by the appliance AI annotator from metadata only — review before relying on it")
    pii_tag = _ensure_pii_tag()
    terms, domains = _term_urns(), _domain_urns()
    max_cols = int(os.environ.get("CATALOG_ANNOTATE_MAX_COLUMNS", "120"))

    def bump(**kw: int) -> None:
        with lock:
            for k, v in kw.items():
                c[k] += v

    def one(urn: str) -> None:
        with lock:
            if c["tables_annotated"] >= max_tables or (stop and stop()):
                return
            c["tables_seen"] += 1
        try:
            d = _dh_describe(urn)
            if not d:
                return bump(skipped=1)
            human_table = bool(d["description"]) and not is_ai_text(d["description"])
            if d["ai_annotated"] and not force:
                return bump(skipped=1)
            # very wide tables: describe the first CATALOG_ANNOTATE_MAX_COLUMNS columns
            todo_cols = [col for col in d["columns"][:max_cols]
                         if not col["description"] or (force and is_ai_text(col["description"]))]
            if human_table and not todo_cols:
                return bump(skipped=1)
            # columns go to the model in chunks so the JSON answer is never cut off
            gen: dict[str, Any] = {}
            gcols: dict[str, Any] = {}
            chunks = [todo_cols[i:i + COLUMN_CHUNK] for i in range(0, len(todo_cols), COLUMN_CHUNK)] or [[]]
            for i, chunk in enumerate(chunks):
                part = _llm_json(model, metadata_for_llm(d, columns=chunk if i else (chunk or d["columns"][:COLUMN_CHUNK])))
                if i == 0:
                    gen = part
                gcols |= {str(k).lower(): v for k, v in (part.get("columns") or {}).items()}
            if not human_table and gen.get("description"):
                _set_description(urn, None, str(gen["description"]).strip())
            described = pii = linked = 0
            for col in todo_cols:
                text = gcols.get(col["name"].lower())
                if text:
                    _set_description(urn, col["field_path"], str(text).strip())
                    described += 1
            for col in d["columns"]:
                if is_pii_name(col["name"]) and PII_TAG not in col["tags"]:
                    _add_tag(pii_tag, urn, col["field_path"])
                    pii += 1
            for term in suggested_terms(d["name"].split(".")[-1], [x["name"] for x in d["columns"]]):
                turn = _resolve_term(term, terms)
                if turn and term not in d["terms"]:
                    _add_term(turn, urn)
                    linked += 1
            dom = domains.get(str(gen.get("domain") or "").lower())
            if dom and not d.get("domain"):
                _gql("mutation s($e: String!, $d: String!) { setDomain(entityUrn: $e, domainUrn: $d) }", {"e": urn, "d": dom})
            if AI_TAG not in d["tags"]:
                _add_tag(ai_tag, urn, None)
            bump(tables_annotated=1, columns_described=described, pii_columns=pii, terms_linked=linked)
            say(f"annotated {d['name']} ({described} columns)")
        except Exception as e:     # noqa: BLE001 — count and carry on
            bump(errors=1)
            say(f"error on {name_from_urn(urn)}: {type(e).__name__}: {str(e)[:160]}")

    workers = max(1, int(os.environ.get("CATALOG_ANNOTATE_WORKERS", "4")))
    if workers == 1:
        for u in urns:
            one(u)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(one, urns))
    if c["tables_annotated"] >= max_tables:
        say(f"cap of {max_tables} tables reached")
    if stop and stop():
        say("cancelled")
    return c


def _set_description(urn: str, field_path: str | None, text: str) -> None:
    text = text.rstrip()
    if not is_ai_text(text):
        text += AI_SUFFIX
    inp: dict[str, Any] = {"description": text, "resourceUrn": urn}
    if field_path:
        inp |= {"subResourceType": "DATASET_FIELD", "subResource": field_path}
    _gql("mutation u($input: DescriptionUpdateInput!) { updateDescription(input: $input) }", {"input": inp})


def _add_tag(tag_urn: str, urn: str, field_path: str | None) -> None:
    inp: dict[str, Any] = {"tagUrns": [tag_urn], "resourceUrn": urn}
    if field_path:
        inp |= {"subResourceType": "DATASET_FIELD", "subResource": field_path}
    _gql("mutation a($input: AddTagsInput!) { addTags(input: $input) }", {"input": inp})


def _add_term(term_urn: str, urn: str) -> None:
    _gql("mutation a($input: AddTermsInput!) { addTerms(input: $input) }",
         {"input": {"termUrns": [term_urn], "resourceUrn": urn}})
