"""Provider adapter contract + OpenAI-message helpers shared by every adapter.

Every adapter takes an OpenAI-style chat request (messages with string or
content-part content, tools, response_format, temperature, max_tokens …) and
returns a normalised result:

    chat()   -> {"message": {"role": "assistant", "content": str|None, "tool_calls": [...]?},
                 "finish_reason": "stop"|"length"|"tool_calls", "usage": {"prompt_tokens", "completion_tokens"}}
    stream() -> async iterator of {"delta": {...}, "finish_reason": str|None, "usage": {...}|None}
    embed()  -> {"vectors": [[float]], "usage": {"prompt_tokens"}}
"""
from __future__ import annotations

import base64
import json
import re
from typing import Any, AsyncIterator

import httpx


class ProviderError(Exception):
    """A provider failure. `retryable` → retry/fallback; status mirrors HTTP when known."""

    def __init__(self, message: str, *, retryable: bool = True, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class ProviderUnavailable(ProviderError):
    """Not configured (no key / disabled) — skip to the next route without retrying."""

    def __init__(self, message: str):
        super().__init__(message, retryable=False)


def http_error(e: Exception, who: str) -> ProviderError:
    if isinstance(e, httpx.TimeoutException):
        return ProviderError(f"{who}: timeout", retryable=True, status=504)
    if isinstance(e, httpx.ConnectError):
        return ProviderError(f"{who}: cannot connect ({e})", retryable=True, status=503)
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        body = e.response.text[:300]
        return ProviderError(f"{who}: HTTP {code} {body}", retryable=code == 429 or code >= 500, status=code)
    return ProviderError(f"{who}: {type(e).__name__}: {e}", retryable=True)


_DATA_URI = re.compile(r"^data:(?P<mime>[\w.+/-]+)?(?P<b64>;base64)?,(?P<data>.*)$", re.S)


def parse_image_url(url: str) -> dict:
    """{'mime','data': bytes} for data: URIs, {'url', 'mime'} otherwise."""
    m = _DATA_URI.match(url or "")
    if m:
        raw = m.group("data")
        data = base64.b64decode(raw) if m.group("b64") else raw.encode()
        return {"mime": m.group("mime") or "image/png", "data": data}
    mime = "image/png"
    low = (url or "").lower().split("?")[0]
    for ext, mt in ((".jpg", "image/jpeg"), (".jpeg", "image/jpeg"), (".webp", "image/webp"), (".gif", "image/gif"),
                    (".pdf", "application/pdf")):
        if low.endswith(ext):
            mime = mt
    return {"url": url, "mime": mime}


async def fetch_image(img: dict, timeout: float = 30) -> dict:
    """Resolve an http(s) image into bytes (for providers that only take inline images)."""
    if "data" in img:
        return img
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
        r = await c.get(img["url"])
        r.raise_for_status()
        return {"mime": r.headers.get("content-type", img.get("mime", "image/png")).split(";")[0], "data": r.content}


def split_content(content: Any) -> tuple[str, list[dict]]:
    """OpenAI message content → (text, images). Images: parse_image_url() dicts."""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    texts, images = [], []
    for part in content:
        if not isinstance(part, dict):
            texts.append(str(part))
            continue
        t = part.get("type")
        if t in ("text", "input_text"):
            texts.append(part.get("text", ""))
        elif t in ("image_url", "input_image"):
            iu = part.get("image_url")
            url = iu.get("url") if isinstance(iu, dict) else iu
            if url:
                images.append(parse_image_url(url))
    return "\n".join(texts), images


def has_images(messages: list[dict]) -> bool:
    return any(split_content(m.get("content"))[1] for m in messages if isinstance(m.get("content"), list))


def wants_json(req: dict) -> bool:
    rf = req.get("response_format") or {}
    return isinstance(rf, dict) and rf.get("type") in ("json_object", "json_schema")


def json_schema_of(req: dict) -> dict | None:
    rf = req.get("response_format") or {}
    if isinstance(rf, dict) and rf.get("type") == "json_schema":
        js = rf.get("json_schema") or {}
        return js.get("schema") if isinstance(js, dict) else None
    return None


def tool_args_str(args: Any) -> str:
    return args if isinstance(args, str) else json.dumps(args or {})


def tool_args_obj(args: Any) -> dict:
    if isinstance(args, dict):
        return args
    try:
        v = json.loads(args or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except ValueError:
        return {"_raw": args}


def tool_names_by_id(messages: list[dict]) -> dict[str, str]:
    out = {}
    for m in messages:
        for tc in m.get("tool_calls") or []:
            out[tc.get("id", "")] = (tc.get("function") or {}).get("name", "")
    return out


JSON_INSTRUCTION = "Respond with a single valid JSON object only — no prose, no markdown fences."


class Provider:
    type = "base"

    def __init__(self, name: str, cfg: dict, defaults: dict | None = None):
        self.name = name
        self.cfg = cfg or {}
        self.defaults = defaults or {}

    @property
    def local(self) -> bool:
        return bool(self.cfg.get("local", False))

    @property
    def timeout(self) -> float:
        return float(self.cfg.get("timeout_s") or self.defaults.get("timeout_s") or 120)

    def configured(self) -> tuple[bool, str]:
        if self.cfg.get("enabled", True) is False:
            return False, "disabled in config"
        return True, ""

    async def chat(self, model: str, req: dict) -> dict:
        raise NotImplementedError

    async def stream(self, model: str, req: dict) -> AsyncIterator[dict]:
        """Default: one chunk from chat() (adapters override with real streaming)."""
        res = await self.chat(model, req)
        msg = res["message"]
        delta: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            delta["tool_calls"] = [{**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])]
        yield {"delta": delta, "finish_reason": res.get("finish_reason", "stop"), "usage": res.get("usage")}

    async def embed(self, model: str, texts: list[str]) -> dict:
        raise ProviderError(f"{self.name}: embeddings not supported", retryable=False, status=400)

    async def status(self) -> dict:
        ok, why = self.configured()
        return {"ok": ok, "detail": why or "configured"}

    async def list_models(self) -> list[str]:
        return [m["id"] for m in self.cfg.get("models") or []]
