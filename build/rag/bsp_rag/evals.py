"""Evaluation over the golden Q&A set (build/rag/evals/golden.yaml).

Retrieval: hit@1/3/5 and MRR@10 at document level (a hit = a retrieved chunk
from one of the expected documents). Answers: faithfulness (share of answer
sentences supported by the cited context — LLM judge when the gateway is up,
lexical overlap otherwise), citation correctness (share of citations that
point at an expected document), fact recall (expected key facts present) and
abstention accuracy on out-of-scope questions. Each run is stored in
rag.eval_runs with its full config so the leaderboard can compare chunking ×
embedding × retrieval configurations.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import yaml

from . import answer as answer_mod, config, gateway, ingest, meta as meta_mod, retrieval
from .textutil import overlap

PRESETS: dict[str, dict[str, Any]] = {
    "dense": {"mode": "dense", "use_mmr": False, "rerank": "none"},
    "lexical": {"mode": "lexical", "use_mmr": False, "rerank": "none"},
    "hybrid": {"mode": "hybrid", "use_mmr": False, "rerank": "none"},
    "hybrid+mmr": {"mode": "hybrid", "use_mmr": True, "rerank": "none"},
    "hybrid+rerank": {"mode": "hybrid", "use_mmr": False, "rerank": "auto"},
    "hybrid+rerank+mmr": {"mode": "hybrid", "use_mmr": True, "rerank": "auto"},
}


def load_golden(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or (config.get().evals_dir / "golden.yaml")
    doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return list(doc.get("questions", []))


def _sentences(text: str) -> list[str]:
    text = re.sub(r"^From the knowledge base:\s*", "", text.strip())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", text) if len(s.strip()) > 15]


def faithfulness_lexical(ans: str, cited_text: str) -> float:
    sents = _sentences(re.sub(r"\[\d+\]", "", ans))
    if not sents:
        return 1.0
    return sum(1 for s in sents if overlap(s, cited_text) >= 0.6) / len(sents)


def faithfulness_llm(question: str, ans: str, cited_text: str) -> float:
    data, _ = gateway.chat_json(
        f"Question: {question}\n\nContext:\n{cited_text[:6000]}\n\nAnswer:\n{ans}\n\n"
        'What fraction (0..1) of the answer\'s claims are directly supported by the context? Return JSON {"faithfulness": x}.',
        system="You are a strict grader of retrieval-augmented answers.")
    return max(0.0, min(1.0, float(data.get("faithfulness", 0))))


def run(collection: str, preset: str = "hybrid+rerank+mmr", k: int = 5, label: str = "", with_answers: bool = True,
        use_llm: bool = True, questions: list[dict[str, Any]] | None = None, save: bool = True) -> dict[str, Any]:
    coll = ingest.get_collection(collection)
    qs = questions or load_golden()
    opts = dict(PRESETS[preset])
    judge = "llm" if (use_llm and gateway.available()) else "lexical"
    per: list[dict[str, Any]] = []
    t0 = time.time()
    for q in qs:
        expected = set(q.get("expected_docs") or [])
        filters = q.get("filters")
        r: dict[str, Any] = {"id": q["id"], "question": q["question"], "expected": sorted(expected)}
        res = retrieval.search(collection, q["question"], k=10, filters=filters, use_llm=use_llm,
                               mode=opts["mode"], use_mmr=opts["use_mmr"], rerank=opts["rerank"])
        docs = [x["doc_key"] for x in res["results"]]
        rank = next((i for i, d in enumerate(docs, 1) if d in expected), None) if expected else None
        r.update({"retrieved": docs[:k], "rank": rank, "hit@1": bool(rank and rank <= 1), "hit@3": bool(rank and rank <= 3),
                  "hit@5": bool(rank and rank <= 5), "rr": (1.0 / rank) if rank else 0.0})
        if with_answers:
            a = answer_mod.answer(collection, q["question"], k=min(k + 1, 6), filters=filters, use_llm=use_llm, log=False,
                                  mode=opts["mode"], search_kw={"use_mmr": opts["use_mmr"], "rerank": opts["rerank"]})
            abstained = a["answer"].startswith("I don't know")
            cited_text = "\n".join(c["snippet"] if len(c["snippet"]) > 390 else next(
                (x["text"] for x in a["chunks"] if x["chunk_id"] == c["chunk_id"]), c["snippet"]) for c in a["citations"])
            if expected:
                cc = (sum(1 for c in a["citations"] if c["doc_key"] in expected) / len(a["citations"])) if a["citations"] else 0.0
                facts = q.get("facts") or []
                fr = (sum(1 for f in facts if f.lower() in a["answer"].lower()) / len(facts)) if facts else None
                if abstained:
                    faith = None
                elif judge == "llm":
                    try:
                        faith = faithfulness_llm(q["question"], a["answer"], cited_text)
                    except Exception:  # noqa: BLE001
                        faith = faithfulness_lexical(a["answer"], cited_text)
                else:
                    faith = faithfulness_lexical(a["answer"], cited_text)
                r.update({"answer": a["answer"][:600], "citation_correct": round(cc, 3), "fact_recall": fr,
                          "faithfulness": None if faith is None else round(faith, 3), "abstained": abstained,
                          "answer_mode": a["mode"]})
            else:
                r.update({"answer": a["answer"][:300], "abstained": abstained, "abstain_correct": abstained,
                          "answer_mode": a["mode"]})
        per.append(r)
    inscope = [r for r in per if r["expected"]]
    oos = [r for r in per if not r["expected"]]

    def avg(key: str, rows: list[dict[str, Any]]) -> float | None:
        vals = [float(r[key]) for r in rows if r.get(key) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    metrics = {"hit@1": avg("hit@1", inscope), "hit@3": avg("hit@3", inscope), "hit@5": avg("hit@5", inscope),
               "mrr": avg("rr", inscope), "n_in_scope": len(inscope), "n_out_of_scope": len(oos),
               "seconds": round(time.time() - t0, 1)}
    if with_answers:
        metrics.update({"faithfulness": avg("faithfulness", inscope), "citation_correctness": avg("citation_correct", inscope),
                        "fact_recall": avg("fact_recall", inscope), "false_abstain": avg("abstained", inscope),
                        "abstain_accuracy": avg("abstain_correct", oos)})
    cfg = {"collection": collection, "backend": coll["backend"], "embed_model": coll["embed_model"], "chunker": coll["chunker"],
           "chunk_size": coll["chunk_size"], "preset": preset, **opts, "k": k, "llm": use_llm and gateway.available()}
    out = {"collection": collection, "label": label or f"{collection} · {preset}", "config": cfg, "metrics": metrics,
           "per_question": per, "n": len(per), "judge": judge}
    if save:
        out["id"] = meta_mod.get().save_eval(out)
    return out


def leaderboard() -> list[dict[str, Any]]:
    rows = meta_mod.get().evals(500)
    best: dict[str, dict[str, Any]] = {}
    for r in rows:      # newest first → keep latest per config
        c = r["config"] or {}
        key = f"{c.get('backend')}|{c.get('embed_model')}|{c.get('chunker')}|{c.get('chunk_size')}|{c.get('preset')}|{c.get('collection')}"
        best.setdefault(key, r)
    return sorted(best.values(), key=lambda r: (-((r["metrics"] or {}).get("mrr") or 0),
                                               -((r["metrics"] or {}).get("hit@5") or 0)))
