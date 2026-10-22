"""Anthropic adapters: the Anthropic API (ANTHROPIC_API_KEY) and Claude on Vertex AI
(anthropic[vertex], same GCP credentials as the Gemini adapter).

OpenAI → Messages API: system → `system`; image parts → base64/url image blocks;
assistant.tool_calls → tool_use blocks; role "tool" → tool_result blocks in a user
turn; consecutive same-role turns are merged. JSON mode = a system instruction
(the Messages API has no JSON switch)."""
from __future__ import annotations

import base64
import json
import os
from typing import Any, AsyncIterator

from .base import (JSON_INSTRUCTION, Provider, ProviderError, ProviderUnavailable, split_content, tool_args_obj,
                   wants_json)

_client_factory = None   # tests: factory(provider) -> AsyncAnthropic-like


def to_anthropic(req: dict) -> tuple[str, list[dict]]:
    system: list[str] = []
    out: list[dict] = []

    def add(role: str, blocks: list[dict]) -> None:
        if not blocks:
            return
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in req.get("messages") or []:
        role = m.get("role", "user")
        text, images = split_content(m.get("content"))
        if role in ("system", "developer"):
            system.append(text)
            continue
        if role == "tool":
            add("user", [{"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": text or "(empty)"}])
            continue
        blocks: list[dict] = []
        for img in images:
            if "data" in img:
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": img["mime"],
                                                           "data": base64.b64encode(img["data"]).decode()}})
            else:
                blocks.append({"type": "image", "source": {"type": "url", "url": img["url"]}})
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"),
                           "input": tool_args_obj(fn.get("arguments"))})
        add("assistant" if role == "assistant" else "user", blocks)
    if wants_json(req):
        system.append(JSON_INSTRUCTION)
    return "\n\n".join(s for s in system if s), out


def _tools(req: dict) -> list[dict]:
    return [{"name": (t.get("function") or {}).get("name"), "description": (t.get("function") or {}).get("description", ""),
             "input_schema": (t.get("function") or {}).get("parameters") or {"type": "object", "properties": {}}}
            for t in req.get("tools") or []]


class AnthropicProvider(Provider):
    type = "anthropic"

    def configured(self) -> tuple[bool, str]:
        ok, why = super().configured()
        if not ok:
            return ok, why
        if _client_factory is None and not os.environ.get(self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")):
            return False, f"{self.cfg.get('api_key_env', 'ANTHROPIC_API_KEY')} not set"
        return True, ""

    def client(self):
        ok, why = self.configured()
        if not ok:
            raise ProviderUnavailable(f"{self.name}: {why}")
        if _client_factory is not None:
            return _client_factory(self)
        import anthropic  # noqa: PLC0415

        return anthropic.AsyncAnthropic(api_key=os.environ[self.cfg.get("api_key_env", "ANTHROPIC_API_KEY")],
                                        timeout=self.timeout, max_retries=0)

    def _kwargs(self, model: str, req: dict) -> dict:
        system, messages = to_anthropic(req)
        kw: dict[str, Any] = {"model": model, "messages": messages,
                              "max_tokens": int(req.get("max_completion_tokens") or req.get("max_tokens")
                                                or self.defaults.get("max_tokens") or 1024)}
        if system:
            kw["system"] = system
        for k in ("temperature", "top_p"):
            if req.get(k) is not None:
                kw[k] = req[k]
        if req.get("stop"):
            kw["stop_sequences"] = req["stop"] if isinstance(req["stop"], list) else [req["stop"]]
        if req.get("tools"):
            kw["tools"] = _tools(req)
            tc = req.get("tool_choice")
            if tc == "required":
                kw["tool_choice"] = {"type": "any"}
            elif tc == "none":
                kw["tool_choice"] = {"type": "none"}
            elif isinstance(tc, dict):
                kw["tool_choice"] = {"type": "tool", "name": (tc.get("function") or {}).get("name")}
        return kw

    def _err(self, e: Exception) -> ProviderError:
        if isinstance(e, ProviderError):
            return e
        code = getattr(e, "status_code", None)
        name = type(e).__name__
        retry = code is None or code == 429 or code >= 500 or "Timeout" in name or "Connection" in name
        return ProviderError(f"{self.name}: {name}: {str(e)[:300]}", retryable=retry, status=code)

    @staticmethod
    def _parse(msg: Any) -> dict:
        texts, calls = [], []
        for b in getattr(msg, "content", None) or []:
            bt = getattr(b, "type", None)
            if bt == "text":
                texts.append(b.text)
            elif bt == "tool_use":
                calls.append({"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input or {})}})
        stop = getattr(msg, "stop_reason", "end_turn")
        finish = "tool_calls" if calls else ("length" if stop == "max_tokens" else "stop")
        u = getattr(msg, "usage", None)
        out: dict[str, Any] = {"role": "assistant", "content": "".join(texts) if (texts or not calls) else None}
        if calls:
            out["tool_calls"] = calls
        return {"message": out, "finish_reason": finish,
                "usage": {"prompt_tokens": int(getattr(u, "input_tokens", 0) or 0),
                          "completion_tokens": int(getattr(u, "output_tokens", 0) or 0)}}

    async def chat(self, model: str, req: dict) -> dict:
        try:
            msg = await self.client().messages.create(**self._kwargs(model, req))
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        return self._parse(msg)

    async def stream(self, model: str, req: dict) -> AsyncIterator[dict]:
        try:
            async with self.client().messages.stream(**self._kwargs(model, req)) as s:
                yield {"delta": {"role": "assistant"}, "finish_reason": None, "usage": None}
                async for text in s.text_stream:
                    if text:
                        yield {"delta": {"content": text}, "finish_reason": None, "usage": None}
                final = await s.get_final_message()
        except ProviderError:
            raise
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        res = self._parse(final)
        delta: dict[str, Any] = {}
        if res["message"].get("tool_calls"):
            delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(res["message"]["tool_calls"])]
        yield {"delta": delta, "finish_reason": res["finish_reason"], "usage": res["usage"]}

    async def status(self) -> dict:
        ok, why = self.configured()
        return {"ok": ok, "detail": why or "API key present (not called — no spend on status checks)"}


class AnthropicVertexProvider(AnthropicProvider):
    type = "anthropic_vertex"

    def configured(self) -> tuple[bool, str]:
        ok, why = Provider.configured(self)
        if not ok:
            return ok, why
        if _client_factory is not None:
            return True, ""
        try:
            from bsp_common import gcp  # noqa: PLC0415

            if gcp.configured("vertex") or os.environ.get("VERTEX_USE_ADC") == "1":
                return True, ""
        except ImportError:
            pass
        return False, "no GCP credentials (Studio → Credentials → GCP_SA_JSON)"

    def client(self):
        ok, why = self.configured()
        if not ok:
            raise ProviderUnavailable(f"{self.name}: {why}")
        if _client_factory is not None:
            return _client_factory(self)
        from anthropic import AsyncAnthropicVertex  # noqa: PLC0415
        from bsp_common import gcp  # noqa: PLC0415

        return AsyncAnthropicVertex(project_id=self.cfg.get("project"), region=self.cfg.get("region"),
                                    credentials=gcp.credentials("vertex"), timeout=self.timeout, max_retries=0)

    async def status(self) -> dict:
        ok, why = self.configured()
        if not ok:
            return {"ok": False, "detail": why}
        model = next((m["id"] for m in self.cfg.get("models") or []), "")
        try:
            r = await self.chat(model, {"messages": [{"role": "user", "content": "Reply with OK."}], "max_tokens": 8})
            return {"ok": True, "detail": f"{model} answered {(r['message'].get('content') or '')[:20]!r}"}
        except ProviderError as e:
            return {"ok": False, "detail": str(e)}
