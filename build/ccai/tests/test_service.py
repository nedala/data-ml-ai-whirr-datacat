"""End-to-end against a real Postgres (skipped unless PG_HOST is set, e.g. the bsp-ccai-pg test container):
full ingest of the fixture folder in mock mode → offline tagging → idempotent re-run → retry failed →
review → exports (CSV / XLSX / Parquet) → publish → portal pages."""
import io
import json
import os

import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("PG_HOST"), reason="needs Postgres (set PG_HOST/PG_PASS)")
H = {"X-Studio": "1"}


@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from bsp_ccai import app as appmod, db
    db.init_schema()
    db.ex("drop schema if exists ccai cascade")
    with TestClient(appmod.app) as c:
        yield c


def _run(client, body=None):
    from bsp_ccai import ingest
    r = client.post("/ccai/api/runs", json=body or {}, headers=H)
    assert r.status_code == 200, r.text
    ingest.RUNNER.join(300)
    return client.get(f"/ccai/api/runs/{r.json()['run_id']}").json()


def test_full_ingest_and_tag(client):
    d = _run(client)
    run = d["run"]
    assert run["status"] == "partial" and run["errors"] == 1  # the truncated upload
    assert run["files_discovered"] == run["files_new"] >= 200
    assert run["calls_parsed"] == 200 and run["calls_tagged"] == 200 and run["manifests"] >= 10
    assert run["source_mode"].endswith(":mock")
    st = client.get("/ccai/api/status").json()
    assert st["counts"]["calls"]["tagged"] == 200 and st["counts"]["files"]["errors"] == 1


def test_accuracy_from_db(client):
    from bsp_ccai import db
    from conftest import GROUND_TRUTH
    truth = json.loads(GROUND_TRUTH.read_text())["calls"]
    rows = db.q("select call_id, primary_leaf, candidates, confidence from ccai.call_tags where is_current")
    n = len(rows)
    top1 = sum(r["primary_leaf"] == truth[r["call_id"]]["primary"] for r in rows) / n
    top3 = sum(truth[r["call_id"]]["primary"] in [c["leaf"] for c in r["candidates"][:3]] for r in rows) / n
    print(f"\nDB accuracy: n={n} top1={top1:.3f} top3={top3:.3f}")
    assert top1 >= 0.75 and top3 >= 0.88


def test_incremental_rerun_is_noop(client):
    run = _run(client)["run"]
    assert run["status"] == "succeeded" and run["files_new"] == 0 and run["files_changed"] == 0
    assert run["calls_parsed"] == 0 and run["calls_tagged"] == 0


def test_retry_failed(client):
    first = client.get("/ccai/api/runs").json()[-1]
    r = client.post(f"/ccai/api/runs/{first['run_id']}/retry", headers=H)
    assert r.status_code == 200
    from bsp_ccai import ingest
    ingest.RUNNER.join(120)
    run = client.get(f"/ccai/api/runs/{r.json()['run_id']}").json()["run"]
    assert run["mode"] == "retry_failed" and run["files_downloaded"] == 1 and run["errors"] == 1


def test_calls_api_and_review(client):
    r = client.get("/ccai/api/calls", params={"limit": 5, "sort": "confidence", "desc": False}).json()
    assert r["total"] == 200 and len(r["rows"]) == 5
    cid = r["rows"][0]["call_id"]
    d = client.get(f"/ccai/api/calls/{cid}").json()
    assert d["call"]["turns"] and d["tag"]["primary"]["name"]
    other = next(c["leaf"] for c in d["tag"]["candidates"] if c["leaf"] != d["tag"]["primary_leaf"])
    assert client.post(f"/ccai/api/calls/{cid}/review", json={"override_leaf": other}).status_code == 403  # CSRF
    ok = client.post(f"/ccai/api/calls/{cid}/review", json={"override_leaf": other, "note": "t"}, headers={**H, "X-Forwarded-User": "mgr@example.net"}).json()
    assert ok["override_leaf"] == other and ok["reviewed_by"] == "mgr@example.net"
    cid2 = r["rows"][1]["call_id"]
    client.post(f"/ccai/api/calls/{cid2}/review", json={"override_leaf": None}, headers=H)
    q = client.get("/ccai/api/quality").json()
    assert q["summary"]["reviewed"] == 2 and q["summary"]["overridden"] == 1 and q["summary"]["accuracy"] == 0.5
    assert len(client.get("/ccai/api/quality/queue").json()["rows"]) == q["summary"]["queue"]


def test_exports(client):
    import pandas as pd
    for fmt in ("csv", "xlsx", "parquet"):
        r = client.get("/ccai/api/exports", params={"format": fmt, "date_from": "2026-10-05", "date_to": "2026-10-09"})
        assert r.status_code == 200, r.text
        n = int(r.headers["x-row-count"])
        assert 50 < n < 200
        df = {"csv": lambda b: pd.read_csv(io.BytesIO(b)), "xlsx": lambda b: pd.read_excel(io.BytesIO(b), sheet_name="Call tags"),
              "parquet": lambda b: pd.read_parquet(io.BytesIO(b))}[fmt](r.content)
        assert len(df) == n and {"final_leaf", "confidence", "sap_reason"} <= set(df.columns)
    pub = client.post("/ccai/api/exports/publish", json={"format": "csv"}, headers=H).json()
    assert pub["folder"] == "CCAI/Tagged" and pub["rows"] == 200
    assert len(client.get("/ccai/api/exports/history").json()) == 5


def test_portal_pages(client):
    html = client.get("/ccai/").text
    assert "CCAI Call Insights" in html and "/ccai/ui-kit/bsp.js" in html
    for p in ("/ccai/static/app.js", "/ccai/ui-kit/bsp.css", "/ccai/healthz"):
        assert client.get(p).status_code == 200
    tax = client.get("/ccai/api/taxonomy").json()
    assert tax["leaves"] == 104 and tax["total"] == 200 and not tax["validation"]
    ov = client.get("/ccai/api/overview").json()
    assert ov["top_reasons"] and sum(x["n"] for x in ov["l1"]) == 200
    assert client.put("/ccai/api/settings", json={"confidence_threshold": 0.7}, headers=H).json()["settings"]["confidence_threshold"] == 0.7
    assert client.put("/ccai/api/settings", json={"route": "bogus"}, headers=H).status_code == 400
