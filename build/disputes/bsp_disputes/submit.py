"""Submission engine: approved cases → SAP BRIM through the connector.

Cases are claimed atomically (approved → submitting, FOR UPDATE SKIP LOCKED)
so nothing is sent twice from here; the connector additionally searches SAP
by the case ref before creating (idempotent across crashes / retries).
Live progress (per worker: case, step, screenshot) is kept in LIVE for the UI.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any

from . import db
from .config import get

log = logging.getLogger("bsp_disputes.submit")
_run_lock = threading.Lock()
LIVE: dict[str, Any] = {"running": False, "batch_id": None, "mode": None, "adapter": None, "workers": {},
                        "events": deque(maxlen=300), "last": None}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def to_case(row: dict) -> dict:
    f = row["fields"]
    notes = (f"{f.get('summary') or ''}\n\nCustomer: {f.get('customer_name') or ''} <{f.get('contact_email') or ''}> "
             f"{f.get('contact_phone') or ''}\nRequested: {f.get('requested_resolution') or ''} · "
             f"Period: {f.get('billing_period') or ''} · Source: Dispute Desk {row['case_ref']}").strip()
    return {"case_ref": row["case_ref"], "ban": f.get("ban"), "customer_name": f.get("customer_name"),
            "invoice_id": f.get("invoice_id"), "disputed_amount": f.get("disputed_amount"),
            "currency": f.get("currency") or "USD", "category": f.get("category"),
            "category_label": f.get("category_label"), "case_type": f.get("case_type"),
            "reason_code": f.get("reason_code"), "summary": f.get("summary"), "notes": notes,
            "contact_email": f.get("contact_email"), "contact_phone": f.get("contact_phone"),
            "priority": "2" if f.get("urgency") == "high" else "3"}


def _event(ev: dict) -> None:
    ev = {**ev, "ts": ev.get("ts") or time.time()}
    LIVE["events"].append(ev)
    if ev.get("event") == "batch-start":
        LIVE["adapter"] = ev.get("adapter")
        LIVE["workers"] = {str(i + 1): {"status": "idle"} for i in range(ev.get("workers", 0))}
        return
    w = str(ev.get("worker") or "")
    if w:
        cur = LIVE["workers"].setdefault(w, {})
        cur.update({k: v for k, v in ev.items() if k in ("case_ref", "step", "status", "attempt", "dispute_id", "error")})
        if ev.get("screenshot"):
            cur["screenshot"] = ev["screenshot"]
        cur["ts"] = ev["ts"]


def claim(case_ids: list[int] | None = None, limit: int = 100) -> list[dict]:
    if case_ids:
        return db.rows("""update disputes.cases set status='submitting', updated_at=now()
                          where id in (select id from disputes.cases where status='approved' and id = any(%s)
                                       order by id for update skip locked) returning *""", (case_ids,))
    return db.rows("""update disputes.cases set status='submitting', updated_at=now()
                      where id in (select id from disputes.cases where status='approved' order by id limit %s
                                   for update skip locked) returning *""", (limit,))


def run(case_ids: list[int] | None = None, mode: str | None = None, concurrency: int | None = None,
        limit: int = 100) -> dict:
    """Submit approved cases (blocking). Returns a batch summary."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "busy", "batch_id": LIVE["batch_id"]}
    prev_mode = os.environ.get("SAP_BRIM_MODE")
    batch = uuid.uuid4().hex[:10]
    t0 = time.time()
    try:
        if mode:
            os.environ["SAP_BRIM_MODE"] = mode
        from bsp_connectors import sap_brim

        eff_mode = sap_brim.mode()
        rows = claim(case_ids, limit)
        LIVE.update(running=True, batch_id=batch, mode=eff_mode, started=time.time(), total=len(rows),
                    adapter="odata" if eff_mode != "playwright" else "connecting", workers={})
        LIVE["events"].clear()
        if not rows:
            return {"status": "empty", "batch_id": batch, "submitted": 0}
        cases = [to_case(r) for r in rows]
        started = _now()
        try:
            results = sap_brim.create_disputes(cases, concurrency=concurrency or get().concurrency, on_event=_event)
        except Exception as e:  # noqa: BLE001 — whole batch failed (e.g. no browser at all)
            log.exception("batch failed")
            results = [{"status": "failed", "error": f"{type(e).__name__}: {e}", "mode": eff_mode} for _ in cases]
        counts: dict[str, int] = {}
        for row, case, r in zip(rows, cases, results):
            st = r.get("status") or "failed"
            counts[st] = counts.get(st, 0) + 1
            ok = st in ("created", "exists") and r.get("dispute_id")
            db.one("""insert into disputes.brim_submissions (case_id, batch_id, mode, adapter, status, sap_dispute_id, url,
                      worker, attempts, started_at, finished_at, duration_ms, error, steps, screenshots, request, response)
                      values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s,%s,%s,%s,%s) returning id""",
                   (row["id"], batch, eff_mode, r.get("adapter"), st, r.get("dispute_id"), r.get("url"), r.get("worker"),
                    r.get("attempts"), started, r.get("duration_ms"), r.get("error"), db.J(r.get("steps") or []),
                    db.J(r.get("screenshots") or []), db.J(case),
                    db.J({k: v for k, v in r.items() if k not in ("steps", "screenshots")})))
            hist = {"ts": _now().isoformat(timespec="seconds"), "action": "submitted" if ok else "submit_failed",
                    "by": "engine", "mode": eff_mode, "adapter": r.get("adapter"), "dispute_id": r.get("dispute_id"),
                    "error": r.get("error")}
            db.one("""update disputes.cases set status=%s, sap_dispute_id=coalesce(%s, sap_dispute_id), sap_url=coalesce(%s, sap_url),
                      submitted_at=case when %s then now() else submitted_at end, updated_at=now(),
                      history = history || %s::jsonb where id=%s returning id""",
                   ("submitted" if ok else "failed", r.get("dispute_id"), r.get("url"), bool(ok), db.J([hist]), row["id"]))
        summary = {"status": "done", "batch_id": batch, "mode": eff_mode,
                   "adapter": next((r.get("adapter") for r in results if r.get("adapter")), None),
                   "cases": len(rows), "counts": counts, "seconds": round(time.time() - t0, 1),
                   "concurrency": concurrency or get().concurrency}
        LIVE["last"] = summary
        return summary
    finally:
        if mode:
            if prev_mode is None:
                os.environ.pop("SAP_BRIM_MODE", None)
            else:
                os.environ["SAP_BRIM_MODE"] = prev_mode
        LIVE["running"] = False
        _run_lock.release()


def run_async(**kw) -> dict:
    if LIVE["running"]:
        return {"status": "busy", "batch_id": LIVE["batch_id"]}
    threading.Thread(target=run, kwargs=kw, daemon=True, name="brim-submit").start()
    time.sleep(0.2)
    return {"status": "started"}


def live() -> dict:
    d = {k: v for k, v in LIVE.items() if k != "events"}
    d["events"] = list(LIVE["events"])[-60:]
    return d
