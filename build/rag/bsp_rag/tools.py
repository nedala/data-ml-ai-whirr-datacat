"""MCP-ready knowledge tools for the AI harness MCP server (build/mcp).

Register them as-is, e.g. with FastMCP:

    from bsp_rag import tools as rag_tools
    for fn in rag_tools.TOOLS:
        mcp.tool(name=fn.__name__)(fn)

Each function has type hints + a docstring written for an LLM tool caller.
By default the functions call the rag service over HTTP (RAG_URL, default
http://rag:8101/rag) so state, logging (rag.queries) and governance stay in
one place; inside the rag service itself (BSP_RAG_IN_PROCESS=1) they call
the library directly. Only `httpx` is needed in HTTP mode.
"""

from __future__ import annotations

import os
from typing import Any

DEFAULT_COLLECTION = os.environ.get("RAG_DEFAULT_COLLECTION", "frontoffice")


def _in_process() -> bool:
    return os.environ.get("BSP_RAG_IN_PROCESS", "") == "1"


def _http(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    import httpx

    base = os.environ.get("RAG_URL", "http://rag:8101/rag").rstrip("/")
    headers = {"X-Auth-User": os.environ.get("RAG_TOOLS_CALLER", "mcp")}
    tok = os.environ.get("RAG_TOOLS_TOKEN", "")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    r = httpx.request(method, base + "/tools" + path, json=body, headers=headers, timeout=120)
    r.raise_for_status()
    return r.json()


def _slim(chunks: list[dict[str, Any]], n: int = 8, chars: int = 900) -> list[dict[str, Any]]:
    return [{"rank": c.get("rank"), "doc_id": c.get("doc_key"), "title": c.get("title"), "section": c.get("section"),
             "doc_type": c.get("doc_type"), "score": round(float(c.get("rerank_score", c.get("score", 0))), 4),
             "text": (c.get("text") or "")[:chars], "source": c.get("uri")} for c in chunks[:n]]


def rag_search(query: str, collection: str = DEFAULT_COLLECTION, k: int = 6, doc_type: str = "") -> dict[str, Any]:
    """Search DataPlatform's governed front-office knowledge base (runbooks, policies,
    network/billing/contact-center design docs, FAQs) with hybrid semantic + keyword
    retrieval. Use this to find the exact runbook steps, policy limits or design facts
    before answering a question about billing disputes, late fees, outages, ONT/fiber
    troubleshooting, truck rolls, retention offers, porting, moves, equipment returns,
    payment arrangements or escalation contacts.

    Args:
        query: natural-language question or keywords (e.g. "late fee reversal autopay failure").
        collection: knowledge collection name (default "frontoffice").
        k: number of passages to return (1-10).
        doc_type: optional filter: runbook | policy | design | faq | reference.
    Returns: {"results": [{rank, doc_id, title, section, doc_type, score, text, source}], "rewritten_query"}.
    """
    k = max(1, min(int(k), 10))
    if _in_process():
        from . import retrieval
        res = retrieval.search(collection, query, k=k, filters={"doc_type": doc_type} if doc_type else None)
        retrieval.log_search(res, user="mcp", mode="tool_search")
        return {"results": _slim(res["results"], k), "rewritten_query": res["rewritten"], "collection": collection}
    return _http("POST", "/search", {"query": query, "collection": collection, "k": k, "doc_type": doc_type})


def rag_answer(question: str, collection: str = DEFAULT_COLLECTION, doc_type: str = "") -> dict[str, Any]:
    """Answer a question from DataPlatform's front-office knowledge base with numbered
    citations ([1], [2] …) that point at the source runbook/policy/design sections.
    Says "I don't know" when the knowledge base does not cover the question — relay that
    honestly instead of guessing. Prefer this over answering from memory for any
    DataPlatform procedure, policy amount, SLA or escalation contact.

    Args:
        question: the question in plain language.
        collection: knowledge collection name (default "frontoffice").
        doc_type: optional filter: runbook | policy | design | faq | reference.
    Returns: {"answer", "citations": [{n, doc_id, title, section, source}], "mode"}.
    """
    if _in_process():
        from . import answer as answer_mod
        a = answer_mod.answer(collection, question, filters={"doc_type": doc_type} if doc_type else None, user="mcp",
                              log_mode="tool_answer")
        return {"answer": a["answer"], "mode": a["mode"], "collection": collection,
                "citations": [{"n": c["n"], "doc_id": c["doc_key"], "title": c["title"], "section": c["section"],
                               "source": c["uri"]} for c in a["citations"]]}
    return _http("POST", "/answer", {"question": question, "collection": collection, "doc_type": doc_type})


def rag_list_collections() -> dict[str, Any]:
    """List the knowledge collections available for rag_search / rag_answer, with their
    vector backend (qdrant | pgvector), embedding model, document and chunk counts.
    Returns: {"collections": [{name, description, backend, embed_model, n_docs, n_chunks}]}.
    """
    if _in_process():
        from . import meta
        return {"collections": [{k: c.get(k) for k in ("name", "description", "backend", "embed_model", "chunker",
                                                       "n_docs", "n_chunks")} for c in meta.get().collections()]}
    return _http("GET", "/collections")


def rag_agent_assist(customer_message: str, channel: str = "email", collection: str = DEFAULT_COLLECTION) -> dict[str, Any]:
    """Front-office agent assist: given a customer email, chat or call transcript, classify
    the intent, extract BAN/invoice/CLLI wire-center entities, retrieve the matching runbook
    and return a step checklist, escalation flags and a drafted reply — all with citations.
    The draft is for a human agent to review; never send it to a customer automatically.

    Args:
        customer_message: the raw customer text (email body, transcript, ticket).
        channel: email | chat | call | ticket.
        collection: knowledge collection name (default "frontoffice").
    """
    if _in_process():
        from . import frontoffice
        r = frontoffice.agent_assist(customer_message, collection, channel, user="mcp")
        return _assist_slim(r)
    return _http("POST", "/agent-assist", {"customer_message": customer_message, "channel": channel, "collection": collection})


def rag_runbook_copilot(situation: str, collection: str = DEFAULT_COLLECTION) -> dict[str, Any]:
    """Runbook copilot for network incidents: describe the situation (e.g. "mass LOS in wire
    center RLGHNCXA, 6,000 customers down") and get the ordered NOC and customer-communication
    steps, the severity, escalation contacts for the region and local wire-center contacts,
    each with citations to the source runbooks.
    """
    if _in_process():
        from . import frontoffice
        r = frontoffice.runbook_copilot(situation, collection, user="mcp")
        return _copilot_slim(r)
    return _http("POST", "/runbook-copilot", {"situation": situation, "collection": collection})


def _assist_slim(r: dict[str, Any]) -> dict[str, Any]:
    return {"intent": r["intent"].get("intent"), "intent_method": r["intent"].get("method"), "entities": r["entities"],
            "flags": r["flags"], "next_best_action": r["next_best_action"], "checklist": r["checklist"],
            "draft_reply": r["draft_reply"], "citations": [{"n": c["n"], "doc_id": c["doc_key"], "title": c["title"],
                                                            "section": c["section"]} for c in r["citations"]]}


def _copilot_slim(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("wire_center", "region", "severity", "noc_steps", "communication_steps", "escalation",
                              "local_contacts", "narrative")} | {
        "citations": [{"n": c["n"], "doc_id": c["doc_key"], "title": c["title"], "section": c["section"]} for c in r["citations"]]}


TOOLS = [rag_search, rag_answer, rag_list_collections, rag_agent_assist, rag_runbook_copilot]
