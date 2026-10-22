"""FastAPI app: gateway (/ai/v1/*), UI JSON API (/ai/api/*), UI (/ai/), UI kit (/ai/ui-kit/).

Routes carry the /ai prefix natively so the same URLs work behind nginx
(path kept) and service-to-service (http://harness:8096/ai/v1)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, agent, evals, history, providers, sqlstudio, telemetry
from . import router as rt
from .config import CONFIG, base_path, deep_merge, interpolate, override_path, validate
from .gateway import api as gateway_api
from .identity import user_of

log = logging.getLogger("bsp_harness")
PREFIX = "/ai"
STATIC = Path(__file__).parent / "static"
UI_KIT = Path(os.environ.get("UI_KIT_DIR", "/opt/ui-kit"))
if not UI_KIT.exists():
    UI_KIT = Path(__file__).resolve().parents[2] / "ui-kit"

ui = APIRouter(prefix=PREFIX + "/api")


def sse(events) -> StreamingResponse:
    async def gen():
        try:
            async for ev in events:
                yield "data: " + json.dumps(ev, default=str) + "\n\n"
        except rt.RouteError as e:
            yield "data: " + json.dumps({"type": "error", "message": str(e), "attempts": e.attempts}) + "\n\n"
        except Exception as e:  # noqa: BLE001
            log.exception("stream failed")
            yield "data: " + json.dumps({"type": "error", "message": f"{type(e).__name__}: {e}"}) + "\n\n"
        yield "data: " + json.dumps({"type": "end"}) + "\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})




@ui.get("/models")
async def ui_models():
    return {"object": "list", "data": rt.model_list()}


@ui.get("/agents")
async def ui_agents():
    """Named Playground presets: model route + tool groups + system prompt (the agent 'soul')."""
    return {"agents": [{"id": k, **v} for k, v in agent.AGENT_PRESETS.items()]}


# ── config ───────────────────────────────────────────────────────────────────
@ui.get("/config")
async def get_config():
    CONFIG.get()
    return {"base_path": str(base_path()), "override_path": str(override_path()), "error": CONFIG.error,
            "base_yaml": base_path().read_text() if base_path().exists() else "",
            "override_yaml": override_path().read_text() if override_path().exists() else "",
            "effective": CONFIG.get(), "raw": CONFIG.raw()}


@ui.post("/config/validate")
async def validate_config(request: Request):
    body = await request.json()
    try:
        ov = yaml.safe_load(body.get("override_yaml") or "") or {}
    except yaml.YAMLError as e:
        return {"ok": False, "errors": [f"YAML: {e}"]}
    if not isinstance(ov, dict):
        return {"ok": False, "errors": ["override must be a mapping"]}
    merged = interpolate(deep_merge(CONFIG.base_raw() or {}, ov))
    errs = validate(merged)
    return {"ok": not errs, "errors": errs, "effective": merged if not errs else None}


@ui.put("/config")
async def put_config(request: Request):
    body = await request.json()
    try:
        ov = yaml.safe_load(body.get("override_yaml") or "") or {}
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML: {e}") from None
    errs = CONFIG.write_override(ov)
    if errs:
        return JSONResponse({"ok": False, "errors": errs}, status_code=400)
    log.info("routing override written by %s", user_of(request))
    return {"ok": True, "effective": CONFIG.get()}


@ui.delete("/config/override")
async def reset_config():
    CONFIG.reset_override()
    return {"ok": True}


@ui.post("/test-route")
async def test_route(request: Request):
    body = await request.json()
    model = body.get("model") or "chat-default"
    try:
        plan = rt.resolve(model, body.get("data_classification"))
    except rt.RouteError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=e.status)
    plan_view = {"routes": [r.label for r in plan.routes], "skipped": plan.skipped, "classification": plan.classification}
    t0 = time.monotonic()
    try:
        if plan.kind == "embed":
            res, info = await rt.embed(model, [body.get("prompt") or "hello"], caller="test-route",
                                       classification=body.get("data_classification"))
            out = f"{len(res['vectors'][0]) if res['vectors'] else 0}-dim vector"
        else:
            res, info = await rt.chat({"model": model, "max_tokens": 64, "messages": [
                {"role": "user", "content": body.get("prompt") or "Reply with one short sentence: which model are you?"}]},
                caller="test-route", classification=body.get("data_classification"))
            out = res["message"].get("content")
        return {"ok": True, "plan": plan_view, "served_by": f"{info['provider']}/{info['model']}", "output": out,
                "attempts": info["attempts"], "ms": int((time.monotonic() - t0) * 1000)}
    except rt.RouteError as e:
        return {"ok": False, "plan": plan_view, "error": str(e), "attempts": e.attempts}


# ── playground ───────────────────────────────────────────────────────────────
async def _ground(body: dict) -> tuple[list[dict], list[dict]]:
    """Optional knowledge-base grounding via the rag service; returns (messages, citations)."""
    kb = body.get("ground") or {}
    msgs = list(body.get("messages") or [])
    if not kb.get("enabled"):
        return msgs, []
    q = next((m.get("content") for m in reversed(msgs) if m.get("role") == "user" and isinstance(m.get("content"), str)), "")
    hits = await rag_search(q, kb.get("collection"), int(kb.get("k") or 5))
    if not hits:
        return msgs, []
    ctx = "\n\n".join(f"[{i + 1}] {h.get('title') or h.get('source') or ''}\n{h.get('text', '')[:1500]}"
                      for i, h in enumerate(hits))
    sys = {"role": "system", "content": "Answer using the numbered knowledge-base excerpts below when relevant and cite "
                                        "them as [n]. If they don't contain the answer, say so.\n\n" + ctx}
    return [sys] + msgs, hits


@ui.post("/chat")
async def playground_chat(request: Request):
    """Streaming playground chat (same router as /v1, no bearer: the UI is Studio-gated)."""
    body = await request.json()
    msgs, cites = await _ground(body)
    req = {k: v for k, v in body.items() if k in ("model", "temperature", "max_tokens", "response_format", "top_p", "seed")}
    req["messages"] = msgs
    if body.get("json_mode"):
        req["response_format"] = {"type": "json_object"}

    async def events():
        if cites:
            yield {"type": "citations", "citations": cites}
        t0 = time.monotonic()
        usage = None
        async for ch in rt.stream(req, caller=user_of(request), classification=body.get("data_classification")):
            if "route" in ch:
                yield {"type": "route", **ch["route"]}
            elif "error" in ch:
                yield {"type": "error", "message": ch["error"]}
            else:
                if ch.get("usage"):
                    usage = ch["usage"]
                d = ch.get("delta") or {}
                if d.get("content"):
                    yield {"type": "delta", "content": d["content"]}
                if d.get("tool_calls"):
                    yield {"type": "tool_calls", "tool_calls": d["tool_calls"]}
        yield {"type": "done", "ms": int((time.monotonic() - t0) * 1000), "usage": usage}

    return sse(events())


@ui.post("/compare")
async def compare(request: Request):
    body = await request.json()
    routes = [r for r in (body.get("routes") or []) if r][:3]
    msgs = body.get("messages") or [{"role": "user", "content": body.get("prompt", "")}]

    async def one(route: str) -> dict:
        t0 = time.monotonic()
        try:
            res, info = await rt.chat({"model": route, "messages": msgs, "temperature": body.get("temperature"),
                                       "max_tokens": body.get("max_tokens") or 512,
                                       **({"response_format": {"type": "json_object"}} if body.get("json_mode") else {})},
                                      caller=f"compare:{user_of(request)}")
            return {"route": route, "ok": True, "served_by": f"{info['provider']}/{info['model']}",
                    "content": res["message"].get("content"), "usage": res.get("usage"),
                    "ms": int((time.monotonic() - t0) * 1000), "attempts": info["attempts"]}
        except rt.RouteError as e:
            return {"route": route, "ok": False, "error": str(e), "ms": int((time.monotonic() - t0) * 1000)}

    return {"results": await asyncio.gather(*(one(r) for r in routes))}


@ui.post("/agent")
async def agent_run(request: Request):
    body = await request.json()
    msgs, cites = await _ground(body)

    async def events():
        if cites:
            yield {"type": "citations", "citations": cites}
        async for ev in agent.run(msgs, body.get("model") or "chat-default", body.get("groups") or None,
                                  int(body.get("max_steps") or 8), body.get("temperature"), caller=user_of(request),
                                  attach_images=bool(body.get("attach_images"))):
            yield ev

    return sse(events())


@ui.get("/mcp/tools")
async def mcp_tools():
    try:
        tools = await agent.list_tools()
        groups: dict[str, int] = {}
        for t in tools:
            groups[t["group"]] = groups.get(t["group"], 0) + 1
        return {"ok": True, "url": agent.mcp_url(), "groups": groups, "tools": tools}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "url": agent.mcp_url(), "error": f"{type(e).__name__}: {e}", "groups": {}, "tools": []}


# ── knowledge base (rag service) ─────────────────────────────────────────────
def rag_url() -> str:
    return os.environ.get("RAG_URL", "http://rag:8101/rag").rstrip("/")


async def rag_search(query: str, collection: str | None, k: int = 5) -> list[dict]:
    body = {"query": query, "k": k, "top_k": k}
    if collection:
        body["collection"] = collection
    try:
        async with httpx.AsyncClient(timeout=20, trust_env=False) as c:
            r = await c.post(rag_url() + "/api/search", json=body)
            r.raise_for_status()
            d = r.json()
    except Exception as e:  # noqa: BLE001
        log.info("rag search unavailable: %s", e)
        return []
    items = d.get("results") or d.get("hits") or d.get("chunks") or (d if isinstance(d, list) else [])
    out = []
    for h in items[:k]:
        md = h.get("metadata") or {}
        out.append({"text": h.get("text") or h.get("content") or h.get("chunk") or "",
                    "title": h.get("title") or md.get("title") or h.get("doc_title") or "",
                    "source": h.get("source") or md.get("source") or h.get("uri") or h.get("url") or "",
                    "score": h.get("score")})
    return out


@ui.get("/rag/collections")
async def rag_collections():
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as c:
            r = await c.get(rag_url() + "/api/collections")
            r.raise_for_status()
            d = r.json()
        cols = d.get("collections") if isinstance(d, dict) else d
        names = [x.get("name") if isinstance(x, dict) else x for x in cols or []]
        return {"ok": True, "collections": names}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "collections": [], "error": f"knowledge base not reachable ({type(e).__name__})"}


# ── providers ────────────────────────────────────────────────────────────────
@ui.get("/providers/status")
async def providers_status():
    async def one(name: str) -> dict:
        try:
            p = providers.get(name)
            st = await asyncio.wait_for(p.status(), 30)
        except Exception as e:  # noqa: BLE001
            st = {"ok": False, "detail": f"{type(e).__name__}: {e}"}
        cfg = CONFIG.provider(name) or {}
        return {"name": name, "type": cfg.get("type"), "local": bool(cfg.get("local")),
                "enabled": cfg.get("enabled", True) is not False,
                "configured_models": [m.get("id") for m in cfg.get("models") or []], **st}

    names = providers.names()
    res = await asyncio.gather(*(one(n) for n in names))
    try:   # which Studio cloud identity Vertex uses (name only — never the key)
        from bsp_common import gcp
        gcp_identity = gcp.assigned_name("vertex")
    except Exception:  # noqa: BLE001
        gcp_identity = ""
    return {"providers": res, "telemetry_db": telemetry.ensure_schema(), "gcp_identity": gcp_identity}


@ui.post("/ollama/pull")
async def ollama_pull(request: Request):
    body = await request.json()
    model = (body.get("model") or "").strip()
    name = body.get("provider") or "ollama"
    if not model:
        raise HTTPException(400, "model is required")
    p = providers.get(name)
    if p.type != "ollama":
        raise HTTPException(400, f"{name} is not an Ollama provider")

    async def events():
        async for ev in p.pull(model):
            yield {"type": "progress", **ev}

    return sse(events())


# ── telemetry & evals ────────────────────────────────────────────────────────
@ui.get("/telemetry")
async def telemetry_view(hours: int = 24):
    bucket = 5 if hours <= 6 else (15 if hours <= 24 else 60)
    return await asyncio.to_thread(telemetry.summary, hours, bucket)


@ui.get("/evals/suites")
async def eval_suites():
    out = []
    for name, s in evals.suites().items():
        out.append({"suite": name, "type": s.get("type"), "description": s.get("description", ""),
                    "cases": len(s.get("cases") or []), "default_routes": s.get("default_routes") or [],
                    "path": s.get("_path")})
    return {"suites": out}


@ui.post("/evals/run")
async def eval_run(request: Request):
    body = await request.json()
    try:
        jid = evals.start_job(body["suite"], body.get("routes") or ["chat-default"], body.get("limit"))
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    return {"job_id": jid}


@ui.get("/evals/jobs/{jid}")
async def eval_job(jid: str):
    j = evals.job_view(jid)
    if j is None:
        raise HTTPException(404, "unknown job")
    return j


@ui.get("/evals/runs")
async def eval_runs():
    return {"runs": await asyncio.to_thread(evals.runs), "leaderboard": await asyncio.to_thread(evals.leaderboard)}


# ── browser live view ────────────────────────────────────────────────────────
@ui.get("/browser/status")
async def browser_status():
    """Is the desktop browser capability up? (noVNC on browser:6080, CDP on browser:9223)."""
    vnc = os.environ.get("BROWSER_VNC_URL", "http://browser:6080")
    out: dict[str, Any] = {"iframe": os.environ.get(
        "BROWSER_VNC_PATH", "/browser/vnc.html?autoconnect=1&reconnect=1&resize=scale&path=browser/websockify"),
        "studio": "/studio/#compose"}
    try:
        async with httpx.AsyncClient(timeout=3, trust_env=False) as c:
            r = await c.get(vnc.rstrip("/") + "/vnc.html")
            out["ok"] = r.status_code < 400
    except Exception as e:  # noqa: BLE001
        out.update(ok=False, error=f"browser capability not running ({type(e).__name__})")
    return out


@ui.get("/info")
async def info():
    return {"version": __version__, "gateway": f"{PREFIX}/v1", "aliases": list((CONFIG.get().get("aliases") or {}).keys()),
            "token_required": bool(os.environ.get("LLM_GATEWAY_TOKEN")), "mcp_url": agent.mcp_url(),
            "config_error": CONFIG.error}


def create_app() -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        CONFIG.get()
        if CONFIG.error:
            log.warning("routing config: %s", CONFIG.error)
        await asyncio.to_thread(telemetry.load_spend)
        yield

    app = FastAPI(title="DataPlatform AI harness", version=__version__, docs_url=PREFIX + "/docs",
                  openapi_url=PREFIX + "/openapi.json", redoc_url=None, lifespan=lifespan)
    app.include_router(history.api, prefix=PREFIX + "/api", tags=["history"])
    app.include_router(sqlstudio.api, prefix=PREFIX + "/api", tags=["federated sql"])
    app.include_router(gateway_api, prefix=PREFIX + "/v1", tags=["gateway"])
    app.include_router(gateway_api, prefix="/v1", include_in_schema=False)
    app.include_router(ui, tags=["ui"])

    @app.get(PREFIX + "/healthz", include_in_schema=False)
    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok", "service": "harness", "version": __version__, "config_ok": not CONFIG.error}

    @app.get(PREFIX, include_in_schema=False)
    async def root_redirect():
        return RedirectResponse(PREFIX + "/")

    @app.get(PREFIX + "/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount(PREFIX + "/static", StaticFiles(directory=STATIC), name="static")
    if UI_KIT.exists():
        app.mount(PREFIX + "/ui-kit", StaticFiles(directory=UI_KIT), name="ui-kit")
    return app


app = create_app()
