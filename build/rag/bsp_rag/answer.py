"""Grounded answers with numbered citations.

LLM path (chat-default via the gateway): numbered context, strict "answer only
from the context, cite [n], otherwise say you don't know". Offline path: an
extractive answerer that selects the sentences / runbook steps that best
cover the question and cites them. Both abstain ("I don't know") when the
retrieved context does not cover the question."""

from __future__ import annotations

import math
import re
import time
from typing import Any

from . import gateway, retrieval
from .textutil import overlap, tokens

MIN_COVERAGE = 0.32
IDK = "I don't know — the knowledge base does not contain enough information to answer that."

SYSTEM = """You are DataPlatform's internal front-office knowledge assistant.
Answer ONLY from the numbered context passages. After every sentence that uses a passage, cite it like [1] or [2][3].
If the context does not contain the answer, reply exactly: "I don't know — the knowledge base does not contain enough information to answer that."
Be concise and procedural: use numbered steps when the source is a procedure. Never invent phone numbers, amounts, or policy limits."""


def context_block(results: list[dict[str, Any]], max_chars: int = 1400) -> str:
    return "\n\n".join(f"[{i}] {r.get('title')} ({r.get('doc_key')}) › {r.get('section') or 'overview'}\n{(r.get('text') or '')[:max_chars]}"
                       for i, r in enumerate(results, 1))


_DF: dict[str, tuple[int, int, dict[str, int]]] = {}


def _df(collection: str) -> tuple[int, dict[str, int]]:
    """Per-collection document frequencies over chunks (cached by chunk count)."""
    from . import meta as meta_mod

    m = meta_mod.get()
    coll = m.collection(collection)
    if not coll:
        return 1, {}
    n = (m.one(f"SELECT COUNT(*) AS n FROM {m.t('chunks')} WHERE collection_id=%s", (coll["id"],)) or {}).get("n", 0)
    cached = _DF.get(collection)
    if cached and cached[0] == coll["id"] and cached[1] == n:
        return n or 1, cached[2]
    df: dict[str, int] = {}
    for r in m.all(f"SELECT text FROM {m.t('chunks')} WHERE collection_id=%s", (coll["id"],)):
        for t in set(tokens(r["text"] or "")):
            df[t] = df.get(t, 0) + 1
    _DF[collection] = (coll["id"], n, df)
    return n or 1, df


def coverage(question: str, results: list[dict[str, Any]], collection: str | None = None) -> float:
    """IDF-weighted share of the question's content terms found in the best
    retrieved chunk. Terms the corpus never uses weigh the most, so an
    off-topic question ("holiday party dress code") scores low even if a
    common word like "code" matches."""
    if not results:
        return 0.0
    qt = list(dict.fromkeys(tokens(question)))
    if not qt:
        return 0.0
    n, df = _df(collection) if collection else (1, {})
    w = {t: math.log((n + 1) / (df.get(t, 0) + 0.5)) for t in qt}
    total = sum(w.values()) or 1.0
    best = 0.0
    for r in results[:4]:
        hay = set(tokens(f"{r.get('title')} {r.get('section')} {r.get('text')}"))
        best = max(best, sum(w[t] for t in qt if t in hay) / total)
    return best


def _units(text: str) -> list[str]:
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("```"):
            continue
        if re.match(r"^(\d+\.|•|\|)", line):
            out.append(line)
        else:
            out.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if len(s.strip()) > 20)
    return out


def extractive(question: str, results: list[dict[str, Any]], max_units: int = 6) -> tuple[str, list[int]]:
    qt = set(tokens(question))
    scored: list[tuple[float, int, int, str]] = []
    for ri, r in enumerate(results[:5]):
        head_bonus = 0.15 * overlap(question, f"{r.get('title')} {r.get('section')}")
        for ui, u in enumerate(_units(r.get("text") or "")):
            if u.startswith("|") and set(u) <= set("|-: "):
                continue
            ut = set(tokens(u))
            if not ut:
                continue
            s = len(qt & ut) / (len(qt) or 1) + head_bonus - 0.04 * ri
            if u[:2].rstrip(".").isdigit() or u.startswith(("1.", "2.", "3.")):
                s += 0.05            # favour procedure steps
            scored.append((s, ri, ui, u))
    if not scored:
        return IDK, []
    scored.sort(key=lambda t: -t[0])
    best = scored[0][0]
    if best < 0.18:
        return IDK, []
    picked = [t for t in scored[: max_units * 2] if t[0] >= max(0.12, best * 0.45)][:max_units]
    # keep the source order (steps stay in sequence)
    picked.sort(key=lambda t: (t[1], t[2]))
    lines, cited = [], []
    for _, ri, _, u in picked:
        u = u.strip()
        if u.startswith("|"):
            u = " — ".join(c.strip() for c in u.strip("|").split("|") if c.strip())
        u = u.lstrip("• ").strip()
        lines.append(f"{u} [{ri + 1}]")
        if ri + 1 not in cited:
            cited.append(ri + 1)
    return "From the knowledge base:\n" + "\n".join(("- " + l) if not re.match(r"^\d+\.", l) else l for l in lines), cited


def grounding(text: str, cited_chunks: list[dict[str, Any]]) -> float:
    """Share of answer sentences lexically supported by the cited passages (0..1)."""
    ctx = " ".join(f"{c.get('title')} {c.get('section')} {c.get('text')}" for c in cited_chunks)
    sents = [x for x in re.split(r"(?<=[.!?])\s+|\n+", re.sub(r"\[\d{1,2}\]", "", text)) if len(x.strip()) > 15]
    if not sents or not ctx:
        return 0.0
    return sum(1 for x in sents if overlap(x, ctx) >= 0.5) / len(sents)


def attach_citations(text: str, results: list[dict[str, Any]]) -> str:
    """Append [n] to each sentence that one retrieved passage clearly supports."""
    hays = [f"{r.get('title')} {r.get('section')} {r.get('text')}" for r in results]
    out = []
    for sent in re.split(r"(?<=[.!?])\s+", text.strip()):
        if len(sent) > 15 and not re.search(r"\[\d{1,2}\]", sent):
            scores = [overlap(sent, h) for h in hays]
            best = max(range(len(scores)), key=lambda i: scores[i]) if scores else -1
            if best >= 0 and scores[best] >= 0.5:
                sent = (sent[:-1] + f" [{best + 1}]" + sent[-1]) if sent[-1] in ".!?" else sent + f" [{best + 1}]"
        out.append(sent)
    return " ".join(out)


def cited_numbers(text: str, n: int) -> list[int]:
    out = []
    for m in re.finditer(r"\[(\d{1,2})\]", text):
        i = int(m.group(1))
        if 1 <= i <= n and i not in out:
            out.append(i)
    return out


def answer(collection: str, question: str, k: int = 6, filters: dict[str, Any] | None = None, mode: str = "hybrid",
           use_llm: bool = True, user: str = "", log: bool = True, search_kw: dict[str, Any] | None = None,
           log_mode: str = "ask") -> dict[str, Any]:
    t0 = time.time()
    res = retrieval.search(collection, question, k=k, filters=filters, mode=mode, use_llm=use_llm, **(search_kw or {}))
    results = res["results"]
    cov = coverage(question, results, collection)
    routes = list(res["model_route"])
    how = "extractive"
    if not results or cov < MIN_COVERAGE:
        text, cited = IDK, []
        how = "abstain"
    elif use_llm and gateway.available():
        try:
            text, route = gateway.chat([{"role": "system", "content": SYSTEM},
                                        {"role": "user", "content": f"Context:\n{context_block(results)}\n\nQuestion: {question}"}],
                                       temperature=0.1, max_tokens=700)
            routes.append({**route, "stage": "answer"})
            how = "llm"
            if not cited_numbers(text, len(results)) and not text.lower().startswith("i don't know"):
                text = attach_citations(text, results)      # small models often forget [n]
                how = "llm (citations attached post-hoc)"
            cited = cited_numbers(text, len(results))
            body_only = re.sub(r"\[\d{1,2}\]", "", text).strip()
            support = grounding(text, [results[i - 1] for i in cited])
            confused = "i don't know" in text.lower() and not text.lower().startswith("i don't know")
            llm_idk = text.lower().startswith("i don't know")
            if llm_idk or not cited or len(body_only) < 25 or support < 0.5 or confused:
                # coverage already passed, so an LLM "I don't know" is a model miss → grounded extract
                ex, ex_c = extractive(question, results)        # uncited LLM text → fall back to grounded extract
                text, cited, how = ex, ex_c, ("extractive (LLM abstained although the context covers the question)" if llm_idk else f"extractive (LLM answer failed the grounding check: support {support:.2f})")
        except Exception as e:  # noqa: BLE001
            text, cited = extractive(question, results)
            how = f"extractive (LLM failed: {type(e).__name__})"
    else:
        text, cited = extractive(question, results)
    if text.startswith("I don't know"):
        cited = []
    citations = [{"n": i, "chunk_id": results[i - 1]["chunk_id"], "title": results[i - 1]["title"],
                  "doc_key": results[i - 1]["doc_key"], "section": results[i - 1]["section"], "uri": results[i - 1]["uri"],
                  "document_id": results[i - 1].get("document_id"), "snippet": (results[i - 1]["text"] or "")[:400]}
                 for i in cited]
    latency = int((time.time() - t0) * 1000)
    out = {"question": question, "answer": text, "citations": citations, "chunks": results, "mode": how,
           "coverage": round(cov, 3), "rewritten": res["rewritten"], "model_route": routes, "latency_ms": latency,
           "collection": collection, "backend": res["backend"], "embed_model": res["embed_model"], "timings": res["timings"]}
    if log:
        res["model_route"] = routes
        retrieval.log_search(res, user=user, mode=log_mode, answer=text, n_citations=len(citations),
                             status="abstain" if text.startswith("I don't know") else "ok", latency_ms=latency)
    return out
