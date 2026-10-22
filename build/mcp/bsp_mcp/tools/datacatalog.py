"""Catalog-backed discovery for text-to-SQL: find the right Trino table for a business term before
writing SQL. Backed by bsp_common.catalog (DataHub today; Atlan can back the same tools later).
Names come back exactly as Trino queries are written: catalog.schema.table."""
from __future__ import annotations

import re
from typing import Annotated

from pydantic import Field

from ..common import UserError, clamp, md_table, result, tool

G = "catalog"


def _cat():
    try:
        from bsp_common import catalog  # noqa: PLC0415
    except ImportError as e:
        raise UserError(f"bsp_common.catalog is not importable here: {e}") from None
    return catalog


def _need():
    cat = _cat()
    if cat.backend() == "none":
        raise UserError("no data catalog is configured (start DataHub: make datahub-up, then set the DATAHUB_TOKEN "
                        "secret in Studio) — fall back to trino_sources / trino_list")
    return cat


def _call(fn, *a, **kw):
    cat = _cat()
    try:
        return fn(*a, **kw)
    except cat.CatalogError as e:
        raise UserError(str(e)) from None


def _row(h: dict) -> dict:
    return {"table": h["name"], "platform": h["platform"], "description": (h.get("description") or "")[:160],
            "has_ban": h.get("has_ban"), "terms": ", ".join(h.get("terms") or []),
            "tags": ", ".join(t for t in h.get("tags") or [] if t != "ai-annotated"),
            "ai": "yes" if h.get("ai_annotated") else "",
            "source": h.get("source") or "", "certified": h.get("certified") or "",
            "identity": h.get("identity") or "", "project": h.get("project") or "",
            "exec_project": h.get("exec_project") or ""}


@tool(G, "Search the data catalog for tables by business term")
def catalog_search(
    query: Annotated[str, Field(description="Business words or names: 'churn', 'wire center', 'BRIM BAN mapping', 'invoice'")],
    platform: Annotated[str | None, Field(description="trino (default: every queryable table, catalog.schema.table), "
                                                      "bigquery (only the BigQuery catalogs bq_*), dbt, postgres, "
                                                      "or 'any'")] = "trino",
    limit: Annotated[int, Field(description="Max results (1–50)")] = 15,
) -> dict:
    """Find tables for a business question in the catalog (DataHub + Atlan side-by-side):
    names as Trino queries are written (catalog.schema.table across warehouse, billing_oracle, care_mssql
    and the BigQuery catalogs bq_*), descriptions, glossary terms, tags (e.g. 'PII (suspected)') and whether
    the table carries the ban join key. Atlan BigQuery assets also return the configured query identity,
    data project and execution project — pass those to bigquery_run_query (identity=, project=). For Trino,
    use the table name with trino_query. Descriptions marked '(AI-suggested)' were generated from metadata
    and may be wrong. Call this before writing SQL whenever the question uses business terms or BigQuery."""
    cat = _need()
    p = (platform or "").strip().lower()
    bq_only = p in ("bigquery", "bq", "google bigquery")      # BigQuery is queried through Trino's bq_* catalogs
    plat = "trino" if bq_only else (None if p in ("", "any", "all", "*") else p)
    n = clamp(limit, 1, 50)
    hits = _call(cat.search, query, limit=n * 3 if bq_only else n, platform=plat)
    if bq_only:
        hits = [h for h in hits if h["name"].startswith(("bq_", "bigquery."))][:n]
    rows = [_row(h) for h in hits]
    head = f"{len(rows)} table(s) for {query!r} from the {cat.backend()} catalog"
    err = getattr(cat, "last_atlan_error", lambda: "")()
    if err:
        first = err.splitlines()[0][:180]
        head += (f"\n\nAtlan API unavailable ({first}). "
                 f"Call atlan_ui_search(query={query!r}, kind='Table') to use the logged-in browser UI, "
                 "then atlan_ui_asset(url=<asset URL>) for its columns/schema — or set a valid Atlan API token "
                 "in Studio → Credentials.")
    return result(head + "\n\n" + md_table(rows, 50), backend=cat.backend(), tables=hits,
                  **({"atlan_error": err} if err else {}))


@tool(G, "Discover tables across the catalog for a whole question (multi-term, fully qualified)", open_world=True)
def catalog_discover(
    question: Annotated[str, Field(description="The full business question; domain synonyms are expanded from it")] = "",
    terms: Annotated[list[str] | None, Field(description="Explicit extra terms, merged with question-derived terms")] = None,
    platform: Annotated[str, Field(description="trino (default; fully qualified catalog.schema.table), bigquery, or any")] = "trino",
    limit_per_term: Annotated[int, Field(description="Results per term (1–10)")] = 5,
    max_terms: Annotated[int, Field(description="Search terms cap (1–24)")] = 20,
) -> dict:
    """Panoramic discovery across the local catalog / Trino names. Decompose a question into churn/inactive,
    noisy/calls/call center/complaints/tickets/dispatch/truck roll, care/service, usage, network/outage,
    account/customer/BAN, billing and NPS/risk terms; search DataHub + Atlan (API) for every term; dedupe
    and return fully qualified catalog.schema.table names with `has_ban` and matched terms. This is the
    fast, joinable-catalog counterpart to atlan_discover_assets (which uses the Atlan UI for Atlan-only
    assets). Use it before writing a multi-table join; then call catalog_join_map for keys."""
    from .catalog import _discovery_terms  # noqa: PLC0415  (shared domain lexicon)

    cat = _need()
    terms_list = _discovery_terms(question, terms, clamp(max_terms, 1, 24))
    if not terms_list:
        raise UserError("give a question with domain words or explicit terms")
    plat = None if (platform or "").strip().lower() in ("", "any", "all", "*") else platform.strip()
    found: dict[str, dict] = {}
    for term in terms_list:
        hits = _call(cat.search, term, limit=clamp(limit_per_term, 1, 10), platform=plat)
        for h in hits:
            row = _row(h)
            key = row["table"]
            if key in found:
                if term not in found[key]["matched_terms"]:
                    found[key]["matched_terms"].append(term)
                found[key]["has_ban"] = found[key].get("has_ban") or row.get("has_ban")
            else:
                row["matched_terms"] = [term]
                found[key] = row
    if not found:
        return result(f"no catalog tables for terms {terms_list}", backend=cat.backend(), terms=terms_list, tables=[])
    qlow = (question or "").lower()
    churn_q = any(w in qlow for w in ("churn", "inactive", "attrition", "retention", "at risk", "risk"))
    noisy_q = any(w in qlow for w in ("noisy", "call", "ticket", "dispatch", "care", "service", "usage",
                                      "network", "outage", "nps", "complaint"))
    for x in found.values():
        name = x["table"].lower()
        score = len(x["matched_terms"]) + (1 if x.get("has_ban") else 0)
        if churn_q and "churn" in name:
            score += 5
        if churn_q and "risk" in name:
            score += 3
        if churn_q and ("feature" in name or "features" in name):
            score += 8
        if churn_q and "mart" in name:
            score += 2
        if noisy_q and any(k in name for k in ("call", "ticket", "dispatch", "network", "usage", "nps", "sentiment")):
            score += 2
        x["_score"] = score
    out = sorted(found.values(), key=lambda x: (-x["_score"], not bool(x.get("has_ban")), x["table"]))
    base = [x["table"] for x in out
            if x.get("has_ban") and any(k in x["table"].lower() for k in ("churn", "risk", "feature", "scored"))][:5]
    signals = [x["table"] for x in out
               if x.get("has_ban") and any(k in x["table"].lower()
                                            for k in ("call", "ticket", "dispatch", "network", "usage", "nps", "sentiment"))][:8]
    for x in out:
        x.pop("_score", None)
    rows = [{"table": x["table"], "platform": x.get("platform") or "", "has_ban": x.get("has_ban"),
             "matched_terms": ", ".join(x["matched_terms"]), "description": (x.get("description") or "")[:120]}
            for x in out[:120]]
    err = getattr(cat, "last_atlan_error", lambda: "")()
    rec = ""
    if base:
        rec += "Likely base table(s): " + ", ".join(base) + "\n"
    if signals:
        rec += "Signal table(s): " + ", ".join(signals) + "\n"
    if rec:
        rec += "\n"
    head = (rec + f"{len(out)} catalog table(s) from {len(terms_list)} term(s) ({', '.join(terms_list)}); "
            f"{sum(1 for x in out if x.get('has_ban'))} carry ban\n\n" + md_table(rows, 120))
    if err:
        head += ("\n\nAtlan API unavailable; use atlan_discover_assets / atlan_ui_search for Atlan-UI-only assets "
                 "and atlan_ui_asset for their columns.")
    return result(head, backend=cat.backend(), terms=terms_list, tables=out, recommended={"base": base, "signals": signals},
                  **({"atlan_error": err} if err else {}))


_KEY_ROLES = [
    ("account", re.compile(r"(^|_)(ban|ban_clean|acct|account|customer|cust|subscriber|sub|end_user|contact)(_|$)", re.I)),
    ("account", re.compile(r"(src_bill_acct_num|acct_master_sk|accountid|account_number|billing_account|customer_id|"
                           r"subscriber_id|subscriptionid|subscription_id|sub_id|end_user_id|contact_id)", re.I)),
    ("location", re.compile(r"(clli|wire_?center|wirecenter)", re.I)),
    ("invoice", re.compile(r"(invoice|inv_doc|opbel|bill_ref)", re.I)),
    ("event", re.compile(r"(order|case|ticket|dispatch|session|call|transaction).*(id|sk|num|key)", re.I)),
]
_DATE_RE = re.compile(r"(date|_dt$|dt$|_at$|_ts$|month|day|timestamp|time$|start|end|created|updated)", re.I)
_METRIC_RE = re.compile(r"(count|amount|usage|score|nps|churn|inactive|risk|status|type|reason|revenue|arpu|speed|qos)", re.I)
_KEY_DEFAULTS = ["ban", "ban_clean", "acct_master_sk", "src_bill_acct_num", "accountid", "account_number",
                 "customer_id", "subscriber_id", "end_user_id", "clli", "wirecenter_clli", "wire_center",
                 "invoice_id", "opbel", "order_id", "case_id", "ticket_id", "dispatch_id"]


@tool(G, "Map join keys and columns across catalog/Trino tables", open_world=True)
def catalog_join_map(
    tables: Annotated[list[str], Field(description="Fully qualified catalog.schema.table names from catalog_discover")],
    keys: Annotated[list[str] | None, Field(description="Exact join keys to look for; defaults cover ban/account/customer/clli/invoice/order/case/ticket/dispatch")] = None,
    include_columns: Annotated[bool, Field(description="Include every column name in each mapped table (default true)")] = True,
    limit: Annotated[int, Field(description="Max tables to read (1–12)")] = 10,
) -> dict:
    """Read the real schema of several catalog/Trino tables and return a join map: account/ban/customer keys,
    location (clli/wire center), invoice, event (order/case/ticket/dispatch) keys, plus date and metric
    columns. Use this before writing a multi-table join; it tells you which tables can join on ban directly
    and which need a mapping (for example SRC_BILL_ACCT_NUM → BAN or WIRECENTER_CLLI → CLLI)."""
    cat = _need()
    wanted = [str(k) for k in (keys or _KEY_DEFAULTS) if str(k).strip()]
    picked = [str(t).strip() for t in (tables or []) if str(t).strip()][:clamp(limit, 1, 12)]
    if not picked:
        raise UserError("give one or more catalog.schema.table names")
    mapped, errors = [], []
    for table in picked:
        try:
            d = _call(cat.describe, table)
        except Exception as e:  # noqa: BLE001
            errors.append({"table": table, "error": f"{type(e).__name__}: {str(e)[:240]}"})
            continue
        if not d:
            errors.append({"table": table, "error": "not found in catalog"})
            continue
        cols = [c.get("name", "") for c in (d.get("columns") or []) if c.get("name")]
        low = {c.lower(): c for c in cols}
        roles: dict[str, list[str]] = {"account": [], "location": [], "invoice": [], "event": []}
        for c in cols:
            for role, rx in _KEY_ROLES:
                if rx.search(c) and c not in roles[role]:
                    roles[role].append(c)
        found = [k for k in wanted if k.lower() in low]
        mapped.append({
            "table": d.get("name") or table,
            "platform": d.get("platform") or "",
            "has_ban": bool(d.get("has_ban")),
            "n_columns": len(cols),
            "join_keys": found,
            **roles,
            "date_columns": [c for c in cols if _DATE_RE.search(c)],
            "metric_columns": [c for c in cols if _METRIC_RE.search(c)],
            **({"columns": cols} if include_columns else {}),
        })
    if not mapped:
        raise UserError("could not read any requested table: " + "; ".join(e["error"] for e in errors)[:500])
    common = sorted(set.intersection(*[set(x["join_keys"]) for x in mapped]))
    acct_common = sorted(set.intersection(*[set(x["account"]) for x in mapped]))
    rows = [{"table": x["table"], "has_ban": x["has_ban"], "account_keys": ", ".join(x["account"][:6]) or "—",
             "location_keys": ", ".join(x["location"][:4]) or "—", "event_keys": ", ".join(x["event"][:5]) or "—",
             "date_columns": ", ".join(x["date_columns"][:6]) or "—",
             "metric_columns": ", ".join(x["metric_columns"][:6]) or "—"} for x in mapped]
    head = (f"{len(mapped)} table(s) mapped"
            + (f" — exact common keys: {', '.join(common)}" if common else "")
            + (f" — common account keys: {', '.join(acct_common)}" if acct_common else "")
            + "\n\n" + md_table(rows, 12))
    if errors:
        head += "\n\nErrors:\n" + "\n".join(f"- {e['table']}: {e['error']}" for e in errors)
    head += ("\n\nJoin on a common account key when present; otherwise map SRC_BILL_ACCT_NUM / ACCT_MASTER_SK / "
             "WIRECENTER_CLLI through the customer/account table. Verify both sides' formats before joining.")
    return result(head, backend=cat.backend(), mapped=mapped, common_keys=common, account_common=acct_common, errors=errors)


@tool(G, "Describe a table from the data catalog")
def catalog_describe(
    table: Annotated[str, Field(description="catalog.schema.table (as from catalog_search) or a DataHub urn")],
) -> dict:
    """Catalog detail of one table: description, domain, glossary terms, tags, and every column with its type,
    description and tags (PII (suspected) marks columns whose NAME suggests personal data). Metadata only —
    no data is read."""
    cat = _need()
    d = _call(cat.describe, table)
    if not d:
        return result(f"{table} is not in the catalog — try catalog_search or trino_describe", table=None)
    cols = [{"column": c["name"], "type": c["type"], "description": c["description"][:120],
             "tags": ", ".join(c["tags"]), "terms": ", ".join(c["terms"])} for c in d["columns"]]
    head = (f"**{d['name']}** ({d['platform']}) — {d['description'] or '(no description)'}\n"
            f"domain: {d.get('domain') or '—'} · terms: {', '.join(d['terms']) or '—'} · tags: {', '.join(d['tags']) or '—'}"
            f" · has ban: {d['has_ban']}")
    if d.get("identity"):
        head += (f"\nquery identity: **{d['identity']}** on {d.get('project') or '—'}"
                 f" · jobs run in {d.get('exec_project') or d.get('project') or '—'}")
    return result(head + "\n\n" + md_table(cols, 120), table=d)


@tool(G, "Business glossary terms and their tables")
def catalog_glossary(
    term: Annotated[str | None, Field(description="Filter: part of a term name, e.g. 'BAN', 'churn'; omit for all")] = None,
) -> dict:
    """The business glossary (BAN, CLLI, invoice, churn, dispute, wire center, copper, fiber, ...): each term's
    definition and the tables linked to it."""
    cat = _need()
    terms = _call(cat.glossary)
    if term:
        t = term.lower()
        terms = [x for x in terms if t in x["name"].lower() or t in (x.get("definition") or "").lower()]
    lines = [f"- **{x['name']}**: {(x.get('definition') or '')[:200]}"
             + (f"\n  tables: {', '.join(x['related_tables'][:10])}" if x.get("related_tables") else "") for x in terms]
    return result(f"{len(terms)} term(s)\n\n" + "\n".join(lines[:60]), terms=terms)


@tool(G, "Browse the catalog: catalogs, schemas, tables")
def catalog_browse(
    catalog: Annotated[str | None, Field(description="Trino catalog; omit to list catalogs")] = None,
    schema: Annotated[str | None, Field(description="Schema in the catalog; omit to list schemas")] = None,
) -> dict:
    """The catalogued hierarchy (only what was ingested into the catalog) with table descriptions."""
    cat = _need()
    t = _call(cat.tree, catalog, schema)
    if not catalog:
        rows = t["catalogs"]
    elif not schema:
        rows = t["schemas"]
    else:
        rows = [{"table": x["name"], "description": (x.get("description") or "")[:140], "has_ban": x["has_ban"],
                 "terms": ", ".join(x["terms"])} for x in t["tables"]]
    return result(f"{len(rows)} item(s)\n\n" + md_table(rows, 80), tree=t)
