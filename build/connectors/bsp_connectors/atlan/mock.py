"""Atlan mock catalog, built from the warehouse itself.

Sources, best first: a dbt manifest pushed via push_dbt_manifest (state) or
found on disk (DBT_MANIFEST_PATH, /opt/dbt_project/target/manifest.json,
<repo>/dbt_project/target/manifest.json) → the live warehouse's
information_schema → fixtures/atlan/warehouse.yaml. Glossary terms, owners,
certificates and PII classifications come from fixtures/atlan/glossary.yaml;
Power BI datasets from the Power BI mock. Asset dicts follow Atlan's search
response shape (typeName, guid, attributes{…}, classificationNames, meanings)
with name / qualifiedName also at the top level for convenience.
"""

from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .._common import NotFound, env, fixtures_dir, read_json, state_dir, write_json

CONN = "default/postgres/bsp_edw"
PBI_CONN = "default/powerbi/dataplatform"
GLOSSARY_QN = "bsp-business-glossary"
SCHEMAS = ("raw", "landing", "staging", "intermediate", "marts", "features")
TTL = 60.0

_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "cat": None}


def _guid(qn: str) -> str:
    return str(uuid.UUID(hashlib.md5(qn.encode()).hexdigest()))


def table_qn(schema: str, table: str) -> str:
    return f"{CONN}/{schema}/{table}"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def pbi_qn(workspace: str, dataset: str) -> str:
    return f"{PBI_CONN}/{_slug(workspace)}/{_slug(dataset)}"


def _asset(type_name: str, qn: str, name: str, **attrs: Any) -> dict:
    cls = attrs.pop("classificationNames", [])
    meanings = attrs.pop("meanings", [])
    a = {"name": name, "qualifiedName": qn, "connectorName": qn.split("/")[1] if qn.count("/") >= 2 else "atlan",
         "connectionQualifiedName": "/".join(qn.split("/")[:3]) if qn.count("/") >= 2 else "",
         "certificateStatus": None, "ownerUsers": [], "ownerGroups": [], "description": "", "userDescription": "", **attrs}
    return {"typeName": type_name, "guid": _guid(qn), "status": "ACTIVE", "qualifiedName": qn, "name": name,
            "displayText": name, "attributes": a, "classificationNames": cls, "meanings": meanings}


# ── sources ──────────────────────────────────────────────────────────────
def _manifest() -> dict | None:
    pushed = read_json(state_dir("atlan") / "dbt_manifest.json", None)
    if pushed:
        return pushed
    cands = [env("DBT_MANIFEST_PATH"), "/opt/dbt_project/target/manifest.json",
             str(Path(__file__).resolve().parents[4] / "dbt_project" / "target" / "manifest.json")]
    for c in cands:
        if c and Path(c).is_file():
            d = read_json(Path(c), None)
            if d:
                return d
    return None


def _information_schema() -> dict[str, dict[str, dict]] | None:
    if env("ATLAN_MOCK_SOURCE") == "static":
        return None
    try:
        import psycopg2

        from bsp_common import settings
        conn = psycopg2.connect(settings.get().pg_dsn, connect_timeout=2)
    except Exception:
        return None
    out: dict[str, dict[str, dict]] = {}
    try:
        with conn.cursor() as cur:
            cur.execute("""select c.table_schema, c.table_name, c.column_name, c.data_type
                           from information_schema.columns c
                           where c.table_schema = any(%s) order by 1, 2, c.ordinal_position""", (list(SCHEMAS),))
            for s, t, c, dt in cur.fetchall():
                out.setdefault(s, {}).setdefault(t, {"description": "", "columns": {}})["columns"][c] = dt
            cur.execute("""select n.nspname, c.relname, c.reltuples::bigint, obj_description(c.oid)
                           from pg_class c join pg_namespace n on n.oid = c.relnamespace
                           where n.nspname = any(%s) and c.relkind in ('r','v','m','p')""", (list(SCHEMAS),))
            for s, t, rows, desc in cur.fetchall():
                if s in out and t in out[s]:
                    out[s][t]["rowCount"] = max(int(rows or 0), 0)
                    if desc:
                        out[s][t]["description"] = desc
    except Exception:
        return None
    finally:
        conn.close()
    return out or None


def _static() -> dict[str, dict[str, dict]]:
    return yaml.safe_load((fixtures_dir("atlan") / "warehouse.yaml").read_text()) or {}


def _glossary() -> dict:
    p = fixtures_dir("atlan") / "glossary.yaml"
    return yaml.safe_load(p.read_text()) if p.is_file() else {"terms": []}


def _powerbi_datasets() -> list[tuple[str, str, dict]]:
    p = fixtures_dir("powerbi") / "workspaces.yaml"
    if not p.is_file():
        return []
    ws = yaml.safe_load(p.read_text()) or {}
    return [(w["name"], d["name"], d) for w in ws.get("workspaces", []) for d in w.get("datasets", [])]


# ── build ────────────────────────────────────────────────────────────────
def build_catalog() -> dict[str, Any]:
    wh = _information_schema()
    source = "warehouse"
    if not wh:
        wh, source = _static(), "static"
    gl = _glossary()
    owners = gl.get("owners", {})
    pii = set(gl.get("pii_columns", []))
    edges: list[tuple[str, str, str]] = []  # (from_qn, to_qn, process name)

    descs: dict[str, str] = {}
    man = _manifest()
    if man:
        source += "+dbt"
        nodes = {**man.get("nodes", {}), **man.get("sources", {})}
        rel: dict[str, str] = {}
        for uid, n in nodes.items():
            if n.get("resource_type") not in ("model", "source", "seed", "snapshot"):
                continue
            schema, name = n.get("schema", ""), n.get("alias") or n.get("identifier") or n.get("name", "")
            rel[uid] = table_qn(schema, name)
            t = wh.setdefault(schema, {}).setdefault(name, {"description": "", "columns": {}})
            if n.get("description"):
                t["description"] = n["description"]
            for cname, c in (n.get("columns") or {}).items():
                t["columns"].setdefault(cname, c.get("data_type") or "")
                if c.get("description"):
                    descs[f"{schema}.{name}.{cname}"] = c["description"]
            t["dbt"] = {"unique_id": uid, "materialized": (n.get("config") or {}).get("materialized"),
                        "tags": n.get("tags", []), "path": n.get("original_file_path")}
        for uid, n in nodes.items():
            for dep in (n.get("depends_on") or {}).get("nodes", []):
                if uid in rel and dep in rel:
                    edges.append((rel[dep], rel[uid], f"dbt: {n.get('name', uid)}"))
    else:
        lin = yaml.safe_load((fixtures_dir("atlan") / "lineage.yaml").read_text()) or {}
        for target, srcs in lin.items():
            if target.startswith("powerbi:"):
                continue
            s, t = target.split(".", 1)
            if s in wh and t in wh[s]:
                for src in srcs:
                    ss, st = src.split(".", 1)
                    if ss in wh and st in wh[ss]:
                        edges.append((table_qn(ss, st), table_qn(s, t), f"dbt: {t}"))

    term_links: dict[str, list[dict]] = {}
    terms = []
    for t in gl.get("terms", []):
        qn = f"{_slug(t['name'])}@{GLOSSARY_QN}"
        term = _asset("AtlasGlossaryTerm", qn, t["name"], description=t.get("description", ""),
                      userDescription=t.get("description", ""), certificateStatus=t.get("certificate"),
                      ownerGroups=[t["owner"]] if t.get("owner") else [], anchor={"name": gl.get("glossary", "")},
                      assignedEntities=[])
        terms.append((term, t.get("assets", [])))
        for path in t.get("assets", []):
            term_links.setdefault(path, []).append({"displayText": t["name"], "qualifiedName": qn, "guid": term["guid"]})

    assets: dict[str, dict] = {}
    db_qn = CONN
    assets[db_qn] = _asset("Database", db_qn, "bsp_edw", description="Local Postgres warehouse (bsp_edw)",
                           schemaCount=len(wh))
    for schema, tables in sorted(wh.items()):
        sqn = f"{CONN}/{schema}"
        assets[sqn] = _asset("Schema", sqn, schema, databaseName="bsp_edw", databaseQualifiedName=db_qn,
                             tableCount=len(tables), ownerGroups=owners.get(schema, []))
        for tname, t in sorted(tables.items()):
            qn = table_qn(schema, tname)
            cols, tcls = [], set()
            for i, (cname, dtype) in enumerate(t.get("columns", {}).items()):
                cqn = f"{qn}/{cname}"
                ccls = ["PII"] if cname in pii else []
                tcls.update(ccls)
                assets[cqn] = _asset("Column", cqn, cname, dataType=str(dtype or ""), order=i + 1, tableName=tname,
                                     tableQualifiedName=qn, schemaName=schema, databaseName="bsp_edw",
                                     description=descs.get(f"{schema}.{tname}.{cname}", ""),
                                     classificationNames=ccls, meanings=term_links.get(f"{schema}.{tname}.{cname}", []),
                                     ownerGroups=owners.get(schema, []))
                cols.append({"guid": assets[cqn]["guid"], "qualifiedName": cqn, "name": cname})
            cert = {"marts": "VERIFIED", "raw": "VERIFIED", "staging": "DRAFT", "intermediate": "DRAFT"}.get(schema)
            assets[qn] = _asset("View" if schema in ("staging", "intermediate") else "Table", qn, tname,
                                schemaName=schema, schemaQualifiedName=sqn, databaseName="bsp_edw",
                                description=t.get("description", ""), userDescription=t.get("description", ""),
                                columnCount=len(cols), rowCount=t.get("rowCount"), columns=cols,
                                certificateStatus=cert, ownerGroups=owners.get(schema, []),
                                classificationNames=sorted(tcls), meanings=term_links.get(f"{schema}.{tname}", []),
                                dbt=t.get("dbt"))

    for ws, ds, d in _powerbi_datasets():
        qn = pbi_qn(ws, ds)
        assets[qn] = _asset("PowerBIDataset", qn, ds, workspaceName=ws, description=d.get("description", ""),
                            certificateStatus="VERIFIED", ownerGroups=[d.get("owner", "bi-team")])
        for src in d.get("sources", []):
            s, t = src.split(".", 1)
            if table_qn(s, t) in assets:
                edges.append((table_qn(s, t), qn, f"Power BI refresh: {ds}"))

    for term, links in terms:
        term["attributes"]["assignedEntities"] = [
            {"qualifiedName": table_qn(*p.split(".")[:2]) + ("/" + p.split(".")[2] if p.count(".") == 2 else ""),
             "typeName": "Column" if p.count(".") == 2 else "Table"} for p in links]
        assets[term["qualifiedName"]] = term

    for p in read_json(state_dir("atlan") / "processes.json", []):  # entity/bulk pushes
        for i in p.get("inputs", []):
            for o in p.get("outputs", []):
                edges.append((i, o, p.get("name", "process")))

    processes = {}
    for src, dst, name in edges:
        pqn = f"{CONN}/process/{_slug(name)}/{_guid(src + '>' + dst)[:8]}"
        processes[pqn] = _asset("Process", pqn, name, inputs=[src], outputs=[dst])
    seen = set()
    uniq = [e for e in edges if not ((e[0], e[1]) in seen or seen.add((e[0], e[1])))]
    return {"assets": assets, "edges": uniq, "processes": processes, "source": source, "built_at": time.time()}


def catalog() -> dict[str, Any]:
    with _lock:
        if _cache["cat"] is None or time.time() - _cache["at"] > TTL:
            _cache["cat"], _cache["at"] = build_catalog(), time.time()
        return _cache["cat"]


def invalidate() -> None:
    with _lock:
        _cache["cat"] = None


# ── SDK ──────────────────────────────────────────────────────────────────
def _score(a: dict, words: list[str]) -> float:
    if not words:
        return 1.0
    at = a["attributes"]
    name = a["name"].lower()
    hay = " ".join([name, a["qualifiedName"].lower(), (at.get("description") or "").lower(),
                    " ".join(m["displayText"].lower() for m in a.get("meanings", [])),
                    " ".join(c["name"].lower() for c in at.get("columns", []) or [])])
    s = 0.0
    for w in words:
        if w == name:
            s += 10
        elif w in name:
            s += 5
        elif w in hay:
            s += 1
        else:
            return 0.0
    return s + {"Table": 0.5, "AtlasGlossaryTerm": 0.4, "PowerBIDataset": 0.3, "View": 0.2}.get(a["typeName"], 0)


def search_assets(query: str, type_names: list[str] | None = None, limit: int = 25, offset: int = 0) -> list[dict]:
    return search_page(query, type_names, limit, offset)[0]


def search_page(query: str, type_names: list[str] | None = None, limit: int = 25,
                offset: int = 0) -> tuple[list[dict], int]:
    words = [w for w in re.split(r"[\s,]+", (query or "").lower().replace("*", "")) if w]
    types = {t.lower() for t in type_names or []}
    hits = []
    for a in catalog()["assets"].values():
        if types and a["typeName"].lower() not in types:
            continue
        sc = _score(a, words)
        if sc > 0:
            hits.append((sc, a["qualifiedName"], a))
    hits.sort(key=lambda h: (-h[0], h[1]))
    return [h[2] for h in hits[offset: offset + limit]], len(hits)


def get_asset(qualified_name: str) -> dict:
    cat = catalog()
    a = cat["assets"].get(qualified_name) or cat["processes"].get(qualified_name)
    if a is None:
        for x in cat["assets"].values():  # also accept a guid
            if x["guid"] == qualified_name:
                return x
        raise NotFound(f"asset {qualified_name!r} not found", status=404, system="atlan")
    return a


def _by_guid(guid: str) -> dict:
    return get_asset(guid)


def lineage(qualified_name: str, direction: str = "both", depth: int = 3) -> dict:
    cat = catalog()
    base = get_asset(qualified_name)
    qn = base["qualifiedName"]
    if base["typeName"] == "Column":  # column lineage → its table's lineage
        qn = base["attributes"]["tableQualifiedName"]
    d = direction.lower()
    up: dict[str, list[str]] = {}
    down: dict[str, list[str]] = {}
    for s, t, _ in cat["edges"]:
        down.setdefault(s, []).append(t)
        up.setdefault(t, []).append(s)
    nodes: dict[str, dict] = {qn: {"depth": 0, "direction": "base"}}
    edges: list[dict] = []

    def walk(adj: dict, label: str, forward: bool) -> list[str]:
        order, frontier = [], [qn]
        for lvl in range(1, depth + 1):
            nxt = []
            for f in frontier:
                for n in adj.get(f, []):
                    e = {"from": f, "to": n} if forward else {"from": n, "to": f}
                    if e not in edges:
                        edges.append(e)
                    if n not in nodes:
                        nodes[n] = {"depth": lvl, "direction": label}
                        order.append(n)
                        nxt.append(n)
            frontier = nxt
        return order

    upstream = walk(up, "upstream", False) if d in ("both", "upstream") else []
    downstream = walk(down, "downstream", True) if d in ("both", "downstream") else []
    assets = cat["assets"]
    guid_map = {assets[n]["guid"]: assets[n] for n in nodes if n in assets}
    g = {n: assets[n]["guid"] for n in nodes if n in assets}
    return {
        "baseEntityGuid": assets[qn]["guid"], "baseQualifiedName": qn, "lineageDirection": d.upper(),
        "lineageDepth": depth, "guidEntityMap": guid_map,
        "relations": [{"fromEntityId": g.get(e["from"]), "toEntityId": g.get(e["to"])} for e in edges],
        "upstream": upstream, "downstream": downstream,
        "nodes": [{"qualifiedName": n, "typeName": assets[n]["typeName"] if n in assets else "Table",
                   "name": assets[n]["name"] if n in assets else n.rsplit("/", 1)[-1], **meta}
                  for n, meta in nodes.items()],
        "edges": edges,
    }


def push_dbt_manifest(manifest: dict, run_results: dict | None = None) -> dict:
    if not isinstance(manifest, dict) or "nodes" not in manifest:
        raise ValueError("manifest must be a dbt manifest.json dict (with 'nodes')")
    sd = state_dir("atlan")
    write_json(sd / "dbt_manifest.json", manifest)
    if run_results is not None:
        write_json(sd / "dbt_run_results.json", run_results)
    invalidate()
    nodes = manifest.get("nodes", {})
    models = [n for n in nodes.values() if n.get("resource_type") == "model"]
    deps = sum(len((n.get("depends_on") or {}).get("nodes", [])) for n in models)
    results = (run_results or {}).get("results", [])
    return {"status": "accepted", "mode": "mock", "models": len(models), "sources": len(manifest.get("sources", {})),
            "lineage_edges": deps, "run_results": len(results),
            "failed": sum(1 for r in results if r.get("status") in ("error", "fail")),
            "catalog_source": catalog()["source"]}


def upsert_processes(entities: list[dict]) -> list[dict]:
    """Mock of POST /api/meta/entity/bulk for Process entities (lineage pushes)."""
    stored = read_json(state_dir("atlan") / "processes.json", [])
    created = []
    for e in entities:
        at = e.get("attributes", {})

        def qns(lst: list) -> list[str]:
            return [x.get("uniqueAttributes", {}).get("qualifiedName") or x.get("qualifiedName", "") for x in lst]
        rec = {"qualifiedName": at.get("qualifiedName"), "name": at.get("name", "process"),
               "inputs": qns(at.get("inputs", [])), "outputs": qns(at.get("outputs", [])),
               "typeName": e.get("typeName", "Process")}
        stored = [s for s in stored if s.get("qualifiedName") != rec["qualifiedName"]] + [rec]
        created.append({"typeName": rec["typeName"], "guid": _guid(rec["qualifiedName"] or ""),
                        "attributes": {"qualifiedName": rec["qualifiedName"], "name": rec["name"]}})
    write_json(state_dir("atlan") / "processes.json", stored)
    invalidate()
    return created
