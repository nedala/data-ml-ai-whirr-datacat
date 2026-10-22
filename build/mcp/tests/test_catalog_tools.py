"""catalog_* tools over bsp_common.catalog (stubbed backend) and the Trino BigQuery guards."""
from test_server import call


def test_catalog_tools_without_backend_explain_how_to_enable(monkeypatch):
    monkeypatch.delenv("DATAHUB_TOKEN", raising=False)
    monkeypatch.setenv("CATALOG_BACKEND", "none")
    from bsp_mcp.server import build
    err, d = call(build(), "catalog_search", query="churn")
    assert err and "DATAHUB_TOKEN" in d


def test_catalog_search_and_describe(monkeypatch):
    from bsp_common import catalog
    monkeypatch.setattr(catalog, "backend", lambda: "datahub")
    hit = {"urn": catalog.dataset_urn("bq_rpt_enterprise_dv.ew_hub_prod.brim_ban_cust"), "platform": "trino",
           "name": "bq_rpt_enterprise_dv.ew_hub_prod.brim_ban_cust", "description": "BRIM BAN to customer map (AI-suggested)",
           "tags": ["ai-annotated"], "terms": ["BAN"], "has_ban": True, "ai_annotated": True}
    monkeypatch.setattr(catalog, "search", lambda q, types=None, limit=20, platform=None: [hit])
    monkeypatch.setattr(catalog, "describe", lambda n, platform="trino": hit | {
        "domain": "Billing", "columns": [{"name": "ban", "type": "varchar", "description": "", "tags": [], "terms": ["BAN"]}]})
    from bsp_mcp.server import build
    srv = build()
    err, d = call(srv, "catalog_search", query="BRIM BAN")
    assert not err and d["tables"][0]["name"].startswith("bq_rpt_enterprise_dv.") and "brim_ban_cust" in d["summary"]
    err, d = call(srv, "catalog_describe", table=hit["name"])
    assert not err and d["table"]["columns"][0]["name"] == "ban" and "Billing" in d["summary"]


def test_bigquery_catalogs_are_never_crawled_or_sampled():
    from bsp_mcp.tools import trino
    assert trino._remote("bq_tsa_dv") and trino._remote("bigquery") and not trino._remote("warehouse")


def test_explain_scan_pattern_matches_every_scan_node():
    from bsp_mcp.tools import trino
    plan = ("ScanFilterProject[table = billing_oracle:billing.invoices BILLING.INVOICES constraint on [STATUS]]\n"
            "ScanFilter[table = warehouse:marts.dim_customer bsp_edw.marts.dim_customer columns=[ban]]\n"
            "ScanProject[table = care_mssql:dbo.care_orders care.dbo.care_orders columns=[ban]]\n"
            "TableScan[table = bq_tsa_dv:da-tsa-dv.ETL_CONTROL.jobs:TABLE columns=[]]")
    systems = {m.group(1) for m in trino._SCAN.finditer(plan)}
    assert systems == {"billing_oracle", "warehouse", "care_mssql", "bq_tsa_dv"}


def test_catalog_search_bigquery_means_bq_catalogs(monkeypatch):
    from bsp_common import catalog
    monkeypatch.setattr(catalog, "backend", lambda: "datahub")
    seen = {}

    def search(q, types=None, limit=20, platform=None):
        seen["platform"] = platform
        return [{"urn": "u1", "platform": "trino", "name": "warehouse.marts.x", "description": "", "tags": [], "terms": [],
                 "has_ban": True, "ai_annotated": False},
                {"urn": "u2", "platform": "trino", "name": "bq_tsa_dv.s.t", "description": "", "tags": [], "terms": [],
                 "has_ban": False, "ai_annotated": False}]
    monkeypatch.setattr(catalog, "search", search)
    from bsp_mcp.server import build
    err, d = call(build(), "catalog_search", query="churn", platform="bigquery")
    assert not err and seen["platform"] == "trino" and [t["name"] for t in d["tables"]] == ["bq_tsa_dv.s.t"]
