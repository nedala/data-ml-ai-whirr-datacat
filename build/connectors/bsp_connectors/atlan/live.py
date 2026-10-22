"""Atlan REST (metadata API) with an API token. Endpoints used:

    POST /api/meta/search/indexsearch     Elasticsearch DSL search
    GET  /api/meta/lineage/{guid}         classic lineage (guidEntityMap + relations)
    POST /api/meta/entity/bulk            upsert Process entities (dbt / canvas lineage)

ATLAN_BASE_URL can point at the mock (…/connectors/atlan) for parity tests.
ATLAN_CONNECTION_QN maps dbt relations to Atlan table qualifiedNames
(e.g. default/bigquery/1712345678 → default/bigquery/1712345678/<db>/<schema>/<table>).
"""

from __future__ import annotations

import re
from typing import Any

from .._common import NotFound, env
from .._http import request

DEFAULT_BASE_URL = "https://atlan.example.com"

ATTRS = ["name", "qualifiedName", "description", "userDescription", "certificateStatus", "ownerUsers",
         "ownerGroups", "connectorName", "connectionQualifiedName", "schemaName", "databaseName", "tableName",
         "dataType", "columnCount", "rowCount", "meanings", "assignedEntities"]


def base() -> str:
    return (env("ATLAN_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def _token(force: bool = False) -> str:
    return env("ATLAN_API_TOKEN", "mock-token")


def _req(method: str, path: str, **kw: Any) -> Any:
    return request(method, base() + path, token=_token, system="atlan", **kw)


def _shape(e: dict) -> dict:
    at = e.get("attributes", {}) or {}
    e.setdefault("qualifiedName", at.get("qualifiedName"))
    e.setdefault("name", at.get("name") or e.get("displayText"))
    return e


def indexsearch(dsl: dict, attributes: list[str] | None = None) -> dict:
    return _req("POST", "/api/meta/search/indexsearch",
                json={"dsl": dsl, "attributes": attributes or ATTRS, "suppressLogs": True}) or {}


def search_assets(query: str, type_names: list[str] | None = None, limit: int = 25) -> list[dict]:
    must: list[dict] = []
    q = (query or "").strip()
    if q and q != "*":
        must.append({"multi_match": {"query": q, "fields": ["name^5", "name.keyword^10", "qualifiedName",
                                                            "description", "userDescription", "__meanings.text"]}})
    filt: list[dict] = [{"term": {"__state": "ACTIVE"}}]
    if type_names:
        filt.append({"terms": {"__typeName.keyword": list(type_names)}})
    dsl = {"from": 0, "size": limit, "query": {"bool": {"must": must or [{"match_all": {}}], "filter": filt}}}
    return [_shape(e) for e in indexsearch(dsl).get("entities", []) or []]


def multi_search(terms: list[str], type_names: list[str] | None = None, limit: int = 25,
                 match: str = "any") -> list[dict]:
    """One Atlas indexsearch DSL query across several terms.

    match='any' → bool.should (OR, minimum_should_match=1); match='all' → bool.must (AND).
    This is the true multi-term API path; the Atlan UI command palette does not
    reliably implement boolean OR across terms.
    """
    ts = [str(t).strip() for t in (terms or []) if str(t).strip()]
    if not ts:
        return []
    fields = ["name^5", "name.keyword^10", "qualifiedName", "description", "userDescription", "__meanings.text"]
    clause: dict[str, Any]
    if (match or "any").strip().lower() == "all":
        clause = {"must": [{"multi_match": {"query": t, "fields": fields}} for t in ts]}
    else:
        clause = {"should": [{"multi_match": {"query": t, "fields": fields}} for t in ts],
                  "minimum_should_match": 1}
    filt: list[dict] = [{"term": {"__state": "ACTIVE"}}]
    if type_names:
        filt.append({"terms": {"__typeName.keyword": list(type_names)}})
    clause["filter"] = filt
    dsl = {"from": 0, "size": limit, "query": {"bool": clause}}
    return [_shape(e) for e in indexsearch(dsl).get("entities", []) or []]


def get_asset(qualified_name: str) -> dict:
    dsl = {"from": 0, "size": 1, "query": {"bool": {"filter": [{"term": {"qualifiedName": qualified_name}},
                                                              {"term": {"__state": "ACTIVE"}}]}}}
    ents = indexsearch(dsl, ATTRS + ["columns", "inputs", "outputs"]).get("entities") or []
    if not ents:
        raise NotFound(f"asset {qualified_name!r} not found", status=404, system="atlan")
    return _shape(ents[0])


def lineage(qualified_name: str, direction: str = "both", depth: int = 3) -> dict:
    a = get_asset(qualified_name)
    d = {"both": "BOTH", "upstream": "INPUT", "downstream": "OUTPUT"}.get(direction.lower(), "BOTH")
    res = _req("GET", f"/api/meta/lineage/{a['guid']}", params={"depth": depth, "direction": d}) or {}
    gmap = res.get("guidEntityMap", {}) or {}
    qn = {g: (e.get("attributes", {}) or {}).get("qualifiedName") or e.get("qualifiedName") for g, e in gmap.items()}
    # Atlan relations go asset → Process → asset; collapse processes into asset→asset edges
    is_proc = {g for g, e in gmap.items() if e.get("typeName", "").endswith("Process")}
    rels = res.get("relations", []) or []
    out_of = {}
    for r in rels:
        out_of.setdefault(r["fromEntityId"], []).append(r["toEntityId"])
    edges = []
    for r in rels:
        f, t = r["fromEntityId"], r["toEntityId"]
        if f in is_proc:
            continue
        targets = out_of.get(t, []) if t in is_proc else [t]
        for t2 in targets:
            e = {"from": qn.get(f), "to": qn.get(t2)}
            if e not in edges:
                edges.append(e)
    base_qn = a["qualifiedName"]
    up, down = _walk(edges, base_qn, "to", "from"), _walk(edges, base_qn, "from", "to")
    nodes = [{"qualifiedName": q, "typeName": gmap[g].get("typeName"), "name": (gmap[g].get("attributes") or {}).get("name"),
              "direction": "base" if q == base_qn else ("upstream" if q in up else "downstream")}
             for g, q in qn.items() if g not in is_proc]
    return {**res, "baseQualifiedName": base_qn, "upstream": [q for q in up], "downstream": [q for q in down],
            "nodes": nodes, "edges": edges}


def _walk(edges: list[dict], start: str, key: str, nxt: str) -> list[str]:
    seen, order, frontier = {start}, [], [start]
    while frontier:
        f = frontier.pop(0)
        for e in edges:
            if e[key] == f and e[nxt] not in seen:
                seen.add(e[nxt])
                order.append(e[nxt])
                frontier.append(e[nxt])
    return order


def _relation_qn(node: dict) -> str:
    conn = env("ATLAN_CONNECTION_QN")
    if not conn:
        raise ValueError("set ATLAN_CONNECTION_QN (e.g. default/bigquery/1712345678) to map dbt relations to Atlan")
    name = node.get("alias") or node.get("identifier") or node.get("name")
    return f"{conn}/{node.get('database')}/{node.get('schema')}/{name}"


def upsert_processes(processes: list[dict]) -> dict:
    """processes: [{qualifiedName, name, inputs:[qn], outputs:[qn], typeName?}] → Atlan Process entities."""
    def ref(q: str) -> dict:
        return {"typeName": "View" if "/view/" in q.lower() else "Table", "uniqueAttributes": {"qualifiedName": q}}
    ents = [{"typeName": p.get("typeName", "Process"),
             "attributes": {"qualifiedName": p["qualifiedName"], "name": p["name"],
                            "connectionQualifiedName": "/".join(p["qualifiedName"].split("/")[:3]),
                            "inputs": [ref(q) for q in p["inputs"]], "outputs": [ref(q) for q in p["outputs"]]}}
            for p in processes]
    out: dict[str, Any] = {"entities": 0, "mutated": {}}
    for i in range(0, len(ents), 20):
        res = _req("POST", "/api/meta/entity/bulk", json={"entities": ents[i:i + 20]}) or {}
        for k, v in (res.get("mutatedEntities") or {}).items():
            out["mutated"].setdefault(k, 0)
            out["mutated"][k] += len(v)
        out["entities"] += len(ents[i:i + 20])
    return out


def push_dbt_manifest(manifest: dict, run_results: dict | None = None) -> dict:
    nodes = {**manifest.get("nodes", {}), **manifest.get("sources", {})}
    conn = env("ATLAN_CONNECTION_QN")
    procs = []
    for uid, n in manifest.get("nodes", {}).items():
        if n.get("resource_type") != "model":
            continue
        ins = [_relation_qn(nodes[d]) for d in (n.get("depends_on") or {}).get("nodes", []) if d in nodes]
        if ins:
            procs.append({"qualifiedName": f"{conn}/dbt/{re.sub(r'[^A-Za-z0-9_.]', '_', uid)}",
                          "name": f"dbt: {n.get('name')}", "inputs": ins, "outputs": [_relation_qn(n)]})
    res = upsert_processes(procs) if procs else {"entities": 0, "mutated": {}}
    results = (run_results or {}).get("results", [])
    return {"status": "accepted", "mode": "live", "models": sum(1 for n in manifest.get("nodes", {}).values()
                                                                 if n.get("resource_type") == "model"),
            "sources": len(manifest.get("sources", {})), "lineage_edges": sum(len(p["inputs"]) for p in procs),
            "processes": res, "run_results": len(results),
            "failed": sum(1 for r in results if r.get("status") in ("error", "fail"))}
