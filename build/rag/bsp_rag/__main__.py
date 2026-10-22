"""CLI:  python -m bsp_rag serve | seed | eval | ask | status

  serve                       run the API/UI on :8101 (prefix /rag)
  seed [--collection N] [--backend qdrant|pgvector|memory] [--embedder auto|hash|gateway]
  eval [--collection N] [--preset P ...] [--no-answers]
  ask "question" [--collection N]
"""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_rag")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=int(os.environ.get("RAG_PORT", "8101")))
    sd = sub.add_parser("seed")
    sd.add_argument("--collection")
    sd.add_argument("--backend")
    sd.add_argument("--embedder", default="auto")
    ev = sub.add_parser("eval")
    ev.add_argument("--collection")
    ev.add_argument("--preset", action="append")
    ev.add_argument("--no-answers", action="store_true")
    ev.add_argument("--no-llm", action="store_true")
    ak = sub.add_parser("ask")
    ak.add_argument("question")
    ak.add_argument("--collection")
    sub.add_parser("status")
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        import uvicorn

        uvicorn.run("bsp_rag.app:app", host=a.host, port=a.port, proxy_headers=True, forwarded_allow_ips="*")
        return 0
    from . import answer, config, evals, ingest

    if a.cmd == "seed":
        print(json.dumps(ingest.ensure_demo(a.collection, a.backend, a.embedder), indent=2, default=str))
    elif a.cmd == "eval":
        for p in a.preset or ["hybrid+rerank+mmr"]:
            r = evals.run(a.collection or config.get().default_collection, p, with_answers=not a.no_answers, use_llm=not a.no_llm)
            print(p, json.dumps(r["metrics"]))
    elif a.cmd == "ask":
        r = answer.answer(a.collection or config.get().default_collection, a.question)
        print(r["answer"])
        for c in r["citations"]:
            print(f"  [{c['n']}] {c['title']} › {c['section']} ({c['doc_key']})")
    elif a.cmd == "status":
        from .app import status
        print(json.dumps(status(), indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
