"""Hybrid retrieval: query rewrite → dense (embeddings) + lexical (BM25 sparse
in Qdrant / Postgres full-text) → reciprocal-rank fusion → optional rerank
(LLM via chat-default, or a lexical-coverage reranker offline) → optional MMR
diversity. Returns per-stage scores so the Explore playground can show them."""

from __future__ import annotations

import logging
import math
import os
import time
from typing import Any

from . import embeddings, gateway, ingest, meta as meta_mod, stores
from .textutil import expand_query, overlap, sparse_query, tokens

log = logging.getLogger("bsp_rag.retrieval")

MODES = ("hybrid", "dense", "lexical")
RRF_K = 60


def rewrite_query(q: str, use_llm: bool) -> tuple[str, dict[str, Any] | None]:
    if use_llm and gateway.available():
        try:
            text, route = gateway.chat([
                {"role": "system", "content": "Rewrite the user's message into ONE concise search query for an internal "
                 "knowledge base of ISP runbooks, policies and design docs. Keep account-independent key terms "
                 "(product, symptom, policy name, wire center). Return only the query."},
                {"role": "user", "content": q[:2000]}], temperature=0, max_tokens=60)
            text = text.strip().strip('"').splitlines()[0][:300] if text.strip() else ""
            if text:
                return expand_query(text), {**route, "stage": "rewrite"}
        except Exception as e:  # noqa: BLE001
            log.info("LLM rewrite failed, using offline expansion: %s", e)
    return expand_query(q), None


def _rrf(lists: list[list[stores.Hit]]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for hits in lists:
        for rank, h in enumerate(hits, 1):
            scores[h.id] = scores.get(h.id, 0.0) + 1.0 / (RRF_K + rank)
    return scores


def _cos(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def mmr(cands: list[dict[str, Any]], vecs: dict[str, list[float]], k: int, lam: float = 0.7) -> list[dict[str, Any]]:
    if not cands:
        return []
    top = max(c["score"] for c in cands) or 1.0
    rel = {c["chunk_id"]: c["score"] / top for c in cands}
    chosen: list[dict[str, Any]] = []
    pool = list(cands)
    while pool and len(chosen) < k:
        def val(c):
            v = vecs.get(c["chunk_id"])
            sim = max((_cos(v, vecs[s["chunk_id"]]) for s in chosen if v and vecs.get(s["chunk_id"])), default=0.0)
            same_doc = sum(1 for s in chosen if s["doc_key"] == c["doc_key"])
            return lam * rel[c["chunk_id"]] - (1 - lam) * max(sim, 0.35 * same_doc)
        best = max(pool, key=val)
        best["mmr_score"] = round(val(best), 4)
        chosen.append(best)
        pool.remove(best)
    return chosen


def lexical_rerank(q: str, cands: list[dict[str, Any]]) -> None:
    qt = set(tokens(q))
    for c in cands:
        hay = f"{c.get('title', '')} {c.get('section', '')} {c.get('text', '')}"
        cov = overlap(q, hay) if qt else 0.0
        head = overlap(q, f"{c.get('title', '')} {c.get('section', '')}") if qt else 0.0
        c["rerank_score"] = round(0.6 * cov + 0.25 * head + 0.15 * (c["score"] / (c["_top"] or 1.0)), 4)


def llm_rerank(q: str, cands: list[dict[str, Any]]) -> dict[str, Any] | None:
    passages = "\n\n".join(f"[{i}] {c.get('title')} › {c.get('section')}\n{c.get('text', '')[:700]}"
                           for i, c in enumerate(cands))
    data, route = gateway.chat_json(
        f"Query: {q}\n\nPassages:\n{passages}\n\nScore each passage 0-10 for how well it answers the query. "
        'Return JSON {"scores": [{"i": <index>, "s": <score>}, ...]}.',
        system="You are a precise relevance judge for an ISP knowledge base.")
    by_i = {int(x.get("i")): float(x.get("s", 0)) for x in data.get("scores", []) if isinstance(x, dict)}
    lexical_rerank(q, cands)        # blend: a weak judge model cannot wreck a good first-stage ranking
    for i, c in enumerate(cands):
        c["llm_score"] = round(by_i.get(i, 0.0) / 10, 3)
        c["rerank_score"] = round(0.5 * c["llm_score"] + 0.5 * c["rerank_score"], 4)
    return {**route, "stage": "rerank"}


def search(collection: str, query: str, k: int = 8, filters: dict[str, Any] | None = None, mode: str = "hybrid",
           rewrite: bool = True, use_mmr: bool = True, mmr_lambda: float = 0.7, rerank: str = "auto",
           fetch_k: int = 40, use_llm: bool = True) -> dict[str, Any]:
    t0 = time.time()
    coll = ingest.get_collection(collection)
    store = stores.get(coll["backend"])
    emb = embeddings.for_collection(coll["embed_model"], int(coll["embed_dim"]))
    routes: list[dict[str, Any]] = []
    q2, route = rewrite_query(query, use_llm) if rewrite else (query, None)
    if route:
        routes.append(route)
    timings: dict[str, int] = {}
    dense: list[stores.Hit] = []
    lex: list[stores.Hit] = []
    if mode in ("hybrid", "dense"):
        t = time.time()
        qv = embeddings.cache().embed(emb, [q2])[0]
        dense = store.dense_search(coll, qv, fetch_k, filters)
        timings["dense_ms"] = int((time.time() - t) * 1000)
        if not emb.offline:
            routes.append({"stage": "embed", "alias": emb.name})
    if mode in ("hybrid", "lexical"):
        t = time.time()
        lex = store.lexical_search(coll, q2, sparse_query(q2), fetch_k, filters)
        timings["lexical_ms"] = int((time.time() - t) * 1000)
    fused = _rrf([dense, lex]) if mode == "hybrid" else {h.id: h.score for h in (dense or lex)}
    info: dict[str, dict[str, Any]] = {}
    for name, hits in (("dense", dense), ("lexical", lex)):
        for rank, h in enumerate(hits, 1):
            d = info.setdefault(h.id, {"payload": h.payload})
            d[f"{name}_score"] = round(h.score, 4)
            d[f"{name}_rank"] = rank
    cands = []
    for cid, s in sorted(fused.items(), key=lambda t: -t[1])[:fetch_k]:
        p = info[cid]["payload"]
        cands.append({"chunk_id": cid, "score": round(s, 5), "doc_key": p.get("doc_key"), "title": p.get("title"),
                      "section": p.get("section"), "doc_type": p.get("doc_type"), "system": p.get("system"),
                      "text": p.get("text", ""), "uri": p.get("uri"), "source": p.get("source"),
                      "document_id": p.get("document_id"), "ordinal": p.get("ordinal"),
                      **{kk: vv for kk, vv in info[cid].items() if kk != "payload"}})
    top = cands[0]["score"] if cands else 1.0
    for c in cands:
        c["_top"] = top
    rr = rerank
    if rr == "auto":   # LLM rerank is opt-in (RAG_RERANK=llm): ~20 passages per query is slow on CPU models
        rr = "llm" if (os.environ.get("RAG_RERANK", "lexical") == "llm" and use_llm and gateway.available()) else "lexical"
    if cands and rr in ("llm", "lexical"):
        t = time.time()
        pool = cands[: min(len(cands), 20)]
        try:
            if rr == "llm":
                r = llm_rerank(q2, pool)
                if r:
                    routes.append(r)
            else:
                lexical_rerank(query, pool)
        except Exception as e:  # noqa: BLE001
            log.info("rerank %s failed (%s); lexical fallback", rr, e)
            rr = "lexical"
            lexical_rerank(query, pool)
        pool.sort(key=lambda c: -c.get("rerank_score", 0))
        cands = pool + cands[len(pool):]
        timings["rerank_ms"] = int((time.time() - t) * 1000)
    if use_mmr and len(cands) > k:
        t = time.time()
        base = [dict(c, score=c.get("rerank_score", c["score"])) for c in cands[: max(k * 3, 15)]]
        vecs = store.vectors(coll, [c["chunk_id"] for c in base])
        results = mmr(base, vecs, k, mmr_lambda)
        timings["mmr_ms"] = int((time.time() - t) * 1000)
    else:
        results = cands[:k]
    for i, c in enumerate(results, 1):
        c["rank"] = i
        c.pop("_top", None)
    timings["total_ms"] = int((time.time() - t0) * 1000)
    return {"collection": collection, "query": query, "rewritten": q2, "mode": mode, "filters": filters or {},
            "rerank": rr if cands else "none", "mmr": use_mmr, "results": results, "timings": timings,
            "model_route": routes, "embed_model": coll["embed_model"], "backend": coll["backend"]}


def log_search(res: dict[str, Any], user: str = "", mode: str = "search", answer: str | None = None,
               n_citations: int = 0, status: str = "ok", latency_ms: int | None = None) -> None:
    meta_mod.get().log_query({
        "collection": res.get("collection"), "user": user, "mode": mode, "query": res.get("query"),
        "rewritten": res.get("rewritten"), "filters": res.get("filters"),
        "top_ids": [r["chunk_id"] for r in res.get("results", [])],
        "scores": [round(r.get("rerank_score", r.get("score", 0)), 4) for r in res.get("results", [])],
        "latency_ms": latency_ms if latency_ms is not None else res.get("timings", {}).get("total_ms"),
        "model_route": {"routes": res.get("model_route", []), "backend": res.get("backend"), "embed": res.get("embed_model"),
                        "rerank": res.get("rerank"), "retrieval": res.get("mode")},
        "answer": answer, "n_citations": n_citations, "status": status})
