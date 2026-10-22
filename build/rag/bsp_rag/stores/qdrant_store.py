"""Qdrant backend: one Qdrant collection per RAG collection (`bsp_<name>`),
named dense vector `dense` (cosine) + sparse vector `bm25` with server-side
IDF (BM25-style lexical search), keyword payload indexes for filters."""

from __future__ import annotations

import os
from typing import Any

from .. import config
from .base import Hit, Point, norm_filters


class QdrantStore:
    name = "qdrant"

    def __init__(self) -> None:
        from qdrant_client import QdrantClient

        cfg = config.get()
        self.c = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, timeout=int(os.environ.get("QDRANT_TIMEOUT", "180")),
                              prefer_grpc=False, check_compatibility=False)

    @staticmethod
    def cname(coll: dict[str, Any]) -> str:
        return "bsp_" + coll["name"].replace("-", "_")

    def status(self) -> dict[str, Any]:
        try:
            info = self.c.get_collections()
            return {"ok": True, "backend": "qdrant", "url": config.get().qdrant_url,
                    "collections": [c.name for c in info.collections]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "backend": "qdrant", "error": f"{type(e).__name__}: {e}"}

    def ensure_collection(self, coll: dict[str, Any]) -> None:
        from qdrant_client import models as m

        name = self.cname(coll)
        if not self.c.collection_exists(name):
            self.c.create_collection(
                name,
                vectors_config={"dense": m.VectorParams(size=int(coll["embed_dim"]), distance=m.Distance.COSINE)},
                sparse_vectors_config={"bm25": m.SparseVectorParams(modifier=m.Modifier.IDF)},
            )
        else:   # left over from an earlier attempt: must match the recorded model's dimension
            info = self.c.get_collection(name)
            vecs = info.config.params.vectors
            dim = vecs["dense"].size if isinstance(vecs, dict) and "dense" in vecs else None
            if dim != int(coll["embed_dim"]):
                self.c.delete_collection(name)
                return self.ensure_collection(coll)
        have = set((self.c.get_collection(name).payload_schema or {}).keys())
        for f in ("doc_type", "system", "acl_group", "doc_key", "tags", "source"):
            if f not in have:   # async: index builds in the background
                self.c.create_payload_index(name, f, field_schema=m.PayloadSchemaType.KEYWORD, wait=False)
        if "document_id" not in have:
            self.c.create_payload_index(name, "document_id", field_schema=m.PayloadSchemaType.INTEGER, wait=False)

    def drop_collection(self, coll: dict[str, Any]) -> None:
        name = self.cname(coll)
        if self.c.collection_exists(name):
            self.c.delete_collection(name)

    def upsert(self, coll: dict[str, Any], points: list[Point]) -> None:
        from qdrant_client import models as m

        name = self.cname(coll)
        for i in range(0, len(points), 128):
            batch = points[i:i + 128]
            self.c.upsert(name, points=[m.PointStruct(
                id=p.id,
                vector={"dense": p.vector, "bm25": m.SparseVector(indices=p.sparse[0], values=p.sparse[1])},
                payload=p.payload) for p in batch], wait=True)

    def delete_document(self, coll: dict[str, Any], document_id: int) -> None:
        from qdrant_client import models as m

        name = self.cname(coll)
        if not self.c.collection_exists(name):
            return
        self.c.delete(name, points_selector=m.FilterSelector(filter=m.Filter(must=[
            m.FieldCondition(key="document_id", match=m.MatchValue(value=int(document_id)))])), wait=True)

    def _filter(self, filters: dict | None):
        from qdrant_client import models as m

        f = norm_filters(filters)
        if not f:
            return None
        must = []
        for k, vals in f.items():
            if k == "document_id":
                must.append(m.FieldCondition(key=k, match=m.MatchAny(any=[int(v) for v in vals])))
            else:
                must.append(m.FieldCondition(key=k, match=m.MatchAny(any=[str(v) for v in vals])))
        return m.Filter(must=must)

    def dense_search(self, coll, vector, k, filters) -> list[Hit]:
        res = self.c.query_points(self.cname(coll), query=vector, using="dense", query_filter=self._filter(filters),
                                  limit=k, with_payload=True)
        return [Hit(str(p.id), float(p.score), p.payload or {}) for p in res.points]

    def lexical_search(self, coll, query, sparse, k, filters) -> list[Hit]:
        from qdrant_client import models as m

        if not sparse[0]:
            return []
        res = self.c.query_points(self.cname(coll), query=m.SparseVector(indices=sparse[0], values=sparse[1]),
                                  using="bm25", query_filter=self._filter(filters), limit=k, with_payload=True)
        return [Hit(str(p.id), float(p.score), p.payload or {}) for p in res.points]

    def count(self, coll) -> int:
        name = self.cname(coll)
        if not self.c.collection_exists(name):
            return 0
        return int(self.c.count(name, exact=True).count)

    def vectors(self, coll, ids) -> dict[str, list[float]]:
        if not ids:
            return {}
        pts = self.c.retrieve(self.cname(coll), ids=ids, with_vectors=["dense"], with_payload=False)
        out = {}
        for p in pts:
            v = p.vector.get("dense") if isinstance(p.vector, dict) else p.vector
            out[str(p.id)] = list(v or [])
        return out
