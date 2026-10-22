"""Thin wrapper over bsp_common.llm that records the model route and honours
RAG_USE_LLM (auto | never | always). Every LLM use in bsp_rag has a
deterministic fallback when `available()` is false (plan §5)."""

from __future__ import annotations

import json
import re
import time
from typing import Any

from . import config
from .embeddings import gateway_ok


def available() -> bool:
    mode = config.get().use_llm
    if mode == "never":
        return False
    return gateway_ok()


def chat(messages: list[dict[str, Any]], alias: str | None = None, **kw: Any) -> tuple[str, dict[str, Any]]:
    """Returns (text, route) where route = {alias, model, latency_ms}."""
    import httpx

    from bsp_common import settings
    from bsp_common.llm import TIMEOUT, _headers

    alias = alias or config.get().chat_alias
    t0 = time.time()
    r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/chat/completions",
                   json={"model": alias, "messages": messages, **kw}, headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    body = r.json()
    text = body["choices"][0]["message"].get("content") or ""
    route = {"alias": alias, "model": body.get("model") or alias, "latency_ms": int((time.time() - t0) * 1000)}
    for h in ("x-bsp-provider", "x-bsp-route", "x-llm-provider"):
        if h in r.headers:
            route["provider"] = r.headers[h]
    return text, route


def chat_json(prompt: str, system: str, alias: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    text, route = chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}], alias=alias,
                       temperature=0, response_format={"type": "json_object"})
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text), route
