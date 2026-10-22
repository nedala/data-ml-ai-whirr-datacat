"""pgvector backend: vectors live in Postgres `rag.chunks.vector` (pgvector),
lexical search uses the generated `tsv` column (ts_rank_cd). If the pgvector
extension is absent the backend degrades gracefully: embeddings go to
`rag.chunks.embedding REAL[]` and cosine similarity is computed in-process
(fine for demo-sized corpora). A partial HNSW index per collection is created
when pgvector is present."""

from __future__ import annotations

import math
import re
import threading
from typing import Any

from .. import meta as meta_mod
from ..textutil import STOP
from .base import Hit, Point, norm_filters


def _vec_lit(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


class PgVectorStore:
    name = "pgvector"

    def __init__(self) -> None:
        self.m = meta_mod.get()
        if self.m.kind != "postgres":
            raise RuntimeError("pgvector backend needs the Postgres metadata store (RAG_META_STORE=postgres)")
        self._cache: dict[int, tuple[int, dict[str, list[float]]]] = {}
        self._lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        try:
            self.m.one("SELECT 1 AS ok")
            return {"ok": True, "backend": "pgvector", "pgvector_extension": self.m.pgvector,
                    "mode": "pgvector (HNSW)" if self.m.pgvector else "degraded: REAL[] + in-process cosine"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "pgvector", "error": str(e)}

    def ensure_collection(self, coll: dict[str, Any]) -> None:
        if not self.m.pgvector:
            return
        cid, dim = int(coll["id"]), int(coll["embed_dim"])
        if dim > 2000:          # HNSW limit for `vector`; exact scan still works
            return
        try:
            self.m.run(f"CREATE INDEX IF NOT EXISTS chunks_vec_{cid} ON rag.chunks USING hnsw "
                       f"((vector::vector({dim})) vector_cosine_ops) WHERE collection_id = {cid}")
        except Exception:  # noqa: BLE001 — index is an optimisation only
            pass

    def drop_collection(self, coll: dict[str, Any]) -> None:
        cid = int(coll["id"])
        try:
            self.m.run(f"DROP INDEX IF EXISTS rag.chunks_vec_{cid}")
        except Exception:  # noqa: BLE001
            pass
        self._cache.pop(cid, None)

    def upsert(self, coll: dict[str, Any], points: list[Point]) -> None:
        # rows were inserted by the ingester (meta.replace_chunks); set vectors
        with self.m.cursor() as cur:
            if self.m.pgvector:
                cur.executemany("UPDATE rag.chunks SET vector = %s::vector WHERE id = %s",
                                [(_vec_lit(p.vector), p.id) for p in points])
            else:
                cur.executemany("UPDATE rag.chunks SET embedding = %s WHERE id = %s",
                                [(p.vector, p.id) for p in points])
        self._cache.pop(int(coll["id"]), None)

    def delete_document(self, coll: dict[str, Any], document_id: int) -> None:
        self._cache.pop(int(coll["id"]), None)          # rows are deleted by the metadata store

    def _where(self, coll: dict[str, Any], filters: dict | None) -> tuple[str, list[Any]]:
        f = norm_filters(filters)
        sql, params = ["c.collection_id = %s"], [int(coll["id"])]
        for k, vals in f.items():
            if k == "tags":
                sql.append("(" + " OR ".join(["d.meta->'tags' ? %s"] * len(vals)) + ")")
                params.extend(str(v) for v in vals)
            elif k == "document_id":
                sql.append(f"c.document_id IN ({','.join(['%s'] * len(vals))})")
                params.extend(int(v) for v in vals)
            else:
                sql.append(f"d.{k} IN ({','.join(['%s'] * len(vals))})")
                params.extend(str(v) for v in vals)
        return " AND ".join(sql), params

    @staticmethod
    def _payload(r: dict[str, Any]) -> dict[str, Any]:
        return {"chunk_id": r["id"], "document_id": r["document_id"], "doc_key": r["doc_key"], "title": r["title"],
                "section": r["section"], "doc_type": r["doc_type"], "system": r["system"], "text": r["body"],
                "acl_group": r["acl_group"], "uri": r["uri"], "source": r["source"], "ordinal": r["ordinal"]}

    _SEL = ("SELECT c.id, c.document_id, c.section, c.body, c.ordinal, d.doc_key, d.title, d.doc_type, d.system, "
            "d.acl_group, d.uri, d.source")

    def dense_search(self, coll, vector, k, filters) -> list[Hit]:
        where, params = self._where(coll, filters)
        if self.m.pgvector:
            dim = int(coll["embed_dim"])
            expr = f"(c.vector::vector({dim}))" if dim <= 2000 else "c.vector"
            lit = _vec_lit(vector)
            rows = self.m.all(f"{self._SEL}, 1 - ({expr} <=> %s::vector) AS score FROM rag.chunks c "
                              f"JOIN rag.documents d ON d.id = c.document_id WHERE {where} AND c.vector IS NOT NULL "
                              f"ORDER BY {expr} <=> %s::vector LIMIT %s", [lit, *params, lit, k])
            return [Hit(r["id"], float(r["score"]), self._payload(r)) for r in rows]
        # degraded: in-process cosine over REAL[] embeddings
        rows = self.m.all(f"{self._SEL}, c.embedding FROM rag.chunks c JOIN rag.documents d ON d.id = c.document_id "
                          f"WHERE {where} AND c.embedding IS NOT NULL", params)
        qn = math.sqrt(sum(x * x for x in vector)) or 1.0
        scored = []
        for r in rows:
            v = r["embedding"]
            dot = sum(a * b for a, b in zip(vector, v))
            n = math.sqrt(sum(b * b for b in v)) or 1.0
            scored.append((dot / (qn * n), r))
        scored.sort(key=lambda t: -t[0])
        return [Hit(r["id"], float(s), self._payload(r)) for s, r in scored[:k]]

    def lexical_search(self, coll, query, sparse, k, filters) -> list[Hit]:
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOP and len(w) > 1]
        if not words:
            return []
        tsq = " | ".join(dict.fromkeys(words))
        where, params = self._where(coll, filters)
        rows = self.m.all(f"{self._SEL}, ts_rank_cd(c.tsv, q, 32) AS score FROM rag.chunks c "
                          f"JOIN rag.documents d ON d.id = c.document_id, to_tsquery('english', %s) q "
                          f"WHERE {where} AND c.tsv @@ q ORDER BY score DESC LIMIT %s", [tsq, *params, k])
        return [Hit(r["id"], float(r["score"]), self._payload(r)) for r in rows]

    def count(self, coll) -> int:
        col = "vector" if self.m.pgvector else "embedding"
        r = self.m.one(f"SELECT COUNT(*) AS n FROM rag.chunks WHERE collection_id = %s AND {col} IS NOT NULL", (int(coll["id"]),))
        return int(r["n"]) if r else 0

    def vectors(self, coll, ids) -> dict[str, list[float]]:
        if not ids:
            return {}
        ph = ",".join(["%s"] * len(ids))
        if self.m.pgvector:
            rows = self.m.all(f"SELECT id, vector::text AS v FROM rag.chunks WHERE id IN ({ph})", ids)
            return {r["id"]: [float(x) for x in r["v"].strip("[]").split(",")] for r in rows if r["v"]}
        rows = self.m.all(f"SELECT id, embedding FROM rag.chunks WHERE id IN ({ph})", ids)
        return {r["id"]: list(r["embedding"]) for r in rows if r["embedding"]}
