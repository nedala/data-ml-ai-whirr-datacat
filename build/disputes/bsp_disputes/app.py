"""Dispute Desk — FastAPI service (port 8099), hosted natively under /disputes.

    python -m uvicorn bsp_disputes.app:app --host 0.0.0.0 --port 8099 --proxy-headers
"""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import db, extract, pipeline, submit
from .config import get, reason_codes

log = logging.getLogger("bsp_disputes")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
PREFIX = "/disputes"
STATIC = Path(__file__).resolve().parent / "static"
UI_KIT = Path(os.environ.get("UI_KIT_DIR", "/opt/ui-kit"))
STATE: dict[str, Any] = {"db_error": None}


async def _poller():
    while True:
        s = get()
        if s.poll_seconds <= 0:
            await asyncio.sleep(30)
            continue
        try:
            await asyncio.to_thread(pipeline.poll)
            if s.auto_submit:
                await asyncio.to_thread(submit.run)
        except Exception as e:  # noqa: BLE001
            log.warning("poll cycle failed: %s", e)
        await asyncio.sleep(s.poll_seconds)


def _init_db() -> None:
    try:
        db.init()
        db.rows("update disputes.cases set status='approved' where status='submitting' returning id")  # crash recovery
        STATE["db_error"] = None
    except Exception as e:  # noqa: BLE001
        STATE["db_error"] = f"{type(e).__name__}: {e}"
        log.error("database init failed: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get().data_dir.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(_init_db)
    task = asyncio.create_task(_poller())
    yield
    task.cancel()


app = FastAPI(title="Dispute Desk", lifespan=lifespan, docs_url=PREFIX + "/docs",
              openapi_url=PREFIX + "/openapi.json", redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    p = request.url.path
    if p.startswith(PREFIX + "/api/") and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-studio") != "1":
        return JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
    if p.startswith(PREFIX + "/api/") and STATE["db_error"] and not p.endswith("/config"):
        await asyncio.to_thread(_init_db)
        if STATE["db_error"]:
            return JSONResponse({"detail": f"warehouse unavailable: {STATE['db_error']}"}, status_code=503)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    if p.startswith(PREFIX + "/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    return resp


r = APIRouter(prefix=PREFIX)


def _user(request: Request) -> str:
    return request.headers.get("x-auth-user") or request.headers.get("x-forwarded-user") or "reviewer"


@r.get("/healthz")
def healthz():
    return {"ok": True}


@r.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


@r.get("/api/config")
def config():
    s = get()
    try:
        from bsp_connectors import sap_brim
        sap = sap_brim.describe()
    except Exception as e:  # noqa: BLE001
        sap = {"mode": os.environ.get("SAP_BRIM_MODE", "mock"), "error": str(e)}
    try:
        from bsp_connectors import mode_of
        m365 = mode_of("m365")
    except Exception:
        m365 = "eml-folder"
    return {"mailbox": s.mailbox, "m365_mode": m365, "auto_approve_threshold": s.auto_approve_threshold,
            "sla_hours": s.sla_hours, "poll_seconds": s.poll_seconds, "auto_submit": s.auto_submit,
            "llm_available": extract.llm_available(), "sap": sap, "concurrency": s.concurrency,
            "browser_path": s.browser_path,
            "browser_embed": s.browser_path.rstrip("/") + "/vnc.html?autoconnect=1&resize=scale&view_only=1&path="
                             + s.browser_path.strip("/") + "/websockify",
            "categories": reason_codes(), "last_poll": pipeline.LAST, "db_error": STATE["db_error"]}


@r.post("/api/poll")
async def poll():
    return await asyncio.to_thread(pipeline.poll)


CASE_COLS = """c.id, c.case_ref, c.status, c.fields, c.overall_confidence, c.extraction_method, c.auto_approved,
               c.assignee, c.sap_dispute_id, c.sla_due_at, c.created_at, c.updated_at, c.submitted_at,
               m.subject, m.sender_name, m.sender_email, m.received_at, m.id as message_id,
               jsonb_array_length(m.attachments) as n_attachments"""


@r.get("/api/cases")
def cases(status: str | None = None, q: str | None = None, limit: int = 500):
    where, params = [], []
    if status:
        where.append("c.status = any(%s)")
        params.append(status.split(","))
    if q:
        where.append("(m.subject ilike %s or c.fields::text ilike %s or c.case_ref ilike %s)")
        params += [f"%{q}%"] * 3
    sql = (f"select {CASE_COLS} from disputes.cases c join disputes.inbox_messages m on m.id=c.message_id "
           + (("where " + " and ".join(where)) if where else "") + " order by m.received_at desc nulls last limit %s")
    return db.rows(sql, params + [limit])


@r.get("/api/messages")
def messages(limit: int = 500):
    return db.rows("""select m.id, m.graph_id, m.internet_message_id, m.subject, m.sender_name, m.sender_email,
                      m.received_at, m.moved_to, m.attachments, c.id as case_id, c.case_ref, c.status
                      from disputes.inbox_messages m left join disputes.cases c on c.message_id=m.id
                      order by m.received_at desc nulls last limit %s""", (limit,))


def _legacy(f: dict | None) -> dict:
    """Cases written before the BAN re-key carry `account_id`; read it as `ban`."""
    if f and "account_id" in f and not f.get("ban"):
        f["ban"] = f.pop("account_id")
    return f or {}


def _case(cid: int) -> dict:
    c = db.one("select * from disputes.cases where id=%s", (cid,))
    if not c:
        raise HTTPException(404, "case not found")
    for k in ("fields", "original_fields", "confidence", "evidence"):
        c[k] = _legacy(c.get(k))
    return c


@r.get("/api/cases/{cid}")
def case_detail(cid: int):
    c = _case(cid)
    c["message"] = db.one("select * from disputes.inbox_messages where id=%s", (c["message_id"],))
    c["submissions"] = db.rows("select * from disputes.brim_submissions where case_id=%s order by id desc", (cid,))
    return c


EDITABLE = {"ban", "customer_name", "contact_email", "contact_phone", "invoice_id", "billing_period",
            "disputed_amount", "currency", "category", "requested_resolution", "service_address", "urgency",
            "sentiment", "summary"}


@r.patch("/api/cases/{cid}")
def edit_case(cid: int, request: Request, body: dict = Body(...)):
    c = _case(cid)
    if c["status"] in ("submitted", "submitting"):
        raise HTTPException(409, "case already submitted")
    f = dict(c["fields"])
    changes = {}
    for k, v in (body.get("fields") or {}).items():
        if k not in EDITABLE:
            continue
        if k == "disputed_amount" and v not in (None, ""):
            v = round(float(v), 2)
        if v == "":
            v = None
        if f.get(k) != v:
            changes[k] = {"from": f.get(k), "to": v}
            f[k] = v
    if "category" in changes:
        rc = reason_codes().get(f["category"] or "other", reason_codes()["other"])
        f.update(case_type=rc["case_type"], reason_code=rc["reason_code"], category_label=rc["label"])
    if "invoice_id" in changes:
        f["invoice_ids"] = [f["invoice_id"]] if f.get("invoice_id") else []
    conf = dict(c["confidence"])
    for k in changes:
        conf[k] = 1.0  # human-confirmed
    hist = [{"ts": _now(), "action": "edited", "by": _user(request), "changes": changes}] if changes else []
    db.one("update disputes.cases set fields=%s, confidence=%s, updated_at=now(), history=history || %s::jsonb "
           "where id=%s returning id", (db.J(f), db.J(conf), db.J(hist), cid))
    return case_detail(cid)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _transition(cid: int, to: str, by: str, allowed: tuple, **extra) -> dict:
    c = _case(cid)
    if c["status"] not in allowed:
        raise HTTPException(409, f"cannot move a {c['status']} case to {to}")
    sets, params = ["status=%s", "updated_at=now()"], [to]
    if to == "approved":
        if not c["fields"].get("ban") or not c["fields"].get("reason_code"):
            raise HTTPException(422, "account and category are required before approval")
        sets += ["approved_by=%s", "approved_at=now()"]
        params.append(by)
    if to == "rejected":
        sets.append("rejected_reason=%s")
        params.append(extra.get("reason"))
    sets.append("history = history || %s::jsonb")
    params.append(db.J([{"ts": _now(), "action": to, "by": by, **extra}]))
    db.one(f"update disputes.cases set {', '.join(sets)} where id=%s returning id", params + [cid])
    return {"id": cid, "status": to}


@r.post("/api/cases/{cid}/approve")
def approve(cid: int, request: Request):
    return _transition(cid, "approved", _user(request), ("needs_review", "failed", "rejected", "not_dispute"))


@r.post("/api/cases/{cid}/reject")
def reject(cid: int, request: Request, body: dict = Body(default={})):
    return _transition(cid, "rejected", _user(request), ("needs_review", "approved", "failed", "not_dispute"),
                       reason=body.get("reason") or "rejected by reviewer")


@r.post("/api/cases/{cid}/reopen")
def reopen(cid: int, request: Request):
    return _transition(cid, "needs_review", _user(request), ("rejected", "not_dispute", "approved", "failed"))


@r.post("/api/cases/{cid}/reassign")
def reassign(cid: int, request: Request, body: dict = Body(...)):
    _case(cid)
    db.one("update disputes.cases set assignee=%s, updated_at=now(), history=history || %s::jsonb where id=%s returning id",
           (body.get("assignee"), db.J([{"ts": _now(), "action": "reassigned", "by": _user(request),
                                         "assignee": body.get("assignee")}]), cid))
    return {"id": cid, "assignee": body.get("assignee")}


@r.post("/api/cases/bulk-approve")
def bulk_approve(request: Request, body: dict = Body(...)):
    ok, failed = [], []
    for cid in body.get("ids") or []:
        try:
            approve(int(cid), request)
            ok.append(cid)
        except HTTPException as e:
            failed.append({"id": cid, "error": e.detail})
    return {"approved": ok, "failed": failed}


@r.post("/api/submit")
def submit_now(body: dict = Body(default={})):
    mode = body.get("mode") or None
    if mode and mode not in ("api", "playwright", "mock"):
        raise HTTPException(422, "mode must be api | playwright | mock")
    kw = {"mode": mode, "concurrency": body.get("concurrency"), "case_ids": body.get("case_ids")}
    if body.get("wait"):
        return submit.run(**kw)
    return submit.run_async(**kw)


@r.get("/api/live")
def live():
    return submit.live()


@r.get("/api/submissions")
def submissions(limit: int = 300, batch_id: str | None = None):
    sql = """select s.id, s.case_id, c.case_ref, s.batch_id, s.mode, s.adapter, s.status, s.sap_dispute_id, s.url,
             s.worker, s.attempts, s.started_at, s.finished_at, s.duration_ms, s.error, s.screenshots, s.steps
             from disputes.brim_submissions s join disputes.cases c on c.id=s.case_id"""
    if batch_id:
        return db.rows(sql + " where s.batch_id=%s order by s.id desc limit %s", (batch_id, limit))
    return db.rows(sql + " order by s.id desc limit %s", (limit,))


@r.get("/api/screenshot")
def screenshot(path: str):
    base = Path(os.environ.get("SAP_BRIM_SCREENSHOT_DIR", str(get().data_dir / "screenshots"))).resolve()
    p = Path(path).resolve()
    if base not in p.parents or not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="image/png")


@r.get("/api/messages/{mid}/attachments/{name}")
def attachment(mid: int, name: str):
    p = pipeline.attachment_path(mid, name)
    if not p.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(p, filename=p.name)


@r.get("/api/metrics")
def metrics():
    by_status = {x["status"]: x["n"] for x in db.rows("select status, count(*) n from disputes.cases group by 1")}
    agg = db.one("""select count(*) total,
        count(*) filter (where status <> 'not_dispute') disputes,
        count(*) filter (where auto_approved) auto_approved,
        avg(overall_confidence) filter (where status <> 'not_dispute') avg_conf,
        count(*) filter (where sla_due_at < now() and status in ('needs_review','approved','failed')) sla_breached,
        count(*) filter (where sla_due_at between now() and now() + interval '8 hours'
                         and status in ('needs_review','approved','failed')) sla_due_soon,
        percentile_cont(0.5) within group (order by extract(epoch from submitted_at - created_at))
          filter (where submitted_at is not null) median_secs_to_submit
        from disputes.cases""")
    reviewed = db.rows("""select fields, original_fields from disputes.cases
                          where status in ('approved','submitted','submitting','failed') and not auto_approved""")
    per_field: dict[str, dict] = {}
    for c in reviewed:
        for k in sorted(EDITABLE):
            d = per_field.setdefault(k, {"reviewed": 0, "corrected": 0})
            d["reviewed"] += 1
            if c["fields"].get(k) != c["original_fields"].get(k):
                d["corrected"] += 1
    for d in per_field.values():
        d["accuracy"] = round(1 - d["corrected"] / d["reviewed"], 3) if d["reviewed"] else None
    subs = db.rows("""select mode, adapter, status, count(*) n, avg(duration_ms)::int avg_ms
                      from disputes.brim_submissions group by 1,2,3 order by 1,2,3""")
    daily = db.rows("""select date_trunc('day', created_at)::date d, count(*) received,
                       count(*) filter (where submitted_at is not null) submitted
                       from disputes.cases group by 1 order by 1 desc limit 14""")
    methods = {x["m"] or "-": x["n"] for x in db.rows("select extraction_method m, count(*) n from disputes.cases group by 1")}
    cats = {x["c"] or "-": x["n"] for x in db.rows(
        "select fields->>'category' c, count(*) n from disputes.cases where status <> 'not_dispute' group by 1 order by 2 desc")}
    return {"by_status": by_status, **{k: (float(v) if v is not None and k in ("avg_conf", "median_secs_to_submit") else v)
                                         for k, v in (agg or {}).items()},
            "field_accuracy": per_field, "submissions": subs, "daily": daily, "methods": methods, "categories": cats}


@r.post("/api/reset-demo")
def reset_demo():
    """Demo helper: clears cases + mock mailbox read state (never touches a live mailbox)."""
    if os.environ.get("DISPUTES_ALLOW_RESET", "true").lower() not in ("1", "true", "yes"):
        raise HTTPException(403, "reset disabled")
    from .mailbox import EmlFolder, client
    mb = client()
    if not isinstance(mb, EmlFolder):
        try:
            from bsp_connectors import mode_of
            if mode_of("m365") != "mock":
                raise HTTPException(409, "mailbox is live; reset refused")
        except HTTPException:
            raise
        except Exception:
            pass
    db.rows("truncate disputes.brim_submissions, disputes.cases, disputes.inbox_messages restart identity cascade")
    if isinstance(mb, EmlFolder):
        mb.reset()
    else:
        from bsp_connectors.m365 import mock as m365_mock
        m365_mock.reset_mailbox(get().mailbox)
    return {"ok": True}


app.include_router(r)
app.mount(PREFIX + "/static", StaticFiles(directory=STATIC), name="static")
if UI_KIT.is_dir():
    app.mount(PREFIX + "/ui-kit", StaticFiles(directory=UI_KIT), name="ui-kit")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(PREFIX + "/")
