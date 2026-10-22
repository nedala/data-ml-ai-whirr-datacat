"""Front-office automation demos, grounded in runbooks and design docs.

1. agent_assist(text)      customer email / call transcript / ticket → intent,
                           entities, retrieved runbook steps (checklist with
                           citations), escalation flags, drafted reply.
2. runbook_copilot(text)   "outage in wire center RLGHNCXA, what do I do?" →
                           ordered steps from the outage runbooks + escalation
                           contacts for the region + local wire-center contacts.
3. design_qa(question)     Q&A restricted to design documents.

LLM-enhanced when the gateway is up (classify-default, chat-default);
deterministic and fully offline otherwise.
"""

from __future__ import annotations

import re
import time
from typing import Any

from . import answer as answer_mod, config, gateway, retrieval
from .textutil import overlap, tokens

INTENTS: dict[str, dict[str, Any]] = {
    "billing_dispute": {"label": "Billing dispute", "kw": ["dispute", "overcharg", "wrong amount", "incorrect charge", "charged twice",
                         "duplicate", "bill is wrong", "promo", "promotion", "price went up", "higher than", "refund", "credit"],
                        "query": "billing dispute procedure verify invoice place disputed amount on hold credit adjustment authority",
                        "topic": "your bill"},
    "late_fee": {"label": "Late fee reversal", "kw": ["late fee", "late charge", "late payment fee", "penalty"],
                 "query": "late fee reversal policy eligibility first late fee autopay failure courtesy",
                 "topic": "the late fee on your account"},
    "autopay": {"label": "Autopay failure", "kw": ["autopay", "auto pay", "automatic payment", "card expired", "declined"],
                "query": "autopay failure result code late fee reversal DataPlatform-caused", "topic": "your automatic payment"},
    "outage": {"label": "Service outage", "kw": ["outage", "whole neighborhood", "neighbors", "area is down", "everyone on my street",
                                                  "service is down", "down since"],
               "query": "outage customer communications talking points ETR outage credit 24 hours", "topic": "the service interruption"},
    "no_internet": {"label": "No internet / ONT fault", "kw": ["no internet", "not working", "red light", "los light", "ont", "offline",
                                                              "can't connect", "no connection", "fiber box"],
                    "query": "fiber ONT troubleshooting LOS light levels reboot dispatch", "topic": "your internet connection"},
    "slow_speed": {"label": "Slow speed / Wi-Fi", "kw": ["slow", "speed", "buffering", "wifi", "wi-fi", "lag", "drops"],
                   "query": "slow speed wifi troubleshooting wired speed test line test mesh extender", "topic": "your internet speed"},
    "appointment": {"label": "Technician / appointment", "kw": ["technician", "appointment", "no-show", "no show", "never showed",
                                                                "install", "installation", "truck"],
                    "query": "truck roll dispatch appointment missed appointment credit priority", "topic": "your appointment"},
    "cancel": {"label": "Cancellation / retention", "weight": 3, "kw": ["cancel", "switching", "switch to", "leaving", "terminate", "competitor",
                                                          "cheaper", "close my account"],
               "query": "retention save offers cancellation reason action matrix offer catalog", "topic": "your service"},
    "move": {"label": "Move / transfer", "kw": ["moving", "move my service", "new address", "relocat", "transfer service"],
             "query": "moves and service transfers serviceability move order install fee waived", "topic": "your move"},
    "porting": {"label": "Number porting", "kw": ["port", "keep my number", "phone number", "home phone", "pin"],
                "query": "number porting port-out PIN port-in letter of authorization", "topic": "your phone number"},
    "equipment_return": {"label": "Equipment return", "kw": ["return", "returned", "modem", "router", "equipment fee", "unreturned",
                                                            "shipping label"],
                         "query": "equipment return unreturned equipment fee reverse depot scan tracking", "topic": "your equipment return"},
    "payment_arrangement": {"label": "Payment arrangement", "kw": ["payment plan", "arrangement", "can't pay", "cannot pay", "behind",
                                                                  "past due", "extension", "suspend", "disconnect notice", "lost my job"],
                            "query": "payment arrangement eligibility installment extension dunning suspension", "topic": "your balance"},
}

ESCALATION_TRIGGERS = {
    "regulatory": (r"\b(puc|public utilit|fcc|attorney general|bbb|better business|regulator|lawyer|attorney|sue|legal action)\b",
                   "Regulatory / legal threat — route to Regulatory Response the same day (Escalation Matrix)."),
    "medical": (r"\b(medical|oxygen|life support|life-support|dialysis|disabled|telehealth)\b",
                "Medical-priority customer — offer callback; P1 dispatch / NOC Duty Manager if without service."),
    "executive": (r"\b(ceo|executive|twitter|social media|post this|news)\b",
                  "Executive / social-media risk — Executive Response team."),
    "repeat": (r"\b(third time|3rd time|again and again|every week|keeps happening|second time|multiple times)\b",
               "Repeat contact — check repeat-dispatch rules and consider supervisor review."),
}

REGIONS = {"East": ["NC", "VA", "SC", "PA"], "Central": ["OH", "IN", "MO", "TN"], "South": ["TX", "FL", "AL"]}
# BAN: 10-digit billing account number (31xxxxxxxx); invoice id: <ban>-<yymm>; wire center: 8-char CLLI
# (4-letter place + 2-letter state + 2-char office, e.g. RLGHNCXA) — state restricted to the served footprint.
RE_BAN = re.compile(r"(?<![\d-])(3\d{9})(?![\d-])")
RE_INVOICE = re.compile(r"(?<![\d-])(3\d{9})-(\d{4})(?!\d)")
RE_WC = re.compile(r"\b([A-Z]{4}(?:" + "|".join(s for sts in REGIONS.values() for s in sts) + r")[A-Z0-9]{2})\b")
RE_AMOUNT = re.compile(r"\$\s?\d{1,5}(?:,\d{3})*(?:\.\d{2})?")


def entities(text: str) -> dict[str, Any]:
    name = ""
    m = re.search(r"(?:my name is|this is)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)", text)
    if m:
        name = m.group(1)
    else:
        m = re.search(r"(?:thanks|thank you|regards|sincerely|best|cheers)[,!.]?\s*\n+\s*([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)\s*$",
                      text.strip(), re.I)
        if m:
            name = m.group(1)
    invoices = RE_INVOICE.findall(text)
    return {"bans": sorted(set(RE_BAN.findall(text)) | {b for b, _ in invoices}),
            "invoices": sorted({f"{b}-{m}" for b, m in invoices}),
            "wire_centers": sorted(set(RE_WC.findall(text))), "amounts": RE_AMOUNT.findall(text)[:6],
            "customer_name": name}


def classify(text: str, use_llm: bool = True) -> dict[str, Any]:
    low = text.lower()
    scores = {k: sum(2 if (" " in w) else 1 for w in v["kw"] if w in low) * v.get("weight", 1) for k, v in INTENTS.items()}
    ranked = sorted(scores.items(), key=lambda t: -t[1])
    rules = {"intent": ranked[0][0] if ranked[0][1] > 0 else "general", "secondary": [k for k, s in ranked[1:3] if s > 0],
             "confidence": round(min(1.0, ranked[0][1] / 4), 2), "method": "keyword rules"}
    if use_llm and gateway.available():
        try:
            labels = ", ".join(INTENTS) + ", general"
            data, route = gateway.chat_json(
                f"Customer message:\n{text[:3000]}\n\nClassify the PRIMARY intent into one of: {labels}. "
                'Also list up to two secondary intents. Return JSON {"intent": "...", "secondary": [...], "confidence": 0..1}.',
                system="You classify ISP customer contacts for a contact-center agent-assist tool.",
                alias=config.get().classify_alias)
            if data.get("intent") in INTENTS or data.get("intent") == "general":
                return {"intent": data["intent"], "secondary": [s for s in data.get("secondary", []) if s in INTENTS][:2],
                        "confidence": float(data.get("confidence", 0.7)), "method": "LLM (classify-default)",
                        "route": route, "rules_intent": rules["intent"]}
        except Exception:  # noqa: BLE001
            pass
    return rules


def flags(text: str) -> list[dict[str, str]]:
    return [{"flag": k, "action": msg} for k, (pat, msg) in ESCALATION_TRIGGERS.items() if re.search(pat, text, re.I)]


def _steps(chunks: list[dict[str, Any]], limit: int = 9, prefer: str = "") -> list[dict[str, Any]]:
    """Numbered procedure lines from the best procedural chunk(s), with citation numbers."""
    out: list[dict[str, Any]] = []
    order = sorted(range(len(chunks)), key=lambda i: (0 if chunks[i].get("doc_type") == "runbook" else 1,
                                                     -overlap(prefer, chunks[i].get("section") or "") if prefer else 0, i))
    for i in order:
        c = chunks[i]
        lines = [ln.strip() for ln in (c.get("text") or "").split("\n") if re.match(r"^\s*\d+\.\s", ln)]
        if len(lines) >= 2:
            for ln in lines:
                out.append({"step": ln, "cite": i + 1, "doc_key": c.get("doc_key"), "section": c.get("section")})
            if len(out) >= limit:
                break
    return out[:limit]


def _cites(chunks: list[dict[str, Any]], ns: set[int]) -> list[dict[str, Any]]:
    return [{"n": n, "chunk_id": chunks[n - 1]["chunk_id"], "title": chunks[n - 1]["title"], "doc_key": chunks[n - 1]["doc_key"],
             "section": chunks[n - 1]["section"], "uri": chunks[n - 1]["uri"], "document_id": chunks[n - 1].get("document_id"),
             "snippet": (chunks[n - 1]["text"] or "")[:300]} for n in sorted(ns) if 0 < n <= len(chunks)]


def _facts(text: str, chunks: list[dict[str, Any]], n: int = 3) -> list[tuple[str, int]]:
    """Customer-safe policy sentences most related to the message (offline draft)."""
    cands = []
    for i, c in enumerate(chunks[:5]):
        for s in re.split(r"(?<=[.!?])\s+|\n", c.get("text") or ""):
            s = s.strip(" -•")
            if "`" in s or s.startswith("**") or s.count("(") != s.count(")"):
                continue                         # agent-internal step text / formats / fragments
            s = s.replace("**", "")
            if 40 < len(s) < 260 and not s.startswith("|") and not re.match(r"^\d+\.", s) and "{" not in s \
                    and not re.search(r"\b(agent|supervisor|queue|reason code|ADJ-|FI-CA|SAP|verify the caller)\b", s, re.I):
                cands.append((overlap(text, s) - 0.05 * i, s, i + 1))
    cands.sort(key=lambda t: -t[0])
    seen, out = set(), []
    for sc, s, i in cands:
        if s not in seen and sc > 0.03:
            out.append((s, i))
            seen.add(s)
        if len(out) >= n:
            break
    return out


ACK = {
    "billing_dispute": "I'm sorry the charge on your bill doesn't look right — I've reviewed it and opened a billing review.",
    "late_fee": "I understand how frustrating an unexpected late fee is.",
    "autopay": "I'm sorry your automatic payment didn't go through as expected.",
    "outage": "I'm sorry your service has been interrupted. Our teams are aware and working to restore it.",
    "no_internet": "I'm sorry you're without internet — let's get you back online.",
    "slow_speed": "I'm sorry your speeds aren't what they should be.",
    "appointment": "I apologise for the trouble with your appointment.",
    "cancel": "I'm sorry to hear you're thinking about leaving, and I'd like to help.",
    "move": "Congratulations on your move — we'd love to keep you connected.",
    "porting": "I can help with your phone number.",
    "equipment_return": "Thank you for returning your equipment.",
    "payment_arrangement": "Thank you for reaching out before your balance gets further behind — we have options that can help.",
    "general": "Thank you for reaching out.",
}


def agent_assist(text: str, collection: str | None = None, channel: str = "email", use_llm: bool = True,
                 user: str = "") -> dict[str, Any]:
    t0 = time.time()
    collection = collection or config.get().default_collection
    cls = classify(text, use_llm)
    ents = entities(text)
    intent = cls["intent"]
    spec = INTENTS.get(intent, {"query": "", "topic": "your account", "label": "General enquiry"})
    query = f"{spec['query']} {text[:400]}"
    res = retrieval.search(collection, query, k=6, filters={"doc_type": ["runbook", "policy"]}, use_llm=use_llm)
    chunks = res["results"]
    fl = flags(text)
    esc_chunks: list[dict[str, Any]] = []
    if fl:
        esc = retrieval.search(collection, "escalation " + " ".join(f["flag"] for f in fl) + " complaint contacts", k=2,
                               filters={"doc_key": "RB-ESC-010"}, rewrite=False, use_mmr=False, use_llm=False)
        esc_chunks = esc["results"]
        chunks = chunks + [c for c in esc_chunks if c["chunk_id"] not in {x["chunk_id"] for x in chunks}]
    steps = _steps(chunks)
    used = {s["cite"] for s in steps}
    routes = list(res["model_route"])
    name = ents["customer_name"].split()[0] if ents["customer_name"] else "there"
    draft_mode = "template (offline)"
    if use_llm and gateway.available():
        try:
            prompt = (f"Customer {channel}:\n{text[:3000]}\n\nDetected intent: {spec['label']}. Entities: {ents}.\n\n"
                      f"Knowledge base context:\n{answer_mod.context_block(chunks)}\n\n"
                      "Write a short, warm, professional reply from DataPlatform Customer Care. Use ONLY facts from the context, "
                      "cite the passages you rely on as [n] after the sentence (the agent removes them before sending), "
                      "never promise credits beyond the policy, and end with clear next steps.")
            draft, route = gateway.chat([{"role": "system", "content": answer_mod.SYSTEM}, {"role": "user", "content": prompt}],
                                        temperature=0.3, max_tokens=600)
            routes.append({**route, "stage": "draft"})
            cited = set(answer_mod.cited_numbers(draft, len(chunks)))
            if cited:
                used |= cited
                draft_mode = "LLM (chat-default)"
            else:                       # ungrounded draft → discard, use the cited template
                draft = ""
                draft_mode = "template (LLM draft discarded: no citations)"
        except Exception:  # noqa: BLE001
            draft = ""
    else:
        draft = ""
    if not draft:
        facts = _facts(text, chunks)
        used |= {i for _, i in facts}
        acct = f" (account {ents['bans'][0]})" if ents["bans"] else ""
        lines = [f"Hello {name},", "", f"Thank you for contacting DataPlatform about {spec['topic']}{acct}. {ACK.get(intent, ACK['general'])}"]
        if facts:
            lines += ["", "Here is what applies to your situation:"] + [f"- {s} [{i}]" for s, i in facts]
        nxt = [re.split(r"(?<=[.!?])\s", s["step"].split(". ", 1)[-1].replace("**", "").strip() or s["step"])[0]
               for s in steps if not s["step"].split(". ", 1)[-1].replace("**", "").lower().startswith("if ") and re.search(r"\b(tell|send|confirm|offer|explain|apply|create|schedule)\b",
                                                                        s["step"], re.I)][:2]
        if nxt:
            lines += ["", "What happens next: we will " + "; then ".join(n[0].lower() + n[1:].rstrip(".") for n in nxt) + "."]
        lines += ["", "If you have any questions, just reply to this message.", "", "Kind regards,", "DataPlatform Customer Care"]
        draft = "\n".join(lines)
    clean = re.sub(r"\s*\[\d{1,2}\]", "", draft)
    nba = (fl[0]["action"] if fl else (steps[0]["step"] if steps else "Review the customer's account and respond."))
    out = {"intent": cls, "entities": ents, "flags": fl, "checklist": steps, "next_best_action": nba,
           "draft_reply": draft, "draft_reply_clean": clean, "draft_mode": draft_mode,
           "citations": _cites(chunks, used), "chunks": chunks, "model_route": routes,
           "latency_ms": int((time.time() - t0) * 1000), "collection": collection}
    res["model_route"] = routes
    retrieval.log_search(res, user=user, mode="agent_assist", answer=clean, n_citations=len(used), latency_ms=out["latency_ms"])
    return out


def _region(wc: str) -> str:
    st = wc[4:6]                        # CLLI chars 5-6 = state
    return next((r for r, sts in REGIONS.items() if st in sts), "")


def _table_rows(text: str) -> tuple[list[str], list[list[str]]]:
    rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in text.split("\n") if ln.strip().startswith("|")]
    return (rows[0], rows[1:]) if rows else ([], [])


def runbook_copilot(question: str, collection: str | None = None, use_llm: bool = True, user: str = "") -> dict[str, Any]:
    t0 = time.time()
    collection = collection or config.get().default_collection
    ents = entities(question)
    wc = ents["wire_centers"][0] if ents["wire_centers"] else ""
    region = _region(wc) if wc else ""
    m = re.search(r"(\d[\d,]{2,})\s*(customers|subscribers|onts|homes)", question, re.I)
    count = int(m.group(1).replace(",", "")) if m else None
    sev = ("SEV1" if count and count > 5000 else "SEV2" if count and count >= 500 else "SEV3" if count else
           "SEV1" if re.search(r"whole wire cent|entire wire cent|all customers", question, re.I) else "assess (SEV1–3)")
    noc = retrieval.search(collection, f"outage wire center triage first 15 minutes restoration {question}", k=4,
                           filters={"doc_key": ["RB-NOC-011"]}, use_mmr=False, use_llm=use_llm)
    comms = retrieval.search(collection, f"outage communications procedure IVR banner first message {question}", k=3,
                             filters={"doc_key": ["RB-OUT-002"]}, use_mmr=False, use_llm=False, rewrite=False)
    esc = retrieval.search(collection, f"network outage SEV1 escalation NOC duty manager regional network ops manager {region}",
                           k=3, filters={"doc_key": ["RB-ESC-010"]}, use_mmr=False, use_llm=False, rewrite=False)
    local = retrieval.search(collection, f"wire center {wc} field ops supervisor contacts OLT", k=3,
                             filters={"doc_key": ["RB-FLD-014"]}, use_mmr=False, use_llm=False, rewrite=False) if wc else {"results": []}
    chunks: list[dict[str, Any]] = []
    for r in (noc["results"], comms["results"], esc["results"], local["results"]):
        for c in r:
            if c["chunk_id"] not in {x["chunk_id"] for x in chunks}:
                chunks.append(c)
    idx = {c["chunk_id"]: i + 1 for i, c in enumerate(chunks)}
    noc_steps = _steps([c for c in chunks if c["doc_key"] == "RB-NOC-011"], limit=14, prefer="triage first 15 minutes restoration")
    for s in noc_steps:
        s["cite"] = next(idx[c["chunk_id"]] for c in chunks if c["doc_key"] == s["doc_key"] and c["section"] == s["section"])
    comm_steps = _steps([c for c in chunks if c["doc_key"] == "RB-OUT-002"], limit=8, prefer="procedure communications lead")
    for s in comm_steps:
        s["cite"] = next(idx[c["chunk_id"]] for c in chunks if c["doc_key"] == s["doc_key"] and c["section"] == s["section"])
    contacts: list[dict[str, Any]] = []
    for c in esc["results"]:
        header, rows = _table_rows(c["text"] or "")
        for r in rows:
            if r and re.search(r"outage sev1|medical", r[0], re.I) and len(r) >= 4:
                lvl2 = r[2]
                if region and region in lvl2:   # narrow the regional list to this wire center's region
                    mm = re.search(rf"{region}:\s*([^;]+)", lvl2)
                    lvl2 = f"Regional Network Ops Manager ({region}) — {mm.group(1).strip()}" if mm else lvl2
                contacts.append({"situation": r[0], "level_1": r[1], "level_2": lvl2, "level_3": r[3], "cite": idx[c["chunk_id"]]})
    local_row: dict[str, Any] = {}
    for c in local["results"]:
        header, rows = _table_rows(c["text"] or "")
        for r in rows:
            if wc and r and r[0].strip().upper() == wc:
                local_row = {**dict(zip(header, r)), "cite": idx[c["chunk_id"]]}
                break
        if local_row:
            break
    narrative = ""
    routes = list(noc["model_route"])
    if use_llm and gateway.available():
        try:
            narrative, route = gateway.chat([{"role": "system", "content": answer_mod.SYSTEM},
                                             {"role": "user", "content": f"Context:\n{answer_mod.context_block(chunks)}\n\n"
                                              f"Question: {question}\nGive a step-by-step response plan with escalation contacts, citing [n]."}],
                                            temperature=0.1, max_tokens=800)
            routes.append({**route, "stage": "answer"})
        except Exception:  # noqa: BLE001
            narrative = ""
    used = {s["cite"] for s in noc_steps + comm_steps} | {c["cite"] for c in contacts} | ({local_row["cite"]} if local_row else set())
    out = {"question": question, "wire_center": wc, "region": region, "severity": sev, "customer_count": count,
           "noc_steps": noc_steps, "communication_steps": comm_steps, "escalation": contacts, "local_contacts": local_row,
           "narrative": narrative, "citations": _cites(chunks, used), "chunks": chunks, "model_route": routes,
           "latency_ms": int((time.time() - t0) * 1000), "collection": collection}
    noc["model_route"] = routes
    retrieval.log_search(noc, user=user, mode="runbook_copilot", answer=narrative or f"{len(noc_steps)} steps, {len(contacts)} contacts",
                         n_citations=len(used), latency_ms=out["latency_ms"])
    return out


def design_qa(question: str, collection: str | None = None, use_llm: bool = True, user: str = "") -> dict[str, Any]:
    return answer_mod.answer(collection or config.get().default_collection, question, k=6, filters={"doc_type": ["design"]},
                             use_llm=use_llm, user=user, log_mode="design_qa")


SAMPLES = [
    {"id": "late-fee-email", "channel": "email", "title": "Late fee with autopay on (email)",
     "text": "Subject: Late fee on my bill??\n\nHi,\n\nMy account number is 3100000008. I have autopay set up, but my latest invoice "
             "3100000008-2609 shows a $10.00 late fee. I updated my card last month and I guess the payment didn't go through. "
             "This is the first time this has happened in years. Can you remove the fee?\n\nThanks,\nMaria Lopez"},
    {"id": "dispute-promo", "channel": "email", "title": "Promo price not applied (email)",
     "text": "Hello, my bill jumped from $65 to $79 this month on account 3100000043. When I signed up for Fiber Gig (FB-1G) I was "
             "promised $65 for 12 months and it has only been 7 months. I want this corrected and refunded. If this isn't fixed "
             "I'll file a complaint with the state PUC.\n\nRegards,\nDavid Chen"},
    {"id": "no-internet-call", "channel": "call", "title": "Red LOS light, no internet (call transcript)",
     "text": "Agent: Thank you for calling DataPlatform, how can I help?\nCustomer: My internet has been down since this morning. "
             "The little box on the wall, the fiber box, has a red light that says LOS.\nAgent: I'm sorry about that. Can I have "
             "your account number?\nCustomer: It's 3100000070. I work from home so I really need this fixed today.\n"
             "Agent: Let me check for outages in your area."},
    {"id": "cancel-chat", "channel": "chat", "title": "Cancel — copper DSL too slow (chat)",
     "text": "I want to cancel my service. Internet 25 on copper (CU-25) is way too slow for my kids' school work and the cable company is offering "
             "me 300 Mbps for less. Account 3100000113. Is fiber ever coming to my street?"},
    {"id": "move-email", "channel": "email", "title": "Moving next month (email)",
     "text": "Hi team, we're moving to a new address in Raleigh on the 15th of next month. Can we take our DataPlatform fiber with us "
             "and keep the same price? Account 3100000156. Also, what do I do with the router?\n\nBest,\nPriya"},
]

RUNBOOK_SAMPLES = [
    "Mass LOS alarms in wire center RLGHNCXA — about 6,200 customers offline since 07:40. What do I do and who do I call?",
    "Dying-gasp burst across all PONs in MNFDOHXA after a storm, roughly 1,200 customers affected. Next steps?",
    "Customers in TYLRTXXA report no service; one OLT shelf is unreachable. Escalation path?",
]

DESIGN_SAMPLES = [
    "How do GPON and XGS-PON coexist on the same fiber plant?",
    "What happens to a disputed amount in contract accounting during dunning?",
    "Where does agent assist get its knowledge suggestions in the contact-center architecture?",
    "What split ratio and optical budget do we design PON networks for?",
]
