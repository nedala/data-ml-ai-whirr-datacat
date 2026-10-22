"""SAP BRIM connector — raise / read dispute cases in SAP BRIM (FI-CA).

    from bsp_connectors import sap_brim
    sap_brim.mode()                       # "api" | "playwright" | "mock"
    sap_brim.create_dispute(case) -> {"dispute_id","status","url",...}
    sap_brim.get_dispute(dispute_id) -> dict
    sap_brim.find_dispute(external_reference) -> dict | None
    sap_brim.create_disputes(cases, concurrency=4, on_event=None) -> list[dict]

Adapters (SAP_BRIM_MODE):
  api         OData V4 client against SAP_BRIM_BASE_URL (technical user
              SAP_BRIM_USER / SAP_BRIM_PASSWORD, CSRF handshake, $metadata-
              driven payload from field_map.yaml).
  playwright  drives the dispute-entry web UI in real browser windows
              (CDP to the appliance browser service, headless fallback).
  mock        the same OData client against the in-appliance mock SAP
              system served by this plugin (CONNECTORS_URL/sap_brim).

`case` keys (from bsp_disputes): case_ref, ban (10-digit BAN), customer_name,
invoice_id, disputed_amount, currency, category, category_label, case_type,
reason_code, summary, notes, contact_email, contact_phone, priority.
"""

from __future__ import annotations

from typing import Any, Callable

from .config import cfg, mode, ui_url  # noqa: F401

__all__ = ["mode", "mode_of", "test_connection", "create_dispute", "get_dispute", "find_dispute", "create_disputes", "describe"]


def mode_of() -> str:
    return mode()


def _odata():
    from .odata import ODataClient

    return ODataClient.from_env()


def describe() -> dict[str, Any]:
    c = cfg()
    return {"mode": c.mode, "base_url": c.base_url, "ui_url": ui_url(), "user_set": bool(c.user),
            "password_set": bool(c.password), "cdp_url": c.cdp_url, "concurrency": c.concurrency}


def test_connection() -> str:
    """Connectors status page hook: proves the configured adapter can reach SAP."""
    c = cfg()
    if c.mode == "playwright":
        import httpx

        from .browser import _resolve_cdp
        r = httpx.get(ui_url(), timeout=10, verify=c.verify_tls, follow_redirects=True)
        cdp = "reachable" if _resolve_cdp(c.cdp_url) else "unreachable (headless fallback)"
        return f"playwright: web UI HTTP {r.status_code} at {ui_url()}; browser CDP {c.cdp_url} {cdp}"
    cl = _odata()
    cl.csrf()
    meta = cl.metadata()
    return f"{c.mode}: OData OK at {cl.root} (CSRF token, {meta['entity_type']} with {len(meta['properties'])} properties)"


def find_dispute(external_reference: str) -> dict | None:
    """Search by external reference (the case ref) over OData (all modes; the
    Playwright adapter does its own search on the account page before create)."""
    return _odata().find_by_external_reference(external_reference)


def get_dispute(dispute_id: str) -> dict:
    """Read a dispute case. Always over OData (the UI adapter has no read API);
    in playwright mode this needs SAP_BRIM_BASE_URL to serve the OData API too."""
    return _odata().get(dispute_id)


def create_dispute(case: dict) -> dict:
    return create_disputes([case], concurrency=1)[0]


def create_disputes(cases: list[dict], concurrency: int | None = None,
                    on_event: Callable[[dict], None] | None = None) -> list[dict]:
    """Create many disputes. Idempotent per `case_ref` (search before create).
    Results are returned in input order; failures carry status "failed" + error."""
    concurrency = concurrency or cfg().concurrency
    if mode() == "playwright":
        from .browser import run_batch_sync
        return run_batch_sync(cases, concurrency=concurrency, on_event=on_event)
    from concurrent.futures import ThreadPoolExecutor

    client = _odata()

    def one(case: dict) -> dict:
        if on_event:
            on_event({"case_ref": case.get("case_ref"), "step": "odata.post", "status": "running"})
        try:
            r = client.create(case)
        except Exception as e:  # noqa: BLE001 — reported per case
            r = {"status": "failed", "error": f"{type(e).__name__}: {e}", "mode": mode(), "adapter": "odata"}
        if on_event:
            on_event({"case_ref": case.get("case_ref"), "step": "done", "status": r.get("status"),
                      "dispute_id": r.get("dispute_id")})
        return r

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
        return list(ex.map(one, cases))
