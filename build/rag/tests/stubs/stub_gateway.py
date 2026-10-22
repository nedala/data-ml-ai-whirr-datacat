"""TEST STUB of the harness LLM gateway (OpenAI-compatible, /ai/v1): maps the
appliance aliases onto a local Ollama (tiny models) so the LLM path of bsp_rag
and Open WebUI can be exercised without the real harness.

    OLLAMA=http://ollama:11434 CHAT_MODEL=qwen2.5:0.5b EMBED_MODEL=all-minilm \
    uvicorn stub_gateway:app --port 8096
"""
import os

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

OLLAMA = os.environ.get("OLLAMA", "http://ollama:11434").rstrip("/")
CHAT = os.environ.get("CHAT_MODEL", "qwen2.5:0.5b")
EMBED = os.environ.get("EMBED_MODEL", "all-minilm")
ALIASES = {"chat-default": CHAT, "extract-default": CHAT, "classify-default": CHAT, "vlm-default": CHAT, "embed-default": EMBED}
app = FastAPI()
CALLS: list[dict] = []


@app.get("/ai/v1/models")
def models():
    return {"object": "list", "data": [{"id": a, "object": "model", "owned_by": "bsp-gateway-stub"} for a in ALIASES]}


@app.get("/ai/v1/calls")
def calls():
    return CALLS[-50:]


@app.post("/ai/v1/chat/completions")
async def chat(request: Request):
    body = await request.json()
    alias = body.get("model", "chat-default")
    body["model"] = ALIASES.get(alias, CHAT)
    CALLS.append({"alias": alias, "model": body["model"], "stream": bool(body.get("stream")), "tools": len(body.get("tools") or [])})
    if body.get("stream"):
        async def gen():
            async with httpx.AsyncClient(timeout=600) as c:
                async with c.stream("POST", OLLAMA + "/v1/chat/completions", json=body) as r:
                    async for chunk in r.aiter_raw():
                        yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream")
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(OLLAMA + "/v1/chat/completions", json=body)
    d = r.json()
    d["model"] = f"ollama/{body['model']}"
    return JSONResponse(d, status_code=r.status_code, headers={"x-bsp-provider": "ollama"})


@app.post("/ai/v1/embeddings")
async def embeddings(request: Request):
    body = await request.json()
    alias = body.get("model", "embed-default")
    body["model"] = ALIASES.get(alias, EMBED)
    CALLS.append({"alias": alias, "model": body["model"], "n": len(body["input"]) if isinstance(body.get("input"), list) else 1})
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(OLLAMA + "/v1/embeddings", json=body)
    d = r.json()
    d["model"] = f"ollama/{body['model']}"
    return JSONResponse(d, status_code=r.status_code)
