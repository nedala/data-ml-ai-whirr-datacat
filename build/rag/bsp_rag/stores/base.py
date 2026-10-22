from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

FILTER_FIELDS = ("doc_type", "system", "acl_group", "doc_key", "tags", "source", "document_id")


@dataclass
class Point:
    id: str
    vector: list[float]
    sparse: tuple[list[int], list[float]]
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any] = field(default_factory=dict)


def norm_filters(filters: dict[str, Any] | None) -> dict[str, list[Any]]:
    out: dict[str, list[Any]] = {}
    for k, v in (filters or {}).items():
        if k not in FILTER_FIELDS or v in (None, "", [], "any"):
            continue
        out[k] = list(v) if isinstance(v, (list, tuple, set)) else [v]
    return out


def match(payload: dict[str, Any], filters: dict[str, list[Any]]) -> bool:
    for k, allowed in filters.items():
        val = payload.get(k)
        if isinstance(val, list):
            if not set(map(str, val)) & set(map(str, allowed)):
                return False
        elif str(val) not in set(map(str, allowed)):
            return False
    return True


class VectorStore(Protocol):
    name: str

    def status(self) -> dict[str, Any]: ...
    def ensure_collection(self, coll: dict[str, Any]) -> None: ...
    def drop_collection(self, coll: dict[str, Any]) -> None: ...
    def upsert(self, coll: dict[str, Any], points: list[Point]) -> None: ...
    def delete_document(self, coll: dict[str, Any], document_id: int) -> None: ...
    def dense_search(self, coll: dict[str, Any], vector: list[float], k: int, filters: dict | None) -> list[Hit]: ...
    def lexical_search(self, coll: dict[str, Any], query: str, sparse: tuple[list[int], list[float]], k: int,
                       filters: dict | None) -> list[Hit]: ...
    def count(self, coll: dict[str, Any]) -> int: ...
    def vectors(self, coll: dict[str, Any], ids: list[str]) -> dict[str, list[float]]: ...
