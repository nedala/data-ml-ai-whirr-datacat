"""Playwright adapter: raise dispute cases through the dispute-entry web UI
with N workers in parallel, each in its own VISIBLE browser window.

Default: `connect_over_cdp(BROWSER_CDP_URL)` to the headful Chromium of the
appliance `browser` service, so people can watch the windows work in the
noVNC desktop. One browser context (= one window) per worker; the windows
are tiled on screen with CDP `Browser.setWindowBounds`. When the browser
service is unreachable a local headless Chromium is launched instead and the
result says so (`adapter: playwright-headless`).

Robustness: label/role based selectors (element ids change per render),
explicit waits for the UI's busy phases, per-case retries, search-before-
create idempotency on the external reference (the case ref), a screenshot
per step, and an event callback for live progress.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .config import cfg, ui_url

BANNER_JS = """
(() => {
  window.__bspBanner = (text) => {
    try { sessionStorage.setItem('bspBanner', text); } catch (e) {}
    let b = document.getElementById('__bsp_banner');
    if (!b) {
      b = document.createElement('div'); b.id = '__bsp_banner';
      b.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:2147483647;background:rgba(11,110,121,.92);color:#fff;' +
        'font:600 13px/1.3 system-ui,sans-serif;padding:6px 10px;border-radius:6px;pointer-events:none;max-width:60%';
      (document.body || document.documentElement).appendChild(b);
    }
    b.textContent = text;
  };
  const show = () => { try { const t = sessionStorage.getItem('bspBanner'); if (t) window.__bspBanner(t); } catch (e) {} };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', show); else show();
})();
"""


class StepError(RuntimeError):
    pass


def _resolve_cdp(url: str) -> str | None:
    """Return a ws:// endpoint for the CDP server, or None when unreachable.
    Chromium rejects DevTools HTTP requests whose Host header is a name other
    than localhost, so the host is resolved to an IP first."""
    if not url:
        return None
    u = urlparse(url)
    if u.scheme in ("ws", "wss"):
        return url
    try:
        ip = socket.gethostbyname(u.hostname or "localhost")
        port = u.port or 9222
        req = urllib.request.Request(f"http://{ip}:{port}/json/version")
        with urllib.request.urlopen(req, timeout=3) as r:
            info = json.loads(r.read())
        ws = urlparse(info["webSocketDebuggerUrl"])
        return f"ws://{ip}:{port}{ws.path}"
    except Exception:
        return None


def _tiles(n: int, screen: tuple[int, int]) -> list[dict]:
    cols = max(1, math.ceil(math.sqrt(n)))
    rows = max(1, math.ceil(n / cols))
    w, h = screen[0] // cols, (screen[1] - 40) // rows
    return [{"left": (i % cols) * w, "top": (i // cols) * h, "width": w, "height": h, "windowState": "normal"}
            for i in range(n)]


async def _open_windows(browser, n: int, cdp: bool, screen: tuple[int, int]) -> list:
    pages = []
    for i, bounds in enumerate(_tiles(n, screen)):
        ctx = await browser.new_context(viewport=None if cdp else {"width": 1280, "height": 820},
                                        ignore_https_errors=True)
        await ctx.add_init_script(BANNER_JS)
        page = await ctx.new_page()
        if cdp:
            try:
                s = await ctx.new_cdp_session(page)
                win = await s.send("Browser.getWindowForTarget")
                await s.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": {"windowState": "normal"}})
                await s.send("Browser.setWindowBounds", {"windowId": win["windowId"], "bounds": bounds})
            except Exception:
                pass  # tiling is cosmetic
        pages.append(page)
    return pages


class Worker:
    def __init__(self, wid: int, page, c, entry: str, emit: Callable[[dict], None], adapter: str):
        self.wid, self.page, self.c, self.entry, self.emit, self.adapter = wid, page, c, entry, emit, adapter
        self.base = entry.split("/ui")[0]
        self.logged_in = False

    # ── helpers ────────────────────────────────────────────────────────
    async def _pause(self):
        if self.c.slow_mo_ms:
            await asyncio.sleep(self.c.slow_mo_ms / 1000)

    async def _mark(self, loc):
        if self.c.highlight:
            try:
                await loc.evaluate("el => { el.style.outline = '3px solid #ff8205'; el.style.outlineOffset = '2px'; }")
            except Exception:
                pass
        await self._pause()

    async def _banner(self, text: str):
        try:
            await self.page.evaluate("t => window.__bspBanner && window.__bspBanner(t)", text)
        except Exception:
            pass

    async def _shot(self, ctx: dict, step: str) -> str | None:
        ctx["n"] += 1
        d = Path(self.c.screenshot_dir) / ctx["case_ref"]
        p = d / f"a{ctx['attempt']}-{ctx['n']:02d}-{step}.png"
        try:
            d.mkdir(parents=True, exist_ok=True)
            await self.page.screenshot(path=str(p))
            ctx["shots"].append(str(p))
            return str(p)
        except Exception:
            return None

    async def _step(self, ctx: dict, step: str, status: str = "running", **extra):
        await self._banner(f"Worker {self.wid} · {ctx['case_ref']} · {step}")
        shot = await self._shot(ctx, step) if status != "running" or step in ("logged-in",) else None
        ev = {"worker": self.wid, "case_ref": ctx["case_ref"], "step": step, "status": status,
              "attempt": ctx["attempt"], "screenshot": shot, "ts": time.time(), "adapter": self.adapter, **extra}
        ctx["steps"].append({k: v for k, v in ev.items() if k != "adapter"})
        self.emit(ev)

    async def _fill(self, label: str, value: str):
        loc = self.page.get_by_label(label, exact=True)
        await loc.wait_for(state="visible", timeout=self.c.timeout_s * 1000)
        await self._mark(loc)
        await loc.fill(value)

    async def _select(self, label: str, value: str):
        loc = self.page.get_by_label(label, exact=True)
        await loc.wait_for(state="visible", timeout=self.c.timeout_s * 1000)
        await self._mark(loc)
        await loc.select_option(value)

    async def _click(self, name: str, role: str = "button"):
        loc = self.page.get_by_role(role, name=name, exact=True)
        await loc.wait_for(state="visible", timeout=self.c.timeout_s * 1000)
        await self._mark(loc)
        await loc.click()

    # ── flow ───────────────────────────────────────────────────────────
    async def login(self, ctx: dict):
        await self.page.goto(self.entry, wait_until="domcontentloaded")
        if await self.page.get_by_label("User", exact=True).count():
            await self._fill("User", self.c.user or "BRIM_TECH_USER")
            await self._fill("Password", self.c.password or "mock")
            await self._click("Log On")
        await self.page.get_by_text("Find Business Partner").wait_for(timeout=self.c.timeout_s * 1000)
        self.logged_in = True
        await self._step(ctx, "logged-in", "ok")

    async def open_account(self, ctx: dict, ban: str):
        await self.page.goto(f"{self.base}/ui/home", wait_until="domcontentloaded")
        if await self.page.get_by_label("User", exact=True).count():  # session expired
            await self.login(ctx)
            await self.page.goto(f"{self.base}/ui/home", wait_until="domcontentloaded")
        await self._fill("BAN / contract account, business partner or name", ban)
        await self._click("Search")
        link = self.page.locator("[data-role=open-account]").first
        try:
            await link.wait_for(state="visible", timeout=self.c.timeout_s * 1000)
        except Exception:
            raise StepError(f"BAN {ban} not found in SAP") from None
        await self._step(ctx, "account-found", "ok")
        await self._mark(link)
        await link.click()
        await self.page.locator("[data-role=dispute-list]").wait_for(timeout=self.c.timeout_s * 1000)

    async def existing(self, ref: str) -> str | None:
        row = self.page.locator(f'tr[data-ext-ref="{ref}"]')
        if await row.count():
            return (await row.first.locator("td").first.inner_text()).strip()
        return None

    async def create(self, case: dict, attempt: int) -> dict:
        ctx = {"case_ref": case["case_ref"], "attempt": attempt, "n": 0, "shots": [], "steps": []}
        try:
            return await self._create(case, ctx)
        except Exception as e:
            await self._step(ctx, "error", "failed", error=str(e)[:300])
            e.ctx = ctx  # type: ignore[attr-defined]
            raise

    async def _create(self, case: dict, ctx: dict) -> dict:
        t0 = time.time()
        if not self.logged_in:
            await self.login(ctx)
        await self.open_account(ctx, case.get("ban") or "")
        await self._step(ctx, "account-opened", "ok")
        found = await self.existing(case["case_ref"])
        if found or case.get("_lookup_only"):
            await self._step(ctx, "exists" if found else "not-found", "ok", dispute_id=found)
            return self._result(ctx, found, "exists" if found else "not_found", t0)
        btn = self.page.get_by_role("button", name="Create Dispute Case")
        await btn.wait_for(timeout=self.c.timeout_s * 1000)
        await self.page.wait_for_function(
            "() => [...document.querySelectorAll('button')].some(b => b.textContent.trim()==='Create Dispute Case' && !b.disabled)",
            timeout=self.c.timeout_s * 1000)
        await self._mark(btn)
        await btn.click()
        # step 1
        await self._select("Case type", case["case_type"])
        reason = self.page.get_by_label("Reason code", exact=True)
        await self.page.locator(f'select[name=CaseReason] option[value="{case["reason_code"]}"]').wait_for(
            state="attached", timeout=self.c.timeout_s * 1000)
        await self._mark(reason)
        await reason.select_option(case["reason_code"])
        await self._step(ctx, "classification", "ok")
        await self._click("Next")
        # step 2
        amt = case.get("disputed_amount")
        await self._fill("Disputed amount", "" if amt in (None, "") else f"{float(amt):.2f}")
        await self._select("Currency", case.get("currency") or "USD")
        await self._fill("Invoice reference", case.get("invoice_id") or "")
        await self._fill("External reference", case["case_ref"])
        await self._step(ctx, "amount-reference", "ok")
        await self._click("Next")
        # step 3
        await self._fill("Notes", (case.get("notes") or case.get("summary") or "")[:1000])
        await self._step(ctx, "notes", "ok")
        for save_try in range(3):
            await self._click("Save Dispute Case")
            ok = self.page.locator("[data-role=dispute-id]")
            alert = self.page.locator("[role=alert]")
            await ok.or_(alert).first.wait_for(timeout=self.c.timeout_s * 1000)
            if await ok.count():
                did = (await ok.inner_text()).strip()
                await self._step(ctx, "confirmed", "ok", dispute_id=did)
                return self._result(ctx, did, "created", t0)
            msg = (await alert.first.inner_text()).strip()
            await self._step(ctx, f"save-rejected-{save_try + 1}", "warning", error=msg)
            if "busy" not in msg.lower():
                raise StepError(msg)
            await asyncio.sleep(0.5 * (save_try + 1))
        raise StepError("save kept failing (system busy)")

    def _result(self, ctx: dict, did: str | None, status: str, t0: float) -> dict:
        return {"dispute_id": did, "status": status, "mode": "playwright", "adapter": self.adapter,
                "url": f"{self.base}/ui/dispute/{did}" if did else None, "worker": self.wid,
                "attempt": ctx["attempt"], "duration_ms": int((time.time() - t0) * 1000),
                "screenshots": ctx["shots"], "steps": ctx["steps"]}


async def run_batch(cases: list[dict], concurrency: int | None = None,
                    on_event: Callable[[dict], None] | None = None, max_attempts: int = 3) -> list[dict]:
    from playwright.async_api import async_playwright

    c = cfg()
    n = max(1, min(concurrency or c.concurrency, len(cases) or 1))
    entry = ui_url()
    emit = on_event or (lambda ev: None)
    results: list[dict | None] = [None] * len(cases)
    async with async_playwright() as p:
        ws = _resolve_cdp(c.cdp_url)
        browser, adapter = None, "playwright-headless"
        if ws:
            try:
                browser = await p.chromium.connect_over_cdp(ws, timeout=10000)
                adapter = "playwright-cdp"
            except Exception:
                browser = None
        if browser is None:
            browser = await p.chromium.launch(headless=True)
        emit({"event": "batch-start", "adapter": adapter, "workers": n, "cases": len(cases), "ts": time.time()})
        pages = await _open_windows(browser, n, adapter == "playwright-cdp", c.screen)
        queue: asyncio.Queue = asyncio.Queue()
        for i, case in enumerate(cases):
            queue.put_nowait((i, case))

        async def run(wid: int, page):
            w = Worker(wid, page, c, entry, emit, adapter)
            while True:
                try:
                    i, case = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                all_steps, all_shots, err = [], [], None
                for attempt in range(1, max_attempts + 1):
                    try:
                        r = await w.create(case, attempt)
                        r["steps"] = all_steps + r["steps"]
                        r["screenshots"] = all_shots + r["screenshots"]
                        r["attempts"] = attempt
                        results[i] = r
                        break
                    except Exception as e:  # noqa: BLE001
                        cx = getattr(e, "ctx", {})
                        all_steps += cx.get("steps", [])
                        all_shots += cx.get("shots", [])
                        err = f"{type(e).__name__}: {e}"
                        w.logged_in = False if "Log On" in str(e) else w.logged_in
                        await asyncio.sleep(0.5 * attempt)
                else:
                    results[i] = {"dispute_id": None, "status": "failed", "error": err, "mode": "playwright",
                                  "adapter": adapter, "worker": wid, "attempts": max_attempts,
                                  "steps": all_steps, "screenshots": all_shots}

        t0 = time.time()
        await asyncio.gather(*(run(i + 1, pg) for i, pg in enumerate(pages)))
        emit({"event": "batch-end", "adapter": adapter, "seconds": round(time.time() - t0, 1), "ts": time.time()})
        await asyncio.sleep(60 if c.keep_windows else 2)  # let viewers see the final state
        for pg in pages:
            try:
                await pg.context.close()
            except Exception:
                pass
        if adapter == "playwright-headless":
            await browser.close()
    return [r or {"status": "failed", "error": "not processed"} for r in results]


def run_batch_sync(cases: list[dict], concurrency: int | None = None,
                   on_event: Callable[[dict], Any] | None = None) -> list[dict]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_batch(cases, concurrency, on_event))
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(1) as ex:  # called from inside an event loop
        return ex.submit(lambda: asyncio.run(run_batch(cases, concurrency, on_event))).result()


_ = re  # noqa
