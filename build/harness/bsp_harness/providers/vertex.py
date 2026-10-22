"""Vertex AI adapter (Gemini) — google-genai SDK with vertexai=True.

OpenAI → Gemini mapping
  system/developer messages   → config.system_instruction
  user / assistant            → Content(role="user" | "model")
  image_url parts             → Part.from_bytes (data: URIs) / Part.from_uri (gs:// or https)
  assistant.tool_calls        → Part(function_call=…)
  role "tool"                 → Part(function_response=…) in a user turn (name from the matching tool_call id)
  tools[].function            → Tool(function_declarations=[…]) (JSON schema sanitised for Gemini)
  response_format json_*      → response_mime_type="application/json" (+ response_json_schema when given)
Credentials come from bsp_common.gcp.credentials("vertex") (Studio → Cloud identities: GCP_VERTEX_JSON → GCP_SA_JSON → ADC); never written to disk.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from .base import (JSON_INSTRUCTION, Provider, ProviderError, ProviderUnavailable, json_schema_of, split_content,
                   tool_args_obj, tool_names_by_id, wants_json)

# Tests replace this with a fake: factory(project, location, credentials) -> client with .aio.models
_client_factory = None
_GEMINI_SCHEMA_DROP = {"$schema", "additionalProperties", "title", "default", "examples", "$defs", "definitions",
                       "$ref", "const", "exclusiveMinimum", "exclusiveMaximum", "patternProperties"}


def sanitize_schema(s: Any) -> Any:
    """Gemini's function schema is an OpenAPI subset: drop keys it rejects, collapse anyOf-null."""
    if isinstance(s, list):
        return [sanitize_schema(x) for x in s]
    if not isinstance(s, dict):
        return s
    if "anyOf" in s:
        opts = [o for o in s["anyOf"] if not (isinstance(o, dict) and o.get("type") == "null")]
        if len(opts) == 1:
            merged = {**{k: v for k, v in s.items() if k != "anyOf"}, **opts[0], "nullable": True}
            return sanitize_schema(merged)
    out = {}
    for k, v in s.items():
        if k in _GEMINI_SCHEMA_DROP:
            continue
        if k == "type" and isinstance(v, list):
            types_ = [t for t in v if t != "null"]
            out["type"] = types_[0] if types_ else "string"
            if "null" in v:
                out["nullable"] = True
            continue
        if k == "properties" and isinstance(v, dict):
            # keys here are PARAMETER NAMES, not schema keywords: a tool argument
            # called `title` / `default` / `examples` must survive
            out[k] = {name: sanitize_schema(sub) for name, sub in v.items()}
            continue
        out[k] = sanitize_schema(v) if isinstance(v, (dict, list)) else v
    if isinstance(out.get("required"), list):
        props = out.get("properties") or {}
        out["required"] = [r for r in out["required"] if r in props]   # Gemini rejects dangling names
        if not out["required"]:
            out.pop("required")
    if out.get("type") == "object" and "properties" in out and not out["properties"]:
        out.pop("properties")
    return out


def _types():
    from google.genai import types  # noqa: PLC0415

    return types


class VertexProvider(Provider):
    type = "vertex"

    def __init__(self, name, cfg, defaults=None):
        super().__init__(name, cfg, defaults)
        self._client = None
        self._client_key: tuple = ()

    def _credentials(self):
        try:
            from bsp_common import gcp  # noqa: PLC0415
        except ImportError:
            return None
        import os  # noqa: PLC0415

        if not (gcp.configured("vertex") or os.environ.get("VERTEX_USE_ADC") == "1"):
            return None   # don't probe the GCE metadata server for ADC unless asked to
        try:
            return gcp.credentials("vertex")
        except Exception as e:  # noqa: BLE001
            raise ProviderUnavailable(f"{self.name}: invalid GCP credentials ({type(e).__name__}: {e})") from None

    def configured(self) -> tuple[bool, str]:
        ok, why = super().configured()
        if not ok:
            return ok, why
        if _client_factory is not None:
            return True, ""
        try:
            from bsp_common import gcp  # noqa: PLC0415

            if gcp.configured("vertex"):
                return True, ""
        except ImportError:
            pass
        import os  # noqa: PLC0415

        if os.environ.get("VERTEX_USE_ADC") == "1":
            return True, "application default credentials"
        return False, "no GCP credentials (Studio → Credentials → GCP_SA_JSON)"

    def client(self):
        ok, why = self.configured()
        if not ok:
            raise ProviderUnavailable(f"{self.name}: {why}")
        import os  # noqa: PLC0415 — Studio → Cloud identities: Vertex AI project wins over the config default
        key = (os.environ.get("GCP_VERTEX_PROJECT", "").strip() or self.cfg.get("project"), self.cfg.get("region"))
        if self._client is None or key != self._client_key:
            project, location = key
            creds = self._credentials()
            if _client_factory is not None:
                self._client = _client_factory(project, location, creds)
            else:
                from google import genai  # noqa: PLC0415

                if creds is None:
                    raise ProviderUnavailable(f"{self.name}: no GCP credentials")
                self._client = genai.Client(vertexai=True, project=project, location=location, credentials=creds)
            self._client_key = key
        return self._client

    # mapping ----------------------------------------------------------------
    def build(self, model: str, req: dict) -> tuple[list, Any]:
        t = _types()
        msgs = req.get("messages") or []
        names = tool_names_by_id(msgs)
        system: list[str] = []
        contents: list = []

        def add(role: str, parts: list) -> None:
            if not parts:
                return
            if contents and contents[-1].role == role:
                contents[-1].parts.extend(parts)
            else:
                contents.append(t.Content(role=role, parts=parts))

        for m in msgs:
            role = m.get("role", "user")
            text, images = split_content(m.get("content"))
            if role in ("system", "developer"):
                system.append(text)
                continue
            if role == "tool":
                name = m.get("name") or names.get(m.get("tool_call_id", ""), "tool")
                try:
                    resp = json.loads(text)
                    if not isinstance(resp, dict):
                        resp = {"result": resp}
                except ValueError:
                    resp = {"result": text}
                add("user", [t.Part(function_response=t.FunctionResponse(name=name, response=resp))])
                continue
            parts = []
            if text:
                parts.append(t.Part(text=text))
            for img in images:
                if "data" in img:
                    parts.append(t.Part.from_bytes(data=img["data"], mime_type=img["mime"]))
                else:
                    parts.append(t.Part.from_uri(file_uri=img["url"], mime_type=img["mime"]))
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                parts.append(t.Part(function_call=t.FunctionCall(name=fn.get("name"), args=tool_args_obj(fn.get("arguments")))))
            add("model" if role == "assistant" else "user", parts)

        cfg: dict[str, Any] = {}
        if wants_json(req):
            cfg["response_mime_type"] = "application/json"
            schema = json_schema_of(req)
            if schema:
                cfg["response_json_schema"] = schema
            if not any("json" in s.lower() for s in system):
                system.append(JSON_INSTRUCTION)
        if system:
            cfg["system_instruction"] = "\n\n".join(s for s in system if s)
        if req.get("temperature") is not None:
            cfg["temperature"] = req["temperature"]
        if req.get("top_p") is not None:
            cfg["top_p"] = req["top_p"]
        mt = req.get("max_completion_tokens") or req.get("max_tokens")
        if mt:
            cfg["max_output_tokens"] = int(mt)
        if req.get("stop"):
            cfg["stop_sequences"] = req["stop"] if isinstance(req["stop"], list) else [req["stop"]]
        if req.get("seed") is not None:
            cfg["seed"] = req["seed"]
        tb = (self._model_cfg(model) or {}).get("thinking_budget")
        if tb is not None:
            cfg["thinking_config"] = t.ThinkingConfig(thinking_budget=int(tb))
        if req.get("tools"):
            decls = []
            for tool in req["tools"]:
                fn = tool.get("function") or {}
                d = {"name": fn.get("name"), "description": fn.get("description", "")}
                params = sanitize_schema(fn.get("parameters") or {})
                if params.get("properties"):
                    d["parameters"] = params
                decls.append(t.FunctionDeclaration(**d))
            cfg["tools"] = [t.Tool(function_declarations=decls)]
            tc = req.get("tool_choice")
            if tc in ("none", "required") or isinstance(tc, dict):
                mode = {"none": "NONE", "required": "ANY"}.get(tc, "ANY") if isinstance(tc, str) else "ANY"
                fcc: dict[str, Any] = {"mode": mode}
                if isinstance(tc, dict):
                    fcc["allowed_function_names"] = [(tc.get("function") or {}).get("name")]
                cfg["tool_config"] = t.ToolConfig(function_calling_config=t.FunctionCallingConfig(**fcc))
            cfg["automatic_function_calling"] = t.AutomaticFunctionCallingConfig(disable=True)
        return contents, t.GenerateContentConfig(**cfg)

    def _model_cfg(self, model: str) -> dict:
        for m in self.cfg.get("models") or []:
            if m.get("id") == model:
                return m
        return {}

    @staticmethod
    def _parse(resp: Any) -> tuple[str, list[dict], str, dict]:
        texts, calls = [], []
        cands = getattr(resp, "candidates", None) or []
        finish = "stop"
        if cands:
            c0 = cands[0]
            content = getattr(c0, "content", None)
            for p in (getattr(content, "parts", None) or []):
                if getattr(p, "thought", False):
                    continue
                if getattr(p, "text", None):
                    texts.append(p.text)
                fc = getattr(p, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    calls.append({"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
                                  "function": {"name": fc.name, "arguments": json.dumps(dict(getattr(fc, "args", None) or {}))}})
            fr = str(getattr(c0, "finish_reason", "") or "")
            if "MAX_TOKENS" in fr:
                finish = "length"
            elif fr and not any(x in fr for x in ("STOP", "UNSPECIFIED")):
                finish = "content_filter" if any(x in fr for x in ("SAFETY", "BLOCK", "PROHIBITED", "RECITATION")) else "stop"
        if calls:
            finish = "tool_calls"
        um = getattr(resp, "usage_metadata", None)
        usage = {"prompt_tokens": int(getattr(um, "prompt_token_count", 0) or 0),
                 "completion_tokens": int(getattr(um, "candidates_token_count", 0) or 0)}
        return "".join(texts), calls, finish, usage

    def _err(self, e: Exception) -> ProviderError:
        if isinstance(e, ProviderError):
            return e
        if isinstance(e, asyncio.TimeoutError):
            return ProviderError(f"{self.name}: timeout", status=504)
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        try:
            code = int(code) if code is not None else None
        except (TypeError, ValueError):
            code = None
        retry = code is None or code == 429 or code >= 500
        return ProviderError(f"{self.name}: {type(e).__name__}: {str(e)[:300]}", retryable=retry, status=code)

    async def chat(self, model: str, req: dict) -> dict:
        contents, config = self.build(model, req)
        try:
            resp = await asyncio.wait_for(self.client().aio.models.generate_content(model=model, contents=contents,
                                                                                    config=config), self.timeout)
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        text, calls, finish, usage = self._parse(resp)
        msg: dict[str, Any] = {"role": "assistant", "content": text if (text or not calls) else None}
        if calls:
            msg["tool_calls"] = calls
        return {"message": msg, "finish_reason": finish, "usage": usage}

    async def stream(self, model: str, req: dict) -> AsyncIterator[dict]:
        contents, config = self.build(model, req)
        try:
            it = await self.client().aio.models.generate_content_stream(model=model, contents=contents, config=config)
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        first, n_tools, usage, finish = True, 0, {"prompt_tokens": 0, "completion_tokens": 0}, "stop"
        try:
            async for chunk in it:
                text, calls, fin, u = self._parse(chunk)
                if u["prompt_tokens"] or u["completion_tokens"]:
                    usage = u
                if fin != "stop":
                    finish = fin
                delta: dict[str, Any] = {}
                if first:
                    delta["role"] = "assistant"
                    first = False
                if text:
                    delta["content"] = text
                if calls:
                    delta["tool_calls"] = [{**c, "index": n_tools + i} for i, c in enumerate(calls)]
                    n_tools += len(calls)
                if delta:
                    yield {"delta": delta, "finish_reason": None, "usage": None}
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        yield {"delta": {}, "finish_reason": "tool_calls" if n_tools else finish, "usage": usage}

    async def embed(self, model: str, texts: list[str]) -> dict:
        try:
            resp = await asyncio.wait_for(self.client().aio.models.embed_content(model=model, contents=texts), self.timeout)
        except Exception as e:  # noqa: BLE001
            raise self._err(e) from None
        vecs, toks = [], 0
        for e in getattr(resp, "embeddings", None) or []:
            vecs.append(list(getattr(e, "values", None) or []))
            st = getattr(e, "statistics", None)
            toks += int(getattr(st, "token_count", 0) or 0) if st is not None else 0
        return {"vectors": vecs, "usage": {"prompt_tokens": toks or sum(len(t.split()) for t in texts)}}

    async def status(self) -> dict:
        ok, why = self.configured()
        if not ok:
            return {"ok": False, "detail": why, "project": self.cfg.get("project"), "region": self.cfg.get("region")}
        chat_models = [m["id"] for m in self.cfg.get("models") or [] if m.get("kind", "chat") != "embed"]
        model = chat_models[0] if chat_models else "gemini-2.5-flash"
        try:
            res = await self.chat(model, {"messages": [{"role": "user", "content": "Reply with the word OK."}],
                                          "max_tokens": 16, "temperature": 0})
            return {"ok": True, "detail": f"{model} answered: {(res['message'].get('content') or '').strip()[:40]!r}",
                    "project": self.cfg.get("project"), "region": self.cfg.get("region")}
        except ProviderError as e:
            return {"ok": False, "detail": str(e), "project": self.cfg.get("project"), "region": self.cfg.get("region")}
