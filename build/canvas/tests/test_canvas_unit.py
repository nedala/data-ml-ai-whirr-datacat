"""Canvas unit tests (no warehouse needed): expressions, validation, preview,
lake → lake run with incremental state, scheduler, API guards.

    PYTHONPATH=build/common:build/connectors:build/canvas pytest -q build/canvas/tests/test_canvas_unit.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd
import pytest


@pytest.fixture(autouse=True, scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("canvas")
    lake = root / "lake"
    (lake / "in").mkdir(parents=True)
    pd.DataFrame({"id": [1, 2, 3], "email": ["a@x.net", "b@x.net", None], "amount": ["10.5", "x", "7"],
                  "ts": ["2026-10-01", "2026-10-02", "2026-10-03"]}).to_csv(lake / "in" / "a.csv", index=False)
    os.environ.update(CANVAS_DIR=str(root / "canvas"), LAKE_DIR=str(lake), CONNECTORS_STATE_DIR=str(root / "cstate"),
                      CANVAS_SCHEDULER="off", PG_HOST="127.0.0.1", PG_PORT="1")
    yield root


def test_expressions():
    from bsp_canvas.expr import ExprError, evaluate
    df = pd.DataFrame({"a": [1, 2, 3], "s": ["OH", "NC", "VA"], "Rev (USD)": [10.0, 0.0, 5.0],
                       "who": ["ban3100000008@example.net", "x@y", None]})
    assert evaluate(df, "a > 1 and s != 'VA'").tolist() == [False, True, False]
    assert evaluate(df, "s in ['OH', 'VA']").tolist() == [True, False, True]
    assert evaluate(df, "round(`Rev (USD)` / a, 2)").tolist() == [10.0, 0.0, 1.67]
    assert evaluate(df, "if_(a >= 2, 'hi', 'lo')").tolist() == ["lo", "hi", "hi"]
    assert evaluate(df, r"regex_extract(who, '^ban(\d{10})@')").tolist()[0] == "3100000008"
    for bad in ("__import__('os')", "a.__class__", "df[0]", "open('x')", "(lambda: 1)()"):
        with pytest.raises(ExprError):
            evaluate(df, bad)


def _doc(**dest):
    return {"id": "t-lake", "name": "t", "nodes": [
        {"id": "src", "type": "source.lake", "config": {"path": "in/*.csv"}},
        {"id": "cast", "type": "transform.cast", "config": {"types": {"amount": "float", "ts": "datetime"}}},
        {"id": "pii", "type": "transform.pii", "config": {"columns": ["email"], "method": "mask"}},
        {"id": "dest", "type": "dest.lake", "config": {"folder": "out/canvas", "table": "t", "format": "parquet", **dest}},
    ], "edges": [{"from": "src", "to": "cast"}, {"from": "cast", "to": "pii"}, {"from": "pii", "to": "dest"}]}


def test_validate():
    from bsp_canvas import engine
    assert [i for i in engine.validate(_doc()) if i["level"] == "error"] == []
    bad = _doc(mode="incremental")
    bad["edges"].append({"from": "dest", "to": "src"})
    msgs = " ".join(i["message"] for i in engine.validate(bad))
    assert "cursor column" in msgs and "cycle" in msgs and "cannot have inputs" in msgs
    assert any("required" in i["message"] for i in engine.validate({"nodes": [{"id": "x", "type": "transform.filter"}]}))


def test_preview_and_types():
    from bsp_canvas import engine
    pv = engine.preview(_doc(), "pii")
    cols = {c["name"]: c["type"] for c in pv["columns"]}
    assert cols["amount"] == "float" and cols["ts"] == "datetime"
    assert pv["rows"][0]["email"] == "a***@x.net" and pv["rows"][1]["amount"] is None
    assert pv["row_count"] == 3 and any("source.lake" in line for line in pv["logs"])


def test_lake_run_incremental(env):
    from bsp_canvas import engine, store
    store.save(_doc(mode="incremental", cursor_column="ts"))
    r1 = engine.start_run("t-lake", wait=True)
    assert r1["status"] == "success", r1.get("error")
    assert r1["loaded"][0]["rows"] == 3
    out = env / "lake" / "out" / "canvas" / "t"
    files = sorted(out.glob("*.parquet"))
    assert files and len(pd.concat(pd.read_parquet(f) for f in files)) == 3
    assert store.get_state("t-lake")["nodes"]["dest"]["cursor"].startswith("2026-10-03")
    r2 = engine.start_run("t-lake", wait=True)
    assert r2["status"] == "success" and r2["loaded"][0]["rows"] == 0
    pd.DataFrame({"id": [4], "email": ["d@x.net"], "amount": ["1"], "ts": ["2026-10-09"]}).to_csv(
        env / "lake" / "in" / "b.csv", index=False)
    r3 = engine.start_run("t-lake", wait=True)
    assert r3["loaded"][0]["rows"] == 1
    ev = store.get_lineage(r3["run_id"])
    assert [e["eventType"] for e in ev] == ["START", "COMPLETE"]
    assert ev[-1]["outputs"][0]["name"] == "out/canvas/t" and ev[-1]["inputs"]
    log, _ = store.read_log("t-lake", r3["run_id"])
    assert any("incremental" in line for line in log)


def test_partitioned_lake_and_failure(env):
    from bsp_canvas import engine, store
    doc = _doc(mode="full_refresh", partition_by="id")
    doc["id"] = "t-part"
    store.save(doc)
    r = engine.start_run("t-part", wait=True)
    assert r["status"] == "success", r
    assert (env / "lake" / "out" / "canvas" / "t" / "id=1").is_dir()
    doc["nodes"][1]["config"]["types"] = {"nope": "float"}
    store.save(doc)
    r = engine.start_run("t-part", wait=True)
    assert r["status"] == "failed" and r["failed_node"] == "cast" and "nope" in r["error"]
    assert store.get_lineage(r["run_id"])[-1]["eventType"] == "FAIL"


def test_scheduler_fires():
    from bsp_canvas import scheduler, store
    doc = _doc()
    doc.update(id="t-sched", schedule={"cron": "*/5 * * * *", "enabled": True})
    store.save(doc)
    t0 = datetime(2026, 10, 18, 10, 1, tzinfo=timezone.utc)
    assert "t-sched" not in scheduler.tick(t0)
    assert "t-sched" in scheduler.tick(datetime(2026, 10, 18, 10, 6, tzinfo=timezone.utc))


def test_connections_reject_literal_secrets():
    from bsp_canvas import connections
    with pytest.raises(ValueError):
        connections.save({"name": "x", "type": "postgres", "params": {"host": "h", "password": "hunter2"}})
    with pytest.raises(ValueError):
        connections.save({"name": "x", "type": "postgres", "params": {"host": "h", "password_ref": "hunter2"}})
    c = connections.save({"name": "x", "type": "postgres", "params": {"host": "h", "database": "d", "username": "u",
                                                                    "password_ref": "vault:X_PG"}})
    assert c["params"]["password_ref"] == "vault:X_PG"
    with pytest.raises(ValueError, match="not set"):
        connections.sqlalchemy_url(c)


def test_api_static_and_guard():
    from fastapi.testclient import TestClient

    from bsp_canvas.app import app
    with TestClient(app) as c:
        r = c.get("/canvas/")
        assert r.status_code == 200 and 'href="ui-kit/bsp.css"' in r.text and 'src="static/canvas.js"' in r.text
        for asset in ("ui-kit/bsp.css", "ui-kit/bsp.js", "static/canvas.js", "static/canvas.css"):
            assert c.get("/canvas/" + asset).status_code == 200, asset
        assert c.get("/canvas/healthz").json()["status"] == "ok"
        pal = c.get("/canvas/api/palette").json()
        types = {n["type"] for n in pal["nodes"]}
        assert {"source.sharepoint", "source.outlook", "source.rest", "source.database", "source.storage",
                "transform.pii", "transform.join", "dest.warehouse", "dest.storage", "dest.lake"} <= types
        assert c.post("/canvas/api/validate", json={}).status_code == 403
        assert c.post("/canvas/api/validate", json=_doc(), headers={"X-Studio": "1"}).json()["issues"] == []
        ids = {p["id"] for p in c.get("/canvas/api/pipelines").json()["pipelines"]}
        assert {"sharepoint-finance-monthly", "outlook-dispute-emails", "rest-atlan-tables", "tickets-pii-to-lake",
                "bucket-parquet-to-landing", "mssql-orders-incremental"} <= ids


def test_principal_switch_grants(monkeypatch):
    """A connection with a named Google identity: saving asks Studio for a grant
    (the saver's session), the grant is kept out of connections.yaml and the
    API, interactive calls act as the request's user and the scheduler as the
    saver; GCS destinations refuse a switched identity."""
    import json
    from bsp_common import gcp
    from bsp_canvas import connections as C
    from bsp_canvas import config

    asked = []

    def grant(identity):
        asked.append((identity, gcp.acting_user()))
        if gcp.acting_user() != "anna":
            raise gcp.StudioTokenError(f"{gcp.acting_user()} may not act as {identity}", 403)
        return {"grant": "signed-grant", "user": "anna", "identity": "data-analyst", "principal_email": "s@example.com"}
    monkeypatch.setattr(gcp, "studio_grant", grant)
    with pytest.raises(PermissionError):
        with gcp.acting("mallory", session="s-m"):
            C.save({"name": "bq-as-analyst", "type": "bigquery", "params": {"identity": "Example Analyst"}})
    with gcp.acting("anna", session="s-a"):
        rec = C.save({"name": "bq-as-analyst", "type": "bigquery", "params": {"identity": "Example Analyst", "project": "da-tsa-dv"}})
        C.save({"name": "bq-as-analyst", "type": "bigquery", "params": {"identity": "Example Analyst", "project": "da-tsa-dv"}})
    assert rec["identity_user"] == "anna" and asked[-1] == ("Example Analyst", "anna") and len(asked) == 2, "re-save reuses the grant"
    yaml_text = (config.canvas_dir() / "connections.yaml").read_text()
    assert "signed-grant" not in yaml_text and "signed-grant" not in json.dumps(C.list_connections())
    conn = C.get("bq-as-analyst")
    # scheduler (no acting user) → the saver's grant
    with C.acting_for(conn):
        a = gcp._acting.get()
        assert a["user"] == "anna" and a["grant"] == "signed-grant" and not a["session"]
    # an interactive request keeps its own user (Studio checks THAT user)
    with gcp.acting("bob", session="s-b"), C.acting_for(conn):
        assert gcp.acting_user() == "bob"
    # no grant → refused, never the assigned key
    C._set_grant("bq-as-analyst", None)
    with pytest.raises(PermissionError):
        with C.acting_for(conn):
            pass
    with pytest.raises(ValueError):
        C.dlt_credentials({"type": "gcs", "params": {"identity": "Example Analyst"}})
    # unset identity → grant dropped
    C.save({"name": "bq-as-analyst", "type": "bigquery", "params": {}})
    assert "bq-as-analyst" not in C._grants()
    C.delete("bq-as-analyst")
