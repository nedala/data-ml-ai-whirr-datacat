"""Ollama adapter — native /api/chat (JSON mode via `format`, images, tools, NDJSON
streaming) and /api/embed. Tries base_url, then alt_base_urls (e.g. Ollama on the
host via host.docker.internal) and sticks with whichever answered."""
from __future__ import annotations

import base64
import json
import uuid
from typing import Any, AsyncIterator

import httpx

from .base import (JSON_INSTRUCTION, Provider, ProviderError, fetch_image, http_error, json_schema_of, split_content,
                   tool_args_obj, tool_args_str, tool_names_by_id, wants_json)


class OllamaProvider(Provider):
    type = "ollama"

    def __init__(self, name, cfg, defaults=None):
        super().__init__(name, cfg, defaults)
        self._good: str | None = None

    def urls(self) -> list[str]:
        urls = [self.cfg.get("base_url") or "http://ollama:11434"] + list(self.cfg.get("alt_base_urls") or [])
        urls = [u.rstrip("/") for u in urls if u]
        if self._good in urls:
            urls.remove(self._good)
            urls.insert(0, self._good)
        return urls

    def _client(self, timeout: float | None = None) -> httpx.AsyncClient:
        # Ollama is always on the internal network / host: never via the egress proxy.
        return httpx.AsyncClient(timeout=httpx.Timeout(timeout or self.timeout, connect=5.0), trust_env=False)

    async def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        last: Exception | None = None
        async with self._client(timeout) as c:
            for base in self.urls():
                try:
                    r = await c.post(base + path, json=body)
                    r.raise_for_status()
                    self._good = base
                    return r.json()
                except httpx.ConnectError as e:
                    last = e
                    continue
                except Exception as e:  # noqa: BLE001
                    raise self._err(e) from None
        raise self._err(last or RuntimeError("no base_url"))

    def _err(self, e: Exception) -> ProviderError:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 404:
            try:
                msg = e.response.json().get("error", "")
            except ValueError:
                msg = e.response.text
            return ProviderError(f"{self.name}: {msg or 'model not found'} (pull it in Provider status)", retryable=False,
                                 status=404)
        return http_error(e, self.name)

    async def _messages(self, req: dict) -> list[dict]:
        msgs = req.get("messages") or []
        names = tool_names_by_id(msgs)
        out: list[dict] = []
        for m in msgs:
            role = m.get("role", "user")
            text, images = split_content(m.get("content"))
            om: dict[str, Any] = {"role": "system" if role == "developer" else role, "content": text}
            if images:
                om["images"] = [base64.b64encode((await fetch_image(i))["data"]).decode() for i in images]
            if m.get("tool_calls"):
                om["tool_calls"] = [{"function": {"name": (tc.get("function") or {}).get("name"),
                                                  "arguments": tool_args_obj((tc.get("function") or {}).get("arguments"))}}
                                    for tc in m["tool_calls"]]
            if role == "tool":
                om["tool_name"] = m.get("name") or names.get(m.get("tool_call_id", ""), "")
            out.append(om)
        return out

    async def _body(self, model: str, req: dict, stream: bool) -> dict:
        opts: dict[str, Any] = {}
        for k_in, k_out in (("temperature", "temperature"), ("top_p", "top_p"), ("seed", "seed"),
                            ("max_tokens", "num_predict"), ("max_completion_tokens", "num_predict"),
                            ("frequency_penalty", "frequency_penalty"), ("presence_penalty", "presence_penalty")):
            if req.get(k_in) is not None:
                opts[k_out] = req[k_in]
        if req.get("stop"):
            opts["stop"] = req["stop"] if isinstance(req["stop"], list) else [req["stop"]]
        body: dict[str, Any] = {"model": model, "messages": await self._messages(req), "stream": stream, "options": opts}
        if self.cfg.get("keep_alive"):
            body["keep_alive"] = self.cfg["keep_alive"]
        if wants_json(req):
            body["format"] = json_schema_of(req) or "json"
            if not any("json" in (m.get("content") or "").lower() for m in body["messages"] if m["role"] == "system"):
                body["messages"].insert(0, {"role": "system", "content": JSON_INSTRUCTION})
        if req.get("tools"):
            body["tools"] = req["tools"]
        return body

    @staticmethod
    def _tool_calls(msg: dict) -> list[dict]:
        return [{"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                 "function": {"name": (tc.get("function") or {}).get("name", ""),
                              "arguments": tool_args_str((tc.get("function") or {}).get("arguments"))}}
                for tc in msg.get("tool_calls") or []]

    async def chat(self, model: str, req: dict) -> dict:
        data = await self._post("/api/chat", await self._body(model, req, False))
        msg = data.get("message") or {}
        tcs = self._tool_calls(msg)
        out_msg: dict[str, Any] = {"role": "assistant", "content": msg.get("content", "")}
        if tcs:
            out_msg["tool_calls"] = tcs
        reason = "tool_calls" if tcs else ("length" if data.get("done_reason") == "length" else "stop")
        return {"message": out_msg, "finish_reason": reason,
                "usage": {"prompt_tokens": data.get("prompt_eval_count", 0), "completion_tokens": data.get("eval_count", 0)}}

    async def stream(self, model: str, req: dict) -> AsyncIterator[dict]:
        body = await self._body(model, req, True)
        last: Exception | None = None
        async with self._client() as c:
            for base in self.urls():
                try:
                    async with c.stream("POST", base + "/api/chat", json=body) as r:
                        if r.status_code >= 400:
                            await r.aread()
                            r.raise_for_status()
                        self._good = base
                        first = True
                        n_tools = 0
                        async for line in r.aiter_lines():
                            if not line.strip():
                                continue
                            d = json.loads(line)
                            if d.get("error"):
                                raise ProviderError(f"{self.name}: {d['error']}", retryable=False)
                            msg = d.get("message") or {}
                            delta: dict[str, Any] = {}
                            if first:
                                delta["role"] = "assistant"
                                first = False
                            if msg.get("content"):
                                delta["content"] = msg["content"]
                            tcs = self._tool_calls(msg)
                            if tcs:
                                delta["tool_calls"] = [{**tc, "index": n_tools + i} for i, tc in enumerate(tcs)]
                                n_tools += len(tcs)
                            if d.get("done"):
                                reason = "tool_calls" if n_tools else ("length" if d.get("done_reason") == "length" else "stop")
                                yield {"delta": delta, "finish_reason": reason,
                                       "usage": {"prompt_tokens": d.get("prompt_eval_count", 0),
                                                 "completion_tokens": d.get("eval_count", 0)}}
                                return
                            if delta:
                                yield {"delta": delta, "finish_reason": None, "usage": None}
                        return
                except httpx.ConnectError as e:
                    last = e
                    continue
                except ProviderError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise self._err(e) from None
        raise self._err(last or RuntimeError("no base_url"))

    async def embed(self, model: str, texts: list[str]) -> dict:
        data = await self._post("/api/embed", {"model": model, "input": texts})
        return {"vectors": data.get("embeddings") or [], "usage": {"prompt_tokens": data.get("prompt_eval_count", 0)}}

    # admin -----------------------------------------------------------------
    async def status(self) -> dict:
        errors = []
        async with self._client(5) as c:
            for base in self.urls():
                try:
                    v = (await c.get(base + "/api/version")).json().get("version")
                    tags = (await c.get(base + "/api/tags")).json().get("models") or []
                    self._good = base
                    return {"ok": True, "detail": f"Ollama {v} at {base}", "base_url": base, "version": v,
                            "models": [{"name": t.get("name"), "size": t.get("size"),
                                        "family": (t.get("details") or {}).get("family"),
                                        "parameter_size": (t.get("details") or {}).get("parameter_size"),
                                        "modified_at": t.get("modified_at")} for t in tags]}
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{base}: {type(e).__name__}")
        return {"ok": False, "detail": "not reachable — " + "; ".join(errors), "models": []}

    async def list_models(self) -> list[str]:
        st = await self.status()
        return [m["name"] for m in st.get("models") or []] if st.get("ok") else await super().list_models()

    async def pull(self, model: str) -> AsyncIterator[dict]:
        """Stream /api/pull progress lines (the container reaches the registry via px-proxy)."""
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=5.0), trust_env=False) as c:
            base = self.urls()[0]
            async with c.stream("POST", base + "/api/pull", json={"model": model, "stream": True}) as r:
                async for line in r.aiter_lines():
                    if line.strip():
                        yield json.loads(line)
