"""Browser automation on the appliance desktop's Chromium (headful, visible live in noVNC) over CDP.

Native Playwright-for-Python (`connect_over_cdp`, no Node runtime). Target: BROWSER_CDP_URL
(default http://browser:9223, the browser service's CDP proxy). Navigation policy
(MCP_BROWSER_POLICY):
  * `egress` (default) — follow the appliance egress policy. The desktop Chromium reaches the
    internet ONLY through px-proxy, whose allowlist + deny rules (Studio → Egress) decide; a
    blocked host is reported as such. One policy, no second list to keep in sync.
  * `allowlist` — additionally restrict to appliance hosts + MCP_BROWSER_ALLOW_DOMAINS
    (comma list, `*.example.com` wildcards).
  * MCP_BROWSER_ALLOW_ALL=1 lifts the tool-level check in either mode.
Only http(s), about:, data: and blob: URLs are ever opened. `browser_evaluate` needs MCP_BROWSER_EVAL=1.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field

from ..common import UserError, clamp, data_dir, env, result, tool

G = "browser"
APPLIANCE_HOSTS = ("localhost", "127.0.0.1", "nginx", "studio", "docs", "appliance", "code", "superset", "trino", "canvas",
                   "connectors", "mlflow", "model-gateway", "mlops", "harness", "mcp", "disputes", "ccai", "browser", "rag",
                   "openwebui", "host.docker.internal", "*.internal", "example.net", "*.example.net")
_state: dict[str, Any] = {"pw": None, "browser": None, "page": None, "url": None}
_lock = asyncio.Lock()


def cdp_url() -> str:
    return env("BROWSER_CDP_URL", "http://browser:9223")


def allowed_hosts() -> list[str]:
    hosts = list(APPLIANCE_HOSTS)
    facade = urlparse(env("PUBLIC_FACADE_URL")).hostname
    if facade:
        hosts.append(facade)
    hosts += [h.strip().lower() for h in env("MCP_BROWSER_ALLOW_DOMAINS").split(",") if h.strip()]
    return hosts


def url_allowed(url: str) -> bool:
    if env("MCP_BROWSER_ALLOW_ALL") == "1":
        return True
    u = urlparse(url)
    if u.scheme in ("about", "data", "blob", "chrome-error") or url in ("", "about:blank"):
        return True
    if u.scheme not in ("http", "https"):
        return False
    if env("MCP_BROWSER_POLICY", "egress").lower() != "allowlist":
        return True          # px-proxy enforces the egress allowlist + deny rules
    host = (u.hostname or "").lower()
    for h in allowed_hosts():
        if h.startswith("*.") and (host.endswith(h[1:]) or host == h[2:]):
            return True
        if host == h:
            return True
    return False


EGRESS_BLOCK_MARKERS = ("ERR_TUNNEL_CONNECTION_FAILED", "ERR_PROXY_CONNECTION_FAILED", "egress_blocked")


def _load_error(url: str, e: Exception) -> UserError:
    msg = str(e).splitlines()[0][:200]
    if any(m in str(e) for m in EGRESS_BLOCK_MARKERS):
        host = urlparse(url if re.match(r"^[a-z]+:", url) else "https://" + url).hostname
        return UserError(f"{host} is blocked by the appliance egress policy — an admin can allow it in "
                         f"Studio → Egress (deny rules, e.g. Google Drive/Gmail, always win)")
    return UserError(f"could not load {url}: {msg}")


def _check(url: str) -> str:
    if not re.match(r"^[a-z]+:", url):
        url = "https://" + url
    if not url_allowed(url):
        raise UserError(f"navigation to {urlparse(url).hostname} is blocked by the browser allow-list "
                        f"(add it to MCP_BROWSER_ALLOW_DOMAINS on the MCP server)")
    return url


async def _browser():
    b = _state["browser"]
    if b is not None and b.is_connected():
        return b
    from playwright.async_api import async_playwright  # noqa: PLC0415

    if _state["pw"] is None:
        _state["pw"] = await async_playwright().start()
    try:
        b = await _state["pw"].chromium.connect_over_cdp(cdp_url(), timeout=10_000)
    except Exception as e:  # noqa: BLE001
        raise UserError(f"desktop browser not reachable over CDP at {cdp_url()} — start the Browser capability in Studio "
                        f"({type(e).__name__}: {str(e).splitlines()[0][:160]})") from None
    _state["browser"] = b
    return b


async def _context():
    b = await _browser()
    if b.contexts:
        return b.contexts[0]
    return await b.new_context()


async def _pages() -> list:
    return list((await _context()).pages)


async def _page(tab: int | None = None):
    pages = await _pages()
    if tab is not None:
        if not 0 <= tab < len(pages):
            raise UserError(f"no tab {tab} (open tabs: {len(pages)})")
        _state["page"] = pages[tab]
        return pages[tab]
    p = _state.get("page")
    if p is not None and not p.is_closed() and p in pages:
        return p
    if pages:
        _state["page"] = pages[-1]
        return pages[-1]
    p = await (await _context()).new_page()
    _state["page"] = p
    return p


async def _info(page, **extra) -> dict:
    pages = await _pages()
    if page.url.startswith("http") and url_allowed(page.url):
        _state["last_ok"] = page.url
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        title = ""
    return {"url": page.url, "title": title, "tab": pages.index(page) if page in pages else None, "tabs": len(pages), **extra}


async def _guard_after(page) -> str | None:
    """After a click/press: if the page left the allow-list, go back and report."""
    if not url_allowed(page.url):
        bad = page.url
        try:
            await page.goto(_state.get("last_ok") or "about:blank", timeout=15_000)
        except Exception:  # noqa: BLE001
            await page.goto("about:blank")
        return f"navigation to {urlparse(bad).hostname} was blocked by the allow-list and reverted"
    return None


def _locator(page, role: str | None, name: str | None, text: str | None, label: str | None, placeholder: str | None,
             selector: str | None, nth: int):
    if role:
        loc = page.get_by_role(role, name=name) if name else page.get_by_role(role)
    elif label:
        loc = page.get_by_label(label)
    elif placeholder:
        loc = page.get_by_placeholder(placeholder)
    elif text:
        loc = page.get_by_text(text)
    elif selector:
        loc = page.locator(selector)
    else:
        raise UserError("give one of: role (+name), label, placeholder, text or selector")
    return loc.nth(nth) if nth else loc.first


Role = Annotated[str | None, Field(description="ARIA role, e.g. button, link, textbox, combobox, checkbox (use with name)")]
Name = Annotated[str | None, Field(description="Accessible name for the role, e.g. 'Submit'")]
Text = Annotated[str | None, Field(description="Visible text to match")]
Label = Annotated[str | None, Field(description="Form label text")]
Placeholder = Annotated[str | None, Field(description="Input placeholder text")]
Selector = Annotated[str | None, Field(description="CSS selector (last resort)")]
Nth = Annotated[int, Field(description="Which match (0 = first)")]
Tab = Annotated[int | None, Field(description="Tab index (default: the active tab)")]


@tool(G, "List browser tabs", open_world=True)
async def browser_tabs() -> dict:
    """Open tabs in the desktop browser (index, url, title); the active tab is marked."""
    async with _lock:
        pages = await _pages()
        active = await _page() if pages else None
        rows = []
        for i, p in enumerate(pages):
            try:
                t = await p.title()
            except Exception:  # noqa: BLE001
                t = ""
            rows.append({"tab": i, "url": p.url, "title": t, "active": p is active})
    return result(f"{len(rows)} tab(s)\n" + "\n".join(f"{'*' if r['active'] else ' '} [{r['tab']}] {r['title']} — {r['url']}" for r in rows),
                  tabs=rows)


@tool(G, "Open a new browser tab", read_only=False, open_world=True)
async def browser_new_tab(url: Annotated[str, Field(description="URL to open (allow-listed hosts)")] = "about:blank") -> dict:
    """Open a new tab (visible on the desktop) and make it active."""
    url = _check(url)
    async with _lock:
        page = await (await _context()).new_page()
        _state["page"] = page
        if url != "about:blank":
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as e:  # noqa: BLE001
                raise _load_error(url, e) from None
        await page.bring_to_front()
        return result(f"opened {page.url}", **await _info(page))


@tool(G, "Navigate the browser", read_only=False, open_world=True)
async def browser_navigate(
    url: Annotated[str, Field(description="URL (allow-listed hosts); 'back' / 'forward' / 'reload' also work")],
    tab: Tab = None,
) -> dict:
    """Load a URL in the active tab (or go back/forward/reload) and wait for the DOM."""
    async with _lock:
        page = await _page(tab)
        await page.bring_to_front()
        if url in ("back", "forward", "reload"):
            await {"back": page.go_back, "forward": page.go_forward, "reload": page.reload}[url](timeout=30_000)
            note = await _guard_after(page)
        else:
            try:
                resp = await page.goto(_check(url), wait_until="domcontentloaded", timeout=30_000)
            except UserError:
                raise
            except Exception as e:  # noqa: BLE001
                raise _load_error(url, e) from None
            note = await _guard_after(page)
            if resp is not None and resp.status >= 400:
                note = f"HTTP {resp.status}"
        return result(f"{page.url}" + (f" ({note})" if note else ""), **await _info(page, note=note))


@tool(G, "Accessibility snapshot + text of the page", open_world=True)
async def browser_snapshot(
    tab: Tab = None,
    max_chars: Annotated[int, Field(description="Truncate (2k–40k)")] = 12000,
    selector: Annotated[str | None, Field(description="Only this part of the page (CSS selector)")] = None,
) -> dict:
    """The page's ARIA tree (roles + accessible names — use them with browser_click/fill) and visible text."""
    async with _lock:
        page = await _page(tab)
        root = page.locator(selector or "body").first
        try:
            aria = await root.aria_snapshot(timeout=10_000)
        except Exception as e:  # noqa: BLE001
            aria = f"(aria snapshot unavailable: {type(e).__name__})"
        try:
            text = await root.inner_text(timeout=10_000)
        except Exception:  # noqa: BLE001
            text = ""
        cap = clamp(max_chars, 2000, 40000)
        info = await _info(page)
    return result(f"{info['title']} — {info['url']}\n\n{aria[:cap]}", aria=aria[:cap], text=text[: cap // 2], **info)


@tool(G, "Screenshot the page", open_world=True)
async def browser_screenshot(
    tab: Tab = None,
    full_page: Annotated[bool, Field(description="Whole scrollable page instead of the viewport")] = False,
    thumbnail: Annotated[bool, Field(description="Small JPEG (for timelines) instead of a full PNG")] = False,
):
    """A screenshot of the active tab, returned as an image."""
    from fastmcp.utilities.types import Image  # noqa: PLC0415

    async with _lock:
        page = await _page(tab)
        if thumbnail:
            png = await page.screenshot(type="jpeg", quality=45, scale="css", timeout=15_000)
            fmt = "jpeg"
        else:
            png = await page.screenshot(full_page=full_page, type="png", timeout=20_000)
            fmt = "png"
        info = await _info(page)
    return [json.dumps({"summary": f"screenshot of {info['url']}", **info}), Image(data=png, format=fmt)]


async def _act(kind: str, tab, loc_args: tuple, fn) -> dict:
    async with _lock:
        page = await _page(tab)
        await page.bring_to_front()
        loc = _locator(page, *loc_args)
        t0 = time.monotonic()
        try:
            await fn(loc)
        except UserError:
            raise
        except Exception as e:  # noqa: BLE001
            raise UserError(f"{kind} failed: {str(e).splitlines()[0][:300]} — take a browser_snapshot to find the element") from None
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        except Exception:  # noqa: BLE001
            pass
        note = await _guard_after(page)
        return result(f"{kind} ok → {page.url}" + (f" ({note})" if note else ""),
                      **await _info(page, ms=int((time.monotonic() - t0) * 1000), note=note))


@tool(G, "Click an element", read_only=False, open_world=True)
async def browser_click(role: Role = None, name: Name = None, text: Text = None, label: Label = None,
                        placeholder: Placeholder = None, selector: Selector = None, nth: Nth = 0, tab: Tab = None,
                        double: Annotated[bool, Field(description="Double-click")] = False) -> dict:
    """Click an element found by role+name (preferred), label, placeholder, text or CSS selector."""
    return await _act("click", tab, (role, name, text, label, placeholder, selector, nth),
                      lambda loc: loc.dblclick(timeout=10_000) if double else loc.click(timeout=10_000))


@tool(G, "Fill a form field", read_only=False, open_world=True)
async def browser_fill(value: Annotated[str, Field(description="Text to type (replaces the current value)")],
                       role: Role = None, name: Name = None, text: Text = None, label: Label = None,
                       placeholder: Placeholder = None, selector: Selector = None, nth: Nth = 0, tab: Tab = None) -> dict:
    """Fill an input/textarea found by label (preferred), role+name, placeholder or selector."""
    return await _act("fill", tab, (role, name, text, label, placeholder, selector, nth),
                      lambda loc: loc.fill(value, timeout=10_000))


@tool(G, "Select an option in a dropdown", read_only=False, open_world=True)
async def browser_select(option: Annotated[str, Field(description="Option label or value")],
                         role: Role = None, name: Name = None, text: Text = None, label: Label = None,
                         placeholder: Placeholder = None, selector: Selector = None, nth: Nth = 0, tab: Tab = None) -> dict:
    """Choose an option of a <select> element."""
    async def sel(loc):
        try:
            await loc.select_option(label=option, timeout=5_000)
        except Exception:  # noqa: BLE001
            await loc.select_option(value=option, timeout=5_000)
    return await _act("select", tab, (role, name, text, label, placeholder, selector, nth), sel)


@tool(G, "Press a key", read_only=False, open_world=True)
async def browser_press(key: Annotated[str, Field(description="Key or chord, e.g. Enter, Tab, Escape, Control+A")],
                        role: Role = None, name: Name = None, text: Text = None, label: Label = None,
                        placeholder: Placeholder = None, selector: Selector = None, nth: Nth = 0, tab: Tab = None) -> dict:
    """Press a key on an element (or on the page when no element is given)."""
    if not any((role, text, label, placeholder, selector)):
        async with _lock:
            page = await _page(tab)
            await page.keyboard.press(key)
            note = await _guard_after(page)
            return result(f"pressed {key} → {page.url}", **await _info(page, note=note))
    return await _act(f"press {key}", tab, (role, name, text, label, placeholder, selector, nth),
                      lambda loc: loc.press(key, timeout=10_000))


@tool(G, "Wait for text, an element or the page to load", open_world=True)
async def browser_wait_for(
    text: Annotated[str | None, Field(description="Wait until this text is visible")] = None,
    selector: Annotated[str | None, Field(description="Wait for this CSS selector")] = None,
    state: Annotated[Literal["visible", "hidden", "attached", "detached"], Field(description="Element state")] = "visible",
    load: Annotated[Literal["load", "domcontentloaded", "networkidle"] | None, Field(description="Or a page load state")] = None,
    timeout_s: Annotated[float, Field(description="Max seconds (1–60)")] = 15,
    tab: Tab = None,
) -> dict:
    """Wait until something appears/disappears or the page reaches a load state."""
    ms = int(max(1, min(60, timeout_s)) * 1000)
    async with _lock:
        page = await _page(tab)
        t0 = time.monotonic()
        try:
            if text:
                await page.get_by_text(text).first.wait_for(state=state, timeout=ms)
            elif selector:
                await page.locator(selector).first.wait_for(state=state, timeout=ms)
            else:
                await page.wait_for_load_state(load or "load", timeout=ms)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"wait timed out: {str(e).splitlines()[0][:200]}") from None
        return result(f"ready after {int((time.monotonic() - t0) * 1000)} ms", **await _info(page))


@tool(G, "Evaluate JavaScript in the page (guarded)", read_only=False, open_world=True)
async def browser_evaluate(expression: Annotated[str, Field(description="JS expression or arrow function, e.g. "
                                                                      "'() => document.title'")], tab: Tab = None) -> dict:
    """Run a JavaScript expression in the active tab and return its JSON value. Disabled unless MCP_BROWSER_EVAL=1."""
    if env("MCP_BROWSER_EVAL") != "1":
        raise UserError("browser_evaluate is disabled on this server (MCP_BROWSER_EVAL=1 enables it); "
                        "use browser_snapshot to read the page")
    if re.search(r"\b(fetch|XMLHttpRequest|WebSocket|sendBeacon|document\.cookie|localStorage|indexedDB)\b", expression):
        raise UserError("expression uses a blocked API (network, cookies, storage)")
    async with _lock:
        page = await _page(tab)
        try:
            v = await page.evaluate(expression)
        except Exception as e:  # noqa: BLE001
            raise UserError(f"evaluate failed: {str(e).splitlines()[0][:300]}") from None
        s = json.dumps(v, default=str)
        return result(s[:2000], value=json.loads(s) if len(s) <= 10000 else s[:10000], **await _info(page))


@tool(G, "Close a browser tab", read_only=False, open_world=True)
async def browser_close_tab(tab: Annotated[int, Field(description="Tab index (from browser_tabs)")]) -> dict:
    """Close one tab (the last remaining tab is kept open as about:blank)."""
    async with _lock:
        pages = await _pages()
        if not 0 <= tab < len(pages):
            raise UserError(f"no tab {tab}")
        if len(pages) == 1:
            await pages[0].goto("about:blank")
        else:
            await pages[tab].close()
        _state["page"] = None
        return result(f"closed tab {tab}", tabs=len(await _pages()))


@tool(G, "Download a file via the browser", read_only=False, open_world=True)
async def browser_download(
    url: Annotated[str | None, Field(description="File URL to fetch with the browser's session (allow-listed)")] = None,
    role: Role = None, name: Name = None, text: Text = None, selector: Selector = None, tab: Tab = None,
) -> dict:
    """Download a file — by URL (uses the browser's cookies) or by clicking a link/button — into
    $MCP_DATA_DIR/downloads; returns the saved path and size."""
    out_dir = data_dir() / "downloads"
    out_dir.mkdir(parents=True, exist_ok=True)
    async with _lock:
        page = await _page(tab)
        if url:
            url = _check(url)
            r = await page.context.request.get(url, timeout=60_000)
            if not r.ok:
                raise UserError(f"download failed: HTTP {r.status}")
            fname = re.sub(r"[^\w.-]+", "_", os.path.basename(urlparse(url).path) or "download.bin")
            p = out_dir / fname
            p.write_bytes(await r.body())
        else:
            loc = _locator(page, role, name, text, None, None, selector, 0)
            async with page.expect_download(timeout=60_000) as dl:
                await loc.click(timeout=10_000)
            d = await dl.value
            p = out_dir / re.sub(r"[^\w.-]+", "_", d.suggested_filename or "download.bin")
            await d.save_as(str(p))
        return result(f"saved {p.name} ({p.stat().st_size:,} bytes)", path=str(p), bytes=p.stat().st_size, **await _info(page))
