"""MCP prompts — reusable starting points for agents."""
from __future__ import annotations

PROMPTS = {
    "explain_churn_drivers": ("Explain churn drivers for an account (by BAN)", ["ban"]),
    "draft_data_quality_check": ("Draft a data-quality check for a table", ["table"]),
    "investigate_billing_dispute": ("Investigate a billing dispute (by BAN and invoice)", ["ban", "invoice_id"]),
    "find_trusted_dataset": ("Find the trusted dataset for a business question", ["question"]),
}


def register(mcp) -> None:
    @mcp.prompt(name="explain_churn_drivers", description=PROMPTS["explain_churn_drivers"][0])
    def explain_churn_drivers(ban: str) -> str:
        return (f"Explain the likely churn drivers for DataPlatform BAN {ban} (billing account number).\n"
                "1. warehouse_query raw.customer_monthly WHERE ban = '<ban>' for the latest months (technology copper|fiber, "
                "plan, tenure, outages, tickets, calls, dispatches, disputes, late-payment history, NPS, recent price increase).\n"
                "2. Pull its plant quality (raw.ont_metrics for fiber, raw.copper_line_metrics for copper), service tickets "
                "(raw.service_tickets), calls (raw.customer_calls), truck rolls (raw.dispatches), outages "
                "(raw.network_events joined on the account's clli), invoices / ledger (raw.invoices, raw.transactions), "
                "disputes (raw.disputes) and NPS verbatims (raw.nps_surveys).\n"
                "3. Compare with the average for the same technology; if MLflow has a churn model, use mlflow_model_alias "
                "to cite its version.\n"
                "Answer with the top 3 drivers, the evidence for each (numbers + table names) and one retention action. "
                "Data is synthetic; do not invent values you did not query.")

    @mcp.prompt(name="draft_data_quality_check", description=PROMPTS["draft_data_quality_check"][0])
    def draft_data_quality_check(table: str) -> str:
        return (f"Draft data-quality checks for {table}.\n"
                "Use warehouse_describe for columns and a sample, atlan_search_assets / atlan_get_asset for the owner, "
                "certification and glossary meaning, and dbt docs (resource dbt://docs) for existing tests.\n"
                "Propose: dbt schema tests (YAML: not_null, unique, accepted_values, relationships) and 2-3 custom SQL "
                "checks (freshness, volume anomaly, business rule). Validate each SQL with warehouse_query (LIMIT 10) "
                "before proposing it, and report the current failure count.")

    @mcp.prompt(name="investigate_billing_dispute", description=PROMPTS["investigate_billing_dispute"][0])
    def investigate_billing_dispute(ban: str, invoice_id: str = "") -> str:
        return (f"Investigate the billing dispute for BAN {ban}{' invoice ' + invoice_id if invoice_id else ''} "
                "(invoice ids are <ban>-<yymm>).\n"
                "Check raw.invoices (recurring / one-time / taxes / credits / late fee), the ledger in raw.transactions "
                "(txn_type payment | late_fee | credit), earlier cases in raw.disputes, the plan/promo in "
                "raw.subscriptions + raw.plans, recent outages on the account's clli (raw.network_events) for credits, "
                "and any e-mail in the disputes mailbox (m365_mail_search with the BAN). "
                "If a SAP BRIM case exists (sap_get_dispute by external reference), include its status. "
                "Conclude: valid / partially valid / not valid, with the amount and the evidence.")

    @mcp.prompt(name="find_trusted_dataset", description=PROMPTS["find_trusted_dataset"][0])
    def find_trusted_dataset(question: str) -> str:
        return (f"Which dataset should be used to answer: {question!r}?\n"
                "Search Atlan (atlan_search_assets, atlan_glossary_lookup) for certified assets and the business term, "
                "check lineage (atlan_lineage) back to sources, and confirm the columns exist (warehouse_describe or "
                "bigquery_describe_table). If the Atlan API returns 401/403, use atlan_ui_search(query, kind='Table') "
                "— the logged-in Linux Desktop browser UI fallback — and continue; do not stop at the API error. "
                "For a specific table's columns, call atlan_ui_asset(url=<asset URL>) (or atlan_ui_asset(name=...)) — "
                "search alone does not return a schema. Recommend one table with owner, certification and a sample query.")
