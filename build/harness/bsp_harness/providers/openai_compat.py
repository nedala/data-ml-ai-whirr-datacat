"""Any OpenAI-compatible server (vLLM, LM Studio, LiteLLM, …): near-passthrough."""
from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator

import httpx

from .base import Provider, ProviderError, http_error

PASS = ("messages", "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed", "tools",
        "tool_choice", "response_format", "frequency_penalty", "presence_penalty", "n", "logprobs", "user")


class OpenAICompatProvider(Provider):
    type = "openai_compatible"

    def _headers(self) -> dict:
        key = os.environ.get(self.cfg.get("api_key_env") or "", "") or self.cfg.get("api_key", "")
        return {"Authorization": f"Bearer {key}"} if key else {}

    def _url(self, path: str) -> str:
        return (self.cfg.get("base_url") or "").rstrip("/") + path

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                 trust_env=bool(self.cfg.get("use_proxy", not self.local)))

    async def chat(self, model: str, req: dict) -> dict:
        body = {k: req[k] for k in PASS if k in req}
        body["model"] = model
        try:
            async with self._client() as c:
                r = await c.post(self._url("/chat/completions"), json=body, headers=self._headers())
                r.raise_for_status()
                d = r.json()
        except Exception as e:  # noqa: BLE001
            raise http_error(e, self.name) from None
        ch = (d.get("choices") or [{}])[0]
        u = d.get("usage") or {}
        return {"message": ch.get("message") or {"role": "assistant", "content": ""},
                "finish_reason": ch.get("finish_reason", "stop"),
                "usage": {"prompt_tokens": u.get("prompt_tokens", 0), "completion_tokens": u.get("completion_tokens", 0)}}

    async def stream(self, model: str, req: dict) -> AsyncIterator[dict]:
        body = {k: req[k] for k in PASS if k in req}
        body.update(model=model, stream=True, stream_options={"include_usage": True})
        try:
            async with self._client() as c:
                async with c.stream("POST", self._url("/chat/completions"), json=body, headers=self._headers()) as r:
                    if r.status_code >= 400:
                        await r.aread()
                        r.raise_for_status()
                    usage = None
                    finish = None
                    async for line in r.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        d = json.loads(data)
                        if d.get("usage"):
                            usage = d["usage"]
                        for ch in d.get("choices") or []:
                            finish = ch.get("finish_reason") or finish
                            if ch.get("delta"):
                                yield {"delta": ch["delta"], "finish_reason": None, "usage": None}
                    yield {"delta": {}, "finish_reason": finish or "stop",
                           "usage": {"prompt_tokens": (usage or {}).get("prompt_tokens", 0),
                                     "completion_tokens": (usage or {}).get("completion_tokens", 0)}}
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise http_error(e, self.name) from None

    async def embed(self, model: str, texts: list[str]) -> dict:
        try:
            async with self._client() as c:
                r = await c.post(self._url("/embeddings"), json={"model": model, "input": texts}, headers=self._headers())
                r.raise_for_status()
                d = r.json()
        except Exception as e:  # noqa: BLE001
            raise http_error(e, self.name) from None
        data = sorted(d.get("data") or [], key=lambda x: x.get("index", 0))
        return {"vectors": [x["embedding"] for x in data], "usage": {"prompt_tokens": (d.get("usage") or {}).get("prompt_tokens", 0)}}

    async def status(self) -> dict:
        ok, why = self.configured()
        if not ok:
            return {"ok": False, "detail": why}
        try:
            async with httpx.AsyncClient(timeout=5, trust_env=bool(self.cfg.get("use_proxy", not self.local))) as c:
                r = await c.get(self._url("/models"), headers=self._headers())
                r.raise_for_status()
                ids = [m.get("id") for m in r.json().get("data") or []]
            return {"ok": True, "detail": f"{len(ids)} models", "models": [{"name": i} for i in ids]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:200]}"}


def make(name: str, cfg: dict, defaults: dict | None = None) -> Provider:
    from .anthropic_ import AnthropicProvider, AnthropicVertexProvider  # noqa: PLC0415
    from .ollama import OllamaProvider  # noqa: PLC0415
    from .vertex import VertexProvider  # noqa: PLC0415

    cls: dict[str, Any] = {"ollama": OllamaProvider, "vertex": VertexProvider, "anthropic": AnthropicProvider,
                           "anthropic_vertex": AnthropicVertexProvider, "openai_compatible": OpenAICompatProvider}
    t = (cfg or {}).get("type")
    if t not in cls:
        raise ProviderError(f"unknown provider type {t!r} for {name}", retryable=False, status=400)
    return cls[t](name, cfg, defaults)
