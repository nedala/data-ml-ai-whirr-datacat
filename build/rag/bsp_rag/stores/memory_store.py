"""In-process backend for tests and notebooks without Qdrant/Postgres:
exact cosine + in-process BM25. Not persistent across restarts."""

from __future__ import annotations

import math
import threading
from typing import Any

from ..textutil import bm25_scores
from .base import Hit, Point, match, norm_filters


class MemoryStore:
    name = "memory"

    def __init__(self) -> None:
        self.data: dict[str, dict[str, Point]] = {}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        return {"ok": True, "backend": "memory", "collections": list(self.data)}

    def ensure_collection(self, coll) -> None:
        self.data.setdefault(coll["name"], {})

    def drop_collection(self, coll) -> None:
        self.data.pop(coll["name"], None)

    def upsert(self, coll, points: list[Point]) -> None:
        with self._lock:
            d = self.data.setdefault(coll["name"], {})
            for p in points:
                d[p.id] = p

    def delete_document(self, coll, document_id: int) -> None:
        with self._lock:
            d = self.data.get(coll["name"], {})
            for pid in [k for k, p in d.items() if p.payload.get("document_id") == document_id]:
                d.pop(pid)

    def _cands(self, coll, filters) -> list[Point]:
        f = norm_filters(filters)
        return [p for p in self.data.get(coll["name"], {}).values() if match(p.payload, f)]

    def dense_search(self, coll, vector, k, filters) -> list[Hit]:
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        out = []
        for p in self._cands(coll, filters):
            n = math.sqrt(sum(x * x for x in p.vector)) or 1.0
            out.append(Hit(p.id, sum(a * b for a, b in zip(vector, p.vector)) / (qn * n), p.payload))
        return sorted(out, key=lambda h: -h.score)[:k]

    def lexical_search(self, coll, query, sparse, k, filters) -> list[Hit]:
        cands = self._cands(coll, filters)
        scores = bm25_scores(query, [p.payload.get("text", "") + " " + p.payload.get("title", "") + " " +
                                     p.payload.get("section", "") for p in cands])
        hits = [Hit(p.id, s, p.payload) for p, s in zip(cands, scores) if s > 0]
        return sorted(hits, key=lambda h: -h.score)[:k]

    def count(self, coll) -> int:
        return len(self.data.get(coll["name"], {}))

    def vectors(self, coll, ids) -> dict[str, list[float]]:
        d = self.data.get(coll["name"], {})
        return {i: d[i].vector for i in ids if i in d}
