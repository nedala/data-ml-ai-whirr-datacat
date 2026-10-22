"""The FastAPI app, the plugin rule, and live clients run against the mock routers."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import TEST_MAILBOX


@pytest.fixture(scope="module")
def client():
    from bsp_connectors.app import app
    return TestClient(app)


def test_health_docs_status(client):
    assert client.get("/connectors/healthz").json()["status"] == "ok"
    r = client.get("/connectors/")
    assert r.status_code == 200 and "ui-kit/bsp.js" in r.text and "static/app.js" in r.text
    assert client.get("/connectors/ui-kit/bsp.css").status_code == 200
    assert client.get("/connectors/static/app.js").status_code == 200
    assert client.get("/connectors/docs").status_code == 200
    spec = client.get("/connectors/openapi.json").json()
    assert any(p.startswith("/m365/graph/v1.0/users") for p in spec["paths"])
    assert client.get("/connectors", follow_redirects=False).headers["location"] == "/connectors/"


def test_systems_and_plugins(client):
    d = client.get("/connectors/api/systems").json()
    by = {s["id"]: s for s in d["systems"]}
    assert set(by) == {"m365", "atlan", "bigquery", "powerbi", "sap_brim"}
    assert all(by[s]["mode"] == "mock" for s in ("m365", "atlan", "bigquery", "powerbi"))
    for s in ("m365", "atlan", "bigquery", "powerbi"):
        assert by[s]["plugin"]["mounted"], by[s]["plugin"]
    assert by["m365"]["configured"]["M365_CLIENT_SECRET"] is False  # names only, never values


def test_test_connection_csrf(client):
    assert client.post("/connectors/api/systems/atlan/test").status_code == 403
    for s in ("m365", "atlan", "bigquery", "powerbi"):
        r = client.post(f"/connectors/api/systems/{s}/test", headers={"X-Studio": "1"}).json()
        assert r["ok"], r


def test_graph_mock_routes(client):
    g = "/connectors/m365/graph/v1.0"
    r = client.get(f"{g}/users/{TEST_MAILBOX}/mailFolders/inbox/messages", params={"$top": 2})
    body = r.json()
    assert len(body["value"]) == 2 and "@odata.nextLink" in body
    assert body["value"][0]["from"]["emailAddress"]["address"].endswith("@example.net")
    r = client.get(f"{g}/sites/root/drive/root:/Finance/Reports:/children")
    assert "Monthly_Revenue_Summary.xlsx" in {i["name"] for i in r.json()["value"]}


def _parity_env(monkeypatch, base: str):
    monkeypatch.setenv("GRAPH_BASE_URL", base + "/m365/graph/v1.0")
    monkeypatch.setenv("POWERBI_BASE_URL", base + "/powerbi/v1.0/myorg")
    monkeypatch.setenv("ATLAN_BASE_URL", base + "/atlan")


def test_live_m365_against_mock(live_server, monkeypatch):
    _parity_env(monkeypatch, live_server)
    from bsp_connectors.m365 import live, mock
    lv = live.list_messages(TEST_MAILBOX, top=50)
    mk = mock.list_messages(TEST_MAILBOX, top=50)
    assert [m["id"] for m in lv] == [m["id"] for m in mk]
    assert lv[0]["from"] == mk[0]["from"]
    with_att = next(m for m in mk if m["hasAttachments"])
    full = live.get_message(TEST_MAILBOX, with_att["id"])
    assert full["attachments"][0]["name"] == mock.get_message(TEST_MAILBOX, with_att["id"])["attachments"][0]["name"]
    data = live.get_attachment(TEST_MAILBOX, with_att["id"], full["attachments"][0]["id"])
    assert data == mock.get_attachment(TEST_MAILBOX, with_att["id"], full["attachments"][0]["id"])
    unread = live.list_messages(TEST_MAILBOX, unread_only=True)
    live.mark_read(TEST_MAILBOX, unread[-1]["id"])
    assert mock.get_message(TEST_MAILBOX, unread[-1]["id"])["isRead"]
    live.move_message(TEST_MAILBOX, unread[-1]["id"], "Archive Review")
    assert unread[-1]["id"] in {m["id"] for m in live.list_messages(TEST_MAILBOX, folder="Archive Review")}
    files = live.list_files(None, "Finance/Reports")
    assert {f["name"] for f in files} == {f["name"] for f in mock.list_files(None, "Finance/Reports")}
    assert files[0]["parentPath"] == "Finance/Reports"
    xl = next(f for f in files if f["name"].endswith(".xlsx"))
    assert live.download_file(None, xl["id"])[:2] == b"PK"
    rec = live.list_files(None, "Finance", recursive=True)
    assert {f["name"] for f in rec} >= {f["name"] for f in files}
    up = live.upload_file(None, "Finance/Exports", "live.txt", b"hi")
    assert up["name"] == "live.txt" and mock.download_file(None, up["id"]) == b"hi"
    assert len(live.list_items(None, "Network Projects")) == 15


def test_live_powerbi_and_atlan_against_mock(live_server, monkeypatch):
    _parity_env(monkeypatch, live_server)
    from bsp_connectors.atlan import live as alive
    from bsp_connectors.atlan import mock as amock
    from bsp_connectors.powerbi import live as plive
    assert [w["name"] for w in plive.list_workspaces()] == ["Revenue & Churn", "Network Ops", "Customer Care"]
    assert plive.list_datasets("Network Ops")[0]["name"] == "Network Reliability"
    rows = plive.execute_dax("Revenue & Churn", "Revenue Model", 'EVALUATE ROW("ARPU", [ARPU])')
    assert list(rows[0]) == ["[ARPU]"]
    assert plive.refresh_dataset("Customer Care", "Care Performance")["requestId"]
    hits = alive.search_assets("fiber penetration", limit=5)
    assert hits and hits[0]["qualifiedName"] == amock.search_assets("fiber penetration", limit=5)[0]["qualifiedName"]
    qn = amock.table_qn("marts", "fct_revenue_monthly")
    assert alive.get_asset(qn)["qualifiedName"] == qn
    lin = alive.lineage(qn)
    assert set(lin["upstream"]) == set(amock.lineage(qn)["upstream"])
    res = alive.upsert_processes([{"qualifiedName": "default/postgres/bsp_edw/process/canvas-test", "name": "canvas test",
                                   "inputs": [amock.table_qn("raw", "plans")], "outputs": [amock.table_qn("marts", "dim_customer")]}])
    assert res["entities"] == 1
