"""Client for the appliance LLM gateway (the AI harness, OpenAI-compatible at
LLM_GATEWAY_URL). Services ask for a *model alias* (`extract-default`,
`classify-default`, `chat-default`, `vlm-default`, `embed-default`) and the
harness routes it to Ollama / Vertex AI / Anthropic per config/ai/providers.yaml.

`available()` lets callers fall back to deterministic rules when no gateway
is running, so every demo works offline."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from . import settings

TIMEOUT = float(os.environ.get("LLM_TIMEOUT", "120"))


def _headers() -> dict[str, str]:
    tok = os.environ.get("LLM_GATEWAY_TOKEN", "")
    return {"Authorization": f"Bearer {tok}"} if tok else {}


def available() -> bool:
    try:
        r = httpx.get(settings.get().llm_gateway_url.rstrip("/") + "/models", headers=_headers(), timeout=3)
        return r.status_code == 200
    except Exception:
        return False


def chat(messages: list[dict[str, Any]], model: str = "chat-default", **kw: Any) -> str:
    body = {"model": model, "messages": messages, **kw}
    r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/chat/completions", json=body,
                   headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"] or ""


def extract_json(prompt: str, schema: dict[str, Any], model: str = "extract-default",
                 system: str = "Return only JSON that matches the schema.") -> dict[str, Any]:
    msgs = [{"role": "system", "content": system + "\nJSON schema:\n" + json.dumps(schema)},
            {"role": "user", "content": prompt}]
    text = chat(msgs, model=model, temperature=0, response_format={"type": "json_object"})
    m = re.search(r"\{.*\}", text, re.S)
    return json.loads(m.group(0) if m else text)


def embed(texts: list[str], model: str = "embed-default") -> list[list[float]]:
    r = httpx.post(settings.get().llm_gateway_url.rstrip("/") + "/embeddings", json={"model": model, "input": texts},
                   headers=_headers(), timeout=TIMEOUT)
    r.raise_for_status()
    return [d["embedding"] for d in r.json()["data"]]
