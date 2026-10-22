"""Playground conversation history and next-question suggestions.

Chats are saved per signed-in user (the Studio gate's X-Auth-User) as JSON
files under $HARNESS_DATA_DIR/chats/<user>/<id>.json — on the appliance's own
disk, never in the browser. Suggestions ask a fast model for three short
follow-up questions grounded in the conversation and the enabled tool groups.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
from pathlib import Path
from fastapi import APIRouter, HTTPException, Request

from . import router as rt
from .config import data_dir
from .identity import user_of

MAX_CHATS = 200
SUGGEST_MODEL = "chat-default"   # the routed alias (Vertex first per LLM_PROVIDER, local fallback)
_ID = re.compile(r"^[a-z0-9]{12}$")


def _user(request: Request) -> str:
    return user_of(request)


def _dir(user: str) -> Path:
    slug = re.sub(r"[^a-z0-9._-]", "_", user)[:80] + "-" + hashlib.sha256(user.encode()).hexdigest()[:8]
    d = data_dir() / "chats" / slug
    d.mkdir(parents=True, exist_ok=True)
    return d


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _title(messages: list[dict]) -> str:
    first = next((m.get("content") for m in messages if m.get("role") == "user"), "") or ""
    if isinstance(first, list):   # multimodal content parts
        first = " ".join(p.get("text", "") for p in first if isinstance(p, dict))
    first = " ".join(str(first).split())
    return (first[:70] + "…") if len(first) > 70 else (first or "New chat")


def _public(doc: dict) -> dict:
    return {k: doc.get(k) for k in ("id", "title", "created", "updated", "model", "turns")}


api = APIRouter()


@api.get("/chats")
def list_chats(request: Request) -> dict:
    out = []
    for f in _dir(_user(request)).glob("*.json"):
        try:
            out.append(_public(json.loads(f.read_text())))
        except (OSError, ValueError):
            continue
    out.sort(key=lambda c: c.get("updated") or "", reverse=True)
    return {"chats": out}


@api.get("/chats/{cid}")
def get_chat(cid: str, request: Request) -> dict:
    if not _ID.match(cid):
        raise HTTPException(400, "bad chat id")
    f = _dir(_user(request)) / f"{cid}.json"
    if not f.is_file():
        raise HTTPException(404, "chat not found")
    return json.loads(f.read_text())


@api.put("/chats/{cid}")
async def save_chat(cid: str, request: Request) -> dict:
    if cid == "new":
        cid = secrets.token_hex(6)
    if not _ID.match(cid):
        raise HTTPException(400, "bad chat id")
    body = await request.json()
    messages = [m for m in (body.get("messages") or []) if m.get("role") != "system"]
    d = _dir(_user(request))
    f = d / f"{cid}.json"
    created = _now()
    if f.is_file():
        try:
            created = json.loads(f.read_text()).get("created") or created
        except ValueError:
            pass
    doc = {"id": cid, "title": body.get("title") or _title(messages), "created": created, "updated": _now(),
           "model": body.get("model"), "turns": sum(1 for m in messages if m.get("role") == "user"),
           "messages": messages, "timeline": body.get("timeline") or []}
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc))
    tmp.replace(f)
    # keep the newest MAX_CHATS
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[MAX_CHATS:]:
        old.unlink(missing_ok=True)
    return _public(doc)


@api.delete("/chats/{cid}")
def delete_chat(cid: str, request: Request) -> dict:
    if not _ID.match(cid):
        raise HTTPException(400, "bad chat id")
    (_dir(_user(request)) / f"{cid}.json").unlink(missing_ok=True)
    return {"deleted": cid}


STARTERS = {
    "browser": ["Summarize today's top stories on news.google.com", "Open dataplatform.com and list the current fiber plans"],
    "warehouse": ["Compare churn for copper vs fiber customers over the last quarter",
                  "Show monthly net revenue and ARPU by technology for the last 6 months",
                  "Summarize BAN 3100000016: plan, invoices, tickets and disputes"],
    "trino": ["Join Oracle billing, SQL Server care orders and the warehouse: which technology has the most open care "
              "orders on unpaid invoices?",
              "Which wire centers have both the most repeat dispatches and the highest overdue balance in Oracle?",
              "Show me how Trino plans a query across the warehouse, Oracle and SQL Server",
              "Find the BigQuery BRIM BAN mapping table in the catalog and compare its row count with our warehouse "
              "customer count",
              "Which BigQuery projects and datasets are catalogued, and which of their tables carry a BAN?",
              "What systems and tables can you query, and how do they join?"],
    "rag": ["What is the runbook for an outage in a wire center (CLLI)?", "How do we handle a late-fee dispute with autopay on?"],
    "atlan": ["Find the certified tables about churn in Atlan and show their lineage"],
    "catalog": ["Search the catalog for tables about wire centers (CLLI) across all systems",
                "Which catalogued tables carry suspected personal data?"],
    "m365": ["Summarize unread emails in the billing-disputes mailbox"],
    "mlflow": ["Which churn model is the current champion, and how does it compare to the challenger?"],
    "powerbi": ["List the Power BI reports in the Revenue & Churn workspace"],
    "bigquery": ["Which BigQuery datasets can I query, and what's in them?"],
    "sap": ["Look up the SAP BRIM dispute status for BAN 3100008373"],
}


@api.post("/suggest")
async def suggest(request: Request) -> dict:
    """Three next-best questions for the conversation (or starters for an empty one)."""
    body = await request.json()
    groups = [g for g in (body.get("groups") or []) if g in STARTERS]
    messages = [m for m in (body.get("messages") or []) if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)]
    if not messages:
        order = groups or list(STARTERS)
        lead = STARTERS["trino"][:3] if "trino" in order else []   # lead with data virtualization
        picks = lead + [q for g in order if g != "trino" for q in STARTERS[g][:1]]
        # cross-system questions run best on the text-to-SQL alias (Gemini 2.5 Pro first)
        return {"questions": picks[:4], "source": "starters", "model_hints": {q: "sql-default" for q in lead}}
    convo = "\n".join(f"{m['role'].upper()}: {m['content'][:1500]}" for m in messages[-6:])
    prompt = ("You suggest the next questions a DataPlatform (US internet provider) analyst would most usefully ask next. "
              f"Tools available to the assistant: {', '.join(groups) or 'none'}. Conversation so far:\n{convo}\n\n"
              'Return JSON {"questions": [three short, specific follow-up questions, each under 90 characters, '
              "that dig deeper into the SAME topic as the last answer, answerable with the available tools, not repeating "
              "what was already answered and not assuming facts that were not in the conversation]}.")
    try:
        res, _ = await rt.chat({"model": body.get("model") or SUGGEST_MODEL, "temperature": 0.4, "max_tokens": 300,
                                "response_format": {"type": "json_object"},
                                "messages": [{"role": "user", "content": prompt}]},
                               caller=_user(request) + ":suggest")
        text = res["message"].get("content") or "{}"
        m = re.search(r"\{.*\}", text, re.S)
        qs = [str(q).strip() for q in json.loads(m.group(0) if m else text).get("questions", []) if str(q).strip()]
        return {"questions": qs[:3], "source": "model"}
    except Exception as e:  # noqa: BLE001 — suggestions are best effort
        return {"questions": [], "source": "error", "error": f"{type(e).__name__}: {e}"[:200]}
