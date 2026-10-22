"""Atlan data catalog — search, assets, lineage, dbt manifest push.

Live when ATLAN_BASE_URL + ATLAN_API_TOKEN are set (ATLAN_MODE=mock|live
overrides); otherwise a mock catalog built from the warehouse + dbt manifest.
Assets are Atlan-shaped dicts: typeName, guid, qualifiedName, name,
attributes{description, certificateStatus, ownerGroups, …},
classificationNames (e.g. ["PII"]), meanings (glossary terms).

Additive extra: ``push_lineage(processes)`` — upsert Process entities
[{qualifiedName, name, inputs:[qn], outputs:[qn]}] (used by the canvas).
"""

from __future__ import annotations

from .._common import mode_of
from . import mock as _mock


def _impl():
    if mode_of("atlan") == "live":
        from . import live
        return live
    return _mock


def mode() -> str:
    return mode_of("atlan")


def search_assets(query: str, type_names: list[str] | None = None, limit: int = 25) -> list[dict]:
    """type_names e.g. ["Table", "View", "Column", "AtlasGlossaryTerm", "PowerBIDataset"]."""
    return _impl().search_assets(query, type_names=type_names, limit=limit)


def multi_search(terms: list[str], type_names: list[str] | None = None, limit: int = 25,
                 match: str = "any") -> list[dict]:
    """One Elasticsearch-DSL query across several terms (API mode).

    match='any' = bool.should (OR); match='all' = bool.must (AND). The UI search
    palette does not reliably support boolean OR, so UI fallbacks search term by
    term; this is the efficient multi-term path once an API token is provisioned.
    """
    impl = _impl()
    if hasattr(impl, "multi_search"):
        return impl.multi_search(terms, type_names=type_names, limit=limit, match=match)
    out: list[dict] = []
    seen: set[str] = set()
    for term in terms:
        for asset in impl.search_assets(term, type_names=type_names, limit=limit):
            key = asset.get("guid") or asset.get("qualifiedName") or asset.get("name")
            if key and key not in seen:
                seen.add(key)
                out.append(asset)
    return out[:limit]


def get_asset(qualified_name: str) -> dict:
    return _impl().get_asset(qualified_name)


def lineage(qualified_name: str, direction: str = "both", depth: int = 3) -> dict:
    """{baseQualifiedName, upstream:[qn], downstream:[qn], nodes:[…], edges:[{from,to}],
    guidEntityMap, relations} — direction: both | upstream | downstream."""
    return _impl().lineage(qualified_name, direction=direction, depth=depth)


def push_dbt_manifest(manifest: dict, run_results: dict | None = None) -> dict:
    return _impl().push_dbt_manifest(manifest, run_results)


def push_lineage(processes: list[dict]) -> dict:
    impl = _impl()
    if impl is _mock:
        ents = [{"typeName": "Process", "attributes": {
            "qualifiedName": p["qualifiedName"], "name": p["name"],
            "inputs": [{"uniqueAttributes": {"qualifiedName": q}} for q in p["inputs"]],
            "outputs": [{"uniqueAttributes": {"qualifiedName": q}} for q in p["outputs"]]}} for p in processes]
        return {"entities": len(_mock.upsert_processes(ents)), "mode": "mock"}
    return {**impl.upsert_processes(processes), "mode": "live"}


def table_qualified_name(schema: str, table: str) -> str:
    """qualifiedName of a local-warehouse table in the mock catalog."""
    return _mock.table_qn(schema, table)
