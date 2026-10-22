"""Provider adapters; `get(name)` returns a cached adapter rebuilt whenever its config changes."""
from __future__ import annotations

import json

from ..config import CONFIG
from .base import Provider, ProviderError, ProviderUnavailable  # noqa: F401
from .openai_compat import make

_cache: dict[str, tuple[str, Provider]] = {}


def get(name: str) -> Provider:
    cfg = CONFIG.provider(name)
    if cfg is None:
        raise ProviderError(f"unknown provider '{name}'", retryable=False, status=404)
    sig = json.dumps(cfg, sort_keys=True, default=str) + json.dumps(CONFIG.defaults(), sort_keys=True)
    hit = _cache.get(name)
    if hit and hit[0] == sig:
        return hit[1]
    p = make(name, cfg, CONFIG.defaults())
    _cache[name] = (sig, p)
    return p


def names() -> list[str]:
    return list((CONFIG.get().get("providers") or {}).keys())
