"""Triage + structured extraction of a dispute e-mail.

Order: deterministic rules always run (they give evidence spans and the
fallback); when the LLM gateway is available the `extract-default` alias
fills the pydantic schema and image/PDF attachments go through
`vlm-default`; results are merged field by field with per-field confidence.
"""

from __future__ import annotations

import base64
import html as htmllib
import io
import json
import logging
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from .config import get, reason_codes

log = logging.getLogger("bsp_disputes.extract")

# aligned with raw.disputes.category (bsp_seed.generate.DISPUTE_CATEGORIES) + "other"
Category = Literal["late_fee_autopay", "promo_expired", "price_increase", "duplicate_charge", "outage_credit",
                   "equipment_fee", "final_bill", "install_fee", "other"]
Resolution = Literal["refund", "credit", "waive", "apply_payment", "explanation", "other"]


class DisputeFields(BaseModel):
    is_dispute: bool = Field(description="true if the customer disputes a charge / asks for a billing correction")
    ban: Optional[str] = Field(None, description="DataPlatform billing account number (BAN): 10 digits, e.g. 3100000008")
    customer_name: Optional[str] = None
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    invoice_ids: list[str] = Field(default_factory=list, description="invoice numbers, format <BAN>-<YYMM>, e.g. 3100000008-2609")
    billing_period: Optional[str] = Field(None, description="disputed bill month, YYYY-MM")
    disputed_amount: Optional[float] = Field(None, description="amount in dispute / requested back, number only")
    currency: str = "USD"
    category: Optional[Category] = None
    requested_resolution: Optional[Resolution] = None
    service_address: Optional[str] = None
    sentiment: Literal["negative", "neutral", "positive"] = "neutral"
    urgency: Literal["low", "normal", "high"] = "normal"
    summary: str = ""


FIELDS = ["ban", "customer_name", "contact_email", "contact_phone", "invoice_id", "billing_period",
          "disputed_amount", "currency", "category", "requested_resolution", "service_address", "sentiment",
          "urgency", "summary"]

# ── text utils ────────────────────────────────────────────────────────────


def html_to_text(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = htmllib.unescape(s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def pdf_text(data: bytes) -> str:
    try:
        from pypdf import PdfReader

        return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(data)).pages[:5])
    except Exception as e:  # noqa: BLE001
        log.warning("pdf text failed: %s", e)
        return ""


def pdf_to_png(data: bytes) -> bytes | None:
    try:
        import fitz  # pymupdf

        doc = fitz.open(stream=data, filetype="pdf")
        return doc[0].get_pixmap(dpi=110).tobytes("png")
    except Exception as e:  # noqa: BLE001
        log.warning("pdf render failed: %s", e)
        return None


# ── rules ─────────────────────────────────────────────────────────────────
# BAN = billing account number, 10 digits (bsp_seed.ban_for); customers also write it grouped "310 000 0008".
_BAN = r"(\d{3}[\s-]?\d{3}[\s-]?\d{4})"
RX_ACCT = [re.compile(r"\bBAN\s*(?:no\.?|number|#)?\s*[:#]?\s*" + _BAN + r"(?![\d-])", re.I),
           re.compile(r"\b(?:billing\s+)?(?:acct|account|acount)\.?\s*(?:no\.?|number|num|#)?\s*[:#]?\s*\(?" + _BAN
                      + r"(?![\d-])", re.I),
           re.compile(r"(?<![\d+-])(3\d{9})(?=\s?-\s?\d{2}(?:0[1-9]|1[0-2])\b)"),   # BAN part of an invoice id
           re.compile(r"(?<![\d+-])(3\d{9})(?![\d-])")]                                # bare BAN-shaped number
ACCT_CONF = [0.95, 0.9, 0.8, 0.6]
# invoice id = <BAN>-<YYMM> (bsp_seed.invoice_id)
RX_INV = re.compile(r"(?<![\d+-])(\d{10})\s?-\s?(\d{2}(?:0[1-9]|1[0-2]))\b")
RX_AMT = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{1,2})?)")
RX_PHONE = re.compile(r"(?<!\d)(\+?1[-.\s]?)?\(?(\d{3})\)?[-.\s](\d{3})[-.\s](\d{4})(?!\d)")
RX_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
RX_FWD_FROM = re.compile(r"^From:\s*\"?([^<\n\"]*?)\"?\s*<([^>\n]+)>", re.M)
RX_MONTH = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(20\d\d)\b")
RX_ADDR = re.compile(r"\b\d{2,6}\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*\s+(?:St|Street|Ave|Avenue|Rd|Road|Dr|Drive|Ln|Lane|Blvd|Ct|Way)\b\.?[^\n]{0,40}")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                      "September", "October", "November", "December"], start=1)}

CAT_KW: dict[str, list[tuple[str, float]]] = {
    "late_fee_autopay": [(r"late (payment )?fee", 3), (r"auto ?pay", 2.5), (r"automatic(ally)?", 1)],
    "promo_expired": [(r"promo(tion(al)?)?\b", 3.5), (r"discount", 2), (r"(promo|promotion|offer|deal) (ended|expired|ran out)", 2),
                      (r"introductory|first (12|6) months", 1.5)],
    "price_increase": [(r"price (increase|went up|hike)", 3), (r"rate (increase|hike)", 3), (r"went up|jumped|increased? by", 2),
                       (r"\bprice\b", 1), (r"locked|no (notice|warning)", 1)],
    "duplicate_charge": [(r"twice|two times|x2\b", 3), (r"duplicate", 3), (r"double", 2)],
    "outage_credit": [(r"outage", 3), (r"no service|without (working )?(internet|service)|service was out|was down|down for|"
                                    r"(kept|keeps) dropping", 3),
                      (r"\bspeeds?\b|mbps|kept dropping|dsl (line|light)|\blos\b|red light", 1.5)],
    "equipment_fee": [(r"modem|router|gateway", 2), (r"equipment", 3), (r"(un|non-)return", 3), (r"tracking", 1),
                      (r"returned", 1)],
    "final_bill": [(r"final (bill|statement)", 3), (r"cancel(l)?ed|cancel(l)?ing", 2), (r"still (getting )?billed", 3),
                   (r"moved out", 1)],
    "install_fee": [(r"install(ation)?", 3), (r"self-install", 1), (r"technician|appointment", 0.5)],
}
DISPUTE_TERMS = r"charge|charged|bill|billed|fee|refund|credit|invoice|paid|payment|price|overcharg|dispute|statement|promo|discount"
NOISE_SUBJ = re.compile(r"^(automatic reply|auto(matic)?[- ]?reply|out of (the )?office|undeliverable)", re.I)
NOISE_BODY = re.compile(r"unsubscribe|out of the office|on vacation|manage preferences|register now|webinar", re.I)
URGENT = re.compile(r"\bwill cancel|switch(ing)? to|lawyer|attorney|\bBBB\b|\bFCC\b|\bASAP\b|overdrew|will not pay|"
                    r"unacceptable", re.I)
NEG = re.compile(r"\?\?|!!|terrible|unacceptable|not my fault|ridiculous|fix it|will not pay|switching|AGAIN", re.I)


class Ev(dict):
    """evidence: {source, start, end, text}"""


def _ev(src: str, m: re.Match | None, text: str | None = None, group: int = 0) -> dict:
    if m is None:
        return {"source": src, "text": text}
    return {"source": src, "start": m.start(group), "end": m.end(group), "text": m.group(group)}


def split_forward(body: str) -> tuple[str, dict | None]:
    """Return (customer text, inner sender) for forwarded mails."""
    m = re.search(r"-{3,}\s*Forwarded message\s*-{3,}|^Begin forwarded message:|^-----Original Message-----", body, re.M | re.I)
    if not m:
        return body, None
    tail = body[m.end():]
    fm = RX_FWD_FROM.search(tail)
    inner = {"name": fm.group(1).strip(), "address": fm.group(2).strip(),
             "start": m.end() + fm.start(), "end": m.end() + fm.end()} if fm else None
    return body, inner


def strip_signature_noise(text: str) -> str:
    return re.split(r"\n-- \n|\nThis email and any attachments", text)[0]


def _amount_value(s: str) -> float:
    return float(s.replace(",", ""))


def classify(text: str, subject: str) -> tuple[str | None, float, dict]:
    scores: dict[str, float] = {}
    hits: dict[str, re.Match] = {}
    low_s = subject.lower()
    for cat, kws in CAT_KW.items():
        s = 0.0
        for pat, w in kws:
            ms = list(re.finditer(pat, text, re.I))
            if ms:
                s += w + 0.3 * (len(ms) - 1)
                hits.setdefault(cat, ms[0])
            if re.search(pat, low_s, re.I):
                s += w * 0.8
        scores[cat] = s
    best = max(scores, key=scores.get)
    if scores[best] < 2:
        return None, 0.0, {}
    ranked = sorted(scores.values(), reverse=True)
    margin = ranked[0] - ranked[1]
    conf = min(0.95, 0.55 + 0.08 * margin + 0.03 * ranked[0])
    return best, round(conf, 2), _ev("body", hits.get(best))


def pick_amount(text: str, category: str | None) -> tuple[float | None, float, dict]:
    amts = list(RX_AMT.finditer(text))
    if not amts:
        return None, 0.0, {}
    # explicit "from $X to $Y" (price increase)
    m = re.search(r"from\s+\$\s?([\d,]+\.?\d*)\s+to\s+\$\s?([\d,]+\.?\d*)", text, re.I)
    if m and category in ("promo_expired", "price_increase", None):
        diff = round(_amount_value(m.group(2)) - _amount_value(m.group(1)), 2)
        if diff > 0:
            return diff, 0.85, _ev("body", m)
    m = re.search(r"was\s+\$\s?([\d,]+\.?\d*)[^$]{0,40}?now(?: billed at)?\s+\$\s?([\d,]+\.?\d*)", text, re.I)
    if m and category in ("promo_expired", "price_increase", None):
        diff = round(_amount_value(m.group(2)) - _amount_value(m.group(1)), 2)
        if diff > 0:
            return diff, 0.8, _ev("body", m)
    # explicit request: "credit of $X", "refund of $X", "a $X credit", "increased by $X", "difference ... $X"
    req = re.compile(r"(credit|refund|refunded|difference|increased? by|went up by|waive)[^$.\n]{0,25}\$\s?([\d,]+\.?\d*)|"
                     r"\$\s?([\d,]+\.?\d*)\s+(credit|refund|difference)", re.I)
    m = req.search(text)
    if m:
        g = m.group(2) or m.group(3)
        return _amount_value(g), 0.85, {"source": "body", "start": m.start(2) if m.group(2) else m.start(3),
                                        "end": m.end(2) if m.group(2) else m.end(3), "text": "$" + g}
    kws = {"late_fee_autopay": r"late (payment )?fee", "duplicate_charge": r"twice|two|x2|duplicate|each",
           "equipment_fee": r"fee|charge|charged", "install_fee": r"install|fee|charge",
           "final_bill": r"final|due|bill", "outage_credit": r"credit"}.get(category or "", r"charge|fee|refund|credit")
    best, best_s = None, -1.0
    for a in amts:
        win = text[max(0, a.start() - 60): a.end() + 60]
        s = len(re.findall(kws, win, re.I)) * 2 + len(re.findall(r"charged|fee|refund|credit|remove", win, re.I))
        if s > best_s:
            best, best_s = a, s
    if category == "outage_credit" and best_s < 2:
        return None, 0.5, {}
    conf = 0.75 if best_s >= 2 else 0.5
    return _amount_value(best.group(1)), conf, {"source": "body", "start": best.start(), "end": best.end(),
                                                "text": best.group(0)}


def resolution(text: str, category: str | None) -> tuple[str | None, float]:
    rules = [("apply_payment", r"apply (the|my) payment|(not|n't)( been)? (been )?applied|not credited"),
             ("refund", r"\brefund"),
             ("credit", r"\bcredit(ed)?\b(?! card)|lower my bill|promo(tional)? (rate|price)"),
             ("waive", r"\b(remove|waive|void|not pay|removed)\b"),
             ("explanation", r"\bexplain|explanation|what is this")]
    for name, pat in rules:
        if re.search(pat, text, re.I):
            return name, 0.8
    if category:
        return reason_codes().get(category, {}).get("default_resolution"), 0.55
    return None, 0.0


def triage(subject: str, text: str, headers: dict) -> tuple[bool, float, str]:
    h = {k.lower(): v for k, v in (headers or {}).items()}
    if h.get("auto-submitted", "no").lower() not in ("", "no") or NOISE_SUBJ.search(subject):
        return False, 0.95, "auto-reply"
    terms = len(set(m.group(0).lower() for m in re.finditer(DISPUTE_TERMS, text + " " + subject, re.I)))
    has_id = bool(any(rx.search(text) for rx in RX_ACCT) or RX_INV.search(text))
    if h.get("list-unsubscribe") or NOISE_BODY.search(text):
        if not (has_id and RX_AMT.search(text)):
            return False, 0.9, "bulk / newsletter"
    cat, _, _ = classify(text, subject)
    if terms >= 2 and (cat or has_id):
        return True, min(0.97, 0.6 + 0.07 * terms + (0.1 if has_id else 0)), "billing terms"
    if terms >= 1 and has_id:
        return True, 0.7, "account reference"
    return False, 0.75 if terms == 0 else 0.6, "no billing dispute language"


def rules_extract(msg: dict, attachments_text: list[tuple[str, str]]) -> dict[str, Any]:
    subject = msg.get("subject") or ""
    body = msg.get("body_text") or ""
    sender = msg.get("from") or {}
    headers = msg.get("headers") or {}
    full, inner = split_forward(body)
    text = strip_signature_noise(body)
    att_all = "\n".join(t for _, t in attachments_text)
    is_dispute, tconf, treason = triage(subject, body, headers)
    out: dict[str, Any] = {"is_dispute": is_dispute, "triage_reason": treason}
    conf: dict[str, float] = {"is_dispute": round(tconf, 2)}
    ev: dict[str, Any] = {}

    def setf(k, v, c, e=None):
        if v in (None, "", []):
            return
        out[k], conf[k] = v, round(c, 2)
        if e:
            ev[k] = e

    # account
    for src, t in [("body", body), ("subject", subject)] + [(f"attachment:{n}", at) for n, at in attachments_text]:
        for i, rx in enumerate(RX_ACCT):
            m = rx.search(t)
            if m:
                setf("ban", re.sub(r"\D", "", m.group(1)), ACCT_CONF[i], _ev(src, m, group=1))
                break
        if "ban" in out:
            break
    # invoice(s)
    invs, inv_ev = [], None
    for src, t in [("body", body), ("subject", subject)] + [(f"attachment:{n}", at) for n, at in attachments_text]:
        for m in RX_INV.finditer(t):
            iid = f"{m.group(1)}-{m.group(2)}"
            if iid not in invs:
                invs.append(iid)
                inv_ev = inv_ev or _ev(src, m)
    if invs:
        setf("invoice_id", invs[0], 0.95, inv_ev)
        out["invoice_ids"] = invs
    # contact
    who = inner or {"name": sender.get("name"), "address": sender.get("address")}
    if who.get("name") and not re.search(r"store|team|billing|support|noreply|no-reply", who["name"], re.I):
        setf("customer_name", who["name"].strip(), 0.85 if inner else 0.8,
             {"source": "body", "start": inner["start"], "end": inner["end"], "text": inner["name"]} if inner
             else {"source": "header:from", "text": who["name"]})
    if who.get("address"):
        setf("contact_email", who["address"].lower(), 0.9 if inner else 0.95,
             {"source": "body" if inner else "header:from", "text": who["address"]})
    m = next((x for x in RX_PHONE.finditer(body)  # a grouped BAN ("310 000 0008") is not a phone number
              if "".join(x.groups()[1:]) != out.get("ban")), None)
    if m:
        setf("contact_phone", f"+1-{m.group(2)}-{m.group(3)}-{m.group(4)}", 0.85, _ev("body", m))
    # category / amount / period / resolution
    cat, cconf, cev = classify(text + "\n" + att_all[:0], subject)
    if is_dispute:
        setf("category", cat or "other", cconf if cat else 0.3, cev or None)
        amt, aconf, aev = pick_amount(text, cat)
        if amt is None and attachments_text:
            amt, aconf, aev = pick_amount(att_all, cat)
            aconf *= 0.8
            if aev:
                aev["source"] = f"attachment:{attachments_text[0][0]}"
        if amt is not None:
            setf("disputed_amount", round(amt, 2), aconf, aev)
        out["currency"], conf["currency"] = "USD", 0.9
        m = RX_MONTH.search(body) or RX_MONTH.search(att_all)
        if m:
            setf("billing_period", f"{m.group(2)}-{MONTHS[m.group(1)]:02d}", 0.8, _ev("body", m))
        elif out.get("invoice_id"):
            iid = out["invoice_id"]
            setf("billing_period", f"20{iid[-4:-2]}-{iid[-2:]}", 0.85, {"source": "derived:invoice_id", "text": iid})
        res, rconf = resolution(text, cat)
        setf("requested_resolution", res, rconf)
        m = RX_ADDR.search(body)
        if m:
            setf("service_address", m.group(0).strip(), 0.6, _ev("body", m))
        urgent = URGENT.search(text + " " + subject) or (subject.isupper() and len(subject) > 6)
        out["urgency"], conf["urgency"] = ("high" if urgent else "normal"), 0.6
        neg = NEG.search(text + " " + subject)
        out["sentiment"], conf["sentiment"] = ("negative" if (neg or urgent) else "neutral"), 0.55
        first = re.split(r"(?<=[.!?])\s+|\n", re.sub(r"^(hi|hello|dear)[^\n]*\n+", "", text.strip(), flags=re.I))[0]
        label = reason_codes().get(out.get("category", "other"), {}).get("label", "Dispute")
        out["summary"] = f"{label}: {first.strip()[:220]}"
        conf["summary"] = 0.5
    else:
        out["summary"] = f"Not a dispute ({treason}): {subject[:120]}"
    out["confidence"], out["evidence"] = conf, ev
    return out


# ── LLM / VLM ─────────────────────────────────────────────────────────────
PROMPT = """You extract billing disputes for DataPlatform (a US internet provider) from customer e-mails.
DataPlatform sells copper (DSL) and fiber internet only.
Categories: late_fee_autopay, promo_expired (promotional discount ended), price_increase (list price raised),
duplicate_charge, outage_credit (outage or degraded service), equipment_fee, final_bill, install_fee, other.
ban = the 10-digit billing account number (BAN), e.g. 3100000008 — customers may call it account number,
billing account or BAN and may write it with spaces or dashes; return digits only.
Invoice ids are <BAN>-<YYMM>, e.g. 3100000008-2609 (the September 2026 bill of BAN 3100000008).
disputed_amount = the money the customer disputes or asks back (for a price increase: new minus old price).
For forwarded mails the customer is the original sender, not the forwarder. Use null when unknown.

Subject: {subject}
From: {sender}
--- body ---
{body}
--- attachment text ---
{att}
"""

VLM_PROMPT = ("This is an image of a customer's internet bill or account screen. Return JSON with keys ban "
              "(10-digit billing account number), invoice_id (<BAN>-<YYMM>, e.g. 3100000008-2609), billing_period "
              "(YYYY-MM), amount_due (number), "
              "line_items (list of {label, amount}). Use null when not visible.")


_llm_cache: list = [0.0, False]


def llm_available() -> bool:
    """Cached for 60 s so a missing gateway costs one probe per minute, not per mail."""
    import time

    if get().use_llm == "off":
        return False
    if time.time() - _llm_cache[0] < 60:
        return _llm_cache[1]
    try:
        from bsp_common import llm

        ok = llm.available()
    except Exception:
        ok = False
    _llm_cache[:] = [time.time(), ok]
    return ok


def vlm_read(image_png: bytes) -> dict:
    from bsp_common import llm

    url = "data:image/png;base64," + base64.b64encode(image_png).decode()
    text = llm.chat([{"role": "user", "content": [{"type": "text", "text": VLM_PROMPT},
                                                 {"type": "image_url", "image_url": {"url": url}}]}],
                    model="vlm-default", temperature=0)
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0)) if m else {}


def llm_extract(msg: dict, attachments_text: list[tuple[str, str]]) -> dict:
    from bsp_common import llm

    sender = msg.get("from") or {}
    prompt = PROMPT.format(subject=msg.get("subject", ""), sender=f"{sender.get('name')} <{sender.get('address')}>",
                           body=(msg.get("body_text") or "")[:6000],
                           att="\n".join(f"[{n}]\n{t[:2000]}" for n, t in attachments_text) or "(none)")
    data = llm.extract_json(prompt, DisputeFields.model_json_schema())
    return DisputeFields.model_validate(data).model_dump()


def _locate(body: str, value: Any) -> dict | None:
    if value in (None, ""):
        return None
    s = f"{value:.2f}" if isinstance(value, float) else str(value)
    i = body.find(s)
    if i < 0 and isinstance(value, str):
        i = body.lower().find(s.lower())
    return {"source": "body", "start": i, "end": i + len(s), "text": body[i:i + len(s)]} if i >= 0 else None


def extract(msg: dict, attachments: list[dict]) -> dict[str, Any]:
    """msg: {subject, from{name,address}, body_text, headers}; attachments: [{name, contentType, data}]"""
    att_text: list[tuple[str, str]] = []
    images: list[tuple[str, bytes]] = []
    for a in attachments:
        ct, data = (a.get("contentType") or "").lower(), a.get("data") or b""
        if "pdf" in ct or a["name"].lower().endswith(".pdf"):
            t = pdf_text(data)
            if t.strip():
                att_text.append((a["name"], t))
            png = pdf_to_png(data) if len(t.strip()) < 40 else None
            if png:
                images.append((a["name"], png))
        elif ct.startswith("image/"):
            images.append((a["name"], data))
        elif ct.startswith("text/"):
            att_text.append((a["name"], data.decode("utf-8", "replace")))
    result = rules_extract(msg, att_text)
    result["method"] = "rules"
    result["notes"] = []
    if images and not llm_available():
        result["notes"].append(f"{len(images)} image attachment(s) not read: VLM unavailable")
    if not llm_available():
        return result
    body = msg.get("body_text") or ""
    methods = ["rules"]
    for name, png in images:
        try:
            v = vlm_read(png)
            lines = [f"{k}: {v.get(k)}" for k in ("ban", "invoice_id", "billing_period", "amount_due") if v.get(k)]
            lines += [f"{li.get('label')}: {li.get('amount')}" for li in v.get("line_items") or [] if isinstance(li, dict)]
            att_text.append((name, "\n".join(lines)))
            methods.append("vlm")
        except Exception as e:  # noqa: BLE001
            result["notes"].append(f"VLM failed on {name}: {e}")
    if "vlm" in methods:  # rerun rules with the VLM transcription as attachment text
        notes = result["notes"]
        result = rules_extract(msg, att_text)
        result["notes"] = notes
    try:
        llm_out = llm_extract(msg, att_text)
        methods.append("llm")
    except Exception as e:  # noqa: BLE001
        result["notes"].append(f"LLM extraction failed, rules used: {e}")
        result["method"] = "+".join(methods)
        return result
    conf, ev = result["confidence"], result["evidence"]
    if llm_out.get("invoice_ids"):
        llm_out["invoice_id"] = llm_out["invoice_ids"][0]
    for k in FIELDS + ["is_dispute"]:
        lv, rv = llm_out.get(k), result.get(k)
        if lv in (None, "", []):
            continue
        if k == "ban":
            digits = re.sub(r"\D", "", str(lv))
            lv = digits if len(digits) == 10 else None
            if not lv:
                continue
        if k == "invoice_id":
            m = RX_INV.search(str(lv))
            lv = f"{m.group(1)}-{m.group(2)}" if m else None
            if not lv:
                continue
        if rv in (None, "", []):
            result[k], conf[k] = lv, 0.75
            if k not in ev and (loc := _locate(body, lv)):
                ev[k] = loc
        elif str(rv).lower() == str(lv).lower() or (isinstance(lv, (int, float)) and isinstance(rv, (int, float))
                                                    and abs(float(lv) - float(rv)) < 0.01):
            conf[k] = min(0.99, max(conf.get(k, 0.5), 0.8) + 0.1)
        elif k in ("summary", "sentiment", "urgency", "requested_resolution", "category", "is_dispute"):
            result[k], conf[k] = lv, 0.7  # LLM is better at semantics
        else:
            conf[k] = min(conf.get(k, 0.5), 0.6)  # disagreement on a literal: keep rules value, flag
            result.setdefault("disagreements", {})[k] = {"rules": rv, "llm": lv}
    result["method"] = "+".join(methods)
    return result
