"""Vector store backends: Qdrant (default), pgvector (Postgres `rag.chunks`),
memory (tests / notebooks without services). Selected per collection; the
default for new collections comes from RAG_VECTOR_BACKEND."""

from __future__ import annotations

from .base import Hit, Point, VectorStore

_instances: dict[str, VectorStore] = {}


def get(backend: str) -> VectorStore:
    if backend not in _instances:
        if backend == "qdrant":
            from .qdrant_store import QdrantStore
            _instances[backend] = QdrantStore()
        elif backend == "pgvector":
            from .pgvector_store import PgVectorStore
            _instances[backend] = PgVectorStore()
        elif backend == "memory":
            from .memory_store import MemoryStore
            _instances[backend] = MemoryStore()
        else:
            raise ValueError(f"unknown vector backend {backend!r}")
    return _instances[backend]


def reset() -> None:
    _instances.clear()


__all__ = ["Hit", "Point", "VectorStore", "get", "reset"]
