"""OpenLineage-shaped run events for every canvas run.

Events are always stored in ${CANVAS_DIR}/lineage/<run_id>.json. They are
also pushed, best effort and never failing the run, to:

* Atlan — via bsp_connectors.atlan.push_lineage (Process entities). In live
  mode only outputs Atlan knows (BigQuery tables under ATLAN_CONNECTION_QN)
  are linked; in mock mode the mock catalog records the process so its
  lineage view shows canvas loads into the warehouse.
* DataHub — optional, off by default: when DATAHUB_GMS_URL is set, each event
  is POSTed to <GMS>/openapi/openlineage/api/v1/lineage (DataHub's OpenLineage
  endpoint), with `Authorization: Bearer $DATAHUB_TOKEN` when a token is set.
* Any OpenLineage HTTP backend (Marquez etc.) — optional OPENLINEAGE_URL
  (POST <url>/api/v1/lineage).
"""

from __future__ import annotations

import logging
import os
import uuid
from typing import Any

from . import store

log = logging.getLogger("bsp_canvas.lineage")
PRODUCER = "https://dataplatform.internal/bsp-appliance/canvas"
SCHEMA_URL = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/definitions/RunEvent"
NAMESPACE = os.environ.get("OPENLINEAGE_NAMESPACE", "bsp-canvas")


def event(event_type: str, run_id: str, pipeline: dict, inputs: list[dict], outputs: list[dict],
          error: str | None = None, stats: dict | None = None) -> dict:
    run_facets: dict[str, Any] = {}
    if error:
        run_facets["errorMessage"] = {"_producer": PRODUCER, "_schemaURL":
                                      "https://openlineage.io/spec/facets/1-0-1/ErrorMessageRunFacet.json",
                                      "message": error[:2000], "programmingLanguage": "python"}
    if stats:
        run_facets["bsp_canvas_stats"] = {"_producer": PRODUCER, "_schemaURL": PRODUCER + "/facets/stats.json", **stats}
    return {
        "eventType": event_type, "eventTime": store.now(), "producer": PRODUCER, "schemaURL": SCHEMA_URL,
        "run": {"runId": str(uuid.uuid5(uuid.NAMESPACE_URL, run_id)), "facets": run_facets},
        "job": {"namespace": NAMESPACE, "name": pipeline["id"], "facets": {
            "documentation": {"_producer": PRODUCER,
                              "_schemaURL": "https://openlineage.io/spec/facets/1-0-1/DocumentationJobFacet.json",
                              "description": pipeline.get("description") or pipeline.get("name", "")},
            "jobType": {"_producer": PRODUCER,
                        "_schemaURL": "https://openlineage.io/spec/facets/2-0-3/JobTypeJobFacet.json",
                        "processingType": "BATCH", "integration": "BSP_CANVAS", "jobType": "PIPELINE"}}},
        "inputs": [_ds(d) for d in inputs], "outputs": [_ds(d) for d in outputs],
    }


def _ds(d: dict) -> dict:
    out = {"namespace": d["namespace"], "name": d["name"], "facets": {}}
    if d.get("fields"):
        out["facets"]["schema"] = {"_producer": PRODUCER,
                                   "_schemaURL": "https://openlineage.io/spec/facets/1-1-1/SchemaDatasetFacet.json",
                                   "fields": [{"name": n, "type": t} for n, t in d["fields"]]}
    if d.get("rows") is not None:
        out["outputFacets"] = {"outputStatistics": {
            "_producer": PRODUCER, "_schemaURL": "https://openlineage.io/spec/facets/1-0-2/OutputStatisticsOutputDatasetFacet.json",
            "rowCount": int(d["rows"])}}
    return out


def emit(run_id: str, events: list[dict], logf=None) -> dict:
    """Store, then push (best effort). Returns {target: status}."""
    store.put_lineage(run_id, events)
    status: dict[str, str] = {"file": "stored"}
    say = logf or (lambda m: None)
    gms = os.environ.get("DATAHUB_GMS_URL", "").strip().rstrip("/")
    if gms:
        status["datahub"] = _post(gms + "/openapi/openlineage/api/v1/lineage", events,
                                  os.environ.get("DATAHUB_TOKEN", "").strip())
        say(f"lineage → DataHub: {status['datahub']}")
    ol = os.environ.get("OPENLINEAGE_URL", "").strip().rstrip("/")
    if ol:
        status["openlineage"] = _post(ol + "/api/v1/lineage", events, os.environ.get("OPENLINEAGE_API_KEY", "").strip())
        say(f"lineage → OpenLineage backend: {status['openlineage']}")
    final = events[-1] if events else None
    if final and final["eventType"] == "COMPLETE" and final["outputs"]:
        status["atlan"] = _push_atlan(final)
        say(f"lineage → Atlan: {status['atlan']}")
    return status


def _post(url: str, events: list[dict], token: str) -> str:
    import httpx
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with httpx.Client(timeout=15) as c:
            for e in events:
                r = c.post(url, json=e, headers=headers)
                if r.status_code >= 300:
                    return f"HTTP {r.status_code}: {r.text[:200]}"
        return f"sent {len(events)} event(s)"
    except Exception as e:
        return f"failed: {type(e).__name__}: {e}"


def _atlan_qn(ds: dict, live: bool) -> str | None:
    from bsp_connectors import atlan
    ns, name = ds["namespace"], ds["name"]
    if ns.startswith("postgres://") and not live:
        parts = name.split(".")
        if len(parts) == 3:
            return atlan.table_qualified_name(parts[1], parts[2])
    if ns.startswith("bigquery") and live:
        conn = os.environ.get("ATLAN_CONNECTION_QN", "").strip()
        if conn:
            return f"{conn}/{name.replace('.', '/')}"
    return None


def _push_atlan(ev: dict) -> str:
    try:
        from bsp_connectors import atlan, mode_of
        live = mode_of("atlan") == "live"
        outs = [q for q in (_atlan_qn(d, live) for d in ev["outputs"]) if q]
        if not outs:
            return "skipped (no outputs known to Atlan)"
        ins = [q for q in (_atlan_qn(d, live) for d in ev["inputs"]) if q] or \
              [f"{d['namespace'].rstrip('/')}/{d['name']}" for d in ev["inputs"]]
        res = atlan.push_lineage([{"qualifiedName": f"default/canvas/{ev['job']['name']}", "name": f"canvas: {ev['job']['name']}",
                                   "inputs": ins, "outputs": outs}])
        return f"{res.get('mode', 'mock')}: {res.get('entities', 0)} process(es)"
    except Exception as e:
        return f"failed: {type(e).__name__}: {str(e)[:200]}"
