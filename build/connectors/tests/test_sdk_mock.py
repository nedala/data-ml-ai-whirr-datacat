"""The §4.1 SDK surface in mock mode."""

from __future__ import annotations

import importlib

import pytest

from conftest import TEST_MAILBOX


def test_mode_of(monkeypatch):
    from bsp_connectors import mode_of
    assert mode_of("m365") == "mock" and mode_of("atlan") == "mock"
    assert mode_of("bigquery") == "mock" and mode_of("powerbi") == "mock"
    monkeypatch.setenv("M365_TENANT_ID", "t")
    monkeypatch.setenv("M365_CLIENT_ID", "c")
    monkeypatch.setenv("M365_CLIENT_SECRET", "s")
    assert mode_of("m365") == "live" and mode_of("powerbi") == "live"
    monkeypatch.setenv("M365_MODE", "mock")
    assert mode_of("m365") == "mock"
    monkeypatch.setenv("ATLAN_BASE_URL", "https://x.atlan.com")
    monkeypatch.setenv("ATLAN_API_TOKEN", "tok")
    assert mode_of("atlan") == "live"
    with pytest.raises(ValueError):
        mode_of("nope")


def test_list_messages_shape_and_order():
    from bsp_connectors import m365
    from bsp_connectors.m365 import mock
    mock.reset_mailbox(TEST_MAILBOX)  # other test modules may have moved messages
    msgs = m365.list_messages(TEST_MAILBOX)
    assert len(msgs) == 5
    keys = {"id", "subject", "from", "receivedDateTime", "bodyPreview", "body", "hasAttachments", "isRead",
            "conversationId"}
    assert keys <= set(msgs[0])
    assert set(msgs[0]["from"]) == {"name", "address"}
    assert set(msgs[0]["body"]) == {"contentType", "content"}
    assert [m["receivedDateTime"] for m in msgs] == sorted((m["receivedDateTime"] for m in msgs), reverse=True)
    assert msgs[0]["subject"] == "Billing dispute #5"
    assert len(m365.list_messages(TEST_MAILBOX, top=2)) == 2
    assert len(m365.list_messages(TEST_MAILBOX, since="2026-10-04")) == 3
    assert len(m365.list_messages(TEST_MAILBOX, since="2026-10-05T00:00:00Z")) == 2


def test_get_message_and_attachment():
    from bsp_connectors import m365
    msg = next(m for m in m365.list_messages(TEST_MAILBOX) if m["subject"].endswith("#3"))
    assert msg["hasAttachments"] is True
    full = m365.get_message(TEST_MAILBOX, msg["id"])
    att = full["attachments"][0]
    assert {"id", "name", "contentType", "size"} <= set(att)
    assert att["name"] == "invoice_3.csv" and att["contentType"] == "text/csv"
    data = m365.get_attachment(TEST_MAILBOX, msg["id"], att["id"])
    assert data.startswith(b"invoice_id,amount") and len(data) == att["size"]


def test_mark_read_and_move():
    from bsp_connectors import m365
    first = m365.list_messages(TEST_MAILBOX, unread_only=True)
    assert len(first) == 5
    target = first[0]["id"]
    m365.mark_read(TEST_MAILBOX, target)
    assert m365.get_message(TEST_MAILBOX, target)["isRead"] is True
    assert len(m365.list_messages(TEST_MAILBOX, unread_only=True)) == 4
    m365.move_message(TEST_MAILBOX, target, "Processed")
    assert target not in {m["id"] for m in m365.list_messages(TEST_MAILBOX)}
    moved = m365.list_messages(TEST_MAILBOX, folder="Processed")
    assert [m["id"] for m in moved] == [target]
    m365.move_message(TEST_MAILBOX, first[1]["id"], "Needs review")
    assert len(m365.list_messages(TEST_MAILBOX, folder="needs review")) == 1
    names = {f["displayName"] for f in m365.list_folders(TEST_MAILBOX)}
    assert {"Inbox", "Processed", "Needs review"} <= names


def test_missing_mailbox_raises_not_found():
    from bsp_connectors import NotFound, m365
    with pytest.raises(NotFound):
        m365.list_messages("nobody@example.net")


def test_sharepoint_files():
    from bsp_connectors import m365
    files = m365.list_files(None, "Finance/Reports")
    names = {f["name"] for f in files}
    assert "Monthly_Revenue_Summary.xlsx" in names
    xl = next(f for f in files if f["name"].endswith(".xlsx"))
    assert {"id", "name", "size", "lastModifiedDateTime", "webUrl", "file", "parentPath"} <= set(xl)
    assert xl["parentPath"] == "Finance/Reports" and "spreadsheetml" in xl["file"]["mimeType"]
    data = m365.download_file(None, xl["id"])
    assert data[:2] == b"PK" and len(data) == xl["size"]
    rec = m365.list_files(None, "Finance", recursive=True)
    assert {f["name"] for f in rec} >= names and all("file" in f for f in rec)
    assert any("folder" in f for f in m365.list_files(None, "Finance", include_folders=True))
    assert m365.list_files("https://contoso.sharepoint.com/sites/Unknown", "Finance/Reports")  # → default site


def test_upload_file_roundtrip():
    from bsp_connectors import m365
    item = m365.upload_file(None, "Finance/Exports", "hello.csv", b"a,b\n1,2\n")
    assert item["name"] == "hello.csv" and item["parentPath"] == "Finance/Exports"
    assert m365.download_file(None, item["id"]) == b"a,b\n1,2\n"
    assert "hello.csv" in {f["name"] for f in m365.list_files(None, "Finance/Exports")}


def test_sharepoint_list_items():
    from bsp_connectors import m365
    assert "Network Projects" in {x["name"] for x in m365.list_lists()}
    rows = m365.list_items(None, "Network Projects")
    assert len(rows) == 15 and {"Title", "State", "Passings"} <= set(rows[0]["fields"])


def test_atlan_search_get_lineage():
    from bsp_connectors import atlan
    hits = atlan.search_assets("ARPU")
    assert hits and hits[0]["typeName"] == "AtlasGlossaryTerm" and hits[0]["name"] == "ARPU"
    tables = atlan.search_assets("churn", type_names=["Table"], limit=5)
    assert tables and all(t["typeName"] == "Table" for t in tables)
    qn = atlan.table_qualified_name("raw", "customers")
    a = atlan.get_asset(qn)
    assert a["attributes"]["certificateStatus"] == "VERIFIED" and "PII" in a["classificationNames"]
    assert any(c["name"] == "email" for c in a["attributes"]["columns"])
    col = atlan.get_asset(qn + "/email")
    assert col["typeName"] == "Column" and col["classificationNames"] == ["PII"]
    lin = atlan.lineage(atlan.table_qualified_name("marts", "fct_revenue_monthly"))
    assert qn in lin["upstream"]
    assert any(n["typeName"] == "PowerBIDataset" for n in lin["nodes"] if n["direction"] == "downstream")
    up_only = atlan.lineage(qn, direction="downstream", depth=1)
    assert up_only["upstream"] == [] and up_only["downstream"]
    from bsp_connectors import NotFound
    with pytest.raises(NotFound):
        atlan.get_asset("default/postgres/bsp_edw/raw/nope")


def test_atlan_push_dbt_manifest():
    from bsp_connectors import atlan
    manifest = {"nodes": {
        "model.bsp_edw.stg_customers": {"resource_type": "model", "name": "stg_customers", "schema": "staging",
                                        "description": "Cleaned customers", "columns": {"ban": {}},
                                        "depends_on": {"nodes": ["source.bsp_edw.raw.customers"]}},
    }, "sources": {"source.bsp_edw.raw.customers": {"resource_type": "source", "name": "customers", "schema": "raw",
                                                     "identifier": "customers"}}}
    res = atlan.push_dbt_manifest(manifest, {"results": [{"status": "success"}]})
    assert res["status"] == "accepted" and res["models"] == 1 and res["lineage_edges"] == 1
    lin = atlan.lineage(atlan.table_qualified_name("staging", "stg_customers"), direction="upstream")
    assert atlan.table_qualified_name("raw", "customers") in lin["upstream"]
    assert atlan.get_asset(atlan.table_qualified_name("staging", "stg_customers"))["attributes"]["description"] == \
        "Cleaned customers"
    # reset so other tests see the static lineage again
    from bsp_connectors._common import state_dir
    (state_dir("atlan") / "dbt_manifest.json").unlink()
    from bsp_connectors.atlan import mock
    mock.invalidate()


def test_bigquery_mock():
    from bsp_connectors import bigquery
    ds = {d["dataset_id"] for d in bigquery.list_datasets()}
    assert "raw" in ds
    assert "customers" in {t["table_id"] for t in bigquery.list_tables("raw")}
    schema = {f["name"]: f["type"] for f in bigquery.table_schema("raw", "customers")}
    assert schema["ban"] == "STRING" and schema["autopay"] == "BOOL"
    rows = bigquery.query("select state, count(*) as n, safe_divide(countif(autopay), count(*)) as autopay_rate "
                          "from `to-be-named-soon.raw.customers` group by state order by state")
    assert rows == [{"state": "NC", "n": 5, "autopay_rate": 0.0}, {"state": "OH", "n": 5, "autopay_rate": 1.0}]
    assert len(bigquery.query("select * from raw.customers", max_rows=3)) == 3
    from bsp_connectors import ConnectorError
    with pytest.raises(ConnectorError):
        bigquery.query("select * from raw.nope")


def test_powerbi_mock():
    from bsp_connectors import powerbi
    names = [w["name"] for w in powerbi.list_workspaces()]
    assert names == ["Revenue & Churn", "Network Ops", "Customer Care"]
    dss = powerbi.list_datasets("Revenue & Churn")
    assert dss[0]["name"] == "Revenue Model"
    assert powerbi.list_reports("Network Ops")[0]["datasetId"]
    rows = powerbi.execute_dax("Revenue & Churn", "Revenue Model",
                               "EVALUATE SUMMARIZECOLUMNS('Revenue'[state], \"Revenue\", [Total Revenue], [ARPU])")
    assert rows and {"Revenue[state]", "[Revenue]", "[ARPU]"} == set(rows[0])
    top = powerbi.execute_dax("Customer Care", "Care Performance", "EVALUATE TOPN(1, 'Tickets', 'Tickets'[tickets], DESC)")
    assert len(top) == 1 and "Tickets[tickets]" in top[0]
    one = powerbi.execute_dax("Customer Care", "Care Performance", 'EVALUATE ROW("NPS", [NPS])')
    assert list(one[0]) == ["[NPS]"]
    res = powerbi.refresh_dataset("Revenue & Churn", dss[0]["id"])
    assert res["status"] == "accepted" and res["requestId"]
    assert powerbi.refresh_history("Revenue & Churn", "Revenue Model")[0]["status"] == "Completed"
    from bsp_connectors import ConnectorError
    with pytest.raises(ConnectorError):
        powerbi.execute_dax("Revenue & Churn", "Revenue Model", "EVALUATE [Nope]")


def test_sap_brim_tolerant_import(monkeypatch):
    import bsp_connectors
    real = importlib.import_module

    def fake(name, package=None):
        if name.endswith("sap_brim"):
            raise ModuleNotFoundError("No module named 'bsp_connectors.sap_brim'")
        return real(name, package)

    monkeypatch.delitem(bsp_connectors.__dict__, "sap_brim", raising=False)
    monkeypatch.setattr(importlib, "import_module", fake)
    mod = bsp_connectors.__getattr__("sap_brim")
    assert not mod
    with pytest.raises(bsp_connectors.ConnectorError, match="sap_brim is not available"):
        mod.create_dispute({})
