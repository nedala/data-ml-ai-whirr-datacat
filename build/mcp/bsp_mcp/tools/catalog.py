"""Atlan (data catalog), Power BI, SAP BRIM (read-only) — thin wrappers over bsp_connectors."""
from __future__ import annotations

import asyncio
import os
import re
from typing import Annotated, Literal

from pydantic import Field

from ..common import UserError, clamp, connector_mode, connectors, md_table, result, tool


def _atlan_base() -> str:
    return (os.environ.get("ATLAN_BASE_URL") or "https://atlan.example.com").rstrip("/")


def _brief(a: dict) -> dict:
    attrs = a.get("attributes") or {}
    guid = a.get("guid") or attrs.get("guid")
    return {"typeName": a.get("typeName"), "name": a.get("name") or attrs.get("name"),
            "qualifiedName": a.get("qualifiedName") or attrs.get("qualifiedName"),
            "guid": guid,
            "url": f"{_atlan_base()}/assets/{guid}/overview" if guid else "",
            "description": (a.get("description") or attrs.get("description") or attrs.get("userDescription") or "")[:200],
            "owners": attrs.get("ownerUsers") or a.get("ownerUsers"),
            "certificate": attrs.get("certificateStatus") or a.get("certificateStatus"),
            "classifications": a.get("classificationNames")}


# ── Atlan ────────────────────────────────────────────────────────────────────
@tool("atlan", "Search Atlan (API first; automatic browser-UI fallback if the token is missing/invalid)", open_world=True)
async def atlan_search_assets(
    query: Annotated[str, Field(description="Free text: table, column, metric or business term, e.g. 'churn'")],
    type_names: Annotated[list[str] | None, Field(description="Filter by type: Table, View, Column, AtlasGlossaryTerm, "
                                                              "PowerBIDataset, DbtModel …")] = None,
    limit: Annotated[int, Field(description="Max results (1–50)")] = 15,
) -> dict:
    """Search Atlan for data assets (tables, columns, dbt models, dashboards, glossary terms) — owners,
    certification and descriptions included. The API is tried first. If it returns 401/403 (no valid
    ATLAN_API_TOKEN), this automatically falls back to the logged-in Linux Desktop browser UI
    (atlan_ui_search), so a guest session can still discover assets."""
    try:
        rows = [_brief(a) for a in connectors("atlan").search_assets(query, type_names=type_names, limit=clamp(limit, 1, 50))]
        return result(f"{len(rows)} asset(s) for {query!r}\n\n" + md_table(rows, 20),
                      mode=connector_mode("atlan"), assets=rows)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if not any(x in msg for x in ("401", "403", "Unauthorized", "Forbidden")):
            raise UserError(f"Atlan API search failed: {msg[:300]}") from None
        kind = "all"
        for t in (type_names or []):
            tl = str(t).strip().lower()
            if tl in ("atlasglossaryterm", "term"):
                kind = "Term"; break
            if tl in ("table", "view", "column", "dashboard", "report", "dataset"):
                kind = str(t); break
        try:
            res = await atlan_ui_search(query, kind=kind, limit=min(clamp(limit, 1, 20), 20))
        except Exception as ui_err:  # noqa: BLE001
            raise UserError(f"Atlan API returned {msg[:140]}; browser-UI fallback failed: {str(ui_err)[:220]}") from None
        res = dict(res)
        res["assets"] = res.get("results", [])
        res["mode"] = "ui-fallback"
        res["api_error"] = msg[:200]
        return res


@tool("atlan", "Multi-term Atlan API search (Elasticsearch DSL OR/AND)", open_world=True)
def atlan_multi_search(
    terms: Annotated[list[str], Field(description="Terms to search in ONE query, e.g. ['churn','tickets','dispatch','care']")],
    type_names: Annotated[list[str] | None, Field(description="Filter by type: Table, View, Column, AtlasGlossaryTerm …")] = None,
    limit: Annotated[int, Field(description="Max results (1–50)")] = 25,
    match: Annotated[Literal["any", "all"], Field(description="any = OR (bool.should); all = AND (bool.must)")] = "any",
) -> dict:
    """One Atlan Elasticsearch-DSL query across several terms. `match='any'` is a true OR
    (`bool.should`, minimum_should_match=1); `match='all'` is AND. This is the efficient
    multi-term path when ATLAN_API_TOKEN works — the UI command palette does not reliably
    implement boolean OR. The tool is hidden while API tools are disabled; use
    atlan_discover_assets / atlan_ui_search for the logged-in UI fallback."""
    ts = [str(t).strip() for t in (terms or []) if str(t).strip()]
    if not ts:
        raise UserError("give at least one term")
    rows = [_brief(a) for a in connectors("atlan").multi_search(ts, type_names=type_names,
                                                               limit=clamp(limit, 1, 50), match=match)]
    return result(f"{len(rows)} asset(s) for {match} terms {ts}\n\n" + md_table(rows, 40),
                  mode=connector_mode("atlan"), terms=ts, match=match, assets=rows)


@tool("atlan", "Get one Atlan asset", open_world=True)
def atlan_get_asset(qualified_name: Annotated[str, Field(description="Asset qualifiedName (from atlan_search_assets)")]) -> dict:
    """Full detail of one asset: attributes, columns, owners, classifications, glossary meanings."""
    a = connectors("atlan").get_asset(qualified_name)
    return result(f"{a.get('typeName')} {a.get('name') or qualified_name}", mode=connector_mode("atlan"), asset=a)


@tool("atlan", "Lineage of an Atlan asset", open_world=True)
def atlan_lineage(
    qualified_name: Annotated[str, Field(description="Asset qualifiedName")],
    direction: Annotated[Literal["upstream", "downstream", "both"], Field(description="Which way")] = "both",
    depth: Annotated[int, Field(description="Hops (1–5)")] = 3,
) -> dict:
    """Upstream sources and downstream consumers (dbt models, dashboards) of an asset."""
    lin = connectors("atlan").lineage(qualified_name, direction=direction, depth=clamp(depth, 1, 5))
    n = len(lin.get("guidEntityMap") or lin.get("nodes") or lin.get("assets") or [])
    return result(f"lineage of {qualified_name}: {n} related asset(s)", mode=connector_mode("atlan"), lineage=lin)


@tool("atlan", "Look up a business glossary term", open_world=True)
def atlan_glossary_lookup(term: Annotated[str, Field(description="Business term, e.g. 'ARPU', 'churn', 'fiber penetration'")]) -> dict:
    """Find glossary definitions for a business term and the assets linked to it."""
    rows = connectors("atlan").search_assets(term, type_names=["AtlasGlossaryTerm"], limit=10)
    out = []
    for r in rows:
        attrs = r.get("attributes") or {}
        out.append({"name": r.get("name") or attrs.get("name"), "qualifiedName": r.get("qualifiedName"),
                    "definition": r.get("description") or attrs.get("description") or attrs.get("longDescription"),
                    "linked_assets": [m.get("qualifiedName") or m for m in (attrs.get("assignedEntities") or r.get("assignedEntities") or [])][:20]})
    if not out:
        return result(f"no glossary term matches {term!r}", mode=connector_mode("atlan"), terms=[])
    return result("\n".join(f"- **{t['name']}**: {t['definition'] or ''}" for t in out), mode=connector_mode("atlan"), terms=out)


@tool("atlan", "Search Atlan through the logged-in Linux Desktop browser (no API token)", open_world=True)
async def atlan_ui_search(
    query: Annotated[str, Field(description="Search text, e.g. 'churn', 'billing calls', 'ALTNPAXA'")],
    kind: Annotated[str, Field(description="Atlan type filter: all, Table, View, Column, Term, Report, Dataset")] = "all",
    limit: Annotated[int, Field(description="Max results (1–20)")] = 10,
) -> dict:
    """Fallback while the Atlan API token is unavailable: drive the appliance's
    logged-in Chromium (Linux Desktop) and read Atlan's search dialog. Returns
    UI-scraped results with the asset/glossary URL. Prefer atlan_search_assets
    once a valid API token is configured; this is slower and page-layout
    dependent, and the guest role may not expose every asset type."""
    from . import browser as ui  # noqa: PLC0415

    n = clamp(limit, 1, 20)
    tabs = await ui.browser_tabs()
    tab = next((t["tab"] for t in tabs.get("tabs", []) if "atlan.com" in (t.get("url") or "")), None)
    if tab is None:
        opened = await ui.browser_new_tab(_atlan_base() + "/")
        tab = opened.get("tab")
    page = await ui._page(tab)
    try:
        # Always start from Atlan home so the page search input is visible.
        await page.goto(_atlan_base() + "/", wait_until="domcontentloaded")
        await page.wait_for_selector("input[placeholder='Search all your assets']", timeout=10_000)
        await page.keyboard.press("Escape")
        # Click the page search box, then type one char to open the command palette.
        await page.get_by_placeholder("Search all your assets").click()
        await page.keyboard.type("x")
        await page.wait_for_selector("[role=dialog]", state="visible", timeout=10_000)
        # Put the real query into the visible modal input (the page input loses focus).
        modal = page.locator("[role=dialog]:visible input[placeholder='Search all assets']").first
        if not await modal.count():
            modal = page.locator("[role=dialog]:visible input[type=text]").first
        await modal.fill(query)
        await page.wait_for_timeout(1800)
        # Click the Atlan type tab (All, Table, View, Column, Term, …). Match by label: the dialog has
        # responsive duplicate tab groups, so an index is not reliable. Filtering is also redone below.
        label = {"all": "All", "table": "Table", "view": "View", "column": "Column", "term": "Term",
                 "dashboard": "Dashboard", "tile": "Tile", "report": "Report",
                 "dataset": "Dataset"}.get((kind or "all").strip().lower())
        if label:
            try:
                dlg = page.locator("[role=dialog]:visible").first
                tabs = dlg.get_by_role("tab", name=re.compile(rf"^{label}\b", re.I))
                for i in range(await tabs.count()):
                    tab = tabs.nth(i)
                    try:
                        if await tab.is_visible():
                            await tab.click(timeout=6000)
                            await page.wait_for_timeout(1500)
                            break
                    except Exception:  # noqa: BLE001 — a covered/hidden duplicate; keep looking
                        continue
            except Exception:  # noqa: BLE001 — type tabs are a narrowing aid, not required
                pass
        try:
            await page.wait_for_selector("[role=dialog] a[href^='/assets/'], [role=dialog] a[href^='/glossary/']",
                                         timeout=10_000)
        except Exception:  # noqa: BLE001 — no structured links; return the dialog text below
            pass
        dlg = page.locator("[role=dialog]:visible").first
        raw = await dlg.locator("a[href^='/assets/'], a[href^='/glossary/']").evaluate_all(
            """els => els.map(e => {
                let n = e;
                for (let i = 0; i < 4 && n; i++) n = n.parentElement;
                const lines = ((n?.innerText) || '').split(/\\n+/).map(s => s.trim()).filter(Boolean);
                return {
                    name: (e.innerText || '').trim(),
                    href: e.getAttribute('href') || '',
                    lines: lines.slice(0, 4)
                };
            })"""
        )
        rows, seen = [], set()
        for r in raw:
            href = r.get("href") or ""
            if not href.startswith(("/assets/", "/glossary/")) or href in seen:
                continue
            seen.add(href)
            lines = r.get("lines") or []
            name = lines[0] if lines else (r.get("name") or "(unnamed)")
            typ = lines[1] if len(lines) > 1 else (
                "Term" if href.startswith("/glossary/") else
                "Column" if "column=" in href else "Field" if "field=" in href else "Asset")
            if (kind or "all").strip().lower() not in ("", "all") and (kind or "").lower() not in typ.lower():
                continue
            rows.append({"name": name, "type": typ,
                         "database": lines[2] if len(lines) > 2 else "",
                         "schema": lines[3] if len(lines) > 3 else "",
                         "url": _atlan_base() + href})
            if len(rows) >= n:
                break
        if not rows:
            text = (await dlg.inner_text())[:2000]
            return result(f"No structured Atlan UI result for {query!r} (kind={kind}). Search dialog text:\n\n{text}",
                          results=[], source="atlan-ui")
        return result(f"{len(rows)} Atlan UI result(s) for {query!r} (kind={kind}) — logged-in desktop browser "
                      f"(guest session; API-token fallback). For a table's columns, call "
                      f"atlan_ui_asset(url=<result url>).\n\n" + md_table(rows, 40),
                      results=rows, source="atlan-ui")
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Atlan UI search failed: {type(e).__name__}: {str(e)[:300]}") from None


_COLUMN_TYPES = {"STRING", "NUMERIC", "TIMESTAMP", "DATE", "BOOL", "INT64", "FLOAT64",
                 "BIGNUMERIC", "BYTES", "RECORD", "TIME", "DATETIME", "GEOGRAPHY", "JSON"}
_COLUMN_MODES = {"NULLABLE", "REQUIRED", "REPEATED"}


def _col_parts(cell: str) -> list[str]:
    parts = [x.strip() for x in (cell or "").splitlines() if x.strip() and x.strip() != "Add"]
    return parts if len(parts) >= 3 else (cell or "").split()


_SECTION_LABELS = {"Overview", "Columns", "Lineage", "Table Summary", "Rows", "Connection",
                   "Description", "Certificate", "Owners", "Column preview", "Sample data", "Properties",
                   "Classification", "Activity", "Resources", "Queries", "Data Freshness & Quality",
                   "Ownership & Responsibility", "Retention & Compliance", "Source System",
                   "Technical Metadata"}


def _after(lines: list[str], label: str) -> str:
    try:
        i = lines.index(label)
    except ValueError:
        return ""
    value = lines[i + 1] if i + 1 < len(lines) else ""
    return "" if value in _SECTION_LABELS else value


async def _click_columns_tab(page) -> bool:
    try:
        loc = page.get_by_role("tab", name=re.compile(r"^Columns", re.I))
        for i in range(await loc.count()):
            tab = loc.nth(i)
            try:
                if await tab.is_visible():
                    await tab.click()
                    return True
            except Exception:  # noqa: BLE001 — try the next duplicate responsive tab
                continue
    except Exception:  # noqa: BLE001
        pass
    for loc in (page.get_by_role("link", name=re.compile(r"^Columns", re.I)),
                page.get_by_text("Columns", exact=False)):
        try:
            if await loc.count():
                target = loc.first
                if await target.is_visible():
                    await target.click()
                    return True
        except Exception:  # noqa: BLE001
            continue
    return False


@tool("atlan", "Read an Atlan asset's columns/schema through the logged-in browser (no API token)", open_world=True)
async def atlan_ui_asset(
    url: Annotated[str, Field(description="Atlan asset URL from atlan_ui_search, e.g. "
                                            "https://atlan.example.com/assets/<guid>/overview")] = "",
    name: Annotated[str, Field(description="Table name or qualifiedName, used to search when url is blank")] = "",
    include_columns: Annotated[bool, Field(description="Open the Columns tab and read the full schema (default true)")] = True,
) -> dict:
    """Open one Atlan asset in the logged-in desktop browser and return its metadata plus every column
    (name, data type, mode, description when shown). Use this after atlan_ui_search when the API token is
    invalid/unavailable: search finds the asset, this reads the schema. If `url` is blank, `name` is searched
    first. The Columns tab is scrolled until all rows are loaded, so large schemas are not truncated."""
    from . import browser as ui  # noqa: PLC0415

    if not url and not name:
        raise UserError("give url (from atlan_ui_search) or name")
    if not url:
        # Atlan's search box does not match a dotted qualifiedName well; search the table name first.
        query = str(name).strip().rsplit(".", 1)[-1] or str(name).strip()
        found = await atlan_ui_search(query, kind="Table", limit=10)
        matches = found.get("results") or []
        if not matches and query != str(name).strip():
            found = await atlan_ui_search(str(name).strip(), kind="Table", limit=10)
            matches = found.get("results") or []
        if not matches:
            raise UserError(f"no Atlan UI result for {name!r}; call atlan_ui_search to inspect matches")
        tail = str(name).strip().rsplit(".", 1)[-1].lower()
        pick = next((r for r in matches if str(r.get("name") or "").strip().lower() == tail), None)
        if pick is None:
            pick = next((r for r in matches if tail in str(r.get("name") or "").lower()), matches[0])
        url = str(pick.get("url") or "")
        if not url:
            raise UserError(f"Atlan UI result for {name!r} has no asset URL")
    try:
        tabs = await ui.browser_tabs()
        tab = next((t["tab"] for t in tabs.get("tabs", []) if "atlan.com" in (t.get("url") or "")), None)
        if tab is None:
            opened = await ui.browser_new_tab(url)
            tab = opened.get("tab")
        page = await ui._page(tab)
        if page.url.rstrip("/") != url.rstrip("/"):
            await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(4500)

        body = await page.locator("body").inner_text()
        lines = [x.strip() for x in body.splitlines() if x.strip()]
        title = (await page.title()).split(" | ")[0].strip()
        ti = next((i for i, x in enumerate(lines) if x == title), None)
        if ti is None:
            ti = next((i for i, x in enumerate(lines) if x.lower() == title.lower()), 0)
        header = lines[ti:ti + 5]
        asset_type = header[1] if len(header) > 1 and header[1].isupper() else ""
        database = header[2] if len(header) > 2 else ""
        schema = header[3] if len(header) > 3 else ""
        asset = {"name": title, "type": asset_type, "database": database, "schema": schema,
                 "url": page.url, "rows": _after(lines, "Rows"), "connection": _after(lines, "Connection"),
                 "description": _after(lines, "Description"), "certificate": _after(lines, "Certificate"),
                 "owners": _after(lines, "Owners")}

        cols: list[dict] = []
        preview_desc: dict[int, str] = {}
        if include_columns:
            # Descriptions live on the Overview preview; capture what it exposes before switching tabs.
            try:
                for ti in range(await page.locator("table").count()):
                    raw = await page.locator("table").nth(ti).locator("tr").evaluate_all(
                        "rs => rs.map(r => [...r.querySelectorAll('td,th')].map(c => (c.innerText||'').trim()))")
                    for r in raw:
                        if len(r) >= 5 and r[0].isdigit() and r[2] in _COLUMN_TYPES and r[3] in _COLUMN_MODES:
                            desc = (r[4] or "").strip()
                            if desc:
                                preview_desc[int(r[0])] = desc
            except Exception:  # noqa: BLE001
                pass

            if await _click_columns_tab(page):
                await page.wait_for_timeout(3500)
                # Atlan renders ~20 rows first; scrolling the .ant-table-body loads the rest.
                # Scroll a few times unconditionally — the virtual list can take >1s to append.
                for _ in range(3):
                    try:
                        await page.evaluate(
                            """() => document.querySelectorAll('.columns-widget .ant-table-body, .ant-table-body')
                                     .forEach(e => { e.scrollTop = e.scrollHeight; })""")
                    except Exception:  # noqa: BLE001
                        pass
                    await page.wait_for_timeout(1400)

                tables = page.locator(".columns-widget table")
                if not await tables.count():
                    tables = page.locator("table")
                best, best_n = None, 0
                for i in range(await tables.count()):
                    n = await tables.nth(i).locator("tr").count()
                    if n > best_n:
                        best, best_n = tables.nth(i), n
                if best is not None:
                    raw = await best.locator("tr").evaluate_all(
                        "rs => rs.map(r => [...r.querySelectorAll('td,th')].map(c => (c.innerText||'').trim()))")
                    by_index: dict[int, dict] = {}
                    for r in raw:
                        if len(r) < 2 or not r[0].isdigit():
                            continue
                        parts = _col_parts(r[1])
                        if len(parts) >= 3 and parts[1] in _COLUMN_TYPES and parts[2] in _COLUMN_MODES:
                            idx = int(r[0])
                            by_index[idx] = {"index": idx, "name": parts[0], "type": parts[1],
                                             "mode": parts[2], "description": preview_desc.get(idx, "")}
                    cols = [by_index[k] for k in sorted(by_index)]

                if not cols:  # last resort: visible-text state machine
                    text = await page.locator("body").inner_text()
                    tl = [x.strip() for x in text.splitlines()]
                    try:
                        start = tl.index("COLUMNS")
                    except ValueError:
                        start = 0
                    by_index = {}
                    i = start + 1
                    while i < len(tl):
                        if tl[i].isdigit():
                            vals, j = [], i + 1
                            while j < len(tl) and len(vals) < 3:
                                if tl[j]:
                                    vals.append(tl[j])
                                j += 1
                            if len(vals) >= 3 and vals[1] in _COLUMN_TYPES and vals[2] in _COLUMN_MODES:
                                idx = int(tl[i])
                                by_index[idx] = {"index": idx, "name": vals[0], "type": vals[1],
                                                 "mode": vals[2], "description": preview_desc.get(idx, "")}
                                i = j
                                continue
                        i += 1
                    cols = [by_index[k] for k in sorted(by_index)]

        if cols:
            rows = [{"#": c["index"], "column": c["name"], "type": c["type"], "mode": c["mode"],
                     **({"description": c["description"]} if c["description"] else {})} for c in cols]
            where = ".".join(x for x in (database, schema) if x)
            summary = (f"{asset_type or 'ASSET'} {title} — {len(cols)} column(s)\n"
                       f"{where + chr(10) if where else ''}{page.url}\n\n" + md_table(rows, 80))
            return result(summary, source="atlan-ui", asset=asset, columns=cols)

        text = (await page.locator("body").inner_text())[:6000]
        note = ("No column table was exposed by this asset page (it may be a glossary term, column, or the "
                "guest role may hide the schema).") if include_columns else "columns not requested"
        return result(f"{asset_type or 'ASSET'} {title} — {note}\n{page.url}\n\n{text}",
                      source="atlan-ui", asset=asset, columns=[])
    except UserError:
        raise
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Atlan UI asset read failed: {type(e).__name__}: {str(e)[:300]}") from None


# ── Panoramic discovery: one question → many candidate assets + join map ─────
_DISCOVERY_RULES = [
    (("churn", "attrition", "retention", "tuned inactive", "inactive", "at risk", "at-risk", "risk"),
     ["churn", "inactive", "retention", "at risk", "risk"]),
    (("noisy", "noise", "complaint", "call", "contact", "interaction", "care", "service", "agent"),
     ["call center", "calls", "contact center", "complaints", "service ticket", "tickets", "care", "agent",
      "service", "interactions"]),
    (("dispatch", "truck roll", "field", "repair", "tech"),
     ["dispatch", "truck roll", "repair", "field operations"]),
    (("usage", "consumption", "bandwidth", "data usage", "voice"),
     ["usage", "consumption", "account daily usage"]),
    (("network", "outage", "incident", "plant", "fiber", "copper", "clli"),
     ["network", "outage", "incident", "ont", "copper", "fiber"]),
    (("account", "customer", "subscriber", "ban", "billing"),
     ["customer", "account", "ban", "subscriber"]),
    (("nps", "satisfaction", "survey", "sentiment"),
     ["nps", "satisfaction", "survey"]),
    (("invoice", "billing", "payment", "chargeback", "dispute"),
     ["invoice", "billing", "payment", "chargeback", "dispute"]),
    (("order", "product", "plan", "speed", "package"),
     ["product", "plan", "service", "order"]),
]
_DISCOVERY_STOP = {"that", "this", "with", "have", "from", "which", "their", "there", "would", "could", "should",
                   "about", "into", "over", "under", "last", "month", "most", "list", "prepare", "compiled",
                   "actually", "look", "see", "already", "since", "likely", "right", "duty", "let", "the", "and",
                   "for", "who", "not", "are", "aka", "etc", "them", "they", "will", "want", "need", "make"}


def _discovery_terms(question: str, extra: list[str] | None, max_terms: int) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(t: str) -> None:
        t = re.sub(r"\s+", " ", str(t or "")).strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            terms.append(t)

    for t in extra or []:
        add(t)
    q = (question or "").lower()
    for triggers, words in _DISCOVERY_RULES:
        if any(tr in q for tr in triggers):
            for w in words:
                add(w)
    if not terms:
        for w in re.findall(r"[a-z][a-z0-9_]{3,}", q):
            if w not in _DISCOVERY_STOP:
                add(w)
    return terms[:max_terms]


def _discovery_row(r: dict, term: str) -> dict:
    attrs = r.get("attributes") or {}
    name = r.get("name") or attrs.get("name") or "(unnamed)"
    typ = r.get("type") or r.get("typeName") or ""
    database = r.get("database") or ""
    schema = r.get("schema") or ""
    qn = r.get("qualifiedName") or attrs.get("qualifiedName") or ""
    url = r.get("url") or ""
    if not url:
        guid = r.get("guid") or attrs.get("guid")
        if guid:
            url = f"{_atlan_base()}/assets/{guid}/overview"
    if not database and qn:
        parts = [p for p in re.split(r"[/.]", qn) if p]
        if len(parts) >= 3:
            database, schema = parts[-3], parts[-2]
        elif len(parts) == 2:
            database, schema = parts
    return {"name": name, "type": typ, "database": database, "schema": schema,
            "qualifiedName": qn, "url": url, "matched_terms": [term]}


@tool("atlan", "Discover many candidate Atlan tables from a question (panoramic multi-term search)", open_world=True)
async def atlan_discover_assets(
    question: Annotated[str, Field(description="The full business question; domain synonyms are expanded from it")] = "",
    terms: Annotated[list[str] | None, Field(description="Explicit extra terms, merged with question-derived terms")] = None,
    kind: Annotated[str, Field(description="Atlan type filter: Table, View, Column, Term, all")] = "Table",
    limit_per_term: Annotated[int, Field(description="Results per term (1–10)")] = 5,
    max_terms: Annotated[int, Field(description="Search terms cap (1–24)")] = 20,
) -> dict:
    """Panoramic discovery for multi-table questions. Decompose the question into domain terms (churn /
    inactive / retention, noisy / calls / call center / contact center / complaints / tickets / dispatch /
    truck roll, care / service / agent, usage / consumption, network / outage / plant, account / customer /
    BAN, billing / invoice / payment, NPS / risk), run an Atlan search for EVERY term (API first, UI fallback),
    dedupe the assets, and return every candidate table/view with database, schema, URL and matched terms.
    This is the tool to use instead of a single literal search such as 'CARE' or 'duty of care'. Follow it
    with atlan_ui_join_map to find shared keys, then write one fully qualified join query."""
    n = clamp(limit_per_term, 1, 10)
    terms_list = _discovery_terms(question, terms, clamp(max_terms, 1, 24))
    if not terms_list:
        raise UserError("give a question with domain words or explicit terms")
    kind_arg = (kind or "Table").strip()
    type_names = None if kind_arg.lower() in ("all", "any", "*") else [kind_arg]
    from ..readiness import disabled_tools  # noqa: PLC0415 — API tools may be disabled
    ui_mode = "atlan_search_assets" in disabled_tools()
    assets: dict[str, dict] = {}
    for term in terms_list:
        try:
            if ui_mode:
                res = await atlan_ui_search(term, kind=kind_arg, limit=n)
            else:
                res = await atlan_search_assets(term, type_names=type_names, limit=n)
                if res.get("mode") == "ui-fallback":
                    ui_mode = True
            rows = res.get("results") or res.get("assets") or []
        except Exception:  # noqa: BLE001 — try the UI directly if the API path failed
            try:
                res = await atlan_ui_search(term, kind=kind_arg, limit=n)
                rows = res.get("results") or []
                ui_mode = True
            except Exception as e2:  # noqa: BLE001
                raise UserError(f"Atlan discovery failed for {term!r}: {str(e2)[:240]}") from None
        for r in rows:
            row = _discovery_row(r, term)
            if not row["name"] or row["name"] == "(unnamed)":
                continue
            key = row["url"] or row["qualifiedName"] or f"{row['database']}.{row['schema']}.{row['name']}"
            if key in assets:
                if term not in assets[key]["matched_terms"]:
                    assets[key]["matched_terms"].append(term)
            else:
                assets[key] = row
    if not assets:
        return result(f"no Atlan assets for terms {terms_list}", source="atlan-ui" if ui_mode else "atlan-api",
                      terms=terms_list, assets=[])
    out = sorted(assets.values(), key=lambda x: (-len(x["matched_terms"]), x["name"].lower()))
    rows = [{"name": x["name"], "type": x["type"], "database": x["database"], "schema": x["schema"],
             "matched_terms": ", ".join(x["matched_terms"])} for x in out[:120]]
    extra = "" if len(out) <= 120 else f"\n… {len(out) - 120} more assets"
    summary = (f"{len(out)} candidate asset(s) from {len(terms_list)} term(s) "
               f"({', '.join(terms_list)})\n\n" + md_table(rows, 120) + extra +
               "\n\nNext: atlan_ui_join_map(assets=[url…]) for columns and join keys.")
    return result(summary, source="atlan-ui" if ui_mode else "atlan-api", terms=terms_list, assets=out)


_KEY_DEFAULTS = ["ban", "ban_clean", "acct_master_sk", "src_bill_acct_num", "accountid", "account_number",
                 "account_id", "customer_id", "subscriber_id", "end_user_id", "contact_id", "clli",
                 "wirecenter_clli", "wire_center", "invoice_id", "opbel", "order_id", "case_id",
                 "ticket_id", "dispatch_id", "session_id"]
_KEY_ROLES = [
    ("account", re.compile(r"(^|_)(ban|ban_clean|acct|account|customer|cust|subscriber|sub|end_user|contact)(_|$)", re.I)),
    ("account", re.compile(r"(src_bill_acct_num|acct_master_sk|accountid|account_number|billing_account|customer_id|"
                           r"subscriber_id|subscriptionid|subscription_id|sub_id|end_user_id|contact_id)", re.I)),
    ("location", re.compile(r"(clli|wire_?center|wirecenter)", re.I)),
    ("invoice", re.compile(r"(invoice|inv_doc|opbel|bill_ref)", re.I)),
    ("event", re.compile(r"(order|case|ticket|dispatch|session|call|transaction).*(id|sk|num|key)", re.I)),
]
_DATE_RE = re.compile(r"(date|_dt$|dt$|_at$|_ts$|month|day|timestamp|time$|start|end|created|updated)", re.I)
_METRIC_RE = re.compile(r"(count|amount|usage|score|nps|churn|inactive|risk|status|type|reason|revenue|arpu|speed|qos)", re.I)


@tool("atlan", "Map join keys and columns across candidate Atlan assets", open_world=True)
async def atlan_ui_join_map(
    assets: Annotated[list[str], Field(description="Asset URLs or qualified names from atlan_discover_assets")],
    keys: Annotated[list[str] | None, Field(description="Join keys to look for; default ban, ban_clean, account_id, customer_id, clli, invoice_id, opbel")] = None,
    include_columns: Annotated[bool, Field(description="Include every column name in each mapped asset (default true)")] = True,
    limit: Annotated[int, Field(description="Max assets to read (1–10)")] = 8,
) -> dict:
    """Read the real schema of several candidate assets (via atlan_ui_asset) and return a join map: which
    assets carry ban / ban_clean / account_id / customer_id / clli / invoice_id / opbel, plus their date and
    metric columns and all column names. Use this before writing a multi-table join so keys and date fields
    are never guessed. Each asset page takes a few seconds; cap `limit` to keep it bounded."""
    wanted = [str(k) for k in (keys or _KEY_DEFAULTS) if str(k).strip()]
    picked = [str(a).strip() for a in (assets or []) if str(a).strip()][:clamp(limit, 1, 10)]
    if not picked:
        raise UserError("give one or more asset URLs/qualified names")
    mapped, errors = [], []
    for asset in picked:
        try:
            r = await atlan_ui_asset(url=asset) if asset.startswith("http") else await atlan_ui_asset(name=asset)
        except Exception as e:  # noqa: BLE001
            errors.append({"asset": asset, "error": f"{type(e).__name__}: {str(e)[:240]}"})
            continue
        info = r.get("asset") or {}
        cols = [c.get("name", "") for c in (r.get("columns") or []) if c.get("name")]
        low = {c.lower(): c for c in cols}
        found = [k for k in wanted if k.lower() in low]
        roles: dict[str, list[str]] = {"account": [], "location": [], "invoice": [], "event": []}
        for c in cols:
            for role, rx in _KEY_ROLES:
                if rx.search(c) and c not in roles[role]:
                    roles[role].append(c)
        mapped.append({
            "asset": info.get("name") or asset,
            "qualified": ".".join(x for x in (info.get("database"), info.get("schema")) if x),
            "url": info.get("url") or asset,
            "n_columns": len(cols),
            "join_keys": found,
            **roles,
            "date_columns": [c for c in cols if _DATE_RE.search(c)],
            "metric_columns": [c for c in cols if _METRIC_RE.search(c)],
            **({"columns": cols} if include_columns else {}),
        })
    if not mapped:
        raise UserError("could not read any of the requested assets: " + "; ".join(e["error"] for e in errors)[:500])
    common = sorted(set.intersection(*[set(x["join_keys"]) for x in mapped])) if mapped else []
    account_common = sorted(set.intersection(*[set(x["account"]) for x in mapped])) if mapped else []
    ban_like = sorted({k for x in mapped for k in x["account"] if k.lower() in ("ban", "ban_clean")})
    rows = [{"asset": x["asset"], "qualified": x["qualified"], "columns": x["n_columns"],
             "join_keys": ", ".join(x["join_keys"]) or "—",
             "account_keys": ", ".join(x["account"][:6]) or "—",
             "location_keys": ", ".join(x["location"][:4]) or "—",
             "event_keys": ", ".join(x["event"][:5]) or "—",
             "date_columns": ", ".join(x["date_columns"][:6]) or "—",
             "metric_columns": ", ".join(x["metric_columns"][:6]) or "—"} for x in mapped]
    summary = (f"{len(mapped)} asset(s) mapped"
               + (f" — exact common keys: {', '.join(common)}" if common else "")
               + (f" — common account keys: {', '.join(account_common)}" if account_common else "")
               + (f" — ban-like keys: {', '.join(ban_like)}" if ban_like else "")
               + "\n\n" + md_table(rows, 10)
               + ("\n\nErrors:\n" + "\n".join(f"- {e['asset']}: {e['error']}" for e in errors) if errors else "")
               + "\n\nUse these keys to write ONE fully qualified join query; verify both sides' formats and code values.")
    return result(summary, source="atlan-ui", mapped=mapped, common_keys=common, account_common=account_common,
                  ban_like=ban_like, errors=errors)


# ── Power BI ─────────────────────────────────────────────────────────────────
@tool("powerbi", "List Power BI workspaces", open_world=True)
def powerbi_list_workspaces() -> dict:
    """Workspaces the configured identity can see."""
    rows = connectors("powerbi").list_workspaces()
    return result(f"{len(rows)} workspace(s)\n\n" + md_table(rows), mode=connector_mode("powerbi"), workspaces=rows)


@tool("powerbi", "List Power BI reports and datasets", open_world=True)
def powerbi_list_content(workspace_id: Annotated[str, Field(description="Workspace id (from powerbi_list_workspaces)")]) -> dict:
    """Reports and semantic models (datasets) in a workspace."""
    pb = connectors("powerbi")
    reps, dss = pb.list_reports(workspace_id), pb.list_datasets(workspace_id)
    return result(f"{len(reps)} report(s), {len(dss)} dataset(s)\n\nReports:\n{md_table(reps)}\n\nDatasets:\n{md_table(dss)}",
                  mode=connector_mode("powerbi"), reports=reps, datasets=dss)


@tool("powerbi", "Run a DAX query on a Power BI dataset", open_world=True)
def powerbi_execute_dax(
    workspace_id: Annotated[str, Field(description="Workspace id")],
    dataset_id: Annotated[str, Field(description="Dataset (semantic model) id")],
    dax: Annotated[str, Field(description="DAX query, e.g. EVALUATE TOPN(10, 'Revenue')")],
) -> dict:
    """Execute a read-only DAX query (executeQueries) and return the rows."""
    if not dax.strip().upper().startswith(("EVALUATE", "DEFINE")):
        raise UserError("DAX must start with EVALUATE or DEFINE")
    rows = connectors("powerbi").execute_dax(workspace_id, dataset_id, dax)
    return result(f"{len(rows)} row(s)\n\n" + md_table(rows), mode=connector_mode("powerbi"), rows=rows[:500])


@tool("powerbi", "Power BI dataset refresh status", open_world=True)
def powerbi_refresh_status(workspace_id: Annotated[str, Field(description="Workspace id")],
                           dataset_id: Annotated[str, Field(description="Dataset id")]) -> dict:
    """Recent refresh history of a dataset (status, start/end, errors). Read-only — does not trigger a refresh."""
    rows = connectors("powerbi").refresh_history(workspace_id, dataset_id)
    last = rows[0] if rows else {}
    return result(f"last refresh: {last.get('status', 'n/a')} at {last.get('endTime') or last.get('startTime') or '—'}\n\n"
                  + md_table(rows, 10), mode=connector_mode("powerbi"), refreshes=rows[:50])


async def _pbi_page(ui):
    tabs = await ui.browser_tabs()
    tab = next((t["tab"] for t in tabs.get("tabs", []) if "powerbi.com" in (t.get("url") or "")), None)
    if tab is None:
        opened = await ui.browser_new_tab("https://app.powerbi.com/home")
        tab = opened.get("tab")
        await asyncio.sleep(5)
    return await ui._page(tab)


@tool("powerbi", "List Power BI reports in the logged-in desktop browser", open_world=True)
async def powerbi_ui_reports() -> dict:
    """Report names visible in the logged-in Power BI desktop session.
    This is the UI fallback when the Power BI API/Graph credentials are not
    configured; it reads the left nav of the current workspace."""
    from . import browser as ui
    page = await _pbi_page(ui)
    try:
        items = await page.locator("[data-testid='item-row']").evaluate_all(
            """els => els.map(e => ({
                name: (e.getAttribute('aria-label') || e.innerText || '').trim(),
                expanded: e.getAttribute('aria-expanded') || ''
            }))"""
        )
    except Exception:
        items = []
    names = []
    for it in items:
        nm = (it.get("name") or "").strip()
        if nm and nm not in names:
            names.append(nm)
    if not names:
        text = (await page.locator("body").inner_text())[:3000]
        return result(f"Power BI UI — {await page.title()}\n{page.url}\n\n{text}",
                      reports=[], source="powerbi-ui")
    rows = [{"report": n} for n in names]
    return result(f"{len(rows)} Power BI item(s) in the logged-in browser\n\n" + md_table(rows, 60),
                  reports=rows, source="powerbi-ui")


@tool("powerbi", "Read visible insights from a Power BI report in the logged-in desktop browser", open_world=True)
async def powerbi_ui_insights(
    report: Annotated[str, Field(description="Report name from powerbi_ui_reports; blank = current report")] = "",
    view: Annotated[str, Field(description="Report page/tab name, e.g. 'Base Management View'; blank = current")] = "",
    max_chars: Annotated[int, Field(description="Text cap (2000–20000)")] = 12000,
) -> dict:
    """Read the visible report text (KPI numbers, table cells, visual labels) from
    the logged-in Power BI desktop session. This is the UI fallback when the
    Power BI REST/Graph credentials are unavailable. It does not run DAX; it
    answers from the currently rendered report page. For a specific report or
    page, pass `report` / `view`; both are optional. Use powerbi_ui_reports to
    discover the visible report names."""
    from . import browser as ui
    page = await _pbi_page(ui)
    n = clamp(max_chars, 2000, 20000)
    if report:
        clicked = False
        try:
            loc = page.get_by_role("button", name=report, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(); clicked = True
        except Exception:
            pass
        if not clicked:
            try:
                loc = page.get_by_text(report, exact=True).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(); clicked = True
            except Exception:
                pass
        if not clicked:
            raise UserError(f"report {report!r} is not visible in the Power BI nav — call powerbi_ui_reports first")
        await asyncio.sleep(6)
    if view:
        clicked = False
        try:
            loc = page.get_by_role("link", name=view, exact=True).first
            if await loc.count() and await loc.is_visible():
                await loc.click(); clicked = True
        except Exception:
            pass
        if not clicked:
            try:
                loc = page.get_by_text(view, exact=True).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(); clicked = True
            except Exception:
                pass
        if not clicked:
            raise UserError(f"view {view!r} is not visible in the report — use powerbi_ui_reports to see the report pages")
        await asyncio.sleep(3)
    title = await page.title()
    body = await page.locator("body").inner_text()
    if "Power BI Report" in body:
        body = body.split("Power BI Report", 1)[1].lstrip()
    body = body[:n]
    return result(f"Power BI UI — {title}\n{page.url}\n\n{body}",
                  report=report, view=view, text=body, source="powerbi-ui")


# ── SAP BRIM (read-only) ─────────────────────────────────────────────────────
@tool("sap", "Look up a dispute case in SAP BRIM", open_world=True)
def sap_get_dispute(
    dispute_id: Annotated[str | None, Field(description="SAP dispute case id")] = None,
    external_reference: Annotated[str | None, Field(description="Our case reference (e.g. the disputes case_ref)")] = None,
) -> dict:
    """Read a dispute case from SAP BRIM (FI-CA) by id or external reference. Read-only — creating disputes is the
    disputes service's job, not an agent tool."""
    brim = connectors("sap_brim")
    if dispute_id:
        d = brim.get_dispute(dispute_id)
    elif external_reference:
        d = brim.find_dispute(external_reference)
        if d is None:
            return result(f"no dispute with external reference {external_reference}", mode=connector_mode("sap_brim"), dispute=None)
    else:
        raise UserError("give dispute_id or external_reference")
    return result(f"dispute {d.get('dispute_id', dispute_id)}: {d.get('status', '')}", mode=connector_mode("sap_brim"), dispute=d)
