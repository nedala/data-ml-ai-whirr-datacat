"""Embedders + an on-disk cache keyed by (model, sha256(text)).

* `gateway`  — the harness LLM gateway alias `embed-default` (OpenAI-compatible
  /embeddings). The recorded model id includes the provider model the gateway
  reports, so an alias that falls back to a different provider model is
  detected and refused for an existing collection.
* `fastembed` — local ONNX model (BAAI/bge-small-en-v1.5) when the package is
  installed and the model files were pre-downloaded into FASTEMBED_CACHE_PATH
  at image build. Never downloads at runtime.
* `hash`     — deterministic offline feature-hashing embedder (word uni/bigrams
  + char 4-grams, signed hashing, log-tf, L2-normalised, 512 dims). No model
  files, works anywhere; lexical in nature, so hybrid fusion still matters.

Every collection records its embedder `name` and `dim`; queries with a
different embedder are refused (MixedModelError).
"""

from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Protocol

from . import config
from .textutil import h32, tokens


class MixedModelError(RuntimeError):
    pass


class EmbedderUnavailable(RuntimeError):
    pass


class Embedder(Protocol):
    name: str
    dim: int
    offline: bool

    def embed(self, texts: list[str]) -> list[list[float]]: ...


# ── offline hashing embedder ─────────────────────────────────────────────
class HashEmbedder:
    offline = True

    def __init__(self, dim: int = 512):
        self.dim = dim
        self.name = f"bsp-hash-{dim}-v1"

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        toks = tokens(text)
        feats: dict[str, float] = {}
        for t in toks:
            feats["w:" + t] = feats.get("w:" + t, 0.0) + 1.0
            if len(t) > 4:
                p = f"#{t}#"
                for i in range(len(p) - 3):
                    g = "c:" + p[i:i + 4]
                    feats[g] = feats.get(g, 0.0) + 0.25
        for a, b in zip(toks, toks[1:]):
            feats["b:" + a + "_" + b] = feats.get("b:" + a + "_" + b, 0.0) + 0.7
        for f, c in feats.items():
            h = h32(f)
            sign = 1.0 if (h >> 30) & 1 else -1.0
            v[h % self.dim] += sign * (1.0 + math.log(c)) if c >= 1 else sign * c
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


# ── fastembed (optional, pre-downloaded only) ────────────────────────────
class FastEmbedder:
    offline = True

    def __init__(self, model: str = "BAAI/bge-small-en-v1.5"):
        try:
            from fastembed import TextEmbedding  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise EmbedderUnavailable("fastembed not installed") from e
        cache = os.environ.get("FASTEMBED_CACHE_PATH", "/opt/models/fastembed")
        if not Path(cache).exists():
            raise EmbedderUnavailable(f"fastembed model cache {cache} not present (pre-download at image build)")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        self._m = TextEmbedding(model_name=model, cache_dir=cache, local_files_only=True)
        self.dim = len(next(iter(self._m.embed(["probe"]))))
        self.name = f"fastembed:{model}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._m.embed(texts)]


# ── LLM gateway ──────────────────────────────────────────────────────────
class GatewayEmbedder:
    offline = False

    def __init__(self, alias: str | None = None):
        import httpx  # noqa: F401  (dependency check)

        self.alias = alias or config.get().gateway_embed_alias
        vec, model = self._call(["dimension probe"])
        self.dim = len(vec[0])
        self.provider_model = model or self.alias
        self.name = f"gateway:{self.alias}" + (f"→{model}" if model and model != self.alias else "")

    def _call(self, texts: list[str]) -> tuple[list[list[float]], str]:
        import httpx

        from bsp_common import settings
        from bsp_common.llm import TIMEOUT, _headers

        r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/embeddings",
                       json={"model": self.alias, "input": texts}, headers=_headers(), timeout=TIMEOUT)
        r.raise_for_status()
        body = r.json()
        data = sorted(body["data"], key=lambda d: d.get("index", 0))
        return [d["embedding"] for d in data], str(body.get("model") or "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), 32):
            vecs, model = self._call(texts[i:i + 32])
            if model and self.provider_model and model != self.provider_model:
                raise MixedModelError(f"gateway alias {self.alias} switched provider model "
                                      f"{self.provider_model} → {model} mid-run (fallback route?)")
            out.extend(vecs)
        return out


# ── factory ──────────────────────────────────────────────────────────────
_lock = threading.Lock()
_instances: dict[str, Embedder] = {}
_gw_state = {"ok": None, "t": 0.0}


def gateway_ok() -> bool:
    if config.get().use_llm == "never":
        return False
    now = time.time()
    if _gw_state["ok"] is None or now - _gw_state["t"] > 30:
        try:
            from bsp_common import llm

            _gw_state["ok"] = llm.available()
        except Exception:  # noqa: BLE001
            _gw_state["ok"] = False
        _gw_state["t"] = now
    return bool(_gw_state["ok"])


def get(spec: str | None = None) -> Embedder:
    """spec: auto | gateway | hash | fastembed | an exact recorded name."""
    spec = (spec or config.get().embedder or "auto").strip()
    if spec.startswith("gateway"):
        key = "gateway"
    elif spec.startswith("fastembed"):
        key = "fastembed"
    elif spec.startswith("bsp-hash") or spec == "hash":
        key = "hash"
    elif spec == "auto":
        key = "gateway" if gateway_ok() else ("fastembed" if _fastembed_ready() else "hash")
    else:
        raise EmbedderUnavailable(f"unknown embedder {spec!r}")
    if key == "gateway" and not gateway_ok():
        raise EmbedderUnavailable("LLM gateway not reachable — the offline embedder cannot stand in for it")
    with _lock:
        if key not in _instances:
            if key == "gateway":
                if not gateway_ok():
                    raise EmbedderUnavailable("LLM gateway not reachable — use the offline embedder")
                _instances[key] = GatewayEmbedder()
            elif key == "fastembed":
                _instances[key] = FastEmbedder()
            else:
                _instances[key] = HashEmbedder()
        return _instances[key]


def _fastembed_ready() -> bool:
    try:
        import fastembed  # noqa: F401
    except ImportError:
        return False
    return Path(os.environ.get("FASTEMBED_CACHE_PATH", "/opt/models/fastembed")).exists()


def for_collection(recorded_name: str, recorded_dim: int) -> Embedder:
    """The embedder matching a collection's recorded model, or MixedModelError."""
    try:
        emb = get(recorded_name)
    except EmbedderUnavailable as e:
        raise MixedModelError(f"collection was embedded with {recorded_name}, which is not available now ({e}); "
                              "refusing to query it with a different model") from e
    if emb.name != recorded_name or emb.dim != recorded_dim:
        raise MixedModelError(f"collection embedded with {recorded_name} ({recorded_dim}d) but the current "
                              f"embedder is {emb.name} ({emb.dim}d) — re-index the collection or switch back")
    return emb


# ── cache ────────────────────────────────────────────────────────────────
class EmbedCache:
    def __init__(self, path: Path | None = None):
        p = path or (config.get().data_dir / "embed_cache.sqlite")
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(str(p), check_same_thread=False)
        except (OSError, sqlite3.Error):
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS emb (model TEXT, h TEXT, v BLOB, PRIMARY KEY (model, h))")
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def embed(self, emb: Embedder, texts: list[str]) -> list[list[float]]:
        if emb.offline and isinstance(emb, HashEmbedder):
            return emb.embed(texts)          # cheaper to recompute than to look up
        hs = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        found: dict[str, list[float]] = {}
        with self._lock:
            for i in range(0, len(hs), 500):
                part = hs[i:i + 500]
                q = f"SELECT h, v FROM emb WHERE model=? AND h IN ({','.join('?' * len(part))})"
                for h, v in self._db.execute(q, [emb.name, *part]):
                    found[h] = list(struct.unpack(f"{len(v) // 4}f", v))
        todo = [(h, t) for h, t in zip(hs, texts) if h not in found]
        self.hits += len(texts) - len(todo)
        self.misses += len(todo)
        if todo:
            uniq = list(dict(todo).items())
            vecs = emb.embed([t for _, t in uniq])
            with self._lock:
                self._db.executemany("INSERT OR REPLACE INTO emb VALUES (?,?,?)",
                                     [(emb.name, h, struct.pack(f"{len(v)}f", *v)) for (h, _), v in zip(uniq, vecs)])
                self._db.commit()
            for (h, _), v in zip(uniq, vecs):
                found[h] = v
        return [found[h] for h in hs]


_cache: EmbedCache | None = None


def cache() -> EmbedCache:
    global _cache
    if _cache is None:
        _cache = EmbedCache()
    return _cache
