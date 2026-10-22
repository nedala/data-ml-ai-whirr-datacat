"""Build a DataHub glossary group from a BigQuery dataset's metadata (names and types only, never rows).

    python glossary_from_bq.py META.json --node vco-cx-out --title "VCO CX Out (dev BigQuery)" \
        --catalog bq_rpt_vco_cn_opq1 [--model sql-default] [--dry-run]

META.json is a list of {dataset, table, type, description, columns: [{name, type, description}]} as read
with the BigQuery API (get_table schemas). The LLM gateway proposes business terms with definitions and the
tables/columns each term describes; every definition is marked AI-suggested. Terms are created under one
glossary node and linked to the Trino-named datasets (catalog.dataset.table, lower case) and their columns.
Re-runnable: existing terms keep human-edited definitions; AI-suggested ones are refreshed.
Runs in the harness container (bsp_common.catalog, DATAHUB_TOKEN, LLM gateway).
"""
from __future__ import annotations

import argparse
import json
import re
import sys

import httpx
from bsp_common import catalog as c
from bsp_common import llm, settings

PROMPT = """You are a data steward for DataPlatform, a US internet provider (copper DSL and fiber).
You receive the METADATA ONLY (table and column names and types) of the BigQuery dataset {dataset}, which
holds customer-experience (contact centre, IVR, chat, tickets, dispatches, outages, devices, subscribers) data.
Propose a business glossary of 20 to 35 terms that a new analyst needs to understand this dataset.
Rules:
- A term is a business concept (e.g. "Call Funnel", "IVR Group", "Dispatch", "ONT", "DSLAM Outage", "BAN"), not a table name.
- Definition: 1 to 2 plain sentences, specific to an ISP contact centre. If the meaning is a guess from names, say "Likely".
- tables: the table names (exactly as given) the term describes; columns: "TABLE.COLUMN" pairs (exactly as given), at most 8.
- synonyms: abbreviations or column-name variants (e.g. "conv_id" for Conversation).
Return JSON: {{"terms": [{{"name": str, "definition": str, "synonyms": [str], "tables": [str], "columns": [str]}}]}}"""


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def propose(meta: list[dict], dataset: str, model: str) -> list[dict]:
    compact = [{"table": t["table"], "columns": [col["name"] for col in t["columns"]][:80]} for t in meta]
    body = {"model": model, "temperature": 0, "max_tokens": 8000, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": PROMPT.format(dataset=dataset)},
                         {"role": "user", "content": json.dumps(compact)}]}
    r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/chat/completions", json=body,
                   headers=llm._headers(), timeout=300)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"] or "{}"
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text).get("terms", [])


def gql(q: str, v: dict) -> dict:
    return c._gql(q, v)


def ensure_node(node_id: str, title: str, about: str) -> str:
    urn = f"urn:li:glossaryNode:{node_id}"
    try:
        gql("mutation n($i: CreateGlossaryEntityInput!) { createGlossaryNode(input: $i) }",
            {"i": {"id": node_id, "name": title, "description": about}})
    except c.CatalogError as e:
        if "already exists" not in str(e).lower():
            raise
    return urn


def existing_definition(term_urn: str) -> str | None:
    d = gql("query t($u: String!) { glossaryTerm(urn: $u) { properties { description } } }", {"u": term_urn})
    t = d.get("glossaryTerm")
    return (t.get("properties") or {}).get("description") if t else None


def upsert_term(node_urn: str, term_id: str, name: str, definition: str, log) -> str:
    urn = f"urn:li:glossaryTerm:{term_id}"
    current = existing_definition(urn)
    text = definition.rstrip() + c.AI_SUFFIX
    if current is None:
        gql("mutation t($i: CreateGlossaryEntityInput!) { createGlossaryTerm(input: $i) }",
            {"i": {"id": term_id, "name": name, "description": text, "parentNode": node_urn}})
        log(f"created  {name}")
    elif c.is_ai_text(current):
        gql("mutation u($i: DescriptionUpdateInput!) { updateDescription(input: $i) }",
            {"i": {"description": text, "resourceUrn": urn}})
        log(f"updated  {name}")
    else:
        log(f"kept     {name} (human-edited definition)")
    return urn


def link(term_urn: str, ds_urn: str, field: str | None = None) -> None:
    inp = {"termUrns": [term_urn], "resourceUrn": ds_urn}
    if field:
        inp |= {"subResourceType": "DATASET_FIELD", "subResource": field}
    gql("mutation a($i: AddTermsInput!) { addTerms(input: $i) }", {"i": inp})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("meta")
    ap.add_argument("--node", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--catalog", required=True, help="Trino catalog the dataset is ingested under, e.g. bq_rpt_vco_cn_opq1")
    ap.add_argument("--model", default="sql-default")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    meta = json.load(open(a.meta))
    datasets = sorted({t["dataset"] for t in meta})
    by_table = {t["table"]: t for t in meta}
    terms = propose(meta, ", ".join(datasets), a.model)
    print(f"{len(terms)} terms proposed from {len(meta)} tables (metadata only)")
    if a.dry_run:
        print(json.dumps(terms, indent=2)[:6000])
        return 0
    node = ensure_node(a.node, a.title, f"Business terms for BigQuery {', '.join(datasets)} ({a.catalog}). "
                                         "Definitions are AI-suggested from table and column names only; edit them in DataHub.")
    n_tab = n_col = skipped = 0
    for t in terms:
        name = (t.get("name") or "").strip()
        if not name or not t.get("definition"):
            continue
        syn = [s for s in (t.get("synonyms") or []) if s]
        definition = t["definition"].strip() + (f" Also seen as: {', '.join(syn)}." if syn else "")
        urn = upsert_term(node, f"{a.node}.{slug(name)}", name, definition, print)
        for tab in t.get("tables") or []:
            src = by_table.get(tab)
            if not src:
                skipped += 1
                continue
            try:
                link(urn, c.dataset_urn(f"{a.catalog}.{src['dataset'].lower()}.{tab.lower()}"))
                n_tab += 1
            except c.CatalogError:
                skipped += 1
        for tc in t.get("columns") or []:
            tab, _, col = tc.partition(".")
            src = by_table.get(tab)
            if not (src and col and any(x["name"] == col for x in src["columns"])):
                skipped += 1
                continue
            try:
                link(urn, c.dataset_urn(f"{a.catalog}.{src['dataset'].lower()}.{tab.lower()}"), col.lower())
                n_col += 1
            except c.CatalogError:
                skipped += 1
    print(f"done: {len(terms)} terms under {node}; {n_tab} table links, {n_col} column links, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
