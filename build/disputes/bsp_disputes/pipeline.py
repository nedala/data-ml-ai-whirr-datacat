"""Inbox poller: mailbox → disputes.inbox_messages → extraction → validation → case."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import db, extract, mailbox, validate
from .config import get, reason_codes

log = logging.getLogger("bsp_disputes.pipeline")
_lock = threading.Lock()
LAST: dict[str, Any] = {}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:120] or "attachment"


def _hist(action: str, by: str = "system", **kw) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "action": action, "by": by, **kw}


def process_message(mb, mbox: str, summary: dict) -> None:
    s = get()
    gid = summary["id"]
    if db.one("select id from disputes.inbox_messages where mailbox=%s and graph_id=%s", (mbox, gid)):
        return
    msg = mb.get_message(mbox, gid)
    body = msg.get("body") or {}
    content = body.get("content") or ""
    text = content if (body.get("contentType") or "text").lower() == "text" else extract.html_to_text(content)
    headers = {h["name"]: h["value"] for h in msg.get("internetMessageHeaders") or []}
    atts_meta, atts = [], []
    for a in msg.get("attachments") or []:
        try:
            data = mb.get_attachment(mbox, gid, a["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("attachment %s: %s", a.get("name"), e)
            continue
        atts.append({"name": a["name"], "contentType": a.get("contentType"), "data": data})
        atts_meta.append({"id": a["id"], "name": a["name"], "contentType": a.get("contentType"), "size": len(data)})
    sender = msg.get("from") or {}
    if "emailAddress" in sender:  # Graph shape {"emailAddress": {...}}
        sender = sender["emailAddress"]
    row = db.one("""insert into disputes.inbox_messages (mailbox, graph_id, internet_message_id, conversation_id, subject,
                    sender_name, sender_email, received_at, body_text, body_html, headers, attachments)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) on conflict do nothing returning id""",
                 (mbox, gid, msg.get("internetMessageId"), msg.get("conversationId"), msg.get("subject"),
                  sender.get("name"), sender.get("address"), msg.get("receivedDateTime"), text,
                  content if text is not content else None, db.J(headers), db.J(atts_meta)))
    if not row:
        return
    mid = row["id"]
    adir = s.data_dir / "attachments" / str(mid)
    for a in atts:
        adir.mkdir(parents=True, exist_ok=True)
        (adir / _safe(a["name"])).write_bytes(a["data"])
    res = extract.extract({"subject": msg.get("subject"), "from": sender, "body_text": text, "headers": headers}, atts)
    fields = {k: res.get(k) for k in extract.FIELDS if k in res}
    fields["invoice_ids"] = res.get("invoice_ids") or ([res["invoice_id"]] if res.get("invoice_id") else [])
    conf, ev = res.get("confidence", {}), res.get("evidence", {})
    if not res.get("is_dispute"):
        status, score, report = "not_dispute", conf.get("is_dispute", 0.8), {"checks": [], "context": {}}
    else:
        report = validate.enrich(fields, conf, ev)
        if fields.get("invoice_id") and fields["invoice_id"] not in fields["invoice_ids"]:
            fields["invoice_ids"] = [fields["invoice_id"]] + fields["invoice_ids"]
        rc = reason_codes().get(fields.get("category") or "other", reason_codes()["other"])
        fields.update(case_type=rc["case_type"], reason_code=rc["reason_code"], category_label=rc["label"])
        score = validate.overall(fields, conf, report)
        status = "approved" if validate.auto_approvable(fields, report, score) else "needs_review"
    report["notes"] = res.get("notes", [])
    report["triage_reason"] = res.get("triage_reason")
    if res.get("disagreements"):
        report["disagreements"] = res["disagreements"]
    received = msg.get("receivedDateTime")
    try:
        rdt = datetime.fromisoformat(str(received).replace("Z", "+00:00"))
    except Exception:
        rdt = datetime.now(timezone.utc)
    hist = [_hist("extracted", method=res.get("method"), confidence=score)]
    if status == "approved":
        hist.append(_hist("auto_approved", confidence=score))
    case = db.one("""insert into disputes.cases (message_id, status, fields, original_fields, confidence, evidence, validation,
                     overall_confidence, extraction_method, auto_approved, approved_by, approved_at, sla_due_at, history)
                     values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                  (mid, status, db.J(fields), db.J(fields), db.J(conf), db.J(ev), db.J(report), score, res.get("method"),
                   status == "approved", "auto" if status == "approved" else None,
                   datetime.now(timezone.utc) if status == "approved" else None,
                   rdt + timedelta(hours=s.sla_hours) if status != "not_dispute" else None, db.J(hist)))
    db.one("update disputes.cases set case_ref = 'DSP-' || lpad(id::text, 6, '0') where id=%s returning id", (case["id"],))
    try:
        mb.mark_read(mbox, gid)
        if s.move_messages:
            folder = s.processed_folder if status in ("approved", "not_dispute") else s.review_folder
            mb.move_message(mbox, gid, folder)
            db.one("update disputes.inbox_messages set moved_to=%s, triage=%s where id=%s returning id",
                   (folder, status, mid))
    except Exception as e:  # noqa: BLE001
        log.warning("mark/move %s failed: %s", gid, e)


def poll(limit: int = 200) -> dict:
    """Fetch unread mail and turn it into cases. Safe to call concurrently."""
    if not _lock.acquire(blocking=False):
        return {"status": "busy"}
    t0 = time.time()
    try:
        s = get()
        mb = mailbox.client()
        msgs = mb.list_messages(s.mailbox, "inbox", unread_only=True, top=limit)
        done, errors = 0, []
        for m in msgs:
            try:
                process_message(mb, s.mailbox, m)
                done += 1
            except Exception as e:  # noqa: BLE001
                log.exception("message %s failed", m.get("id"))
                errors.append({"id": m.get("id"), "error": f"{type(e).__name__}: {e}"})
        LAST.update(at=datetime.now(timezone.utc).isoformat(timespec="seconds"), fetched=len(msgs), processed=done,
                    errors=errors, seconds=round(time.time() - t0, 2), source=getattr(mb, "name", "m365"),
                    llm=extract.llm_available())
        return dict(LAST)
    finally:
        _lock.release()


def attachment_path(message_id: int, name: str) -> Path:
    return get().data_dir / "attachments" / str(message_id) / _safe(name)
