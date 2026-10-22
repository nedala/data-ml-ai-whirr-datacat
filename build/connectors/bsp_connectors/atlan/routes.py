"""Atlan mock under /connectors/atlan/: Atlan-shaped metadata endpoints (so
ATLAN_BASE_URL=http://connectors:8092/connectors/atlan exercises the live
client) plus simple REST endpoints for the canvas and curl."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query

from .._common import NotFound
from . import mock

router = APIRouter(tags=["atlan (mock)"])


def _walk_dsl(node: Any, out: dict) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("multi_match", "match", "query_string", "simple_query_string") and isinstance(v, dict):
                q = v.get("query")
                if q is None and len(v) == 1:  # {"match": {"name": "x"}}
                    inner = next(iter(v.values()))
                    q = inner.get("query") if isinstance(inner, dict) else inner
                if isinstance(q, str):
                    out["q"].append(q)
            elif k == "terms" and isinstance(v, dict):
                for f, vals in v.items():
                    if f.startswith("__typeName"):
                        out["types"].extend(vals)
            elif k == "term" and isinstance(v, dict):
                for f, val in v.items():
                    val = val.get("value") if isinstance(val, dict) else val
                    if f.startswith("__typeName"):
                        out["types"].append(val)
                    elif f in ("qualifiedName", "qualifiedName.keyword", "__guid"):
                        out["qn"] = val
            else:
                _walk_dsl(v, out)
    elif isinstance(node, list):
        for x in node:
            _walk_dsl(x, out)


@router.post("/api/meta/search/indexsearch", summary="Atlan index search (Elasticsearch DSL subset)")
def indexsearch(body: dict = Body(...)) -> dict:
    dsl = body.get("dsl", {}) or {}
    parsed: dict[str, Any] = {"q": [], "types": [], "qn": None}
    _walk_dsl(dsl.get("query", {}), parsed)
    if parsed["qn"]:
        try:
            ents = [mock.get_asset(parsed["qn"])]
        except NotFound:
            ents = []
        return {"approximateCount": len(ents), "entities": ents}
    ents, total = mock.search_page(" ".join(parsed["q"]), parsed["types"] or None,
                                   int(dsl.get("size", 25)), int(dsl.get("from", 0)))
    return {"approximateCount": total, "entities": ents}


@router.get("/api/meta/lineage/{guid}", summary="Atlan classic lineage (asset → Process → asset relations)")
def lineage_guid(guid: str, depth: int = 3, direction: str = "BOTH") -> dict:
    d = {"INPUT": "upstream", "OUTPUT": "downstream"}.get(direction.upper(), "both")
    try:
        res = mock.lineage(guid, direction=d, depth=depth)
    except NotFound as e:
        raise HTTPException(404, detail={"errorCode": "ATLAS-404-00-005", "errorMessage": str(e)})
    cat = mock.catalog()
    gmap = dict(res["guidEntityMap"])
    rels = []
    for e in res["edges"]:
        f, t = cat["assets"].get(e["from"]), cat["assets"].get(e["to"])
        if not f or not t:
            continue
        proc = next((p for p in cat["processes"].values()
                     if p["attributes"]["inputs"] == [e["from"]] and p["attributes"]["outputs"] == [e["to"]]), None)
        if proc:
            gmap[proc["guid"]] = proc
            rels += [{"fromEntityId": f["guid"], "toEntityId": proc["guid"]},
                     {"fromEntityId": proc["guid"], "toEntityId": t["guid"]}]
        else:
            rels.append({"fromEntityId": f["guid"], "toEntityId": t["guid"]})
    return {"baseEntityGuid": res["baseEntityGuid"], "lineageDirection": direction.upper(), "lineageDepth": depth,
            "guidEntityMap": gmap, "relations": rels}


@router.post("/api/meta/entity/bulk", summary="Atlan bulk entity upsert (Process lineage entities)")
def entity_bulk(body: dict = Body(...)) -> dict:
    created = mock.upsert_processes(body.get("entities", []))
    return {"mutatedEntities": {"CREATE": created}, "guidAssignments": {}}


# ── simple REST ──────────────────────────────────────────────────────────
@router.get("/assets", summary="Search assets (offset-paged; good for the canvas REST source)")
def assets(q: str = "", type: list[str] | None = Query(None), limit: int = 25, offset: int = 0) -> dict:
    ents, total = mock.search_page(q, type, limit, offset)
    return {"count": total, "offset": offset, "limit": limit, "entities": ents,
            "next_offset": offset + limit if offset + limit < total else None}


@router.get("/asset", summary="Get one asset by qualifiedName")
def asset(qn: str) -> dict:
    try:
        return mock.get_asset(qn)
    except NotFound as e:
        raise HTTPException(404, str(e))


@router.get("/lineage", summary="Lineage by qualifiedName (SDK shape)")
def lineage(qn: str, direction: str = "both", depth: int = 3) -> dict:
    try:
        return mock.lineage(qn, direction=direction, depth=depth)
    except NotFound as e:
        raise HTTPException(404, str(e))


@router.get("/glossary", summary="Glossary terms")
def glossary() -> dict:
    return {"terms": mock.search_assets("", ["AtlasGlossaryTerm"], limit=500)}


@router.post("/dbt/manifest", summary="Push a dbt manifest (and optional run_results)")
def dbt_manifest(body: dict = Body(...)) -> dict:
    manifest = body.get("manifest", body)
    try:
        return mock.push_dbt_manifest(manifest, body.get("run_results"))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/info", summary="Where the mock catalog came from")
def info() -> dict:
    cat = mock.catalog()
    by_type: dict[str, int] = {}
    for a in cat["assets"].values():
        by_type[a["typeName"]] = by_type.get(a["typeName"], 0) + 1
    return {"source": cat["source"], "assets": by_type, "lineage_edges": len(cat["edges"])}
