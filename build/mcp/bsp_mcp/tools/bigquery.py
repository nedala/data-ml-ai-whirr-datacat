"""BigQuery (enterprise EDW) via bsp_connectors.bigquery — SELECT-only, dry-run cost estimate, row cap.

Principal switch: every tool takes an optional `identity` (a Studio cloud
identity name or id) and `project`. Studio issues a short-lived token only for
the HMAC-verified caller (delegated.caller()) when that user is on the
identity's access list; the caller signature is forwarded so Studio verifies it
too. Without an identity — or without a verified caller — the assigned
BigQuery · read identity is used, as before."""
from __future__ import annotations

import asyncio
import contextlib
import csv
import datetime as dt
import json
import os
import re
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field

from ..common import (MAX_ROWS, UserError, check_select, clamp, connector_mode, connectors, data_dir, ident,
                      md_table, result, tool)

G = "bigquery"
USD_PER_TIB = float(os.environ.get("BQ_USD_PER_TIB", "6.25"))
MAX_BYTES = int(float(os.environ.get("MCP_BQ_MAX_BYTES", str(50 * 1024**3))))   # refuse queries scanning more


def _bq():
    return connectors("bigquery")


IDENTITY = Annotated[str, Field(description="Optional: act as this named Google identity (Studio → Cloud identities) "
                                            "instead of the assigned one; you must be on its access list")]
PROJECT = Annotated[str, Field(description="Optional: data project for a named identity (default: its default project)")]


def _caller_proof() -> tuple[str, str]:
    try:
        from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

        h = {k.lower(): v for k, v in (get_http_headers(include_all=True) or {}).items()}
    except Exception:  # noqa: BLE001
        return "", ""
    return h.get("x-caller-ts") or "", h.get("x-caller-sig") or ""


@contextlib.contextmanager
def _acting(identity: str, project: str = ""):
    """Yield "principal on project" for a named identity ('' = assigned)."""
    identity, project = (identity or "").strip(), (project or "").strip()
    if not identity:
        yield ""
        return
    if connector_mode("bigquery") != "live":
        yield f"{identity} (mock mode — no Google call)"
        return
    from .. import delegated  # noqa: PLC0415

    user = delegated.caller()
    if not user:
        raise UserError("acting as a named Google identity needs a verified caller (the AI harness signs requests); "
                        "without one only the assigned BigQuery identity is available")
    from bsp_common import gcp  # noqa: PLC0415

    ts, sig = _caller_proof()
    bq = _bq()
    if not hasattr(bq, "using_principal"):
        raise UserError("this image's BigQuery connector cannot switch identity")
    with gcp.acting(user, caller_ts=ts, caller_sig=sig), bq.using_principal(identity, project or None):
        try:
            d = gcp.studio_token(identity, consumer="bigquery_read")
        except gcp.StudioTokenError as e:
            raise UserError(f"Studio refused identity {identity!r}: {e}") from None
        yield f"{d.get('principal_email') or identity} on {project or bq.project()}"


def _dry_run(sql: str) -> dict:
    bq = _bq()
    if hasattr(bq, "dry_run"):
        return dict(bq.dry_run(sql))
    if connector_mode("bigquery") != "live":
        return {"bytes_processed": None, "note": "mock mode — no BigQuery dry run (connect GCP_SA_JSON for real estimates)"}
    from google.cloud import bigquery as gbq  # noqa: PLC0415

    job = bq.client().query(sql, job_config=gbq.QueryJobConfig(dry_run=True, use_query_cache=False),
                            location=os.environ.get("BQ_LOCATION", "US"))
    return {"bytes_processed": int(job.total_bytes_processed or 0)}


def _cost(est: dict) -> dict:
    b = est.get("bytes_processed")
    if b is None:
        return est
    return {**est, "gib": round(b / 1024**3, 3), "est_usd": round(b / 1024**4 * USD_PER_TIB, 4)}


def _as(who: str) -> str:
    return f" — acting as {who}" if who else ""


@tool(G, "List BigQuery datasets")
def bigquery_list_datasets(identity: IDENTITY = "", project: PROJECT = "") -> dict:
    """Datasets in the assigned identity's data projects (Studio → Cloud identities → BigQuery · read),
    or in `project` as the named `identity`."""
    with _acting(identity, project) as who:
        rows = _bq().list_datasets()
    return result(f"{len(rows)} dataset(s){_as(who)}\n\n" + md_table(rows, 50), mode=connector_mode("bigquery"),
                  datasets=rows, **({"acting_as": who} if who else {}))


@tool(G, "List tables in a BigQuery dataset")
def bigquery_list_tables(dataset: Annotated[str, Field(description="Dataset id, e.g. 'raw' or 'marts'")],
                         identity: IDENTITY = "", project: PROJECT = "") -> dict:
    """Tables and views in one dataset."""
    with _acting(identity, project) as who:
        rows = _bq().list_tables(ident(dataset, "dataset"))
    return result(f"{len(rows)} table(s) in {dataset}{_as(who)}\n\n" + md_table(rows, 50), mode=connector_mode("bigquery"),
                  tables=rows, **({"acting_as": who} if who else {}))


@tool(G, "Describe a BigQuery table")
def bigquery_describe_table(dataset: Annotated[str, Field(description="Dataset id")],
                            table: Annotated[str, Field(description="Table id")],
                            identity: IDENTITY = "", project: PROJECT = "") -> dict:
    """Column names, types, modes and descriptions of one table."""
    with _acting(identity, project) as who:
        cols = _bq().table_schema(ident(dataset, "dataset"), ident(table, "table"))
    return result(f"{dataset}.{table}: {len(cols)} columns{_as(who)}\n\n" + md_table(cols, 80),
                  mode=connector_mode("bigquery"), columns=cols, **({"acting_as": who} if who else {}))


@tool(G, "Estimate BigQuery query cost (dry run)")
def bigquery_dry_run(sql: Annotated[str, Field(description="One SELECT / WITH statement (GoogleSQL)")],
                     identity: IDENTITY = "", project: PROJECT = "") -> dict:
    """Validate a SELECT and estimate bytes scanned and on-demand cost without running it."""
    s = check_select(sql)
    with _acting(identity, project) as who:
        est = _cost(_dry_run(s))
    msg = (f"would scan {est['gib']} GiB ≈ ${est['est_usd']} at ${USD_PER_TIB}/TiB" if est.get("gib") is not None
           else est.get("note", "no estimate"))
    return result(msg + _as(who), mode=connector_mode("bigquery"), estimate=est, **({"acting_as": who} if who else {}))


@tool(G, "Run a read-only BigQuery query")
def bigquery_run_query(
    sql: Annotated[str, Field(description="One SELECT / WITH statement (GoogleSQL); qualify tables as dataset.table")],
    max_rows: Annotated[int, Field(description="Row cap (1–500)")] = 100,
    identity: IDENTITY = "",
    project: PROJECT = "",
) -> dict:
    """Run a SELECT on BigQuery with a row cap. A dry run first refuses queries above MCP_BQ_MAX_BYTES (default 50 GiB)."""
    s = check_select(sql)
    est = {}
    with _acting(identity, project) as who:
        try:
            est = _cost(_dry_run(s))
        except Exception as e:  # noqa: BLE001
            raise UserError(f"dry run failed: {str(e).splitlines()[0][:300]}") from None
        if est.get("bytes_processed") and est["bytes_processed"] > MAX_BYTES:
            raise UserError(f"query would scan {est['gib']} GiB (> {MAX_BYTES / 1024**3:.0f} GiB cap) — add filters/partitions")
        lim = clamp(max_rows, 1, MAX_ROWS)
        rows = _bq().query(s, max_rows=lim + 1)
    more = len(rows) > lim
    rows = rows[:lim]
    return result(f"{len(rows)} row(s){' (truncated)' if more else ''}{_as(who)}\n\n" + md_table(rows),
                  mode=connector_mode("bigquery"), rows=rows, truncated=more, estimate=est,
                  **({"acting_as": who} if who else {}))


# ── BigQuery Studio UI fallback ───────────────────────────────────────────────
# The authenticated desktop Chromium has a BigQuery Studio tab. The new UI
# renders the result grid on a canvas, so the reliable read path is the JSON
# results tab (a read-only Monaco editor). These tools drive that tab with the
# same logged-in session used by atlan_ui_* and powerbi_ui_*.
_UI_HOST = "console.cloud.google.com/bigquery"
_UI_HOME = "https://console.cloud.google.com/bigquery"
_SQL_EDITOR = "textarea[aria-label^='SQL Code Editor area']"


def _norm_ui_text(s: str) -> str:
    # Monaco reformats the script with newlines/indentation; collapse ALL whitespace for comparison.
    return re.sub(r"\s+", " ", s or "").strip()


async def _ui_page():
    from . import browser as ui  # noqa: PLC0415

    tabs = await ui.browser_tabs()
    tab = next((t["tab"] for t in tabs.get("tabs", []) if _UI_HOST in (t.get("url") or "")), None)
    if tab is None:
        opened = await ui.browser_new_tab(_UI_HOME + "?authuser=1")
        tab = opened.get("tab")
        await asyncio.sleep(8)
    page = await ui._page(tab)
    if _UI_HOST not in (page.url or ""):
        await page.goto(_UI_HOME + "?authuser=1", wait_until="domcontentloaded")
        await asyncio.sleep(6)
    return ui, page


async def _open_sql_editor(page):
    ed = page.locator(_SQL_EDITOR)
    if await ed.count():
        return ed.first
    # Select an existing query tab.
    for pattern in (r"Untitled query", r"\bquery\b"):
        tabs = page.get_by_role("tab", name=re.compile(pattern, re.I))
        for i in range(await tabs.count()):
            try:
                t = tabs.nth(i)
                if not await t.is_visible():
                    continue
                await t.click()
                await page.wait_for_timeout(3500)
                ed = page.locator(_SQL_EDITOR)
                if await ed.count():
                    return ed.first
            except Exception:  # noqa: BLE001
                continue
    # Create a new SQL query tab.
    for loc in (page.get_by_role("button", name="SQL query", exact=True),
                page.get_by_role("button", name=re.compile(r"^SQL query", re.I)),
                page.get_by_text("SQL query", exact=True)):
        try:
            for i in range(await loc.count()):
                b = loc.nth(i)
                if await b.is_visible():
                    await b.click()
                    await page.wait_for_timeout(4000)
                    ed = page.locator(_SQL_EDITOR)
                    if await ed.count():
                        return ed.first
        except Exception:  # noqa: BLE001
            continue
    raise UserError("BigQuery Studio SQL editor not found — open the BigQuery tab and create/select a SQL query tab")


async def _set_sql(page, sql: str):
    ed = await _open_sql_editor(page)
    mon = ed.locator("xpath=ancestor::div[contains(@class,'monaco-editor')]").first
    for _ in range(2):
        # The hidden textarea is covered by Monaco's rendered lines, so focus the editor container.
        try:
            await mon.click()
        except Exception:  # noqa: BLE001
            await mon.click(force=True)
        await page.keyboard.press("Control+A")
        await page.keyboard.press("Backspace")
        await page.wait_for_timeout(350)
        await page.keyboard.insert_text(sql)
        await page.wait_for_timeout(800)
        try:
            text = _norm_ui_text(await mon.locator(".view-lines").first.inner_text())
        except Exception:  # noqa: BLE001
            text = ""
        if _norm_ui_text(sql)[:60].lower() in text.lower():
            return mon, text
    raise UserError(f"could not write the SQL into BigQuery Studio (editor now shows: {text[:300]!r})")


def _error_text(body: str) -> str:
    markers = ("Query failed", "Query error", "Error running query", "Syntax error", "Invalid query",
               "Access denied", "Not found: Table", "Not found: Dataset", "Not found: Project")
    hits = [(body.lower().rfind(m.lower()), m) for m in markers]
    i, _ = max(hits, default=(-1, ""))
    return body[max(0, i):i + 1600] if i >= 0 else body[-1600:]


async def _run_ui(page, sql: str, timeout_s: int) -> str:
    await _set_sql(page, sql)
    panel = page.locator(".bqui-query-results")
    before_panel = await panel.inner_text() if await panel.count() else ""
    # _set_sql leaves focus in the SQL editor; Ctrl+Enter runs the whole script.
    await page.keyboard.press("Control+Enter")
    deadline = time.monotonic() + clamp(timeout_s, 15, 900)
    result_count = re.compile(r"\b\d+\s*[\u2013-]\s*\d+\s+of\s+\d+\b", re.I)
    errors = ("query failed", "query error", "error running query", "syntax error", "invalid query",
              "access denied", "not found: table", "not found: dataset", "not found: project")
    last_panel = ""
    while time.monotonic() < deadline:
        await asyncio.sleep(0.8)
        panel_text = ""
        if await panel.count():
            try:
                panel_text = await panel.inner_text()
            except Exception:  # noqa: BLE001
                panel_text = ""
        last_panel = panel_text
        panel_low = panel_text.lower()
        busy = any(x in panel_low for x in ("running", "querying", "in progress", "pending"))
        # A new error is authoritative once the panel changed or the error text is new.
        if not busy and any(x in panel_low for x in errors):
            err = _error_text(panel_text)
            if err and (err not in before_panel or panel_text != before_panel):
                raise UserError(f"BigQuery UI query failed: {err[:800]}")
        completed = "query completed" in panel_low and panel_text != before_panel
        count_changed = bool(result_count.search(panel_text)) and panel_text != before_panel
        no_results = ("no results" in panel_low or "0 rows" in panel_low) and panel_text != before_panel
        if not busy and (completed or count_changed or no_results):
            return page.url
    # One last look: a fast failure may have landed at the deadline.
    if any(x in last_panel.lower() for x in errors):
        err = _error_text(last_panel)
        if err and (err not in before_panel or last_panel != before_panel):
            raise UserError(f"BigQuery UI query failed: {err[:800]}")
    raise UserError(f"BigQuery UI query timed out after {timeout_s}s; result panel: {last_panel[-600:]!r}")


def _rows_columns(data, max_rows: int) -> tuple[list[dict], list[str], bool]:
    if data is None:
        data = []
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise UserError(f"BigQuery UI JSON result is {type(data).__name__}, expected a list of rows")
    rows = [r if isinstance(r, dict) else {"value": r} for r in data]
    lim = clamp(max_rows, 1, MAX_ROWS)
    truncated = len(rows) > lim
    rows = rows[:lim]
    cols: list[str] = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    return rows, cols, truncated


async def _read_ui_json(page, max_rows: int):
    panel = page.locator(".bqui-query-results")
    if not await panel.count():
        raise UserError("BigQuery results panel not found — run a query first")
    tabs = panel.get_by_text("JSON", exact=True)
    clicked = False
    for i in range(await tabs.count()):
        try:
            if await tabs.nth(i).is_visible():
                await tabs.nth(i).click()
                clicked = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not clicked:
        raise UserError("BigQuery UI has no JSON results tab — the query may not have finished")
    await page.wait_for_timeout(1500)
    txt = ""
    mon = panel.locator(".monaco-editor")
    if await mon.count():
        try:
            txt = await mon.first.locator(".view-lines").first.inner_text()
        except Exception:  # noqa: BLE001
            txt = ""
    if not txt.strip():
        pre = panel.locator("pre")
        if await pre.count():
            txt = await pre.first.inner_text()
    txt = (txt or "").replace("\xa0", " ").strip()
    if not txt:
        try:
            ptext = await panel.inner_text()
        except Exception:  # noqa: BLE001
            ptext = ""
        if any(x in ptext.lower() for x in ("query failed", "query error", "error running query", "syntax error",
                                            "invalid query", "access denied", "not found: table", "not found: dataset",
                                            "not found: project")):
            raise UserError(f"BigQuery UI query failed: {_error_text(ptext)[:800]}")
        raise UserError("BigQuery UI JSON result is empty — the query may not have completed or may have failed")
    start = min([p for p in (txt.find("["), txt.find("{")) if p >= 0], default=-1)
    if start > 0:
        txt = txt[start:]
    try:
        data = json.loads(txt)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"could not parse BigQuery UI result JSON ({type(e).__name__}); first 400 chars: {txt[:400]!r}") from None
    return _rows_columns(data, max_rows)


@tool(G, "Run a read-only query in the logged-in BigQuery Studio tab", open_world=True)
async def bigquery_ui_query(
    sql: Annotated[str, Field(description="One SELECT / WITH statement (GoogleSQL); fully qualify project.dataset.table")],
    max_rows: Annotated[int, Field(description="Rows to return (1–500)")] = 100,
    timeout_s: Annotated[int, Field(description="Seconds to wait for completion (15–900)")] = 180,
) -> dict:
    """Fallback when the BigQuery REST identity cannot reach a project or returns 401/403/404: type the SQL into
    the logged-in BigQuery Studio query tab, run it with Ctrl+Enter, read the JSON results tab, and return the
    rows. BigQuery renders its grid on a canvas, so the JSON tab is the reliable read path. Read-only: exactly
    one SELECT/WITH statement. Use fully qualified `project.dataset.table` names; the UI's current project is
    not assumed. Prefer bigquery_run_query when the API identity works; use this for the logged-in user's data."""
    s = check_select(sql)
    _ui, page = await _ui_page()
    job_url = await _run_ui(page, s, timeout_s)
    rows, cols, truncated = await _read_ui_json(page, max_rows)
    summary = f"{len(rows)} row(s) from BigQuery Studio UI" + (" (truncated)" if truncated else "")
    return result(summary + "\n\n" + md_table(rows, 50), source="bigquery-ui", sql=s, rows=rows,
                  columns=cols, truncated=truncated, job_url=job_url)


@tool(G, "Peek at the current BigQuery Studio UI results", open_world=True)
async def bigquery_ui_results(
    max_rows: Annotated[int, Field(description="Rows to return (1–500)")] = 100,
) -> dict:
    """Re-read the current BigQuery Studio query results (JSON tab) without running SQL. Use after
    bigquery_ui_query, or to inspect a query the user ran manually in the logged-in browser."""
    _ui, page = await _ui_page()
    rows, cols, truncated = await _read_ui_json(page, max_rows)
    summary = f"{len(rows)} row(s) in the current BigQuery Studio UI result" + (" (truncated)" if truncated else "")
    return result(summary + "\n\n" + md_table(rows, 50), source="bigquery-ui", rows=rows,
                  columns=cols, truncated=truncated, job_url=page.url)


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return v


@tool(G, "Export current BigQuery Studio UI results to CSV/JSON", open_world=True)
async def bigquery_ui_export(
    format: Annotated[Literal["csv", "json"], Field(description="csv or json")] = "csv",
    max_rows: Annotated[int, Field(description="Rows to export (1–500)")] = 500,
    name: Annotated[str, Field(description="Optional filename; default bigquery-ui-<timestamp>.<format>")] = "",
) -> dict:
    """Export the current BigQuery Studio UI result (the loaded JSON-tab rows) to a file under the MCP data
    directory. Use this when the answer needs a portable extract; for aggregates the rows are already small.
    The export is bounded by max_rows and the rows loaded by the UI, not the full table."""
    fmt = (format or "csv").strip().lower()
    if fmt not in ("csv", "json"):
        raise UserError("format must be csv or json")
    _ui, page = await _ui_page()
    rows, cols, truncated = await _read_ui_json(page, max_rows)
    base = Path(name).name if name else f"bigquery-ui-{dt.datetime.now():%Y%m%d-%H%M%S}.{fmt}"
    if not base.lower().endswith("." + fmt):
        base += "." + fmt
    out_dir = data_dir() / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / base
    if fmt == "json":
        path.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in rows:
                w.writerow({c: _csv_value(r.get(c)) for c in cols})
    return result(f"exported {len(rows)} row(s) to {path}" + (" (truncated)" if truncated else ""),
                  source="bigquery-ui", path=str(path), rows=len(rows), columns=cols, truncated=truncated)


@tool(G, "Check the logged-in BigQuery Studio tab", open_world=True)
async def bigquery_ui_status() -> dict:
    """Report the current BigQuery Studio tab, its SQL editor text and whether a results panel is present.
    Use this to confirm the desktop session is logged in before using the BigQuery UI fallback."""
    _ui, page = await _ui_page()
    ed = page.locator(_SQL_EDITOR)
    sql = ""
    if await ed.count():
        mon = ed.first.locator("xpath=ancestor::div[contains(@class,'monaco-editor')]").first
        try:
            sql = _norm_ui_text(await mon.locator(".view-lines").first.inner_text())[:1500]
        except Exception:  # noqa: BLE001
            pass
    panel = page.locator(".bqui-query-results")
    results = ""
    if await panel.count():
        try:
            results = (await panel.inner_text())[:1500]
        except Exception:  # noqa: BLE001
            pass
    summary = (f"BigQuery Studio UI: {await page.title()}\n{page.url}\n"
               f"SQL editor: {'yes' if sql else 'no'}\nResults panel: {'yes' if results else 'no'}\n\n"
               f"SQL (first 500 chars):\n{sql[:500]}\n\nResults panel text:\n{results[:1000]}")
    return result(summary, source="bigquery-ui", sql=sql, has_editor=bool(sql), has_results=bool(results))
