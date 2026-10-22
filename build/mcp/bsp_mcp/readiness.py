"""Tool readiness: keep API tools out of the roster until credentials work.

The UI tools (Atlan browser, BigQuery Studio browser) do not need API tokens and
stay enabled.  API tools are gated by ``MCP_API_TOOLS``:

    off      disable the Atlan + BigQuery API tools (default while tokens are missing)
    on       enable them
    auto     enable Atlan when ATLAN_API_TOKEN is set (length >= 20) and BigQuery
             when MCP_BQ_API_ENABLED=1; otherwise keep them disabled

Extra per-tool exclusions can be added with ``MCP_DISABLE_TOOLS``.  The MCP
process builds its tool roster at startup, so a credential change is picked up
on the next MCP restart (Studio → Services → restart MCP, or ``docker restart``).
"""
from __future__ import annotations

import os

ATLAN_API_TOOLS = {
    "atlan_search_assets", "atlan_multi_search", "atlan_get_asset", "atlan_lineage",
    "atlan_glossary_lookup",
}
BIGQUERY_API_TOOLS = {
    "bigquery_list_datasets", "bigquery_list_tables", "bigquery_describe_table",
    "bigquery_dry_run", "bigquery_run_query",
}
UI_TOOLS = {
    "atlan_ui_search", "atlan_ui_asset", "atlan_discover_assets", "atlan_ui_join_map",
    "bigquery_ui_status", "bigquery_ui_query", "bigquery_ui_results", "bigquery_ui_export",
}


def api_mode() -> str:
    return (os.environ.get("MCP_API_TOOLS") or "off").strip().lower()


def disabled_tools() -> set[str]:
    raw = {x.strip() for x in (os.environ.get("MCP_DISABLE_TOOLS") or "").split(",") if x.strip()}
    mode = api_mode()
    if mode in ("on", "1", "true", "enable", "enabled"):
        return raw
    if mode in ("auto", "detect"):
        token = (os.environ.get("ATLAN_API_TOKEN") or "").strip()
        if len(token) < 20:
            raw |= ATLAN_API_TOOLS
        if (os.environ.get("MCP_BQ_API_ENABLED") or "").strip().lower() not in ("1", "true", "on", "yes"):
            raw |= BIGQUERY_API_TOOLS
        return raw
    # off / disabled / anything else = API tools stay out of the roster
    return raw | ATLAN_API_TOOLS | BIGQUERY_API_TOOLS


def status() -> dict:
    disabled = disabled_tools()
    token = (os.environ.get("ATLAN_API_TOKEN") or "").strip()
    bq_creds = any(os.environ.get(k) for k in ("GCP_BQ_JSON", "GOOGLE_APPLICATION_CREDENTIALS", "GCP_SA_JSON"))
    return {
        "api_tools_mode": api_mode(),
        "disabled": sorted(disabled),
        "atlan": {
            "base_url": os.environ.get("ATLAN_BASE_URL") or "https://atlan.example.com",
            "api_token_present": bool(token),
            "api_token_length": len(token),
            "api_state": "enabled" if "atlan_search_assets" not in disabled else "disabled",
        },
        "bigquery": {
            "credentials_present": bq_creds,
            "data_projects": os.environ.get("GCP_BQ_DATA_PROJECTS") or "",
            "exec_project": os.environ.get("GCP_BQ_EXEC_PROJECT") or "",
            "api_state": "enabled" if "bigquery_run_query" not in disabled else "disabled",
        },
        "ui_tools_enabled": sorted(UI_TOOLS - disabled),
        "enable": (
            "Provision the credential, then set MCP_API_TOOLS=on (or MCP_API_TOOLS=auto with a valid "
            "ATLAN_API_TOKEN and MCP_BQ_API_ENABLED=1) and restart the MCP service."
        ),
    }


def probe() -> dict:
    """Optional live check; call only from the status endpoint with ?probe=1."""
    out: dict = {}
    try:
        from bsp_connectors import atlan  # noqa: PLC0415

        atlan.search_assets("dataplatform", limit=1)
        out["atlan"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        state = "unauthorized" if any(x in msg for x in ("401", "403", "Unauthorized", "Forbidden")) else "error"
        out["atlan"] = {"ok": False, "state": state, "error": msg[:300]}
    try:
        from bsp_connectors import bigquery  # noqa: PLC0415

        bigquery.list_datasets()
        out["bigquery"] = {"ok": True}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        state = "unauthorized" if any(x in msg for x in ("401", "403", "Access Denied", "Forbidden")) else "error"
        out["bigquery"] = {"ok": False, "state": state, "error": msg[:300]}
    return out
