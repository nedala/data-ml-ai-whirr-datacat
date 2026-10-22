"""Playground agent loop: the gateway (any alias/route) + tools from the MCP server
(MCP_URL, bearer MCP_TOKEN). Emits events for the UI timeline:

  {"type":"tools", "tools":[…]}                       tools offered this run
  {"type":"step", "n"}                                model turn started
  {"type":"route", …}                                 which provider/model answered
  {"type":"tool_call", "id","name","group","args"}    tool call started
  {"type":"tool_result", "id","name","ok","ms","text","images":[data-uri]}
  {"type":"message", "content"}                       final assistant answer
  {"type":"error", "message"} / {"type":"done", "messages":[…]}
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import time
from typing import Any, AsyncIterator

from . import router as rt

MAX_RESULT_CHARS = 20_000
_target_override: Any = None   # tests: a FastMCP server object (in-process)


def mcp_url() -> str:
    return os.environ.get("MCP_URL", "http://mcp:8095/mcp")


def group_of(name: str) -> str:
    return name.split("_", 1)[0] if "_" in name else "other"


def _httpx_factory(headers=None, timeout=None, auth=None, **kw):
    import httpx  # noqa: PLC0415

    return httpx.AsyncClient(headers=headers, auth=auth, trust_env=False,
                             timeout=timeout or httpx.Timeout(30.0, read=300.0), follow_redirects=False)


def caller_headers(user: str) -> dict[str, str]:
    """Signed "acting as <user>" headers. The MCP server trusts the identity only
    with a valid HMAC (MCP_DELEGATION_KEY, shared by harness and MCP alone), so
    other MCP-token holders cannot impersonate a user."""
    import hashlib
    import hmac
    import time

    key = os.environ.get("MCP_DELEGATION_KEY", "")
    if not key or not user or user == "playground":
        return {}
    ts = str(int(time.time()))
    sig = hmac.new(key.encode(), f"{user.lower()}|{ts}".encode(), hashlib.sha256).hexdigest()
    return {"X-Caller": user.lower(), "X-Caller-Ts": ts, "X-Caller-Sig": sig}


def client(timeout: float = 300.0, caller: str = ""):
    from fastmcp import Client  # noqa: PLC0415
    from fastmcp.client.transports import StreamableHttpTransport  # noqa: PLC0415

    if _target_override is not None:
        return Client(_target_override, timeout=timeout)
    tok = os.environ.get("MCP_TOKEN", "")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    headers.update(caller_headers(caller))
    return Client(StreamableHttpTransport(mcp_url(), headers=headers, httpx_client_factory=_httpx_factory), timeout=timeout)


async def list_tools() -> list[dict]:
    async with client(30) as c:
        return await list_tools_with(c)


def to_openai_tools(tools: list[dict]) -> list[dict]:
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"][:1024],
                                              "parameters": t["input_schema"]}} for t in tools]


def _result_parts(res: Any) -> tuple[str, list[str], bool]:
    texts, images = [], []
    for c in getattr(res, "content", None) or []:
        ct = getattr(c, "type", "")
        if ct == "text":
            texts.append(c.text)
        elif ct == "image":
            images.append(f"data:{getattr(c, 'mimeType', 'image/png')};base64,{c.data}")
    text = "\n".join(texts)
    if not text and getattr(res, "structured_content", None) is not None:
        text = json.dumps(res.structured_content, default=str)
    return text, images, bool(getattr(res, "is_error", False))


GROUP_HINTS = {
    "browser": "live web browsing in the appliance's desktop Chromium (navigate, read, click, screenshot) — "
               "you CAN browse the internet with these; sites are subject to the appliance egress policy. "
               "The desktop is logged in to Atlan, Power BI and BigQuery Studio; prefer the dedicated "
               "atlan_ui_*, powerbi_ui_* and bigquery_ui_* tools over raw clicking",
    "warehouse": "SQL over the DataPlatform warehouse (Postgres; copper DSL + fiber accounts keyed by ban, "
                 "wire centers by clli, invoice ids <ban>-<yymm>; raw.transactions is the payment/fee/credit ledger)",
    "trino": "federated SQL that joins systems in ONE query without copying data: warehouse (Postgres marts), "
             "billing_oracle (Oracle invoices, transactions), care_mssql (SQL Server care orders, agents), "
             "BigQuery projects as bq_<project> catalogs, and lake; every local customer table joins on ban",
    "catalog": "the data catalog (DataHub + Atlan API): catalog_discover for panoramic multi-term discovery with "
               "fully qualified catalog.schema.table names and has_ban, catalog_join_map for account/location/event "
               "join keys, descriptions, glossary terms and PII tags — before writing SQL",
    "bigquery": "BigQuery datasets and read-only REST queries, plus the logged-in BigQuery Studio UI fallback "
                "(bigquery_ui_query / bigquery_ui_results / bigquery_ui_export) for the user's own projects",
    "atlan": "the Atlan data catalog (search, lineage, glossary) plus the logged-in UI fallbacks: "
             "atlan_multi_search for a true API DSL OR/AND query when the token works; atlan_ui_search when "
             "the API token is missing/invalid; atlan_discover_assets for panoramic multi-term discovery "
             "across churn/noisy/care/usage/network/dispatch etc.; atlan_ui_join_map for shared keys across "
             "candidates; and atlan_ui_asset for a table's real column schema",
    "m365": "Microsoft 365: Outlook mail, calendar, Teams, OneDrive/SharePoint files, people",
    "powerbi": "Power BI workspaces, reports, datasets, DAX, plus the logged-in UI fallback (powerbi_ui_*)",
    "mlflow": "MLflow experiments, models, leaderboard",
    "rag": "the DataPlatform knowledge base (runbooks, designs) with citations", "sap": "SAP BRIM dispute look-ups",
}


AGENT_PRESETS = {
    "sql-analyst": {
        "name": "SQL Analyst",
        "description": "Disambiguate → Atlan discovery (UI fallback) → fully qualified BigQuery → run, peek, export",
        "model": "sql-default",
        "groups": ["atlan", "bigquery", "catalog", "trino", "warehouse", "browser"],
        "max_steps": 12,
        "temperature": 0,
        "system": (
            "You are the SQL Analyst. Turn a business question into ONE correct, read-only BigQuery "
            "(GoogleSQL) query, run it, and report grounded results.\n\n"
            "1. Disambiguate first. Restate the metric, grain (BAN/account/invoice/day), time window, filters and "
            "comparison. If a term is ambiguous ('offender', 'churn', 'active', 'revenue', 'repeat'), state the "
            "definition you will use; ask at most one short question if it changes the query materially.\n"
            "2. Discover panoramically — never search one literal word. Decompose the question into entities, metrics, "
            "filters and synonyms (churn/inactive/retention; noisy/calls/call center/contact center/complaints/"
            "tickets/dispatch/truck roll; care/service/agent; usage/consumption; network/outage/plant; "
            "account/customer/BAN; billing/invoice/payment; NPS/risk). Call catalog_discover(question=..., terms=[...]) "
            "for fully qualified catalog.schema.table names with has_ban — the appliance warehouse/marts/raw tables "
            "are here — and atlan_discover_assets(question=..., terms=[...]) for Atlan-only assets. Do not search "
            "'duty of care' literally; map it to care/service interactions. Then call catalog_join_map(tables=[...]) "
            "and/or atlan_ui_join_map(assets=[...]) to find shared keys (ban, ban_clean, acct_master_sk, "
            "src_bill_acct_num, accountid, customer_id, clli) and date/metric columns. For a single known table, "
            "atlan_ui_asset(url=...) or catalog_describe gets its real columns. Cite the table names and asset URL.\n"
            "3. Build one fully qualified query in the engine that owns the tables: `catalog.schema.table` for the "
            "warehouse/Trino (use trino_query) or `project.dataset.table` for BigQuery (use bigquery_run_query, or "
            "bigquery_ui_query when the REST identity cannot reach the project). Join as many tables as the question "
            "needs, qualifying every table and using the keys you verified with catalog_join_map / atlan_ui_join_map. "
            "The BigQuery UI's current project is not assumed.\n"
            "4. Learn text codes before filtering: SELECT DISTINCT col ... LIMIT 20. Aggregate in SQL; do not pull "
            "raw PII. Check that the numeric measure you sum is populated on the filtered rows (a similarly named "
            "column may be the payment amount, not the event amount), and do not call a column unique until you "
            "have checked uniqueness. For churn/inactive questions, prefer a wide feature mart with the churn label "
            "and pre-aggregated signals (calls_30d, tickets_month, dispatches_90d, disputes_90d, outage_minutes, "
            "nps_last, churned_next_90d); filter the latest snapshot/month and the not-churned label (e.g. "
            "churned_next_90d = false) to exclude already-churned accounts, and join risk rules on ban + snapshot "
            "month. For a 'most at-risk' list, rank by a transparent combination of risk score and noise signals "
            "(calls, tickets, dispatch, disputes, outage, NPS), not call+ticket counts alone. Use raw "
            "calls/tickets/dispatch tables only for metrics the mart lacks. When the user says 'last "
            "month', use the latest complete month present in the data (check MAX(date/month) first), not CURRENT_DATE "
            "minus 30 by assumption. If a query returns no rows or a surprising result, check the filter values and "
            "retry (up to twice) before concluding.\n"
            "5. Report numbers first, then a short 'How I got this' naming the systems/tables and the asset URL, and "
            "finish with the exact query in a ```sql block. Never state a number that did not come from a tool "
            "result in this conversation. For a portable extract, call bigquery_ui_export and give the path.\n"
            "6. Do not stop at 'I cannot'. If the Atlan API token is missing, use the UI fallback. If the REST "
            "BigQuery identity cannot reach a prod project, use the UI fallback. If a UI element is missing, inspect "
            "the live browser or say exactly what the tool reported."
        ),
    },
    "bigquery-analyst": {
        "name": "BigQuery Analyst",
        "description": "Atlan UI discovery → fully qualified BigQuery → run/peek/export in the logged-in UI (no warehouse, no hardcoded SQL)",
        "model": "sql-default",
        "groups": ["atlan", "bigquery", "browser"],
        "max_steps": 12,
        "temperature": 0,
        "system": (
            "You are the BigQuery Analyst. Answer from BigQuery assets discovered through Atlan and run "
            "through the logged-in BigQuery Studio UI (or the BigQuery REST tool only when it has access). Do not use "
            "the local warehouse/Trino path unless the user explicitly asks for it.\n\n"
            "1. Disambiguate first. Restate the metric, grain (BAN/account/invoice/day), time window, filters and "
            "comparison. If a term is ambiguous ('offender', 'churn', 'inactive', 'noisy', 'at risk', 'active'), "
            "state the definition you will use; ask one short question only if it changes the query materially.\n"
            "2. Discover in Atlan; never start from a hardcoded table list or a single literal search. Decompose the "
            "question into entities, metrics, filters and synonyms and call atlan_discover_assets(question=..., "
            "terms=[...]) with all of them: churn/inactive/tuned inactive/attrition; noisy/calls/call center/contact "
            "center/complaints/tickets/dispatch/truck roll; care/service/duty of care; usage/consumption; "
            "network/outage/plant; customer/account/BAN; billing/invoice/payment; NPS/risk. Do not search 'CARE' or "
            "'duty of care' as a literal table name. If atlan_multi_search is available (API token provisioned), "
            "use one atlan_multi_search(terms=[...], match='any') call instead of many UI searches; otherwise the "
            "UI per-term discovery above is intentional because the command palette does not honor OR. For every "
            "promising asset call atlan_ui_asset(url=...) or atlan_ui_join_map(assets=[...]) to read the real "
            "columns and keys. The Atlan database is the BigQuery project, the schema is the dataset, the name is "
            "the table — fully qualify it as `project.dataset.table`.\n"
            "3. Build the SQL dynamically from the discovered columns. Never hardcode a query and never assume a column "
            "exists. Qualify every table. Join on verified keys; when names differ (BAN, BAN_CLEAN, ACCT, "
            "ACCT_MASTER_SK, SRC_BILL_ACCT_NUM, CUSTOMER_ID, END_USER_ID, CLLI), use the join map and map through the "
            "customer/account table. Before filtering on a text code, profile it with SELECT DISTINCT ... LIMIT 20. "
            "When the user says 'last month', use the latest complete month present in the data, not CURRENT_DATE.\n"
            "4. Run it with bigquery_ui_query(sql=...) — the logged-in BigQuery Studio tab is the primary runner. Use "
            "bigquery_run_query only if you have confirmed the REST identity can access the project. Use "
            "bigquery_ui_results to re-read the current result and bigquery_ui_export to save CSV/JSON. All paths are "
            "read-only: exactly one SELECT/WITH statement.\n"
            "5. Report numbers first, then 'How I got this' naming the Atlan assets/URLs and the exact SQL run. Never "
            "state a number that did not come from a tool result in this conversation. If a table or column is "
            "missing, say exactly what you discovered and what failed; do not fall back to the warehouse and do not "
            "invent a query."
        ),
    },
}


def tool_guidance(tools: list[dict]) -> str:
    groups = sorted({t["group"] for t in tools})
    lines = [f"- {g}: {GROUP_HINTS.get(g, 'tools')}" for g in groups]
    return ("You have these tools available now:\n" + "\n".join(lines) + "\n"
            "Use the relevant tool whenever a request needs it, instead of saying you cannot do it. "
            "Tool availability and policies can change between turns: if a tool failed earlier in this "
            "conversation, try it again rather than assuming it still fails. If a tool reports a block or "
            "error, say exactly what it reported. API tools (e.g. atlan_search_assets, bigquery_run_query) "
            "may be intentionally absent while credentials are provisioned; the logged-in UI tools "
            "(atlan_ui_*, atlan_discover_assets, bigquery_ui_*) remain available — use those instead of "
            "saying the capability is unavailable.")


SQL_PLAYBOOK = (
    "When a question needs data, answer it with SQL yourself (text-to-SQL); never ask the user to write SQL.\n"
    "1. Discover first, before any query: {discover}. Use only column names you have seen; never guess them. "
    "Before filtering on a text code (status, category, type, technology), look up its actual values with "
    "SELECT DISTINCT ... LIMIT 20 (values are often lower case, e.g. 'paid', 'late').\n"
    "2. Write ONE query that answers the whole question. It may join and aggregate many tables{federate}. "
    "Join customer data on ban (a 10-digit string). Aggregate in SQL rather than pulling raw rows.\n"
    "3. Run it. If it errors, read the message, fix the query and retry (up to twice). If it returns no rows or "
    "a surprising result, do not conclude yet: check each filter's values with SELECT DISTINCT and rerun. "
    "Map business words to real codes (for example 'open' orders = statuses that are not completed or cancelled; "
    "'unpaid' invoices = status other than paid) and say which codes you used.\n"
    "Never state a number that did not come from a tool result in this conversation.\n"
    "4. Answer with the numbers first, then a short 'How I got this' line naming the systems used, then the SQL "
    "in a ```sql block.{explain}")


CATALOG_FIRST = ("for business terms (BAN, churn, invoice, dispute, wire center, CLLI, copper, fiber, ...) or any "
                 "BigQuery data, discover panoramically first: call catalog_discover(question=..., terms=[...]) for "
                 "fully qualified catalog.schema.table names with has_ban (the warehouse/marts/raw tables), and "
                 "atlan_discover_assets(question=..., terms=[...]) for Atlan-only assets; then catalog_join_map / "
                 "atlan_ui_join_map to find shared keys. For a single asset use atlan_search_assets (on 401/403 "
                 "atlan_ui_search) and atlan_ui_asset(url=...) for its real columns; catalog_describe gives catalog "
                 "columns. Then ")
BQ_RULE = (" BigQuery catalogs (bq_*) hold real enterprise data: aggregate only (COUNT(*), optionally with simple "
           "WHERE filters), never list rows, never SELECT DISTINCT on them, and never touch columns tagged "
           "'PII (suspected)'. Treat '(AI-suggested)' descriptions as hints, not facts.")


BQ_UI_ADDENDUM = (
    "BigQuery UI fallback (the logged-in desktop has a BigQuery Studio tab): if bigquery_run_query cannot reach the "
    "project or returns 401/403/404, call bigquery_ui_query(sql=...) — it types the query into BigQuery Studio, runs "
    "it, and returns the JSON-tab rows. Fully qualify `project.dataset.table`; do not assume the UI's current "
    "project. Use bigquery_ui_results to re-read the current result and bigquery_ui_export to save CSV/JSON. For "
    "discovery: for a complex question call catalog_discover(question=..., terms=[...]) for fully qualified "
    "catalog.schema.table names with has_ban, and atlan_discover_assets(question=..., terms=[...]) for Atlan-only "
    "assets; then catalog_join_map(tables=[...]) / atlan_ui_join_map(assets=[...]) for shared keys. For a single "
    "asset use atlan_search_assets/atlan_ui_search and atlan_ui_asset(url=...) for the real columns. Never search "
    "one literal phrase like 'CARE' and never guess a column or a code value. The BigQuery UI/REST path is separate "
    "from Trino's bq_* catalogs: on the UI/REST path you may use SELECT DISTINCT ... LIMIT 20 to learn text codes, "
    "but still aggregate and avoid raw PII."
)


def sql_playbook(groups: set[str]) -> str:
    base = ""
    if "trino" in groups:
        cat = "catalog" in groups
        base = SQL_PLAYBOOK.format(
            discover=(CATALOG_FIRST if cat else "") + "call trino_sources to see every connected system, its tables "
                     "and columns, and which tables carry ban",
            federate=" across systems (warehouse, billing_oracle, care_mssql, lake, bq_<project>) by qualifying them "
                     "as catalog.schema.table; prefer trino_query whenever the question spans more than one system."
                     + BQ_RULE,
            explain=" When the question is about how the data was combined, or the user asks, call trino_explain "
                    "and say which systems were read and what was pushed down to each.")
    elif "warehouse" in groups:
        base = SQL_PLAYBOOK.format(discover="call warehouse_list_tables (marts first)", federate="", explain="")
    elif "bigquery" in groups:
        base = SQL_PLAYBOOK.format(
            discover="call catalog_search for the business term, or atlan_search_assets (then atlan_ui_asset for "
                     "the table's columns); use bigquery_list_datasets / bigquery_describe_table only when the REST "
                     "identity works",
            federate="", explain="")
    if "bigquery" in groups:
        base = (base + "\n\n" + BQ_UI_ADDENDUM).strip()
    return base


def with_guidance(msgs: list[dict], tools: list[dict]) -> list[dict]:
    """Prepend (or extend the existing) system message with the current tool guidance."""
    if not tools:
        return msgs
    note = tool_guidance(tools)
    playbook = sql_playbook({t["group"] for t in tools})
    if playbook:
        note += "\n\n" + playbook
    if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
        return [{**msgs[0], "content": msgs[0]["content"].rstrip() + "\n\n" + note}] + msgs[1:]
    return [{"role": "system", "content": note}] + msgs


SQL_TOOLS = {"trino_query", "warehouse_query", "bigquery_query", "bigquery_ui_query"}
_SQL_BLOCK = re.compile(r"```sql\s*(.*?)```", re.S | re.I)


def _norm_sql(q: str) -> str:
    return re.sub(r"\s+", " ", (q or "").strip().rstrip(";")).lower()


def unexecuted_sql(content: str, executed: set[str]) -> str | None:
    """The first ```sql block in a final answer that no SQL tool actually ran this turn (None when all ran)."""
    for block in _SQL_BLOCK.findall(content or ""):
        if _norm_sql(block) and _norm_sql(block) not in executed:
            return block.strip()
    return None


GROUNDING_NUDGE = ("You wrote SQL in your answer that was never run, so its numbers are not grounded. Run exactly "
                   "that query now with the SQL tool, then answer using only the rows it returns.")


async def run(messages: list[dict], model: str, groups: list[str] | None = None, max_steps: int = 8,
              temperature: float | None = None, caller: str = "playground",
              attach_images: bool = False, thumbnails: bool = True) -> AsyncIterator[dict]:
    msgs = [dict(m) for m in messages]
    try:
        cm = client(caller=caller)
        session = await cm.__aenter__()
    except Exception as e:  # noqa: BLE001
        yield {"type": "error", "message": f"MCP server not reachable at {mcp_url()}: {type(e).__name__}: {e}"}
        return
    try:
        all_tools = await list_tools_with(session)
        tools = [t for t in all_tools if not groups or t["group"] in groups]
        yield {"type": "tools", "tools": [{"name": t["name"], "group": t["group"]} for t in tools]}
        oa_tools = to_openai_tools(tools)
        msgs = with_guidance(msgs, tools)
        executed: set[str] = set()   # SQL actually run this turn (grounding check)
        nudged = False
        for step in range(1, max_steps + 1):
            yield {"type": "step", "n": step}
            req: dict[str, Any] = {"model": model, "messages": msgs}
            if oa_tools:
                req["tools"] = oa_tools
            if temperature is not None:
                req["temperature"] = temperature
            try:
                res, info = await rt.chat(req, caller=caller)
            except rt.RouteError as e:
                yield {"type": "error", "message": str(e), "attempts": e.attempts}
                return
            yield {"type": "route", **{k: info[k] for k in ("provider", "model", "latency_ms")},
                   "usage": res.get("usage")}
            msg = res["message"]
            msgs.append({k: v for k, v in msg.items() if v is not None})
            calls = msg.get("tool_calls") or []
            if not calls:
                pending = unexecuted_sql(msg.get("content") or "", executed) if any(
                    t["name"] in SQL_TOOLS for t in tools) else None
                if pending and not nudged and step < max_steps:
                    nudged = True   # one retry: make the model run the SQL it showed instead of guessing results
                    yield {"type": "thought", "content": "Checking: running the SQL from the draft answer before replying."}
                    msgs.append({"role": "user", "content": GROUNDING_NUDGE})
                    continue
                yield {"type": "message", "content": msg.get("content") or ""}
                yield {"type": "done", "messages": msgs}
                return
            if msg.get("content"):
                yield {"type": "thought", "content": msg["content"]}
            extra_images: list[str] = []
            for tc in calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except ValueError:
                    args = {}
                yield {"type": "tool_call", "id": tc.get("id"), "name": name, "group": group_of(name), "args": args}
                if name in SQL_TOOLS and isinstance(args.get("sql"), str):
                    executed.add(_norm_sql(args["sql"]))
                t0 = time.monotonic()
                try:
                    r = await session.call_tool(name, args, raise_on_error=False)
                    text, images, is_err = _result_parts(r)
                except Exception as e:  # noqa: BLE001
                    text, images, is_err = f"{type(e).__name__}: {e}", [], True
                ms = int((time.monotonic() - t0) * 1000)
                thumb = None
                if group_of(name) == "browser" and name != "browser_screenshot" and not images and thumbnails:
                    try:   # a thumbnail for the UI timeline only (not sent to the model)
                        _t, timgs, _e = _result_parts(await session.call_tool("browser_screenshot", {"thumbnail": True},
                                                                              raise_on_error=False))
                        thumb = timgs[0] if timgs else None
                    except Exception:  # noqa: BLE001
                        thumb = None
                yield {"type": "tool_result", "thumbnail": thumb or (images[0] if images else None), "id": tc.get("id"), "name": name, "group": group_of(name), "ok": not is_err,
                       "ms": ms, "text": text[:4000], "images": images[:2]}
                body = text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n…[truncated]"
                if images and not body:
                    body = f"[{len(images)} image(s) returned]"
                msgs.append({"role": "tool", "tool_call_id": tc.get("id"), "name": name, "content": body or "(empty)"})
                extra_images += images
            if attach_images and extra_images:
                msgs.append({"role": "user", "content": [{"type": "text", "text": "Screenshot(s) from the last tool call:"}]
                             + [{"type": "image_url", "image_url": {"url": u}} for u in extra_images[-1:]]})
        yield {"type": "error", "message": f"stopped after {max_steps} steps"}
        yield {"type": "done", "messages": msgs}
    finally:
        with contextlib.suppress(Exception):
            await cm.__aexit__(None, None, None)


async def list_tools_with(session) -> list[dict]:
    tools = await session.list_tools()
    out = []
    for t in tools:
        ann = getattr(t, "annotations", None)
        out.append({"name": t.name, "group": group_of(t.name), "description": (t.description or "").strip(),
                    "read_only": getattr(ann, "readOnlyHint", None) if ann else None,
                    "input_schema": dict(t.inputSchema or {"type": "object", "properties": {}})})
    return sorted(out, key=lambda x: x["name"])


def b64_image(data: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"
