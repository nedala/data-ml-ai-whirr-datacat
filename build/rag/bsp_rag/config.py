"""Runtime configuration (env-driven; plan/architecture.md §1, §3, §5)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


PKG_DIR = Path(__file__).resolve().parent
RAG_DIR = PKG_DIR.parent                     # build/rag (mounted at /opt/rag)


@dataclass
class Config:
    backend: str = field(default_factory=lambda: env("RAG_VECTOR_BACKEND", "qdrant").lower())
    qdrant_url: str = field(default_factory=lambda: env("QDRANT_URL", "http://qdrant:6333"))
    qdrant_api_key: str = field(default_factory=lambda: env("QDRANT_API_KEY"))
    # metadata store: "postgres" (default in the appliance) or "sqlite" (tests / notebooks)
    meta_store: str = field(default_factory=lambda: env("RAG_META_STORE", "postgres").lower())
    sqlite_path: str = field(default_factory=lambda: env("RAG_SQLITE_PATH", "/data/rag/rag_meta.sqlite"))
    data_dir: Path = field(default_factory=lambda: Path(env("RAG_DATA_DIR", "/data/rag")))
    corpus_dir: Path = field(default_factory=lambda: Path(env("RAG_CORPUS_DIR", str(RAG_DIR / "corpus"))))
    evals_dir: Path = field(default_factory=lambda: Path(env("RAG_EVALS_DIR", str(RAG_DIR / "evals"))))
    lake_dir: Path = field(default_factory=lambda: Path(env("LAKE_DIR", "/data/lake")))
    # embeddings: "auto" = gateway embed-default when reachable, else offline
    embedder: str = field(default_factory=lambda: env("RAG_EMBEDDER", "auto"))
    gateway_embed_alias: str = field(default_factory=lambda: env("RAG_EMBED_ALIAS", "embed-default"))
    chat_alias: str = field(default_factory=lambda: env("RAG_CHAT_ALIAS", "chat-default"))
    classify_alias: str = field(default_factory=lambda: env("RAG_CLASSIFY_ALIAS", "classify-default"))
    use_llm: str = field(default_factory=lambda: env("RAG_USE_LLM", "auto"))   # auto | never | always
    default_collection: str = field(default_factory=lambda: env("RAG_DEFAULT_COLLECTION", "frontoffice"))
    sharepoint_folder: str = field(default_factory=lambda: env("RAG_SHAREPOINT_FOLDER", "FrontOffice/Runbooks"))
    tools_token: str = field(default_factory=lambda: env("RAG_TOOLS_TOKEN"))
    openwebui_port: str = field(default_factory=lambda: env("OPENWEBUI_PORT", "37081"))
    public_host: str = field(default_factory=lambda: env("PUBLIC_FACADE_HOST", "localhost"))
    autoseed: bool = field(default_factory=lambda: env("RAG_AUTOSEED", "1") == "1")

    def __post_init__(self) -> None:
        if self.backend not in ("qdrant", "pgvector"):
            self.backend = "qdrant"


@lru_cache(maxsize=1)
def get() -> Config:
    return Config()


def reset() -> None:
    get.cache_clear()
