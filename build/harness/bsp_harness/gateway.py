"""OpenAI-compatible gateway: GET /v1/models, POST /v1/chat/completions (incl. SSE
streaming, tools, JSON mode, image parts), POST /v1/embeddings. Mounted at /ai/v1
(and /v1 for convenience). Optional bearer token LLM_GATEWAY_TOKEN."""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import router as rt

api = APIRouter()


def check_token(request: Request) -> None:
    tok = os.environ.get("LLM_GATEWAY_TOKEN", "")
    if not tok:
        return
    got = request.headers.get("authorization", "")
    if not hmac.compare_digest(got.strip().encode(), f"Bearer {tok}".encode()):
        raise HTTPException(401, {"error": {"message": "missing or invalid bearer token (LLM_GATEWAY_TOKEN)",
                                            "type": "invalid_request_error", "code": "unauthorized"}})


def caller_of(request: Request) -> str:
    return (request.headers.get("x-caller") or request.headers.get("x-auth-user")
            or (request.client.host if request.client else ""))[:120]


def classification_of(request: Request, body: dict) -> str | None:
    md = body.get("metadata") if isinstance(body.get("metadata"), dict) else {}
    return request.headers.get("x-data-classification") or md.get("data_classification")


def _err(status: int, message: str, attempts: list | None = None, code: str = "routing_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "api_error" if status >= 500 else "invalid_request_error",
                                   "code": code, "attempts": attempts or []}}, status_code=status)


def _clean(body: dict) -> dict:
    """Drop gateway-only fields before routing."""
    return {k: v for k, v in body.items() if k not in ("metadata", "stream_options")}


@api.get("/models", dependencies=[Depends(check_token)])
async def models():
    return {"object": "list", "data": rt.model_list()}


@api.post("/chat/completions", dependencies=[Depends(check_token)])
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _err(400, "body must be JSON", code="invalid_json")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        return _err(400, "messages: a non-empty list is required", code="invalid_request")
    caller, dc = caller_of(request), classification_of(request, body)
    model = body.get("model") or "chat-default"
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    if body.get("stream"):
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        gen = rt.stream(_clean(body), caller=caller, classification=dc)
        # Resolve routing up front so policy / all-down errors come back as proper HTTP errors.
        try:
            first = await gen.__anext__()
        except rt.RouteError as e:
            return _err(e.status, str(e), e.attempts)
        except StopAsyncIteration:
            return _err(502, "empty stream")

        async def sse():
            route = first.get("route") or {}
            served = f"{route.get('provider')}/{route.get('model')}" if route else model

            def frame(delta: dict, finish, usage=None) -> str:
                d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": served,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
                if usage is not None:
                    d["usage"] = usage
                return "data: " + json.dumps(d) + "\n\n"

            yield ": route " + json.dumps(route) + "\n\n"
            try:
                async for ch in gen:
                    if "error" in ch:
                        yield "data: " + json.dumps({"error": {"message": ch["error"]}}) + "\n\n"
                        break
                    delta, fin = ch.get("delta") or {}, ch.get("finish_reason")
                    if delta or fin:
                        yield frame(delta, fin)
                    if fin and include_usage and ch.get("usage"):
                        u = ch["usage"]
                        pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
                        d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": served,
                             "choices": [], "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}
                        yield "data: " + json.dumps(d) + "\n\n"
            except rt.RouteError as e:
                yield "data: " + json.dumps({"error": {"message": str(e), "attempts": e.attempts}}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    try:
        res, info = await rt.chat(_clean(body), caller=caller, classification=dc)
    except rt.RouteError as e:
        return _err(e.status, str(e), e.attempts)
    u = res.get("usage") or {}
    pt, ct = int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)
    return JSONResponse({
        "id": cid, "object": "chat.completion", "created": created, "model": f"{info['provider']}/{info['model']}",
        "choices": [{"index": 0, "message": res["message"], "finish_reason": res.get("finish_reason", "stop")}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
        "x_route": info,
    }, headers={"X-Route": f"{info['provider']}/{info['model']}", "X-Request-Id": info["request_id"]})


def _enc(v: list[float], fmt: str | None):
    if fmt == "base64":
        import base64  # noqa: PLC0415
        import struct  # noqa: PLC0415

        return base64.b64encode(struct.pack(f"<{len(v)}f", *v)).decode()
    return v


@api.post("/embeddings", dependencies=[Depends(check_token)])
async def embeddings(request: Request):
    try:
        body = await request.json()
    except ValueError:
        return _err(400, "body must be JSON", code="invalid_json")
    inp = body.get("input")
    texts = [inp] if isinstance(inp, str) else list(inp or [])
    if not texts or not all(isinstance(t, str) for t in texts):
        return _err(400, "input: a string or a list of strings is required", code="invalid_request")
    try:
        res, info = await rt.embed(body.get("model") or "embed-default", texts, caller=caller_of(request),
                                   classification=classification_of(request, body))
    except rt.RouteError as e:
        return _err(e.status, str(e), e.attempts)
    pt = int((res.get("usage") or {}).get("prompt_tokens") or 0)
    return JSONResponse({"object": "list", "model": f"{info['provider']}/{info['model']}",
                         "data": [{"object": "embedding", "index": i, "embedding": _enc(v, body.get("encoding_format"))}
                                  for i, v in enumerate(res["vectors"])],
                         "usage": {"prompt_tokens": pt, "total_tokens": pt}, "x_route": info},
                        headers={"X-Route": f"{info['provider']}/{info['model']}"})
