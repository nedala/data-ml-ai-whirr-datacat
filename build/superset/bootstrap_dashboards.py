#!/usr/bin/env python3
"""Prime Superset with the DataPlatform analytics content (idempotent).

Run by ``build/superset/superset.sh`` once Superset answers; safe to re-run at
any time (e.g. after ``dbt build`` refreshed the marts)::

    python bootstrap_dashboards.py                 # against $SUPERSET_URL (default http://127.0.0.1:8088/superset)
    python bootstrap_dashboards.py --verify        # … and run every chart query
    python bootstrap_dashboards.py --export-dir /tmp/bundle   # write the YAML bundle only (no Superset calls)
    python bootstrap_dashboards.py --zip /tmp/bsp_dashboards.zip  # one importable dashboard ZIP
                                                   #   (superset import-dashboards -p … -u admin)

What it creates / updates (deterministic uuids → re-imports overwrite in place)
-------------------------------------------------------------------------------
* Database **"DataPlatform warehouse"** — postgresql+psycopg2://analyst:***@postgres:5432/bsp_edw
  (password = PG_PASS at runtime; SQL Lab on, DML/CTAS off).
* Database **"Trino (federated)"** — trino://analyst@trino:8080/warehouse, when Trino answers
  (or SUPERSET_REGISTER_TRINO=1): SQL Lab across warehouse ⨝ bigquery ⨝ tpch ⨝ lake.
* Physical datasets for every dbt mart in schema ``marts`` (columns read live from
  information_schema, or from assets/marts_columns.json before dbt has run) with metrics.
  Data model: accounts keyed by ``ban`` (10-digit billing account number), wire centers by
  ``clli``; technology is ``copper`` or ``fiber``. Physical datasets whose marts table no longer
  exists any more (retired marts tables) are pruned, together with their orphaned charts.
* Ten dashboards with native filters:
    bsp-executive · bsp-fiber-penetration · bsp-network-reliability ·
    bsp-customer-sentiment · bsp-billing-collections · bsp-churn-retention ·
    bsp-revenue-bridge · bsp-field-ops · bsp-contact-center · bsp-ai-ml-operations
* SQL Lab saved queries.

Environment: SUPERSET_URL, SUPERSET_ADMIN_USER (admin), SUPERSET_ADMIN_PASS, PG_HOST, PG_PORT,
PG_DB, PG_USER, PG_PASS, TRINO_HOST, TRINO_PORT, SUPERSET_REGISTER_TRINO.
Exit code 0 on success, 1 on failure.
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import socket
import sys
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("bsp-superset")
NS = uuid.UUID("0c7d3b6e-5a1f-4f43-9d0e-6b2f8e1a4c55")  # deterministic uuids → idempotent imports
HERE = Path(__file__).resolve().parent


def uid(name: str) -> str:
    return str(uuid.uuid5(NS, name))


MASK = "XXXXXXXXXX"
SCHEMA = "marts"
DB_NAME = "DataPlatform warehouse"
TRINO_NAME = "Trino (federated)"
PG_HOST = os.environ.get("PG_HOST", "postgres")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DB = os.environ.get("PG_DB", "bsp_edw")
PG_USER = os.environ.get("PG_USER", "analyst")
TRINO_HOST = os.environ.get("TRINO_HOST", "trino")
TRINO_PORT = int(os.environ.get("TRINO_PORT", "8080"))
TS = "2026-01-01T00:00:00+00:00"
ROOT = "bsp_bundle"

# ── dataset metrics (Postgres / Trino-compatible SQL) ────────────────────────────
FLAG = "CASE WHEN {} THEN 1.0 ELSE 0 END"
PAY_ONLY = "CASE WHEN txn_type = 'payment' THEN {} END"
METRICS: dict[str, list[tuple[str, str, str]]] = {
    "fct_revenue_monthly": [
        ("subscribers", "SUM(subscribers)", ",d"),
        ("service_revenue", "SUM(service_revenue)", "$,.3s"),
        ("recurring_revenue", "SUM(recurring_revenue)", "$,.3s"),
        ("one_time_revenue", "SUM(one_time_revenue)", "$,.3s"),
        ("late_fee_revenue", "SUM(late_fee_revenue)", "$,.3s"),
        ("credits", "SUM(credits)", "$,.3s"),
        ("net_revenue", "SUM(net_revenue)", "$,.3s"),
        ("arpu", "SUM(service_revenue) / NULLIF(SUM(subscribers), 0)", "$,.2f"),
        ("churn_rate", "SUM(churned_subscribers) * 1.0 / NULLIF(SUM(subscribers), 0)", ".2%"),
        ("churned", "SUM(churned_subscribers)", ",d"),
        ("billed", "SUM(billed_amount)", "$,.3s"),
        ("collected", "SUM(collected_amount)", "$,.3s"),
        ("collection_rate", "SUM(collected_amount) / NULLIF(SUM(billed_amount), 0)", ".1%"),
        ("disputes", "SUM(disputes)", ",d"),
    ],
    "mart_arpu_by_plan": [
        ("arpu", "SUM(service_revenue) / NULLIF(SUM(subscribers), 0)", "$,.2f"),
        ("subscribers", "SUM(subscribers)", ",d"),
        ("discount_share", "SUM(discount_amount) / NULLIF(SUM(service_revenue) + SUM(discount_amount), 0)", ".1%"),
    ],
    "mart_fiber_penetration": [
        ("fiber_subscribers", "SUM(fiber_subscribers)", ",d"),
        ("copper_subscribers", "SUM(copper_subscribers)", ",d"),
        ("active_accounts", "SUM(active_accounts)", ",d"),
        ("fiber_passings", "SUM(fiber_passings)", ",d"),
        ("fiber_take_rate", "SUM(fiber_subscribers) * 1.0 / NULLIF(SUM(fiber_passed_active), 0)", ".1%"),
        ("fiber_availability", "SUM(fiber_passed_active) * 1.0 / NULLIF(SUM(active_accounts), 0)", ".1%"),
        ("fiber_penetration", "SUM(fiber_subscribers) * 1.0 / NULLIF(SUM(fiber_passings), 0)", ".1%"),
        ("upgrade_opportunity", "SUM(upgrade_opportunity_accounts)", ",d"),
        ("upgrade_mrr", "SUM(upgrade_opportunity_mrr)", "$,.0f"),
        ("competitor_overlap", "SUM(competitor_fiber_active) * 1.0 / NULLIF(SUM(active_accounts), 0)", ".1%"),
        ("copper_at_risk", "SUM(copper_at_risk_competitor)", ",d"),
    ],
    "mart_network_reliability": [
        ("outage_minutes", "SUM(outage_minutes)", ",.0f"),
        ("customer_outage_minutes", "SUM(customer_outage_minutes)", ",.3s"),
        ("outage_events", "SUM(outage_events)", ",d"),
        ("tickets", "SUM(tickets)", ",d"),
        ("truck_rolls", "SUM(truck_rolls)", ",d"),
        ("dispatches", "SUM(dispatches)", ",d"),
        ("repeat_dispatches", "SUM(repeat_dispatches)", ",d"),
        ("repeat_dispatch_rate", "SUM(repeat_dispatches) * 1.0 / NULLIF(SUM(dispatches), 0)", ".1%"),
        ("avg_rx_power_dbm", "AVG(avg_ont_rx_power_dbm)", ",.1f"),
        ("weak_optics_share", "SUM(weak_optics_onts) * 1.0 / NULLIF(SUM(fiber_subscribers), 0)", ".1%"),
        ("avg_snr_margin_db", "AVG(avg_dsl_snr_margin_db)", ",.1f"),
        ("marginal_line_share", "SUM(marginal_dsl_lines) * 1.0 / NULLIF(SUM(copper_subscribers), 0)", ".1%"),
        ("fcr_rate", "SUM(first_contact_resolved) * 1.0 / NULLIF(SUM(tickets), 0)", ".1%"),
        ("repeat_rate", "SUM(repeat_tickets) * 1.0 / NULLIF(SUM(tickets), 0)", ".1%"),
        ("tickets_per_1k", "1000.0 * SUM(tickets) / NULLIF(SUM(subscribers), 0)", ",.1f"),
        ("truck_rolls_per_1k", "1000.0 * SUM(truck_rolls) / NULLIF(SUM(subscribers), 0)", ",.1f"),
    ],
    "fct_service_ticket": [
        ("tickets", "COUNT(*)", ",d"),
        ("truck_rolls", f"SUM({FLAG.format('is_truck_roll')})", ",d"),
        ("fcr_rate", f"AVG({FLAG.format('is_first_contact_resolved')})", ".1%"),
        ("repeat_rate", f"AVG({FLAG.format('is_repeat_30d')})", ".1%"),
        ("avg_resolution_hours", "AVG(resolution_hours)", ",.1f"),
    ],
    "mart_customer_sentiment": [
        ("responses", "COUNT(*)", ",d"),
        ("nps", "100.0 * (SUM(is_promoter) - SUM(is_detractor)) / NULLIF(COUNT(*), 0)", ",.1f"),
        ("promoter_share", "AVG(is_promoter * 1.0)", ".1%"),
        ("detractor_share", "AVG(is_detractor * 1.0)", ".1%"),
        ("avg_score", "AVG(nps_score)", ",.2f"),
    ],
    "fct_invoice": [
        ("invoices", "COUNT(*)", ",d"),
        ("late_invoices", f"SUM({FLAG.format('is_late')})", ",d"),
        ("late_rate", f"AVG({FLAG.format('is_late')})", ".1%"),
        ("avg_days_to_pay", "AVG(days_to_pay)", ",.1f"),
        ("billed", "SUM(amount_due)", "$,.3s"),
        ("service_revenue", "SUM(service_revenue)", "$,.3s"),
        ("collected", "SUM(paid_amount)", "$,.3s"),
        ("late_fees", "SUM(late_fee_amount)", "$,.3s"),
        ("credits", "SUM(credit_amount)", "$,.3s"),
        ("disputed_share", f"AVG({FLAG.format('is_disputed')})", ".1%"),
        ("written_off", "SUM(CASE WHEN is_written_off THEN amount_due ELSE 0 END)", "$,.3s"),
    ],
    "fct_transaction": [
        ("transactions", "COUNT(*)", ",d"),
        ("payments", f"SUM({PAY_ONLY.format('1')})", ",d"),
        ("payment_amount", f"SUM({PAY_ONLY.format('abs_amount')})", "$,.3s"),
        ("transaction_amount", "SUM(abs_amount)", "$,.3s"),
        ("net_amount", "SUM(amount)", "$,.3s"),
        ("late_fees", "SUM(CASE WHEN txn_type = 'late_fee' THEN abs_amount ELSE 0 END)", "$,.3s"),
        ("credits", "SUM(CASE WHEN txn_type = 'credit' THEN abs_amount ELSE 0 END)", "$,.3s"),
        ("paid_late_share", f"AVG({PAY_ONLY.format(FLAG.format('is_paid_late'))})", ".1%"),
        ("avg_days_after_due", f"AVG({PAY_ONLY.format('days_after_due')})", ",.1f"),
    ],
    "mart_billing_disputes": [
        ("disputes", "COUNT(*)", ",d"),
        ("disputed", "SUM(disputed_amount)", "$,.3s"),
        ("credits", "SUM(credit_amount)", "$,.3s"),
        ("credit_ratio", "SUM(credit_amount) / NULLIF(SUM(disputed_amount), 0)", ".1%"),
        ("credited_share", f"AVG({FLAG.format('is_credited')})", ".1%"),
        ("avg_days_to_resolve", "AVG(days_to_resolve)", ",.1f"),
        ("with_billing_call", f"AVG({FLAG.format('billing_calls_30d > 0')})", ".1%"),
        ("with_billing_ticket", f"AVG({FLAG.format('billing_tickets_30d > 0')})", ".1%"),
        ("churned_after", f"AVG({FLAG.format('churned_after_dispute')})", ".1%"),
    ],
    "fct_dispute": [
        ("disputes", "COUNT(*)", ",d"),
        ("disputed", "SUM(disputed_amount)", "$,.3s"),
        ("credits", "SUM(credit_amount)", "$,.3s"),
        ("credited_share", f"AVG({FLAG.format('is_credited')})", ".1%"),
        ("open_disputes", f"SUM({FLAG.format('is_open')})", ",d"),
        ("avg_days_to_resolve", "AVG(days_to_resolve)", ",.1f"),
    ],
    "dim_customer": [
        ("customers", "COUNT(*)", ",d"),
        ("active_customers", f"SUM({FLAG.format('is_active')})", ",d"),
        ("avg_tenure_months", "AVG(tenure_months)", ",.1f"),
    ],
    "dim_wire_center": [
        ("wire_centers", "COUNT(*)", ",d"),
        ("fiber_passings", "SUM(fiber_passings)", ",d"),
        ("copper_lines", "SUM(copper_lines)", ",d"),
        ("active_accounts", "SUM(active_accounts)", ",d"),
        ("fiber_share", "SUM(fiber_subscribers) * 1.0 / NULLIF(SUM(active_accounts), 0)", ".1%"),
    ],
    "mart_churn_features": [
        ("rows", "COUNT(*)", ",d"),
        ("churn_label_rate", f"AVG({FLAG.format('churned_next_90d')})", ".1%"),
    ],
    "fct_dispatch": [
        ("dispatches", "COUNT(*)", ",d"),
        ("repeat_dispatches", f"SUM({FLAG.format('repeat_dispatch_30d')})", ",d"),
        ("repeat_dispatch_rate", f"AVG({FLAG.format('repeat_dispatch_30d')})", ".1%"),
        ("completion_rate", f"AVG({FLAG.format('is_completed')})", ".1%"),
        ("ntf_rate", f"AVG({FLAG.format('is_no_trouble_found')})", ".1%"),
        ("avg_onsite_hours", "AVG(onsite_hours)", ",.2f"),
    ],
    "fct_plant_quality_monthly": [
        ("line_months", "COUNT(*)", ",d"),
        ("avg_rx_power_dbm", "AVG(rx_power_dbm)", ",.1f"),
        ("weak_optics_share", f"AVG(CASE WHEN technology = 'fiber' THEN {FLAG.format('is_weak_optics')} END)", ".1%"),
        ("los_events", "SUM(los_events)", ",d"),
        ("avg_snr_margin_db", "AVG(snr_margin_db)", ",.1f"),
        ("marginal_line_share",
         f"AVG(CASE WHEN technology = 'copper' THEN {FLAG.format('is_marginal_line')} END)", ".1%"),
        ("retrains", "SUM(retrains)", ",d"),
    ],
    "fct_customer_call": [
        ("calls", "COUNT(*)", ",d"),
        ("repeat_call_rate", f"AVG({FLAG.format('is_repeat_call_7d')})", ".1%"),
        ("avg_handle_min", "AVG(handle_time_min)", ",.1f"),
        ("avg_sentiment", "AVG(sentiment_score)", ",.2f"),
        ("resolution_rate", f"AVG({FLAG.format('is_resolved')})", ".1%"),
    ],
    "mart_contact_center": [
        ("calls", "SUM(calls)", ",d"),
        ("repeat_calls", "SUM(repeat_calls)", ",d"),
        ("repeat_call_rate", "SUM(repeat_calls) * 1.0 / NULLIF(SUM(calls), 0)", ".1%"),
        ("avg_handle_min", "SUM(avg_handle_time_sec * calls) / 60.0 / NULLIF(SUM(calls), 0)", ",.1f"),
        ("handle_hours", "SUM(handle_hours)", ",.0f"),
        ("resolution_rate", "SUM(resolved_calls) * 1.0 / NULLIF(SUM(calls), 0)", ".1%"),
        ("avg_sentiment", "SUM(avg_sentiment * calls) / NULLIF(SUM(calls), 0)", ",.2f"),
        ("negative_share", "SUM(negative_calls) * 1.0 / NULLIF(SUM(calls), 0)", ".1%"),
        ("escalation_rate", "SUM(escalated_calls) * 1.0 / NULLIF(SUM(calls), 0)", ".1%"),
    ],
}
VERBOSE = {"arpu": "ARPU", "nps": "NPS", "fcr_rate": "FCR rate", "upgrade_mrr": "Upgrade MRR uplift",
           "tickets_per_1k": "Tickets per 1k subs", "truck_rolls_per_1k": "Truck rolls per 1k subs",
           "ntf_rate": "No-trouble-found rate", "avg_rx_power_dbm": "Avg ONT rx power (dBm)",
           "avg_snr_margin_db": "Avg DSL SNR margin (dB)", "avg_handle_min": "Avg handle time (min)"}
DESCRIPTIONS = {
    "dim_customer": "Customer dimension keyed by BAN (contains PII) — dbt marts.dim_customer",
    "dim_wire_center": "Wire centers (CLLI) with fiber passings, copper lines and subscriber mix",
    "fct_invoice": "One row per invoice — charges, credits, late fees, payment and dispute flags",
    "fct_transaction": "One row per account transaction: payment (negative), late fee, credit (negative)",
    "fct_dispute": "One row per billing dispute with category, outcome and credit",
    "fct_revenue_monthly": "Revenue, subscribers, ARPU, churn by month × state × technology (copper / fiber) × segment",
    "fct_service_ticket": "One row per care / repair ticket",
    "fct_dispatch": "One row per field dispatch (install / repair) with cause, repeat flag and plant readings",
    "fct_plant_quality_monthly": "Plant quality per BAN × month: ONT optics (fiber) and DSL line metrics (copper)",
    "fct_customer_call": "One row per contact-center call: queue, reason, handle time, repeat flag, sentiment",
    "mart_arpu_by_plan": "ARPU and discount depth by month × plan",
    "mart_billing_disputes": "Billing disputes (raw.disputes) with outcome, credit, billing calls and churn after dispute",
    "mart_churn_features": "Churn model features + label per BAN × month",
    "mart_contact_center": "Calls, repeat calls, handle time, resolution and sentiment by month × queue × reason × technology",
    "mart_customer_sentiment": "Survey-level NPS with verbatim theme",
    "mart_fiber_penetration": "Fiber passings, take rate and copper → fiber upgrade opportunity by wire center (CLLI)",
    "mart_network_reliability": "Outages, tickets, dispatches and plant quality by wire center (CLLI) × month",
}

# ── chart builders ───────────────────────────────────────────────────────────────


def _no_time(col: str) -> list[dict]:
    return [{"clause": "WHERE", "comparator": "No filter", "expressionType": "SIMPLE",
             "operator": "TEMPORAL_RANGE", "subject": col}]


def _sqlf(expr: str) -> dict:
    return {"clause": "WHERE", "expressionType": "SQL", "sqlExpression": expr}


def kpi(key, name, ds, metric, fmt, sub, row=0, w=3, filters=None) -> dict:
    return dict(key=key, name=name, ds=ds, viz="big_number_total", row=row, w=w, h=28,
                params={"metric": metric, "y_axis_format": fmt, "subheader": sub, "header_font_size": 0.35,
                        "subheader_font_size": 0.15, "time_format": "smart_date", "adhoc_filters": filters or []})


def trend(key, name, ds, x, metric, fmt, row=0, w=3) -> dict:
    """Big number = latest month, with the monthly trend line."""
    return dict(key=key, name=name, ds=ds, viz="big_number", row=row, w=w, h=34,
                params={"x_axis": x, "time_grain_sqla": "P1M", "metric": metric, "y_axis_format": fmt,
                        "show_trend_line": True, "start_y_axis_at_zero": False, "compare_lag": 1,
                        "compare_suffix": "vs prior month", "header_font_size": 0.35,
                        "subheader_font_size": 0.15, "rolling_type": "None", "time_format": "smart_date",
                        "adhoc_filters": _no_time(x)})


def _ts_common(x, metrics, groupby, fmt, filters, temporal) -> dict:
    return {"x_axis": x, "metrics": metrics, "groupby": groupby, "row_limit": 10000,
            "adhoc_filters": (_no_time(x) if temporal else []) + (filters or []),
            **({"time_grain_sqla": "P1M"} if temporal else {"xAxisForceCategorical": True}),
            "x_axis_sort_asc": True, "y_axis_format": fmt, "show_legend": True, "legendOrientation": "top",
            "legendType": "scroll", "rich_tooltip": True, "order_desc": True, "truncate_metric": True,
            "comparison_type": "values", "color_scheme": "supersetColors", "show_empty_columns": True}


def bar(key, name, ds, x, metrics, groupby, row, w, fmt=",d", h=50, filters=None, stack=True,
        temporal=True, horizontal=False) -> dict:
    p = _ts_common(x, metrics, groupby, fmt, filters, temporal)
    p.update({"stack": "Stack" if stack else None, "orientation": "horizontal" if horizontal else "vertical"})
    if not temporal:
        p.update({"x_axis_sort": metrics[0], "x_axis_sort_asc": False})
    return dict(key=key, name=name, ds=ds, viz="echarts_timeseries_bar", row=row, w=w, h=h, params=p)


def line(key, name, ds, x, metrics, groupby, row, w, fmt=",d", h=50, filters=None, area=False) -> dict:
    p = _ts_common(x, metrics, groupby, fmt, filters, True)
    p.update({"markerEnabled": True, "markerSize": 4})
    if area:
        p.update({"stack": "Stack", "opacity": 0.6})
    return dict(key=key, name=name, ds=ds, viz="echarts_area" if area else "echarts_timeseries_line",
                row=row, w=w, h=h, params=p)


def pie(key, name, ds, groupby, metric, row, w, fmt=",d", h=50, filters=None) -> dict:
    return dict(key=key, name=name, ds=ds, viz="pie", row=row, w=w, h=h,
                params={"groupby": groupby, "metric": metric, "row_limit": 100, "adhoc_filters": filters or [],
                        "show_labels": True, "labels_outside": True, "label_type": "key_percent",
                        "number_format": fmt, "donut": True, "show_legend": True, "legendOrientation": "top",
                        "color_scheme": "supersetColors", "sort_by_metric": True})


def treemap(key, name, ds, groupby, metric, row, w, fmt=",d", h=50) -> dict:
    return dict(key=key, name=name, ds=ds, viz="treemap_v2", row=row, w=w, h=h,
                params={"groupby": groupby, "metric": metric, "row_limit": 10000, "show_labels": True,
                        "show_upper_labels": True, "label_type": "key_value", "number_format": fmt,
                        "color_scheme": "supersetColors", "adhoc_filters": []})


def table(key, name, ds, columns, row, w, order_by, h=55, formats=None, filters=None, metrics=None,
          groupby=None) -> dict:
    p = {"row_limit": 1000, "adhoc_filters": filters or [], "table_timestamp_format": "smart_date",
         "page_length": 15, "include_search": True, "show_cell_bars": True, "server_pagination": False,
         "column_config": {c: {"d3NumberFormat": f} for c, f in (formats or {}).items()}}
    if metrics:   # aggregate mode
        p.update({"query_mode": "aggregate", "groupby": groupby or [], "metrics": metrics,
                  "timeseries_limit_metric": order_by, "order_desc": True})
    else:
        p.update({"query_mode": "raw", "all_columns": columns, "order_by_cols": [json.dumps([order_by, False])]})
    return dict(key=key, name=name, ds=ds, viz="table", row=row, w=w, h=h, params=p)


REV, ARPU, FIB, NET, TIX = "fct_revenue_monthly", "mart_arpu_by_plan", "mart_fiber_penetration", \
    "mart_network_reliability", "fct_service_ticket"
NPS, INV, TXN, DSP, FDS = ("mart_customer_sentiment", "fct_invoice", "fct_transaction", "mart_billing_disputes",
                          "fct_dispute")
PAYMENTS = [_sqlf("txn_type = 'payment'")]

DASHBOARDS = [
    dict(slug="bsp-executive", title="DataPlatform Executive Overview",
         description="Revenue, ARPU, subscribers by technology and churn-rate trend.",
         intro=("### Executive overview\nMonthly billed subscribers, recurring service revenue, ARPU and churn "
                "from the dbt marts (`marts.fct_revenue_monthly`, `marts.mart_arpu_by_plan`). Big numbers show the "
                "latest bill month with the trend. Filters: State · Technology · Segment."),
         filters=[("state", "State", REV), ("technology", "Technology", REV), ("segment", "Segment", REV)],
         charts=[
             trend("kpi_subs", "Billed subscribers", REV, "revenue_month", "subscribers", ",d"),
             trend("kpi_rev", "Service revenue (month)", REV, "revenue_month", "service_revenue", "$,.3s"),
             trend("kpi_arpu", "ARPU", REV, "revenue_month", "arpu", "$,.2f"),
             trend("kpi_churn", "Monthly churn rate", REV, "revenue_month", "churn_rate", ".2%"),
             line("subs_by_tech", "Subscribers by technology", REV, "revenue_month", ["subscribers"],
                  ["technology"], row=1, w=6, area=True),
             bar("rev_by_region", "Service revenue by region", REV, "revenue_month", ["service_revenue"],
                 ["region"], row=1, w=6, fmt="$,.3s"),
             line("churn_by_tech", "Churn rate by technology", REV, "revenue_month", ["churn_rate"],
                  ["technology"], row=2, w=6, fmt=".1%"),
             line("arpu_by_tech", "ARPU by technology", REV, "revenue_month", ["arpu"], ["technology"],
                  row=2, w=6, fmt="$,.0f"),
             bar("arpu_by_plan", "ARPU by plan (all months)", ARPU, "plan_name", ["arpu"], [], row=3, w=6,
                 fmt="$,.2f", temporal=False, stack=False),
             bar("subs_by_state", "Billed subscriber-months by state", REV, "state", ["subscribers"],
                 ["technology"], row=3, w=6, temporal=False),
         ]),
    dict(slug="bsp-fiber-penetration", title="Fiber Penetration & Upgrade Opportunity",
         description="Fiber availability, take rate and copper → fiber upgrade opportunity by state and wire center (CLLI).",
         intro=("### Fiber penetration & upgrade opportunity\nActive copper and fiber accounts, fiber passings, "
                "availability and take rate, and the **upgrade opportunity**: copper customers already passed by "
                "fiber — one row per wire center (CLLI) in `marts.mart_fiber_penetration`."),
         filters=[("region", "Region", FIB), ("state", "State", FIB)],
         charts=[
             kpi("kpi_fiber", "Fiber subscribers", FIB, "fiber_subscribers", ",d", "active fiber accounts"),
             kpi("kpi_take", "Fiber take rate", FIB, "fiber_take_rate", ".1%", "fiber subs ÷ fiber-passed accounts"),
             kpi("kpi_opp", "Upgrade opportunity", FIB, "upgrade_opportunity", ",d", "copper accounts passed by fiber"),
             kpi("kpi_mrr", "Upgrade MRR uplift", FIB, "upgrade_mrr", "$,.0f", "list-price uplift per month"),
             bar("tech_by_state", "Active accounts by technology and state", FIB, "state",
                 ["fiber_subscribers", "copper_subscribers"], [], row=1, w=7,
                 temporal=False),
             bar("take_by_state", "Fiber availability vs take rate by state", FIB, "state",
                 ["fiber_availability", "fiber_take_rate"], [], row=1, w=5, fmt=".0%", temporal=False, stack=False),
             treemap("opp_treemap", "Upgrade opportunity by region and state", FIB, ["region", "state"],
                     "upgrade_opportunity", row=2, w=5),
             table("wc_table", "Wire centers (CLLI) ranked by upgrade opportunity", FIB, [], row=2, w=7,
                   order_by="upgrade_opportunity", groupby=["state", "clli", "wire_center_name"],
                   metrics=["upgrade_opportunity", "upgrade_mrr", "fiber_penetration", "fiber_take_rate",
                            "competitor_overlap", "active_accounts"]),
         ]),
    dict(slug="bsp-network-reliability", title="Network Reliability & Servicing",
         description="Outage minutes, tickets, truck rolls and first-contact resolution.",
         intro=("### Network reliability & servicing\nOutage and degradation minutes by wire center (CLLI), ticket "
                "volume and mix, truck rolls, repeat dispatches and first-contact resolution "
                "(`marts.mart_network_reliability`, `marts.fct_service_ticket`). Field dispatches and plant quality "
                "in detail: **Field Ops & Plant Quality**."),
         filters=[("region", "Region", NET), ("state", "State", NET)],
         charts=[
             trend("kpi_outage", "Network outage minutes", NET, "report_month", "outage_minutes", ",.0f"),
             trend("kpi_tickets", "Tickets", NET, "report_month", "tickets", ",d"),
             trend("kpi_trucks", "Truck rolls", NET, "report_month", "truck_rolls", ",d"),
             trend("kpi_fcr", "First-contact resolution", NET, "report_month", "fcr_rate", ".1%"),
             bar("outage_by_region", "Outage minutes by region", NET, "report_month", ["outage_minutes"],
                 ["region"], row=1, w=6, fmt=",.0f"),
             bar("tickets_by_cat", "Tickets by category", TIX, "opened_month", ["tickets"], ["category"],
                 row=1, w=6),
             line("per_1k", "Tickets and truck rolls per 1k subscribers", NET, "report_month",
                  ["tickets_per_1k", "truck_rolls_per_1k"], [], row=2, w=6, fmt=",.1f"),
             line("fcr_by_channel", "FCR rate by channel", TIX, "opened_month", ["fcr_rate"], ["channel"],
                  row=2, w=6, fmt=".0%"),
             table("worst_wc", "Wire centers (CLLI): outage minutes and ticket rates", NET, [], row=3, w=12,
                   order_by="outage_minutes", groupby=["state", "clli", "wire_center_name"],
                   metrics=["outage_minutes", "outage_events", "tickets_per_1k", "truck_rolls_per_1k", "fcr_rate",
                            "repeat_rate", "repeat_dispatch_rate"]),
         ]),
    dict(slug="bsp-customer-sentiment", title="Customer Sentiment (NPS)",
         description="NPS trend by technology and verbatim themes.",
         intro=("### Customer sentiment\nRelationship NPS = % promoters (9–10) − % detractors (0–6), from survey "
                "responses in `marts.mart_customer_sentiment`. Verbatim themes are keyword-classified in dbt."),
         filters=[("technology", "Technology", NPS), ("region", "Region", NPS),
                  ("verbatim_theme", "Theme", NPS)],
         charts=[
             kpi("kpi_nps", "NPS", NPS, "nps", ",.1f", "promoters − detractors"),
             kpi("kpi_resp", "Responses", NPS, "responses", ",d", "survey responses"),
             kpi("kpi_det", "Detractor share", NPS, "detractor_share", ".1%", "scores 0–6"),
             kpi("kpi_prom", "Promoter share", NPS, "promoter_share", ".1%", "scores 9–10"),
             line("nps_by_tech", "NPS trend by technology", NPS, "survey_month", ["nps"], ["technology"],
                  row=1, w=7, fmt=",.0f"),
             bar("nps_by_theme", "NPS by verbatim theme", NPS, "verbatim_theme", ["nps"], [], row=1, w=5,
                 fmt=",.0f", temporal=False, stack=False),
             bar("theme_mix", "Responses by theme and NPS band", NPS, "verbatim_theme", ["responses"],
                 ["nps_band"], row=2, w=6, temporal=False),
             table("verbatims", "Latest verbatims", NPS, ["survey_date", "technology", "state", "nps_score",
                                                          "verbatim_theme", "verbatim"],
                   row=2, w=6, order_by="survey_date"),
         ]),
    dict(slug="bsp-billing-collections", title="Billing & Collections",
         description="Late payments, days to pay, payments / late fees / credits, and billing disputes by category "
                     "and outcome.",
         intro=("### Billing & collections\nLate invoices and days to pay (`marts.fct_invoice`), the transaction "
                "ledger — payments, late fees and credits (`marts.fct_transaction`) — and billing disputes by "
                "category and outcome with the credits granted (`marts.fct_dispute`, "
                "`marts.mart_billing_disputes`)."),
         filters=[("state", "State", INV), ("technology", "Technology", INV)],
         charts=[
             kpi("kpi_late", "Late invoice rate", INV, "late_rate", ".1%", "late, open or written off"),
             kpi("kpi_dtp", "Avg days to pay", INV, "avg_days_to_pay", ",.1f", "invoice date → payment"),
             kpi("kpi_wo", "Written off", INV, "written_off", "$,.3s", "amount due on written-off invoices"),
             kpi("kpi_credits", "Dispute credits granted", DSP, "credits", "$,.3s", "credits on approved / partial disputes"),
             bar("late_by_month", "Late invoices by segment", INV, "bill_month", ["late_invoices"], ["segment"],
                 row=1, w=6),
             line("dtp_trend", "Average days to pay by technology", INV, "bill_month", ["avg_days_to_pay"],
                  ["technology"], row=1, w=6, fmt=",.1f"),
             bar("txn_mix", "Transactions by type (payments, late fees, credits)", TXN, "txn_month",
                 ["transaction_amount"], ["txn_type"], row=2, w=6, fmt="$,.3s"),
             pie("pay_mix", "Payment method mix", TXN, ["payment_method"], "payments", row=2, w=6, filters=PAYMENTS),
             bar("credits_by_type", "Disputes by category", DSP, "opened_month", ["disputes"], ["category"],
                 row=3, w=6),
             bar("dispute_outcomes", "Dispute outcomes by category", DSP, "category", ["disputes"],
                 ["dispute_status"], row=3, w=6, temporal=False),
             bar("dispute_resolve", "Average days to resolve by category", FDS, "category", ["avg_days_to_resolve"],
                 [], row=4, w=6, fmt=",.1f", temporal=False, stack=False),
             bar("late_by_method", "Paid-late share by payment method", TXN, "payment_method", ["paid_late_share"],
                 [], row=4, w=6, fmt=".1%", temporal=False, stack=False, filters=PAYMENTS),
             table("dispute_table", "Disputes by category, outcome, autopay and technology", DSP, [], row=5, w=12,
                   order_by="credits", groupby=["category", "dispute_status", "is_autopay", "technology"],
                   metrics=["disputes", "disputed", "credits", "credit_ratio", "avg_days_to_resolve",
                            "with_billing_call", "churned_after"],
                   formats={"credit_ratio": ".1%", "with_billing_call": ".1%", "churned_after": ".1%"}),
         ]),
]

# ── 6 · Churn & Retention, 7 · Revenue Bridge (marts + one virtual dataset) ─────
DIM, CHF, COH, MOV, BRG, RISK = ("dim_customer", "mart_churn_features", "mart_cohort_retention",
                                 "mart_subscriber_movement", "mart_mrr_bridge", "v_high_risk")
METRICS.update({
    "dim_customer": METRICS["dim_customer"] + [
        ("churned_customers", f"SUM({FLAG.format('NOT is_active')})", ",d"),
        ("churned_share", f"AVG({FLAG.format('NOT is_active')})", ".1%"),
    ],
    "mart_churn_features": METRICS["mart_churn_features"] + [("accounts", "COUNT(DISTINCT ban)", ",d")],
    "mart_cohort_retention": [("retention", "SUM(retained_accounts) * 1.0 / NULLIF(SUM(cohort_size), 0)", ".0%"),
                              ("retained_accounts", "SUM(retained_accounts)", ",d")],
    "mart_subscriber_movement": [("mrr_churned", "SUM(mrr_churned)", "$,.3s"), ("gross_adds", "SUM(gross_adds)", ",d"),
                                 ("disconnects", "SUM(disconnects)", ",d"), ("net_adds", "SUM(net_adds)", ",d"),
                                 ("subscribers", "SUM(subscribers)", ",d"), ("mrr", "SUM(mrr)", "$,.3s")],
    "mart_mrr_bridge": [("mrr_change", "SUM(mrr_change)", "$,.3s"), ("accounts", "SUM(accounts)", ",d")],
    "mart_churn_risk_rules": [("accounts", "COUNT(*)", ",d"), ("avg_risk", "AVG(risk_score)", ".0%")],
})
DESCRIPTIONS.update({
    "mart_churn_risk_rules": "Rules-based churn risk for the latest month (fallback for model scores)",
    "mart_cohort_retention": "Retention by signup-year cohort × months since window start",
    "mart_subscriber_movement": "Gross adds, disconnects, net adds, MRR churned by month × technology",
    "mart_mrr_bridge": "Month-over-month MRR movement by type × technology × state",
})


def heat(key, name, ds, x, y, metric, row, w, fmt, h=55, filters=None) -> dict:
    return dict(key=key, name=name, ds=ds, viz="heatmap_v2", row=row, w=w, h=h,
                params={"x_axis": x, "groupby": y, "metric": metric, "row_limit": 10000,
                        "adhoc_filters": filters or [], "normalize_across": "heatmap", "legend_type": "continuous",
                        "linear_color_scheme": "superset_seq_1", "show_legend": True, "show_values": True,
                        "show_percentage": False, "sort_x_axis": "alpha_asc", "sort_y_axis": "alpha_asc",
                        "value_bounds": [None, None], "xscale_interval": -1, "yscale_interval": -1,
                        "y_axis_format": fmt, "left_margin": "auto", "bottom_margin": "auto"})


def md(key, text, row, w=12, h=10) -> dict:
    return dict(key=key, md=text, row=row, w=w, h=h)


DASHBOARDS += [
    dict(slug="bsp-churn-retention", title="Churn & Retention",
         description="Churn drivers, cohort retention, MRR lost to churn, net adds and the high-risk accounts list.",
         intro=("### Churn & retention\nChurn rate vs. the price-increase cohort, churn by cancel reason, tenure, "
                "contract and competitor-fiber overlap; signup-cohort retention; MRR lost to churn and net adds. "
                "**High-risk accounts** come from the churn model (`mlops.scored_customers`) when it has scored "
                "the base, otherwise from the transparent rules in `marts.mart_churn_risk_rules`."),
         filters=[("technology", "Technology", DIM), ("state", "State", DIM)],
         charts=[
             trend("kpi_churn", "Monthly churn rate", REV, "revenue_month", "churn_rate", ".2%"),
             trend("kpi_mrr_churned", "MRR lost to churn", MOV, "report_month", "mrr_churned", "$,.3s"),
             trend("kpi_net_adds", "Net subscriber adds", MOV, "report_month", "net_adds", ",d"),
             kpi("kpi_high_risk", "High-risk accounts", RISK, "accounts", ",d", "risk decile 9–10 (model or rules)",
                 filters=[_sqlf("risk_decile >= 9")]),
             line("churn_vs_price", "90-day churn rate: price-increase cohort vs rest", CHF, "snapshot_month",
                  ["churn_label_rate"], ["recent_price_increase"], row=1, w=7, fmt=".1%",
                  filters=[_sqlf("label_complete")]),
             bar("churn_reason", "Churned customers by cancel reason", DIM, "cancel_reason", ["churned_customers"],
                 [], row=1, w=5, temporal=False, stack=False, filters=[_sqlf("cancel_reason IS NOT NULL")]),
             bar("churn_tenure", "Churned share by tenure band", DIM, "tenure_band", ["churned_share"], [],
                 row=2, w=4, fmt=".0%", temporal=False, stack=False),
             bar("churn_contract", "Churned share by contract term", DIM, "contract_term", ["churned_share"], [],
                 row=2, w=4, fmt=".0%", temporal=False, stack=False),
             heat("churn_tech_comp", "Churned share: technology × competitor fiber", DIM, "technology",
                  "is_competitor_fiber_available", "churned_share", row=2, w=4, fmt=".0%", h=50),
             heat("cohort_heat", "Signup-cohort retention by months since window start", COH, "months_since_start",
                  "signup_cohort", "retention", row=3, w=12, fmt=".0%", h=60),
             bar("mrr_churned", "MRR lost to churn by technology", MOV, "report_month", ["mrr_churned"],
                 ["technology"], row=4, w=6, fmt="$,.0f"),
             line("net_adds", "Gross adds, disconnects and net adds", MOV, "report_month",
                  ["gross_adds", "disconnects", "net_adds"], [], row=4, w=6),
             table("high_risk", "High-risk accounts (latest scoring)", RISK,
                   ["ban", "risk_score", "risk_decile", "risk_reasons", "technology", "state", "plan_id",
                    "monthly_charge", "model_version", "risk_source"], row=5, w=12, order_by="risk_score",
                   formats={"risk_score": ".0%", "monthly_charge": "$,.2f"}, h=65),
         ]),
    dict(slug="bsp-revenue-bridge", title="Revenue Bridge",
         description="Month-over-month MRR movement: new, upgrade, downgrade, price change, churn; ARPU by plan mix.",
         intro=("### Revenue bridge\nRecurring service revenue (MRR) movement month over month from "
                "`marts.mart_mrr_bridge`: **new**, **upgrade to fiber**, **plan upgrade**, **downgrade**, "
                "**price change** (rate-card increases, promo roll-offs) and **churn**. Movements reconcile "
                "exactly to the change in total MRR (dbt test). ARPU by plan mix from `marts.mart_arpu_by_plan`."),
         filters=[("technology", "Technology", BRG), ("state", "State", BRG)],
         charts=[
             trend("kpi_mrr", "MRR (service revenue)", REV, "revenue_month", "service_revenue", "$,.3s"),
             trend("kpi_net_change", "Net MRR change", BRG, "movement_month", "mrr_change", "$,.3s"),
             kpi("kpi_price", "Price-change MRR (all months)", BRG, "mrr_change", "$,.3s", "rate card + promo roll-off",
                 filters=[_sqlf("movement_type = 'price_change'")]),
             kpi("kpi_churn_mrr", "Churned MRR (all months)", BRG, "mrr_change", "$,.3s", "MRR of disconnects",
                 filters=[_sqlf("movement_type = 'churn'")]),
             bar("bridge_month", "MRR movement by type per month", BRG, "movement_month", ["mrr_change"],
                 ["movement_type"], row=1, w=8, fmt="$,.0f", h=60),
             bar("bridge_total", "MRR bridge by movement type (all months)", BRG, "movement_type", ["mrr_change"],
                 [], row=1, w=4, fmt="$,.3s", temporal=False, stack=False, h=60),
             bar("bridge_tech", "MRR movement by technology", BRG, "movement_month", ["mrr_change"], ["technology"],
                 row=2, w=6, fmt="$,.0f"),
             table("bridge_table", "Bridge detail: accounts and MRR by type and technology", BRG, [], row=2, w=6,
                   order_by="mrr_change", groupby=["movement_type", "technology"], metrics=["accounts", "mrr_change"]),
             line("arpu_plan", "ARPU by plan", ARPU, "bill_month", ["arpu"], ["plan_name"], row=3, w=6, fmt="$,.0f"),
             line("plan_mix", "Subscriber mix by plan", ARPU, "bill_month", ["subscribers"], ["plan_name"],
                  row=3, w=6, area=True),
         ]),
]


# ── 8 · Field Ops & Plant Quality, 9 · Contact Center ───────────────────────────
DSPCH, PQ, CC, CALL = "fct_dispatch", "fct_plant_quality_monthly", "mart_contact_center", "fct_customer_call"


def hist(key, name, ds, column, groupby, row, w, bins=30, h=50, filters=None) -> dict:
    return dict(key=key, name=name, ds=ds, viz="histogram_v2", row=row, w=w, h=h,
                params={"column": column, "groupby": groupby, "bins": bins, "row_limit": 100000,
                        "adhoc_filters": filters or [], "normalize": False, "cumulative": False,
                        "show_legend": bool(groupby), "show_value": False, "color_scheme": "supersetColors",
                        "x_axis_title": column, "y_axis_title": "count"})


DASHBOARDS += [
    dict(slug="bsp-field-ops", title="Field Ops & Plant Quality",
         description="Dispatches and repeat dispatches by technology and cause, ONT optics and DSL line quality by "
                     "wire center (CLLI).",
         intro=("### Field ops & plant quality\nField dispatches (install / repair) and **repeat dispatches within "
                "30 days** by technology and cause (`marts.fct_dispatch`); fiber ONT receive power and copper DSL "
                "SNR margin per BAN × month (`marts.fct_plant_quality_monthly`); worst wire centers (CLLI) by repeat "
                "dispatch rate and outage minutes (`marts.mart_network_reliability`). Weak optics: rx power below the "
                "ONT threshold; marginal line: low SNR margin."),
         filters=[("region", "Region", DSPCH), ("state", "State", DSPCH), ("technology", "Technology", DSPCH)],
         charts=[
             trend("kpi_dispatches", "Dispatches", DSPCH, "dispatch_month", "dispatches", ",d"),
             trend("kpi_repeat", "Repeat dispatch rate (30 days)", DSPCH, "dispatch_month", "repeat_dispatch_rate",
                   ".1%"),
             trend("kpi_ntf", "No-trouble-found rate", DSPCH, "dispatch_month", "ntf_rate", ".1%"),
             trend("kpi_onsite", "Avg onsite hours", DSPCH, "dispatch_month", "avg_onsite_hours", ",.2f"),
             bar("dispatch_by_tech", "Dispatches by technology", DSPCH, "dispatch_month", ["dispatches"],
                 ["technology"], row=1, w=6),
             line("repeat_trend", "Repeat dispatch rate by technology", DSPCH, "dispatch_month",
                  ["repeat_dispatch_rate"], ["technology"], row=1, w=6, fmt=".1%"),
             bar("dispatch_cause", "Dispatches by cause", DSPCH, "cause", ["dispatches"], ["technology"], row=2, w=6,
                 temporal=False),
             hist("rx_hist", "ONT rx power distribution (fiber, dBm)", PQ, "rx_power_dbm", [], row=2, w=6,
                  filters=[_sqlf("technology = 'fiber' AND rx_power_dbm IS NOT NULL")]),
             bar("snr_by_clli", "Average DSL SNR margin by CLLI (copper, dB)", PQ, "clli", ["avg_snr_margin_db"], [],
                 row=3, w=6, fmt=",.1f", temporal=False, stack=False,
                 filters=[_sqlf("technology = 'copper' AND snr_margin_db IS NOT NULL")]),
             line("plant_share", "Weak-optics share (fiber) and marginal-line share (copper)", NET, "report_month",
                  ["weak_optics_share", "marginal_line_share"], [], row=3, w=6, fmt=".1%"),
             table("worst_wc_field", "Worst wire centers (CLLI) by repeat dispatch rate", NET, [], row=4, w=12,
                   order_by="repeat_dispatch_rate", groupby=["state", "clli", "wire_center_name"],
                   metrics=["repeat_dispatch_rate", "dispatches", "repeat_dispatches", "outage_minutes",
                            "avg_rx_power_dbm", "weak_optics_share", "avg_snr_margin_db", "marginal_line_share"],
                   formats={"repeat_dispatch_rate": ".1%", "weak_optics_share": ".1%",
                            "marginal_line_share": ".1%", "avg_rx_power_dbm": ",.1f", "avg_snr_margin_db": ",.1f"},
                   h=65),
         ]),
    dict(slug="bsp-contact-center", title="Contact Center",
         description="Calls by queue and reason, repeat-call rate, average handle time and sentiment.",
         intro=("### Contact center\nCall volume by queue and reason, **repeat calls within 7 days**, average "
                "handle time, resolution and call sentiment, by month × queue × reason × technology "
                "(`marts.mart_contact_center`); sentiment bands per call from `marts.fct_customer_call`."),
         filters=[("queue", "Queue", CC), ("reason", "Reason", CC), ("technology", "Technology", CC)],
         charts=[
             trend("kpi_calls", "Calls", CC, "call_month", "calls", ",d"),
             trend("kpi_repeat_calls", "Repeat-call rate (7 days)", CC, "call_month", "repeat_call_rate", ".1%"),
             trend("kpi_aht", "Avg handle time (min)", CC, "call_month", "avg_handle_min", ",.1f"),
             trend("kpi_sentiment", "Avg call sentiment", CC, "call_month", "avg_sentiment", ",.2f"),
             bar("calls_by_queue", "Calls by queue", CC, "call_month", ["calls"], ["queue"], row=1, w=6),
             bar("calls_by_reason", "Calls by reason and queue", CC, "reason", ["calls"], ["queue"], row=1, w=6,
                 temporal=False),
             line("repeat_call_trend", "Repeat-call rate by queue", CC, "call_month", ["repeat_call_rate"], ["queue"],
                  row=2, w=6, fmt=".1%"),
             bar("aht_by_queue", "Average handle time by queue (min)", CC, "queue", ["avg_handle_min"],
                 ["technology"], row=2, w=6, fmt=",.1f", temporal=False, stack=False),
             line("sentiment_trend", "Average sentiment by queue", CC, "call_month", ["avg_sentiment"], ["queue"],
                  row=3, w=6, fmt=",.2f"),
             bar("sentiment_band", "Calls by queue and sentiment band", CALL, "queue", ["calls"], ["sentiment_band"],
                 row=3, w=6, temporal=False),
             table("cc_table", "Queue × reason: volume, repeats, handle time, resolution, sentiment", CC, [], row=4,
                   w=12, order_by="calls", groupby=["queue", "reason"],
                   metrics=["calls", "repeat_call_rate", "avg_handle_min", "resolution_rate", "avg_sentiment",
                            "negative_share"],
                   formats={"repeat_call_rate": ".1%", "resolution_rate": ".1%", "negative_share": ".1%",
                            "avg_handle_min": ",.1f", "avg_sentiment": ",.2f"}, h=65),
         ]),
]


# ── virtual datasets whose SQL depends on what exists at import time ────────────
class Src:
    """Columns of one live table, with best-match lookup of candidate names."""

    def __init__(self, present: dict, schema: str, table: str):
        self.schema, self.table = schema, table
        self.cols = {c.lower(): t for c, t in present.get(schema, {}).get(table, [])}

    @property
    def exists(self) -> bool:
        return bool(self.cols)

    @property
    def fq(self) -> str:
        return f"{self.schema}.{self.table}"

    def col(self, *cands: str) -> str | None:
        for c in cands:
            if c.lower() in self.cols:
                return c.lower()
        return None

    def type(self, col: str) -> str:
        return self.cols.get(col, "")


def _expr(src: Src, alias: str, cands: tuple, default: str = "NULL", prefix: str = "") -> tuple[str, bool]:
    c = src.col(*cands)
    return (f"{prefix}{c} AS {alias}" if c else f"{default} AS {alias}"), bool(c)


def high_risk_sql(present: dict) -> str:
    rules = """SELECT r.ban, r.risk_score, r.risk_decile, r.risk_band, r.risk_reasons, r.technology, r.state,
       r.plan_id, r.monthly_charge, r.model_version, 'rules' AS risk_source, r.snapshot_month
FROM marts.mart_churn_risk_rules r"""
    sc = Src(present, "mlops", "scored_customers")
    if not (sc.exists and sc.col("ban") and sc.col("score", "churn_score", "probability", "risk_score")):
        return rules
    score = sc.col("score", "churn_score", "probability", "risk_score")
    decile = sc.col("decile", "risk_decile")
    month = sc.col("snapshot_month", "score_month", "as_of_month")
    when = sc.col("scored_at", "created_at", "ts")
    reasons = [c for c in ("reason_1", "reason_2", "reason_3") if sc.col(c)]
    reason_expr = (f"CONCAT_WS('; ', {', '.join('s.' + r for r in reasons)})" if reasons else
                   f"s.{sc.col('top_reasons', 'reasons', 'reason_codes')}::text" if sc.col('top_reasons', 'reasons', 'reason_codes')
                   else "NULL")
    band = f"s.{sc.col('risk_band', 'band')}" if sc.col("risk_band", "band") else "NULL"
    version = f"s.{sc.col('model_version', 'version')}" if sc.col("model_version", "version") else "'model'"
    latest = (f"WHERE s.{month} = (SELECT MAX({month}) FROM mlops.scored_customers)" if month else
              f"WHERE s.{when} >= (SELECT MAX({when}) FROM mlops.scored_customers) - INTERVAL '1' DAY" if when else "")
    decile_expr = f"s.{decile}" if decile else f"NTILE(10) OVER (ORDER BY s.{score})"
    return f"""WITH m AS (
    SELECT s.ban, s.{score} AS risk_score, {decile_expr} AS risk_decile, {band} AS risk_band,
           {reason_expr} AS risk_reasons, c.technology, c.state, c.plan_id, c.list_price AS monthly_charge,
           {version} AS model_version, 'model' AS risk_source, {('s.' + month) if month else 'NULL'} AS snapshot_month
    FROM mlops.scored_customers s
    LEFT JOIN marts.dim_customer c ON c.ban = s.ban
    {latest}
), r AS (
{rules}
)
SELECT * FROM m
UNION ALL
SELECT * FROM r WHERE NOT EXISTS (SELECT 1 FROM m)"""


HIGH_RISK_COLS = [("ban", "VARCHAR"), ("risk_score", "FLOAT"), ("risk_decile", "INTEGER"),
                  ("risk_band", "VARCHAR"), ("risk_reasons", "VARCHAR"), ("technology", "VARCHAR"),
                  ("state", "VARCHAR"), ("plan_id", "VARCHAR"), ("monthly_charge", "FLOAT"),
                  ("model_version", "VARCHAR"), ("risk_source", "VARCHAR"), ("snapshot_month", "DATE")]


def ai_ml_section(present: dict) -> tuple[dict, dict]:
    """The AI & ML Operations dashboard: one section per source that exists now.
    → (dashboard dict, {virtual dataset key: spec}). Charts whose columns are missing are skipped + logged."""
    vds: dict[str, dict] = {}
    charts: list[dict] = []
    skipped: list[str] = []
    row = [0]

    def add_vds(key, name, sql, cols, metrics, desc):
        vds[key] = {"name": name, "sql": sql, "cols": cols, "metrics": metrics, "desc": desc}

    def need(chart: dict, *cols: str) -> None:
        have = {c for c, _ in vds[chart["ds"]]["cols"]}
        missing = [c for c in cols if c not in have]
        if missing:
            skipped.append(f"{chart['name']} (missing {', '.join(missing)})")
        else:
            charts.append(chart)

    def section(title: str, text: str) -> int:
        row[0] += 1
        charts.append(md(f"md_{row[0]}", f"#### {title}\n{text}", row=row[0], h=8))
        row[0] += 1
        return row[0]

    # ── Model monitoring (mlops.*) ───────────────────────────────────────────────
    inf, dr, dep, rt = (Src(present, "mlops", t) for t in ("inference_log", "drift_reports", "deployments",
                                                            "retrain_runs"))
    if any(s.exists for s in (inf, dr, dep, rt)):
        r = section("Model monitoring", "Churn-model serving through the model gateway (blue/green, canary, shadow), "
                    "feature drift (PSI) and retraining — `mlops.*`.")
        if inf.exists and inf.col("ts", "created_at", "logged_at", "requested_at"):
            ts = inf.col("ts", "created_at", "logged_at", "requested_at")
            parts, cols = [f"CAST(DATE_TRUNC('day', {ts}) AS DATE) AS request_day"], [("request_day", "DATE")]
            for alias, cands, typ in (("model_version", ("model_version", "version"), "VARCHAR"),
                                      ("colour", ("colour", "color", "live_color", "variant"), "VARCHAR"),
                                      ("deployment", ("deployment", "model_name"), "VARCHAR"),
                                      ("mode", ("mode",), "VARCHAR"),
                                      ("score", ("score", "prediction", "probability"), "FLOAT"),
                                      ("latency_ms", ("latency_ms", "latency"), "FLOAT")):
                e, ok = _expr(inf, alias, cands)
                if ok:
                    parts.append(e)
                    cols.append((alias, typ))
            if inf.col("score", "prediction", "probability"):
                parts.append(f"FLOOR({inf.col('score', 'prediction', 'probability')} * 10) / 10.0 AS score_bucket")
                cols.append(("score_bucket", "FLOAT"))
            shadow = "WHERE NOT shadow" if inf.col("shadow") else ""
            add_vds("v_ml_inference", "ML inference log", f"SELECT {', '.join(parts)}\nFROM {inf.fq} {shadow}", cols,
                    [("requests", "COUNT(*)", ",d"), ("avg_score", "AVG(score)", ".3f"),
                     ("avg_latency_ms", "AVG(latency_ms)", ",.1f")] if any(c == "latency_ms" for c, _ in cols) else
                    [("requests", "COUNT(*)", ",d")], "mlops.inference_log (non-shadow requests)")
            need(trend("ml_kpi_req", "Requests per day", "v_ml_inference", "request_day", "requests", ",d", row=r, w=4),
                 "request_day")
            need(bar("ml_req_version", "Requests per day by model version and colour", "v_ml_inference",
                     "request_day", ["requests"], ["model_version", "colour"], row=r, w=8), "model_version", "colour")
            r += 1
            need(bar("ml_score_dist", "Score distribution", "v_ml_inference", "score_bucket", ["requests"],
                     ["model_version"], row=r, w=6, temporal=False), "score_bucket", "model_version")
            need(line("ml_latency", "Average latency (ms) by deployment mode", "v_ml_inference", "request_day",
                      ["avg_latency_ms"], ["mode"], row=r, w=6, fmt=",.1f"), "latency_ms", "mode")
            r += 1
        if dr.exists and dr.col("feature_drift") and dr.col("created_at", "ts", "window_end"):
            t = dr.col("created_at", "ts", "window_end")
            ver = f"d.{dr.col('model_version', 'version')}" if dr.col("model_version", "version") else "NULL"
            sql = f"""SELECT CAST(d.{t} AS DATE) AS report_day, {ver} AS model_version,
       COALESCE(e.value ->> 'feature', o.key) AS feature,
       CAST(COALESCE(e.value ->> 'psi', CASE WHEN jsonb_typeof(o.value) = 'object' THEN o.value ->> 'psi'
                                             WHEN jsonb_typeof(o.value) = 'number' THEN o.value #>> '{{}}' END) AS DOUBLE PRECISION) AS psi
FROM {dr.fq} d
LEFT JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(d.feature_drift::jsonb) = 'array' THEN d.feature_drift::jsonb ELSE '[]'::jsonb END) e ON TRUE
LEFT JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(d.feature_drift::jsonb) = 'object' THEN d.feature_drift::jsonb ELSE '{{}}'::jsonb END) o ON TRUE
WHERE e.value IS NOT NULL OR o.key IS NOT NULL"""
            add_vds("v_ml_drift", "ML feature drift (PSI)", sql,
                    [("report_day", "DATE"), ("model_version", "VARCHAR"), ("feature", "VARCHAR"), ("psi", "FLOAT")],
                    [("max_psi", "MAX(psi)", ".3f"), ("reports", "COUNT(DISTINCT report_day)", ",d")],
                    "mlops.drift_reports.feature_drift exploded per feature")
            need(line("ml_psi", "PSI by feature over time", "v_ml_drift", "report_day", ["max_psi"], ["feature"],
                      row=r, w=8, fmt=".2f"), "feature", "psi")
            need(table("ml_psi_top", "Highest PSI by feature", "v_ml_drift", [], row=r, w=4, order_by="max_psi",
                       groupby=["feature"], metrics=["max_psi"], formats={"max_psi": ".3f"}), "feature")
            r += 1
        if rt.exists and rt.col("created_at", "started_at"):
            t = rt.col("created_at", "started_at")
            parts, cols = [f"CAST({t} AS DATE) AS run_day"], [("run_day", "DATE")]
            for alias, cands, typ in (("status", ("status",), "VARCHAR"), ("trigger", ("trigger", "reason"), "VARCHAR"),
                                      ("model_version", ("model_version",), "VARCHAR"),
                                      ("decision", ("decision",), "VARCHAR"), ("promoted", ("promoted",), "BOOLEAN"),
                                      ("challenger_auc", ("challenger_auc",), "FLOAT"),
                                      ("champion_auc", ("champion_auc",), "FLOAT")):
                e, ok = _expr(rt, alias, cands, prefix='')
                if ok:
                    parts.append(e.replace(f"{rt.col(*cands)} AS", f'"{rt.col(*cands)}" AS'))
                    cols.append((alias, typ))
            add_vds("v_ml_retrain", "ML retrain runs", f"SELECT {', '.join(parts)}\nFROM {rt.fq}", cols,
                    [("runs", "COUNT(*)", ",d")], "mlops.retrain_runs")
            need(bar("ml_retrain", "Retrain events by status", "v_ml_retrain", "run_day", ["runs"], ["status"],
                     row=r, w=6), "status")
            need(table("ml_retrain_tbl", "Recent retrain runs", "v_ml_retrain",
                       [c for c, _ in cols if c in ("run_day", "trigger", "status", "decision", "model_version",
                                                    "challenger_auc", "champion_auc", "promoted")],
                       row=r, w=6, order_by="run_day"), "run_day")
            r += 1
        if dep.exists and dep.col("deployment"):
            keep = [c for c in ("deployment", "model_name", "mode", "blue_version", "green_version", "live_color",
                                "canary_weight", "shadow_enabled", "updated_at") if dep.col(c)]
            add_vds("v_ml_deployments", "ML deployments", f"SELECT {', '.join(keep)}\nFROM {dep.fq}",
                    [(c, "TIMESTAMP" if c == "updated_at" else "FLOAT" if c == "canary_weight" else
                      "BOOLEAN" if c == "shadow_enabled" else "VARCHAR") for c in keep],
                    [("deployments", "COUNT(*)", ",d")], "mlops.deployments")
            need(table("ml_deployments", "Deployments (blue / green, canary, shadow)", "v_ml_deployments", keep,
                       row=r, w=12, order_by="deployment", h=35), "deployment")
        row[0] = r

    # ── CCAI contact reasons (ccai.*) ───────────────────────────────────────────
    tags, calls, tax = (Src(present, "ccai", t) for t in ("call_tags", "calls", "taxonomy"))
    leaf_c = tags.col("primary_leaf", "leaf_id", "leaf")
    if tags.exists and leaf_c:
        r = section("CCAI contact reasons", "Call transcripts from SharePoint tagged to the 104-leaf contact-reason "
                    "taxonomy — `ccai.call_tags`, `ccai.calls`, `ccai.taxonomy`.")
        leaf = f"COALESCE(t.{tags.col('override_leaf')}, t.{leaf_c})" if tags.col("override_leaf") else f"t.{leaf_c}"
        join_tax = (f"LEFT JOIN (SELECT leaf_id, MAX(l1_name) AS l1, MAX(l2_name) AS l2, MAX(name) AS leaf_name "
                    f"FROM {tax.fq} GROUP BY leaf_id) x ON x.leaf_id = {leaf}"
                    if tax.exists and tax.col("leaf_id") and tax.col("l1_name") else "")
        l1 = "COALESCE(x.l1, 'unmapped')" if join_tax else f"SPLIT_PART({leaf}, '.', 1)"
        l2 = "COALESCE(x.l2, 'unmapped')" if join_tax else leaf
        name = f"COALESCE(x.leaf_name, {leaf})" if join_tax else leaf
        join_calls = (f"LEFT JOIN {calls.fq} c ON c.call_id = t.call_id"
                      if calls.exists and calls.col("call_id") and tags.col("call_id") else "")
        day = (f"CAST(COALESCE(c.{calls.col('started_at', 'ingested_at')}, t.{tags.col('created_at')}) AS DATE)"
               if join_calls and calls.col("started_at", "ingested_at") and tags.col("created_at")
               else f"CAST(t.{tags.col('created_at')} AS DATE)" if tags.col("created_at") else "NULL")
        parts = [f"{leaf} AS leaf", f"{name} AS leaf_name", f"{l1} AS l1", f"{l2} AS l2", f"{day} AS call_day"]
        cols = [("leaf", "VARCHAR"), ("leaf_name", "VARCHAR"), ("l1", "VARCHAR"), ("l2", "VARCHAR"), ("call_day", "DATE")]
        if tags.col("confidence"):
            parts.append("t.confidence AS confidence")
            cols.append(("confidence", "FLOAT"))
            band = f"t.{tags.col('confidence_band')} = 'low' OR " if tags.col("confidence_band") else ""
            parts.append(f"CASE WHEN {band}t.confidence < 0.6 THEN 1 ELSE 0 END AS low_confidence")
            cols.append(("low_confidence", "INTEGER"))
        if tags.col("churn_risk"):
            parts.append("CASE WHEN t.churn_risk THEN 1 ELSE 0 END AS churn_risk_signal")
            cols.append(("churn_risk_signal", "INTEGER"))
        if join_calls and calls.col("ban"):   # ccai.calls is owned by the CCAI service
            parts.append("c.ban AS ban")
            cols.append(("ban", "VARCHAR"))
        where = "WHERE t.is_current" if tags.col("is_current") else ""
        add_vds("v_ccai_tags", "CCAI tagged calls",
                f"SELECT {', '.join(parts)}\nFROM {tags.fq} t\n{join_calls}\n{join_tax}\n{where}", cols,
                [("calls", "COUNT(*)", ",d"), ("avg_confidence", "AVG(confidence)", ".2f"),
                 ("churn_risk_calls", "SUM(churn_risk_signal)", ",d"),
                 ("low_confidence_share", "AVG(low_confidence * 1.0)", ".1%")], "ccai.call_tags (current tags)")
        need(kpi("cc_calls", "Tagged calls", "v_ccai_tags", "calls", ",d", "current tags", row=r, w=3), "leaf")
        need(kpi("cc_conf", "Average confidence", "v_ccai_tags", "avg_confidence", ".2f", "tagger confidence",
                 row=r, w=3), "confidence")
        need(kpi("cc_churn", "Churn-risk-signal calls", "v_ccai_tags", "churn_risk_calls", ",d",
                 "calls flagged churn risk", row=r, w=3), "churn_risk_signal")
        need(kpi("cc_low", "Low-confidence share", "v_ccai_tags", "low_confidence_share", ".1%",
                 "confidence < 0.6 or band = low", row=r, w=3), "low_confidence")
        r += 1
        need(bar("cc_l1_l2", "Call volume by L1 and L2", "v_ccai_tags", "l1", ["calls"], ["l2"], row=r, w=7,
                 temporal=False), "l1", "l2")
        need(table("cc_leaves", "Top leaves: volume, confidence, churn-risk calls", "v_ccai_tags", [], row=r, w=5,
                   order_by="calls", groupby=["l1", "leaf_name"],
                   metrics=["calls", "avg_confidence", "churn_risk_calls"]), "confidence", "churn_risk_signal")
        r += 1
        need(bar("cc_daily", "Tagged calls per day by L1", "v_ccai_tags", "call_day", ["calls"], ["l1"], row=r,
                 w=12, h=45), "call_day")
        row[0] = r

    # ── Dispute desk (disputes.*) ───────────────────────────────────────────────
    cases, subs = Src(present, "disputes", "cases"), Src(present, "disputes", "brim_submissions")
    if cases.exists or subs.exists:
        r = section("Dispute desk", "Billing-dispute e-mails turned into structured cases and submitted to SAP BRIM "
                    "— `disputes.cases`, `disputes.brim_submissions`.")
        if cases.exists and cases.col("created_at"):
            cat = (f"COALESCE(fields ->> 'category', 'uncategorised')" if cases.col("fields") and
                   "json" in cases.type("fields") else
                   f"COALESCE({cases.col('category', 'dispute_category', 'reason')}, 'uncategorised')"
                   if cases.col("category", "dispute_category", "reason") else "'uncategorised'")
            parts = [f"{cat} AS category", "CAST(created_at AS DATE) AS case_day"]
            cols = [("category", "VARCHAR"), ("case_day", "DATE")]
            for alias, cands, typ in (("status", ("status",), "VARCHAR"),
                                      ("extraction_method", ("extraction_method",), "VARCHAR"),
                                      ("overall_confidence", ("overall_confidence", "confidence"), "FLOAT")):
                c = cases.col(*cands)
                if c and ("json" not in cases.type(c)):
                    parts.append(f"{c} AS {alias}")
                    cols.append((alias, typ))
            if cases.col("auto_approved"):
                parts.append("CASE WHEN auto_approved THEN 1 ELSE 0 END AS auto_approved")
                cols.append(("auto_approved", "INTEGER"))
            done = [c for c in ("submitted_at", "approved_at", "closed_at") if cases.col(c)]
            if done:
                parts.append(f"EXTRACT(EPOCH FROM (COALESCE({', '.join(done)}) - created_at)) / 60.0 AS handle_minutes")
                cols.append(("handle_minutes", "FLOAT"))
            add_vds("v_disputes_cases", "Dispute cases", f"SELECT {', '.join(parts)}\nFROM {cases.fq}", cols,
                    [("cases", "COUNT(*)", ",d"), ("auto_approve_rate", "AVG(auto_approved * 1.0)", ".1%"),
                     ("avg_handle_minutes", "AVG(handle_minutes)", ",.1f")], "disputes.cases")
            need(kpi("dp_cases", "Cases", "v_disputes_cases", "cases", ",d", "dispute cases", row=r, w=4), "category")
            need(kpi("dp_auto", "Auto-approve rate", "v_disputes_cases", "auto_approve_rate", ".1%",
                     "approved without a human", row=r, w=4), "auto_approved")
            need(kpi("dp_aht", "Average handle time (min)", "v_disputes_cases", "avg_handle_minutes", ",.1f",
                     "created → approved / submitted", row=r, w=4), "handle_minutes")
            r += 1
            need(bar("dp_cat", "Cases by category and status", "v_disputes_cases", "category", ["cases"], ["status"],
                     row=r, w=6, temporal=False), "category", "status")
            need(bar("dp_daily", "Cases per day by category", "v_disputes_cases", "case_day", ["cases"], ["category"],
                     row=r, w=6), "case_day")
            r += 1
        if subs.exists:
            t = subs.col("started_at", "finished_at", "created_at")
            parts, cols = ([f"CAST({t} AS DATE) AS submit_day"], [("submit_day", "DATE")]) if t else ([], [])
            for alias, cands, typ in (("mode", ("mode", "adapter"), "VARCHAR"), ("status", ("status",), "VARCHAR"),
                                      ("duration_ms", ("duration_ms",), "FLOAT"), ("attempts", ("attempts",), "INTEGER")):
                c = subs.col(*cands)
                if c:
                    parts.append(f"{c} AS {alias}")
                    cols.append((alias, typ))
            if parts:
                add_vds("v_disputes_brim", "SAP BRIM submissions", f"SELECT {', '.join(parts)}\nFROM {subs.fq}", cols,
                        [("submissions", "COUNT(*)", ",d"), ("avg_duration_s", "AVG(duration_ms) / 1000.0", ",.1f")],
                        "disputes.brim_submissions")
                need(bar("dp_brim", "SAP BRIM submissions by mode and status", "v_disputes_brim", "mode",
                         ["submissions"], ["status"], row=r, w=6, temporal=False), "mode", "status")
                need(line("dp_brim_dur", "Average submission duration (s) by mode", "v_disputes_brim", "submit_day",
                          ["avg_duration_s"], ["mode"], row=r, w=6, fmt=",.1f"), "submit_day", "duration_ms", "mode")
        row[0] = r

    if skipped:
        log.info("AI & ML Operations: skipped %d chart(s): %s", len(skipped), "; ".join(skipped))
    sections = [c for c in charts if "md" in c]
    log.info("AI & ML Operations: %d section(s), %d chart(s)", len(sections), len(charts) - len(sections))
    intro = ("### AI & ML Operations\nModel monitoring, CCAI contact reasons and the dispute desk. Sections appear "
             "once their services have created their tables — re-run the bootstrap (`make superset-refresh`, or "
             "Studio → Superset → Refresh dashboards) after the ML Scientist / AI Engineer services have run."
             + ("" if sections else "\n\n**No AI / ML tables found yet** (`mlops.*`, `ccai.*`, `disputes.*`)."))
    dash = dict(slug="bsp-ai-ml-operations", title="AI & ML Operations",
                description="Model monitoring, CCAI contact reasons and dispute-desk automation.",
                intro=intro, filters=[], charts=charts)
    return dash, vds

# Saved queries on the "Trino (federated)" connection: one SQL statement that reads several systems at
# once (warehouse Postgres, Oracle billing, SQL Server care, BigQuery) without copying data.
TRINO_SAVED_QUERIES = [
    ("Federated · Late balances and open care orders by region",
     "Warehouse customers × Oracle late invoices × SQL Server open care orders, joined on BAN",
     """-- Three systems in one query: warehouse (Postgres), billing_oracle (Oracle), care_mssql (SQL Server)
WITH late AS (
  SELECT ban, sum(amount_due) AS amount_due
  FROM billing_oracle.billing.invoices
  WHERE status = 'late'
  GROUP BY ban
), care AS (
  SELECT ban, count(*) AS open_orders
  FROM care_mssql.dbo.care_orders
  WHERE status NOT IN ('completed', 'cancelled')
  GROUP BY ban
)
SELECT c.region,
       count(*)                         AS customers,
       count(l.ban)                     AS accounts_late,
       round(sum(l.amount_due), 0)      AS late_amount_due,
       coalesce(sum(o.open_orders), 0)  AS open_care_orders
FROM warehouse.marts.dim_customer c
LEFT JOIN late l ON l.ban = c.ban
LEFT JOIN care o ON o.ban = c.ban
GROUP BY c.region
ORDER BY late_amount_due DESC"""),
    ("Federated · Open care orders on unpaid accounts (Oracle × SQL Server)",
     "Which technology has the most open care orders on accounts with an unpaid Oracle invoice",
     """WITH unpaid AS (
  SELECT DISTINCT ban FROM billing_oracle.billing.invoices WHERE status <> 'paid'
)
SELECT o.technology, count(o.order_id) AS open_orders
FROM care_mssql.dbo.care_orders o
JOIN unpaid u ON u.ban = o.ban
WHERE o.status NOT IN ('completed', 'cancelled')
GROUP BY o.technology
ORDER BY open_orders DESC"""),
    ("Federated · Overdue balance by technology and state (warehouse × Oracle)",
     "Customer attributes from the warehouse with late balances from Oracle billing",
     """SELECT c.technology, c.state,
       count(DISTINCT i.ban)        AS accounts_late,
       round(sum(i.amount_due), 0)  AS late_amount_due
FROM warehouse.marts.dim_customer c
JOIN billing_oracle.billing.invoices i ON i.ban = c.ban AND i.status = 'late'
GROUP BY c.technology, c.state
ORDER BY late_amount_due DESC
LIMIT 25"""),
    ("Federated · How Trino plans a cross-system query",
     "EXPLAIN shows which system each table is read from and what is pushed down to it",
     """EXPLAIN (TYPE DISTRIBUTED)
SELECT c.technology, count(*) AS late_invoices
FROM warehouse.marts.dim_customer c
JOIN billing_oracle.billing.invoices i ON i.ban = c.ban
WHERE i.status = 'late'
GROUP BY c.technology"""),
    ("Federated · What can I query? Systems and schemas",
     "The local systems with their schemas and table counts (BigQuery projects: see 'Connected systems')",
     """SELECT table_cat AS system, table_schem AS schema, count(*) AS tables
FROM system.jdbc.tables
WHERE (table_cat = 'warehouse' AND table_schem IN ('raw', 'staging', 'intermediate', 'marts', 'features', 'landing'))
   OR (table_cat = 'billing_oracle' AND table_schem = 'billing')
   OR (table_cat = 'care_mssql' AND table_schem = 'dbo')
GROUP BY 1, 2
ORDER BY 1, 2"""),
    ("Federated · Connected systems (catalogs)",
     "Lists every Trino catalog, including one per BigQuery project (bq_*) when connected",
     "SHOW CATALOGS"),
]

SAVED_QUERIES = [
    ("Revenue & ARPU by month", "Monthly subscribers, service revenue, ARPU and churn rate",
     """SELECT revenue_month, SUM(subscribers) AS subscribers, SUM(service_revenue) AS service_revenue,
       SUM(service_revenue) / NULLIF(SUM(subscribers), 0) AS arpu,
       SUM(churned_subscribers) * 1.0 / NULLIF(SUM(subscribers), 0) AS churn_rate
FROM marts.fct_revenue_monthly
GROUP BY revenue_month
ORDER BY revenue_month"""),
    ("Top fiber upgrade wire centers", "Copper accounts passed by fiber, by wire center (CLLI)",
     """SELECT state, clli, wire_center_name, upgrade_opportunity_accounts, upgrade_opportunity_mrr, fiber_take_rate
FROM marts.mart_fiber_penetration
ORDER BY upgrade_opportunity_accounts DESC
LIMIT 25"""),
    ("Worst wire centers — last 3 months", "Outage minutes and tickets per 1k subscribers",
     """SELECT clli, wire_center_name, state, SUM(outage_minutes) AS outage_minutes,
       1000.0 * SUM(tickets) / NULLIF(SUM(subscribers), 0) AS tickets_per_1k
FROM marts.mart_network_reliability
WHERE report_month >= (SELECT MAX(report_month) FROM marts.mart_network_reliability) - INTERVAL '2' MONTH
GROUP BY clli, wire_center_name, state
ORDER BY outage_minutes DESC
LIMIT 20"""),
    ("NPS by theme", "NPS and responses by verbatim theme",
     """SELECT verbatim_theme, COUNT(*) AS responses,
       100.0 * (SUM(is_promoter) - SUM(is_detractor)) / COUNT(*) AS nps
FROM marts.mart_customer_sentiment
GROUP BY verbatim_theme
ORDER BY responses DESC"""),
    ("Churn training set (label complete)", "Feature rows the churn model trains on",
     """SELECT *
FROM marts.mart_churn_features
WHERE label_complete
LIMIT 1000"""),
]


# ── bundle building ──────────────────────────────────────────────────────────────
def _yaml(obj) -> str:
    import yaml  # noqa: PLC0415

    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True)


def _meta(kind: str) -> str:
    return _yaml({"version": "1.0.0", "type": kind, "timestamp": TS})


def _col(name: str, typ: str) -> dict:
    t = typ.lower()
    return {"column_name": name, "verbose_name": None,
            "is_dttm": t in ("date", "timestamp without time zone", "timestamp with time zone", "timestamp"),
            "is_active": True, "type": typ.upper(), "advanced_data_type": None, "groupby": True, "filterable": True,
            "expression": None, "description": None, "python_date_format": None, "extra": None}


def _metric(name: str, expr: str, fmt: str) -> dict:
    return {"metric_name": name, "verbose_name": VERBOSE.get(name, name.replace("_", " ").capitalize()),
            "metric_type": None, "expression": expr, "description": None, "d3format": fmt, "currency": None,
            "extra": None, "warning_text": None}


def mart_columns(present: dict) -> dict[str, list]:
    """Live marts columns when dbt has built them, else the packaged spec."""
    spec = json.loads((HERE / "assets" / "marts_columns.json").read_text())
    out = {k.split(".", 1)[1]: [tuple(c) for c in v] for k, v in spec.items()}
    out.update(present.get(SCHEMA, {}))
    return out


def all_dashboards(present: dict) -> tuple[list[dict], dict[str, dict]]:
    """Static dashboards + the AI & ML Operations dashboard, and every virtual dataset they use."""
    vds = {"v_high_risk": {"name": "Churn high-risk accounts", "sql": high_risk_sql(present), "cols": HIGH_RISK_COLS,
                           "metrics": [("accounts", "COUNT(*)", ",d"), ("avg_risk", "AVG(risk_score)", ".0%")],
                           "desc": "mlops.scored_customers (latest scoring) when populated, else "
                                   "marts.mart_churn_risk_rules"}}
    ai_dash, ai_vds = ai_ml_section(present)
    vds.update(ai_vds)
    return DASHBOARDS + [ai_dash], vds


def build_bundles(db: dict, cols: dict[str, list], dashboards_spec: list[dict], vds: dict[str, dict]
                  ) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """→ (database + dataset files, {slug: dashboard bundle files})."""
    db_yaml = _yaml({
        "database_name": db["name"], "sqlalchemy_uri": db["uri"], "password": None,
        "cache_timeout": None, "expose_in_sqllab": True, "allow_run_async": False, "allow_ctas": False,
        "allow_cvas": False, "allow_dml": False, "allow_csv_upload": False, "allow_file_upload": False,
        "impersonate_user": False,
        "extra": {"metadata_params": {}, "engine_params": {}, "metadata_cache_timeout": {},
                  "schemas_allowed_for_file_upload": [], "allows_virtual_table_explore": True},
        "uuid": db["uuid"], "version": "1.0.0"})
    datasets: dict[str, str] = {f"{ROOT}/databases/{db['file']}.yaml": db_yaml}
    ds_uuid: dict[str, str] = {}
    ds_file: dict[str, str] = {}
    ds_cols: dict[str, set] = {}

    def add(key, table, schema, sql, columns, metrics, desc):
        ds_uuid[key] = uid(f"warehouse::{schema}.{table}" if sql is None else f"warehouse::virtual::{key}")
        dttm = next((c for c, t in columns if _col(c, t)["is_dttm"]), None)
        ds_file[key] = f"{ROOT}/datasets/{db['file']}/{schema}_{table if sql is None else key}.yaml"
        ds_cols[key] = {c for c, _ in columns}
        datasets[ds_file[key]] = _yaml({
            "table_name": table, "main_dttm_col": dttm, "description": desc,
            "default_endpoint": None, "offset": 0, "cache_timeout": None, "catalog": PG_DB, "schema": schema,
            "sql": sql, "params": None, "template_params": None, "filter_select_enabled": True,
            "fetch_values_predicate": None, "extra": None, "normalize_columns": False,
            "always_filter_main_dttm": False, "uuid": ds_uuid[key],
            "metrics": [_metric(*m) for m in metrics] or [_metric("count", "COUNT(*)", ",d")],
            "columns": [_col(c, t) for c, t in columns], "version": "1.0.0", "database_uuid": db["uuid"]})

    for table, columns in sorted(cols.items()):
        add(table, table, SCHEMA, None, columns, METRICS.get(table, []), DESCRIPTIONS.get(table, f"dbt model {table}"))
    for key, v in vds.items():
        add(key, v["name"], SCHEMA, v["sql"], v["cols"], v["metrics"], f"Virtual dataset — {v['desc']}")

    dashboards: dict[str, dict[str, str]] = {}
    chart_id = 0
    for d in dashboards_spec:
        real = [c for c in d["charts"] if "md" not in c]
        used = {c["ds"] for c in real} | {f[2] for f in d["filters"]}
        files = {k: t for k, t in datasets.items() if "/databases/" in k or k in {ds_file[u] for u in used}}
        position: dict = {"DASHBOARD_VERSION_KEY": "v2",
                          "ROOT_ID": {"children": ["GRID_ID"], "id": "ROOT_ID", "type": "ROOT"},
                          "GRID_ID": {"children": [], "id": "GRID_ID", "parents": ["ROOT_ID"], "type": "GRID"},
                          "HEADER_ID": {"id": "HEADER_ID", "meta": {"text": d["title"]}, "type": "HEADER"}}
        rows: dict[int, str] = {}

        def row_id(n: int) -> str:
            if n not in rows:
                rows[n] = f"ROW-{d['slug']}-{n}"
                position["GRID_ID"]["children"].append(rows[n])
                position[rows[n]] = {"children": [], "id": rows[n], "meta": {"background": "BACKGROUND_TRANSPARENT"},
                                     "parents": ["ROOT_ID", "GRID_ID"], "type": "ROW"}
            return rows[n]

        def markdown(key: str, text: str, n: int, w: int, h: int) -> None:
            rid, mid = row_id(n), f"MARKDOWN-{d['slug']}-{key}"
            position[rid]["children"].append(mid)
            position[mid] = {"children": [], "id": mid, "parents": ["ROOT_ID", "GRID_ID", rid], "type": "MARKDOWN",
                             "meta": {"width": w, "height": h, "code": text}}

        markdown("intro", d["intro"], -1, 12, 16)
        ids: dict[str, int] = {}
        for c in sorted(d["charts"], key=lambda c: c["row"]):
            if "md" in c:
                markdown(c["key"], c["md"], c["row"], c["w"], c["h"])
                continue
            chart_id += 1
            ids[c["key"]] = chart_id
            c_uuid = uid(f"chart::{d['slug']}::{c['key']}")
            rid, cid = row_id(c["row"]), f"CHART-{d['slug']}-{c['key']}"
            position[rid]["children"].append(cid)
            position[cid] = {"children": [], "id": cid, "parents": ["ROOT_ID", "GRID_ID", rid],
                             "type": "CHART", "meta": {"chartId": chart_id, "height": c["h"], "width": c["w"],
                                                       "sliceName": c["name"], "uuid": c_uuid}}
            params = {"datasource": f"{chart_id}__table", "viz_type": c["viz"], "extra_form_data": {},
                      "dashboards": [], "annotation_layers": [], **c["params"]}
            files[f"{ROOT}/charts/{d['slug']}_{c['key']}.yaml"] = _yaml({
                "slice_name": c["name"], "description": None, "certified_by": None,
                "certification_details": None, "viz_type": c["viz"], "params": params, "query_context": None,
                "cache_timeout": None, "uuid": c_uuid, "version": "1.0.0", "dataset_uuid": ds_uuid[c["ds"]]})
        nf = []
        for col, label, target in d["filters"]:
            excluded = [ids[c["key"]] for c in real if col not in ds_cols.get(c["ds"], set())]
            nf.append({"id": f"NATIVE_FILTER-{d['slug']}-{col}", "name": label, "filterType": "filter_select",
                       "type": "NATIVE_FILTER", "targets": [{"column": {"name": col}, "datasetUuid": ds_uuid[target]}],
                       "controlValues": {"enableEmptyFilter": False, "defaultToFirstItem": False,
                                         "multiSelect": True, "searchAllOptions": False, "inverseSelection": False},
                       "defaultDataMask": {"extraFormData": {}, "filterState": {}, "ownState": {}},
                       "cascadeParentIds": [], "scope": {"rootPath": ["ROOT_ID"], "excluded": excluded},
                       "description": f"Filter every compatible chart by {label}"})
        files[f"{ROOT}/dashboards/{d['slug']}.yaml"] = _yaml({
            "dashboard_title": d["title"], "description": d["description"], "css": "", "slug": d["slug"],
            "certified_by": "DataPlatform Data Professional appliance",
            "certification_details": "Created by build/superset/bootstrap_dashboards.py",
            "published": True, "uuid": uid(f"dashboard::{d['slug']}"), "position": position,
            "metadata": {"color_scheme": "supersetColors", "refresh_frequency": 0, "expanded_slices": {},
                         "label_colors": {}, "shared_label_colors": [], "map_label_colors": {},
                         "color_scheme_domain": [], "cross_filters_enabled": True,
                         "timed_refresh_immune_slices": [], "default_filters": "{}", "chart_configuration": {},
                         "global_chart_configuration": {}, "native_filter_configuration": nf},
            "version": "1.0.0"})
        dashboards[d["slug"]] = files
    return datasets, dashboards


def zip_bundle(files: dict[str, str], kind: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{ROOT}/metadata.yaml", _meta(kind))
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


# ── environment → connections ───────────────────────────────────────────────────
def warehouse_spec() -> dict:
    return {"file": "DataPlatform_warehouse", "name": DB_NAME, "uuid": uid("db::warehouse"),
            "uri": f"postgresql+psycopg2://{quote(PG_USER)}:{MASK}@{PG_HOST}:{PG_PORT}/{PG_DB}",
            "password": os.environ.get("PG_PASS") or None}


def trino_yaml() -> str:
    user = quote(os.environ.get("TRINO_USER", os.environ.get("USER_HANDLE", "analyst")))
    return _yaml({"database_name": TRINO_NAME, "sqlalchemy_uri": f"trino://{user}@{TRINO_HOST}:{TRINO_PORT}/warehouse",
                  "password": None, "cache_timeout": None, "expose_in_sqllab": True, "allow_run_async": False,
                  "allow_ctas": False, "allow_cvas": False, "allow_dml": False, "allow_csv_upload": False,
                  "impersonate_user": False,
                  "extra": {"allows_virtual_table_explore": True, "metadata_params": {}, "engine_params": {},
                            "schemas_allowed_for_file_upload": [], "allow_multi_catalog": True},
                  "uuid": uid("db::trino"), "version": "1.0.0"})


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


PROBE_SCHEMAS = (SCHEMA, "mlops", "ccai", "disputes")


def probe_warehouse() -> dict[str, dict[str, list[tuple[str, str]]]]:
    """{schema: {table: [(column, data_type), …]}} for marts + the AI/ML schemas — {} when unreachable."""
    if not os.environ.get("PG_PASS") or not _port_open(PG_HOST, PG_PORT):
        return {}
    try:
        import psycopg2  # noqa: PLC0415

        conn = psycopg2.connect(host=PG_HOST, port=PG_PORT, dbname=PG_DB, user=PG_USER,
                                password=os.environ["PG_PASS"], connect_timeout=5)
        try:
            cur = conn.cursor()
            cur.execute("""SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns
                           WHERE table_schema = ANY(%s) ORDER BY table_schema, table_name, ordinal_position""",
                        (list(PROBE_SCHEMAS),))
            out: dict[str, dict[str, list]] = {}
            for sch, t, c, typ in cur.fetchall():
                out.setdefault(sch, {}).setdefault(t, []).append((c, typ))
            return out
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("warehouse probe failed: %s", exc)
        return {}


# ── Superset REST client ────────────────────────────────────────────────────────
class Superset:
    def __init__(self, user: str, password: str, timeout: float = 30):
        import requests  # noqa: PLC0415

        self.s = requests.Session()
        self.s.trust_env = False   # never send local API calls through the egress proxy
        self.user, self.password, self.timeout = user, password, timeout
        self.base = ""

    def wait_healthy(self, candidates: list[str], deadline_s: int) -> None:
        end, last = time.time() + deadline_s, None
        while time.time() < end:
            for b in candidates:
                try:
                    r = self.s.get(f"{b}/api/v1/security/csrf_token/", timeout=5)
                    if r.status_code in (200, 401):
                        self.base = b
                        log.info("Superset API at %s", b)
                        return
                    last = f"{b}: HTTP {r.status_code}"
                except Exception as exc:  # noqa: BLE001
                    last = f"{b}: {type(exc).__name__}"
            time.sleep(5)
        raise RuntimeError(f"Superset API not reachable ({last})")

    def login(self) -> None:
        r = self.s.post(f"{self.base}/api/v1/security/login", timeout=self.timeout,
                        json={"username": self.user, "password": self.password, "provider": "db", "refresh": True})
        if r.status_code != 200:
            raise RuntimeError(f"login failed: HTTP {r.status_code}")
        self.s.headers["Authorization"] = f"Bearer {r.json()['access_token']}"
        r = self.s.get(f"{self.base}/api/v1/security/csrf_token/", timeout=self.timeout)
        if r.ok:
            self.s.headers["X-CSRFToken"] = r.json()["result"]
            # SESSION_COOKIE_SECURE: requests would not send that cookie back over this local http://
            # hop, and CSRF needs it — pin it explicitly.
            jar = "; ".join(f"{c.name}={c.value}" for c in self.s.cookies)
            if jar:
                self.s.headers["Cookie"] = jar
        self.s.headers["Referer"] = self.base + "/"

    def req(self, method: str, path: str, retries: int = 3, **kw):
        kw.setdefault("timeout", self.timeout)
        kw.setdefault("allow_redirects", False)
        for i in range(retries):
            try:
                r = self.s.request(method, f"{self.base}{path}", **kw)
                if r.status_code in (502, 503, 504) and i < retries - 1:
                    time.sleep(3 * (i + 1))
                    continue
                return r
            except Exception:  # noqa: BLE001
                if i == retries - 1:
                    raise
                time.sleep(3 * (i + 1))
        raise RuntimeError("unreachable")

    def list_all(self, resource: str, columns: str = "") -> list[dict]:
        out, page = [], 0
        while True:
            q = f"(page:{page},page_size:100{',columns:!(' + columns + ')' if columns else ''})"
            r = self.req("GET", f"/api/v1/{resource}/?q={q}")
            if not r.ok:
                raise RuntimeError(f"list {resource}: HTTP {r.status_code} {r.text[:200]}")
            res = r.json().get("result", [])
            out += res
            if len(res) < 100:
                return out
            page += 1

    def import_bundle(self, kind: str, data: bytes, passwords: dict | None = None) -> None:
        form = {"overwrite": "true"}
        if passwords:
            form["passwords"] = json.dumps(passwords)
        r = self.req("POST", f"/api/v1/{kind}/import/", files={"formData": ("bundle.zip", data, "application/zip")},
                     data=form, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"{kind} import failed: HTTP {r.status_code} {r.text[:1500]}")


# ── main flow ───────────────────────────────────────────────────────────────────
def export(args, datasets: dict, dashboards: dict) -> int:
    if args.export_dir:
        out = Path(args.export_dir)
        for files in [datasets, *dashboards.values()]:
            for p, text in files.items():
                rel = Path(*Path(p).parts[1:])
                (out / rel).parent.mkdir(parents=True, exist_ok=True)
                (out / rel).write_text(text)
        (out / "metadata.yaml").write_text(_meta("Dashboard"))
        (out / "saved_queries.json").write_text(json.dumps(
            [{"label": a, "description": b, "sql": c} for a, b, c in SAVED_QUERIES], indent=2) + "\n")
        log.info("wrote the bundle to %s", out)
    if args.zip:
        merged: dict[str, str] = {}
        for files in dashboards.values():
            merged.update(files)
        Path(args.zip).write_bytes(zip_bundle(merged, "Dashboard"))
        log.info("wrote %s (%d files)", args.zip, len(merged) + 1)
    return 0


def run(args) -> int:
    db = warehouse_spec()
    offline = bool(args.export_dir or args.zip)
    present = {} if offline else probe_warehouse()
    if args.present_json:   # offline testing of the AI/ML section against a captured probe
        present = json.loads(Path(args.present_json).read_text())
    cols = mart_columns(present)
    dash_spec, vds = all_dashboards(present)
    if not offline:
        log.info("warehouse probe: %s", ", ".join(f"{s}: {len(present.get(s, {}))} tables" for s in PROBE_SCHEMAS)
                 if present else "unreachable — dashboards are created anyway; run `dbt build` and they fill in")
    datasets, dashboards = build_bundles(db, cols, dash_spec, vds)
    if offline:
        return export(args, datasets, dashboards)

    pw = os.environ.get("SUPERSET_ADMIN_PASS", "")
    if not pw:
        log.error("SUPERSET_ADMIN_PASS is not set")
        return 1
    url = os.environ.get("SUPERSET_URL")
    candidates = [url.rstrip("/")] if url else ["http://127.0.0.1:8088/superset", "http://127.0.0.1:8088"]
    ss = Superset(os.environ.get("SUPERSET_ADMIN_USER", "admin"), pw)
    ss.wait_healthy(candidates, args.wait)
    for i in range(5):
        try:
            ss.login()
            break
        except Exception as exc:  # noqa: BLE001
            if i == 4:
                raise
            log.warning("login attempt %d failed (%s); retrying", i + 1, exc)
            time.sleep(6)

    # 1) connections — keep the uuid of a same-named connection created by hand
    existing = {d["database_name"]: d for d in ss.list_all("database", "id,database_name,uuid")}
    e = existing.get(db["name"])
    if e and e.get("uuid") and e["uuid"] != db["uuid"]:
        log.info("database %r exists with uuid %s — updating it in place", db["name"], e["uuid"])
        db["uuid"] = e["uuid"]
        datasets, dashboards = build_bundles(db, cols, dash_spec, vds)
    db_file = f"{ROOT}/databases/{db['file']}.yaml"
    pw_map = {f"databases/{db['file']}.yaml": db["password"]} if db["password"] else None
    ss.import_bundle("database", zip_bundle({db_file: datasets[db_file]}, "Database"), pw_map)
    log.info("connection %r ready", db["name"])
    if os.environ.get("SUPERSET_REGISTER_TRINO", "auto") == "1" or (
            os.environ.get("SUPERSET_REGISTER_TRINO", "auto") == "auto" and _port_open(TRINO_HOST, TRINO_PORT)):
        try:
            ss.import_bundle("database", zip_bundle({f"{ROOT}/databases/Trino.yaml": trino_yaml()}, "Database"))
            log.info("connection %r ready", TRINO_NAME)
        except Exception as exc:  # noqa: BLE001
            log.warning("Trino connection not imported: %s", str(exc)[:300])
    else:
        log.info("Trino not reachable at %s:%s — skipping %r (re-run once the trino profile is up)",
                 TRINO_HOST, TRINO_PORT, TRINO_NAME)

    # 2) datasets, then charts + dashboards (dashboard imports never overwrite related objects)
    ss.import_bundle("dataset", zip_bundle(datasets, "SqlaTable"), pw_map)
    for _slug, files in dashboards.items():
        ss.import_bundle("chart", zip_bundle({p: t for p, t in files.items() if "/dashboards/" not in p}, "Slice"),
                         pw_map)
        ss.import_bundle("dashboard", zip_bundle(files, "Dashboard"), pw_map)
        fix_filter_scope(ss, _slug)
        detach_stale_charts(ss, _slug)
    log.info("imported %d datasets and %d dashboards", sum("/datasets/" in p for p in datasets), len(dashboards))
    prune_stale_datasets(ss, db["name"], present)

    # 3) saved queries (create or update by label)
    db_id = {d["database_name"]: d for d in ss.list_all("database", "id,database_name")}[db["name"]]["id"]
    saved = {q["label"]: q for q in ss.list_all("saved_query", "id,label")}
    for label, desc, sql in SAVED_QUERIES:
        body = {"db_id": db_id, "label": label, "description": desc, "sql": sql, "schema": SCHEMA}
        r = (ss.req("PUT", f"/api/v1/saved_query/{saved[label]['id']}", json=body) if label in saved
             else ss.req("POST", "/api/v1/saved_query/", json=body))
        if r.status_code not in (200, 201):
            log.warning("saved query %r: HTTP %s %s", label, r.status_code, r.text[:200])
    log.info("saved queries: %d", len(SAVED_QUERIES))
    trino_db = {d["database_name"]: d for d in ss.list_all("database", "id,database_name")}.get(TRINO_NAME)
    if trino_db:
        saved = {q["label"]: q for q in ss.list_all("saved_query", "id,label")}
        for label, desc, sql in TRINO_SAVED_QUERIES:
            body = {"db_id": trino_db["id"], "label": label, "description": desc, "sql": sql, "schema": "marts",
                    "catalog": "warehouse"}
            r = (ss.req("PUT", f"/api/v1/saved_query/{saved[label]['id']}", json=body) if label in saved
                 else ss.req("POST", "/api/v1/saved_query/", json=body))
            if r.status_code not in (200, 201):
                log.warning("saved query %r: HTTP %s %s", label, r.status_code, r.text[:200])
        log.info("federated saved queries on %r: %d", TRINO_NAME, len(TRINO_SAVED_QUERIES))

    rc = 0
    if args.verify:
        rc = 0 if all([verify_charts(ss, d["slug"]) == 0 for d in dash_spec]) else 1
    for d in dash_spec:
        log.info("dashboard → %s/superset/dashboard/%s/", ss.base.replace("http://127.0.0.1:8088", ""), d["slug"])
    return rc


def verify_charts(ss: Superset, slug: str) -> int:
    """Run a query equivalent to every chart through POST /api/v1/chart/data."""
    r = ss.req("GET", f"/api/v1/dashboard/{slug}/charts")
    if not r.ok:
        log.warning("verify: cannot list charts of %s (HTTP %s)", slug, r.status_code)
        return 1
    bad = 0
    for ch in r.json()["result"]:
        p = ch.get("form_data") or {}
        ds_id = int(str(p.get("datasource", "0__table")).split("__")[0])
        cols: list = []
        for k in ("x_axis", "column", "groupby", "all_columns"):
            v = p.get(k)
            cols += [v] if isinstance(v, str) else list(v or [])
        metrics = list(p.get("metrics") or ([p["metric"]] if p.get("metric") else []))
        where = " AND ".join(f"({f['sqlExpression']})" for f in p.get("adhoc_filters", [])
                             if f.get("expressionType") == "SQL")
        q = {"columns": list(dict.fromkeys(cols)), "metrics": metrics, "row_limit": 50,
             "extras": {"where": where} if where else {}, "orderby": [], "filters": []}
        if not metrics:
            q["is_timeseries"] = False
        body = {"datasource": {"id": ds_id, "type": "table"}, "queries": [q], "result_format": "json",
                "result_type": "full", "force": True}
        d = ss.req("POST", "/api/v1/chart/data", json=body, timeout=120)
        rows = sum(x.get("rowcount", 0) for x in d.json().get("result", [])) if d.status_code == 200 else None
        ok = d.status_code == 200 and bool(rows)
        bad += not ok
        log.info("verify %-26s %-52s %s", slug, ch["slice_name"],
                 f"ok ({rows} rows)" if ok else f"FAIL HTTP {d.status_code} {d.text[:300]}")
    log.info("verify %s: %d chart queries failed", slug, bad)
    return 1 if bad else 0


def detach_stale_charts(ss: Superset, slug: str) -> None:
    """Dashboard imports add chart links but never drop them: unlink charts that are no longer in the
    dashboard layout (e.g. after a chart was removed from the spec). The charts themselves are kept."""
    r = ss.req("GET", f"/api/v1/dashboard/{slug}")
    if not r.ok:
        return
    dash = r.json()["result"]
    pos = json.loads(dash.get("position_json") or "{}")
    placed = {v["meta"].get("chartId") for v in pos.values() if isinstance(v, dict) and v.get("type") == "CHART"}
    r = ss.req("GET", f"/api/v1/dashboard/{dash['id']}/charts")
    if not r.ok:
        return
    for ch in r.json()["result"]:
        if ch["id"] in placed:
            continue
        g = ss.req("GET", f"/api/v1/chart/{ch['id']}")
        keep = [d["id"] for d in (g.json()["result"].get("dashboards") or [])
                if d["id"] != dash["id"]] if g.ok else None
        if keep is None:
            continue
        u = ss.req("PUT", f"/api/v1/chart/{ch['id']}", json={"dashboards": keep})
        log.info("unlinked chart %r from %s (not in layout): HTTP %s", ch["slice_name"], slug, u.status_code)


def prune_stale_datasets(ss: Superset, db_name: str, present: dict) -> None:
    """Delete physical marts datasets of this connection whose table no longer exists in the warehouse
    (retired marts tables), and the charts built on them — they can only
    error. Runs only when the live probe saw the marts schema, so an unreachable warehouse prunes nothing."""
    live = set(present.get(SCHEMA, {}))
    if not live:
        return
    stale = [d for d in ss.list_all("dataset", "id,table_name,schema,sql,database.database_name")
             if d.get("schema") == SCHEMA and not (d.get("sql") or "").strip()
             and (d.get("database") or {}).get("database_name") == db_name and d["table_name"] not in live]
    if not stale:
        return
    ids = {d["id"] for d in stale}
    for ch in ss.list_all("chart", "id,slice_name,datasource_id,datasource_type"):
        if ch.get("datasource_type") == "table" and ch.get("datasource_id") in ids:
            r = ss.req("DELETE", f"/api/v1/chart/{ch['id']}")
            log.info("deleted chart %r on a dropped table: HTTP %s", ch["slice_name"], r.status_code)
    for d in stale:
        r = ss.req("DELETE", f"/api/v1/dataset/{d['id']}")
        log.info("deleted dataset %s.%s (table no longer exists): HTTP %s", SCHEMA, d["table_name"], r.status_code)


def fix_filter_scope(ss: Superset, slug: str) -> None:
    """Give the native filters real chart ids (the frontend scopes by id, the bundle by position)."""
    r = ss.req("GET", f"/api/v1/dashboard/{slug}")
    if not r.ok:
        log.warning("dashboard lookup %s failed: HTTP %s", slug, r.status_code)
        return
    dash = r.json()["result"]
    meta = json.loads(dash.get("json_metadata") or "{}")
    pos = json.loads(dash.get("position_json") or "{}")
    all_ids = [v["meta"]["chartId"] for v in pos.values() if isinstance(v, dict) and v.get("type") == "CHART"]
    changed = False
    for f in meta.get("native_filter_configuration", []):
        excl = set(f.get("scope", {}).get("excluded", []))
        scope = [i for i in all_ids if i not in excl]
        if f.get("chartsInScope") != scope:
            f["chartsInScope"], changed = scope, True
    if changed:
        r = ss.req("PUT", f"/api/v1/dashboard/{dash['id']}", json={"json_metadata": json.dumps(meta)})
        if not r.ok:
            log.warning("native filter scope update failed: HTTP %s %s", r.status_code, r.text[:200])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bootstrap the DataPlatform Superset dashboards (idempotent)")
    ap.add_argument("--export-dir", help="only write the YAML bundle to this folder; no Superset calls")
    ap.add_argument("--zip", help="only write one importable dashboard ZIP; no Superset calls")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for the Superset API")
    ap.add_argument("--verify", action="store_true", help="run every dashboard chart query and report")
    ap.add_argument("--present-json", help=argparse.SUPPRESS)
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        return run(a)
    except Exception as exc:  # noqa: BLE001
        log.error("bootstrap failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
