"""Alias → ordered routes, policies (data classification, budgets), retries with
backoff and fallback. Every attempt is logged to telemetry."""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from . import providers, telemetry
from .config import CONFIG
from .providers.base import ProviderError, ProviderUnavailable, has_images, wants_json


class RouteError(Exception):
    def __init__(self, message: str, status: int = 502, attempts: list | None = None):
        super().__init__(message)
        self.status = status
        self.attempts = attempts or []


@dataclass
class Route:
    provider: str
    model: str
    params: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"


@dataclass
class Plan:
    requested: str
    alias: dict | None
    routes: list[Route]
    classification: str
    skipped: list[dict]
    kind: str = "chat"


def resolve(model: str, classification: str | None = None, kind: str = "chat") -> Plan:
    """Turn a requested model (alias or provider/model) into the ordered, policy-filtered routes."""
    cfg = CONFIG.get()
    if not cfg:
        raise RouteError(f"routing config not loaded: {CONFIG.error}", 500)
    alias = CONFIG.alias(model)
    if alias:
        params = dict(alias.get("params") or {})
        routes = [Route(*str(r).split("/", 1), params=params) for r in alias.get("routes") or []]
        classification = classification or alias.get("data_classification")
        kind = alias.get("kind", kind)
    elif "/" in model and model.split("/", 1)[0] in (cfg.get("providers") or {}):
        routes = [Route(*model.split("/", 1))]
    else:
        raise RouteError(f"unknown model '{model}' — use an alias ({', '.join(cfg.get('aliases') or {})}) or "
                         f"provider/model", 404)
    pol = CONFIG.policies()
    dc = pol.get("data_classification") or {}
    classification = (classification or dc.get("default") or "internal").lower()
    rule = (dc.get("rules") or {}).get(classification) or {}
    skipped: list[dict] = []
    kept: list[Route] = []
    for r in routes:
        pcfg = CONFIG.provider(r.provider) or {}
        if pcfg.get("enabled", True) is False:
            skipped.append({"route": r.label, "reason": "provider disabled"})
            continue
        if rule.get("local_only") and not pcfg.get("local"):
            skipped.append({"route": r.label, "reason": f"policy: {classification} data → local providers only"})
            continue
        if rule.get("allowed_providers") and r.provider not in rule["allowed_providers"]:
            skipped.append({"route": r.label, "reason": f"policy: {classification} not allowed on {r.provider}"})
            continue
        kept.append(r)
    return Plan(model, alias, kept, classification, skipped, kind)


def _budget_block(provider: str) -> str | None:
    b = CONFIG.policies().get("budgets") or {}
    spend = telemetry.spend_today(provider)
    usd_cap = float((b.get("per_provider_daily_usd") or {}).get(provider) or 0)
    tok_cap = float((b.get("per_provider_daily_tokens") or {}).get(provider) or 0)
    if usd_cap and spend["usd"] >= usd_cap:
        return f"budget: daily ${usd_cap:g} cap reached for {provider}"
    if tok_cap and spend["tokens"] >= tok_cap:
        return f"budget: daily {tok_cap:,.0f}-token cap reached for {provider}"
    return None


def merge_params(req: dict, params: dict) -> dict:
    out = dict(req)
    for k, v in (params or {}).items():
        if k == "json":
            if v and not out.get("response_format"):
                out["response_format"] = {"type": "json_object"}
        elif k in ("temperature", "max_tokens", "top_p", "seed", "stop") and out.get(k) is None:
            out[k] = v
    if out.get("max_tokens") is None and out.get("max_completion_tokens") is None:
        out["max_tokens"] = CONFIG.defaults().get("max_tokens", 1024)
    return out


def _retry_conf(provider: str) -> tuple[int, float, float]:
    d = CONFIG.defaults()
    p = CONFIG.provider(provider) or {}
    return (int(p.get("retries", d.get("retries", 1))), float(p.get("backoff_s", d.get("backoff_s", 0.5))),
            float(p.get("timeout_s", d.get("timeout_s", 120))))


def _meta(req: dict, endpoint: str, plan: Plan, caller: str, stream: bool) -> dict:
    msgs = req.get("messages") or []
    return {"endpoint": endpoint, "alias": plan.requested, "stream": stream, "json_mode": wants_json(req),
            "has_images": has_images(msgs) if msgs else False, "has_tools": bool(req.get("tools")),
            "data_classification": plan.classification, "caller": caller}


def _preview(req: dict, text: str) -> str | None:
    if not (CONFIG.policies().get("logging") or {}).get("store_prompts"):
        return None
    last = next((m for m in reversed(req.get("messages") or []) if m.get("role") == "user"), {})
    c = last.get("content")
    return (str(c if isinstance(c, str) else "[parts]")[:300] + " → " + (text or "")[:300])


async def chat(req: dict, *, caller: str = "", classification: str | None = None) -> tuple[dict, dict]:
    """Non-streaming chat with fallback. Returns (normalised result, route info)."""
    plan = resolve(req.get("model") or "chat-default", classification)
    rid = uuid.uuid4().hex[:16]
    meta = _meta(req, "chat", plan, caller, False)
    attempts: list[dict] = [dict(s, status="skipped") for s in plan.skipped]
    n = 0
    for route in plan.routes:
        blocked = _budget_block(route.provider)
        if blocked:
            attempts.append({"route": route.label, "status": "skipped", "reason": blocked})
            telemetry.record(request_id=rid, provider=route.provider, model=route.model, status="skipped", error=blocked, **meta)
            continue
        retries, backoff, timeout = _retry_conf(route.provider)
        body = merge_params(req, route.params)
        for k in range(retries + 1):
            n += 1
            t0 = time.monotonic()
            try:
                prov = providers.get(route.provider)
                res = await asyncio.wait_for(prov.chat(route.model, body), timeout + 5)
                ms = int((time.monotonic() - t0) * 1000)
                u = res.get("usage") or {}
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n, status="ok",
                                 latency_ms=ms, prompt_tokens=u.get("prompt_tokens"), completion_tokens=u.get("completion_tokens"),
                                 preview=_preview(body, res["message"].get("content") or ""), **meta)
                attempts.append({"route": route.label, "status": "ok", "ms": ms})
                return res, {"request_id": rid, "provider": route.provider, "model": route.model, "latency_ms": ms,
                             "attempts": attempts, "classification": plan.classification}
            except (ProviderError, asyncio.TimeoutError) as e:
                err = e if isinstance(e, ProviderError) else ProviderError(f"{route.provider}: timeout", status=504)
                ms = int((time.monotonic() - t0) * 1000)
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n,
                                 status="skipped" if isinstance(err, ProviderUnavailable) else "error",
                                 error=str(err)[:500], latency_ms=ms, **meta)
                attempts.append({"route": route.label, "status": "error", "reason": str(err)[:300], "ms": ms})
                if not err.retryable or k == retries:
                    break
                await asyncio.sleep(backoff * (2 ** k))
    raise RouteError(_fail_msg(plan, attempts), _fail_status(attempts), attempts)


def _fail_msg(plan: Plan, attempts: list[dict]) -> str:
    if not plan.routes and plan.skipped:
        return f"no route allowed for '{plan.requested}': " + "; ".join(f"{s['route']} ({s['reason']})" for s in plan.skipped)
    return f"all routes failed for '{plan.requested}': " + "; ".join(
        f"{a['route']}: {a.get('reason', a['status'])}" for a in attempts if a["status"] != "ok")


def _fail_status(attempts: list[dict]) -> int:
    reasons = " ".join(a.get("reason", "") for a in attempts)
    return 403 if "policy:" in reasons and not any(a["status"] == "error" for a in attempts) else 502


async def stream(req: dict, *, caller: str = "", classification: str | None = None) -> AsyncIterator[dict]:
    """Streaming chat. Falls back only before the first chunk is sent. Yields
    {"route": info} first, then provider chunks."""
    plan = resolve(req.get("model") or "chat-default", classification)
    rid = uuid.uuid4().hex[:16]
    meta = _meta(req, "chat", plan, caller, True)
    attempts: list[dict] = [dict(s, status="skipped") for s in plan.skipped]
    n = 0
    for route in plan.routes:
        blocked = _budget_block(route.provider)
        if blocked:
            attempts.append({"route": route.label, "status": "skipped", "reason": blocked})
            continue
        retries, backoff, timeout = _retry_conf(route.provider)
        body = merge_params(req, route.params)
        for k in range(retries + 1):
            n += 1
            t0 = time.monotonic()
            started = False
            ttft = None
            usage: dict = {}
            text_len = 0
            try:
                prov = providers.get(route.provider)
                agen = prov.stream(route.model, body).__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(agen.__anext__(), timeout + 5)
                    except StopAsyncIteration:
                        break
                    if not started:
                        started = True
                        ttft = int((time.monotonic() - t0) * 1000)
                        attempts.append({"route": route.label, "status": "ok"})
                        yield {"route": {"request_id": rid, "provider": route.provider, "model": route.model,
                                         "attempts": attempts, "classification": plan.classification}}
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    text_len += len((chunk.get("delta") or {}).get("content") or "")
                    yield chunk
                ms = int((time.monotonic() - t0) * 1000)
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n, status="ok",
                                 latency_ms=ms, ttft_ms=ttft, prompt_tokens=usage.get("prompt_tokens"),
                                 completion_tokens=usage.get("completion_tokens"), **meta)
                return
            except (ProviderError, asyncio.TimeoutError) as e:
                err = e if isinstance(e, ProviderError) else ProviderError(f"{route.provider}: timeout", status=504)
                ms = int((time.monotonic() - t0) * 1000)
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n,
                                 status="skipped" if isinstance(err, ProviderUnavailable) else "error",
                                 error=str(err)[:500], latency_ms=ms, **meta)
                if started:  # mid-stream failure: cannot fall back transparently
                    yield {"error": str(err)}
                    return
                attempts.append({"route": route.label, "status": "error", "reason": str(err)[:300], "ms": ms})
                if not err.retryable or k == retries:
                    break
                await asyncio.sleep(backoff * (2 ** k))
    raise RouteError(_fail_msg(plan, attempts), _fail_status(attempts), attempts)


async def _embed_batched(route: Route, texts: list[str], timeout: float) -> dict:
    """Split large inputs (RAG ingestion) into provider-sized batches, preserving order."""
    prov = providers.get(route.provider)
    pcfg = CONFIG.provider(route.provider) or {}
    size = int(pcfg.get("embed_batch") or {"vertex": 100, "ollama": 64}.get(prov.type, 128))
    vecs: list = []
    toks = 0
    for i in range(0, len(texts), size):
        part = await asyncio.wait_for(prov.embed(route.model, texts[i:i + size]), timeout + 5)
        if len(part["vectors"]) != len(texts[i:i + size]):
            raise ProviderError(f"{route.provider}: got {len(part['vectors'])} vectors for {len(texts[i:i + size])} inputs")
        vecs.extend(part["vectors"])
        toks += int((part.get("usage") or {}).get("prompt_tokens") or 0)
    return {"vectors": vecs, "usage": {"prompt_tokens": toks}}


async def embed(model: str, texts: list[str], *, caller: str = "", classification: str | None = None) -> tuple[dict, dict]:
    plan = resolve(model or "embed-default", classification, kind="embed")
    rid = uuid.uuid4().hex[:16]
    meta = {"endpoint": "embeddings", "alias": plan.requested, "stream": False, "json_mode": False, "has_images": False,
            "has_tools": False, "data_classification": plan.classification, "caller": caller}
    attempts: list[dict] = [dict(s, status="skipped") for s in plan.skipped]
    n = 0
    for route in plan.routes:
        blocked = _budget_block(route.provider)
        if blocked:
            attempts.append({"route": route.label, "status": "skipped", "reason": blocked})
            continue
        retries, backoff, timeout = _retry_conf(route.provider)
        for k in range(retries + 1):
            n += 1
            t0 = time.monotonic()
            try:
                res = await _embed_batched(route, texts, timeout)
                ms = int((time.monotonic() - t0) * 1000)
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n, status="ok",
                                 latency_ms=ms, prompt_tokens=(res.get("usage") or {}).get("prompt_tokens"), **meta)
                attempts.append({"route": route.label, "status": "ok", "ms": ms})
                return res, {"request_id": rid, "provider": route.provider, "model": route.model, "latency_ms": ms,
                             "attempts": attempts}
            except (ProviderError, asyncio.TimeoutError) as e:
                err = e if isinstance(e, ProviderError) else ProviderError(f"{route.provider}: timeout", status=504)
                ms = int((time.monotonic() - t0) * 1000)
                telemetry.record(request_id=rid, provider=route.provider, model=route.model, attempt=n,
                                 status="skipped" if isinstance(err, ProviderUnavailable) else "error",
                                 error=str(err)[:500], latency_ms=ms, **meta)
                attempts.append({"route": route.label, "status": "error", "reason": str(err)[:300], "ms": ms})
                if not err.retryable or k == retries:
                    break
                await asyncio.sleep(backoff * (2 ** k))
    raise RouteError(_fail_msg(plan, attempts), _fail_status(attempts), attempts)


def model_list() -> list[dict[str, Any]]:
    cfg = CONFIG.get()
    out = []
    for name, a in (cfg.get("aliases") or {}).items():
        out.append({"id": name, "object": "model", "created": 0, "owned_by": "bsp-harness",
                    "name": a.get("display_name") or ("DataPlatform · " + name.replace("-default", "").replace("-", " ").title()),
                    "kind": a.get("kind", "chat"),
                    "alias": True, "routes": a.get("routes"), "description": a.get("description", "")})
    for pname, p in (cfg.get("providers") or {}).items():
        if p.get("enabled", True) is False:
            continue
        for m in p.get("models") or []:
            out.append({"id": f"{pname}/{m['id']}", "object": "model", "created": 0, "owned_by": pname,
                        "name": f"{pname} · {m['id']}", "kind": m.get("kind", "chat"),
                        "alias": False, "local": bool(p.get("local"))})
    return out
