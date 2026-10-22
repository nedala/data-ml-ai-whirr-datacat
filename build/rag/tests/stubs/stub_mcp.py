"""TEST STUB of the appliance MCP server (build/mcp): exposes tools with the
same group prefixes (browser_, atlan_, bigquery_, warehouse_, trino_, powerbi_,
m365_, mlflow_, sap_brim_, rag_) over streamable HTTP at /mcp, bearer token
optional. rag_* tools call the real rag service through bsp_rag.tools.
Run with the Open WebUI image's Python (it ships the `mcp` SDK):

    MCP_TOKEN=secret python stub_mcp.py   # → http://0.0.0.0:8095/mcp
"""
import json
import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.environ.get("BSP_RAG_PATH", "/opt/rag"))
mcp = FastMCP("bsp-appliance-stub", host="0.0.0.0", port=8095, streamable_http_path="/mcp")


def stub(name: str, doc: str):
    def fn(query: str = "") -> str:
        return json.dumps({"tool": name, "stub": True, "query": query, "result": f"(stub) {name} would run here"})
    fn.__name__ = name
    fn.__doc__ = doc
    mcp.tool(name=name, description=doc)(fn)


for n, d in [
    ("browser_navigate", "Open a URL in the desktop Chromium."), ("browser_snapshot", "Accessibility snapshot of the current page."),
    ("browser_click", "Click an element on the current page."),
    ("atlan_search_assets", "Search the Atlan catalog."), ("atlan_get_asset", "Get an Atlan asset."), ("atlan_lineage", "Lineage of an asset."),
    ("bigquery_list_datasets", "List BigQuery datasets."), ("bigquery_query", "Run read-only BigQuery SQL."),
    ("warehouse_list_tables", "List warehouse tables."), ("warehouse_query", "Run read-only SQL on bsp_edw."),
    ("trino_query", "Run federated read-only SQL in Trino."),
    ("powerbi_list_reports", "List Power BI reports."), ("powerbi_execute_dax", "Run a DAX query."),
    ("m365_list_messages", "List Outlook messages."), ("m365_get_message", "Read one Outlook message."), ("m365_list_files", "List SharePoint files."),
    ("mlflow_search_runs", "Search MLflow runs."), ("mlflow_get_model_version", "Get a registered model version."),
    ("sap_brim_get_account", "Look up a contract account in SAP BRIM."), ("sap_brim_list_disputes", "List dispute cases."),
]:
    stub(n, d)

try:
    from bsp_rag import tools as rag_tools  # the real thing, over HTTP to the rag service

    for fn in rag_tools.TOOLS:
        mcp.tool(name=fn.__name__)(fn)
except Exception as e:  # noqa: BLE001
    print("rag tools unavailable:", e)

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
