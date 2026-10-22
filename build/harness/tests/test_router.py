import asyncio

import pytest

from bsp_harness import router as rt
from bsp_harness import telemetry
from fakes import DOWN, Fake, install


def run(c):
    return asyncio.get_event_loop().run_until_complete(c) if False else asyncio.run(c)


def test_alias_params_and_json(monkeypatch):
    f = install(monkeypatch, local=Fake("local"), cloud=Fake("cloud", local=False))
    res, info = run(rt.chat({"model": "extract-default", "messages": [{"role": "user", "content": "x"}]}))
    body = f["local"].calls[0][1]
    assert body["response_format"] == {"type": "json_object"} and body["max_tokens"] == 64
    assert info["provider"] == "local" and res["message"]["content"] == "hello"


def test_fallback_after_retries(monkeypatch):
    f = install(monkeypatch, local=Fake("local", fail=DOWN), cloud=Fake("cloud", text="from cloud", local=False))
    res, info = run(rt.chat({"model": "chat-default", "messages": [{"role": "user", "content": "x"}]}))
    assert info["provider"] == "cloud" and len(f["local"].calls) == 2   # 1 try + 1 retry
    assert [a["status"] for a in info["attempts"]] == ["error", "error", "ok"]
    rows = list(telemetry.RING)
    assert [r["status"] for r in rows] == ["error", "error", "ok"] and rows[-1]["cost_usd"] > 0


def test_non_retryable_skips_straight_to_fallback(monkeypatch):
    from bsp_harness.providers.base import ProviderError
    f = install(monkeypatch, local=Fake("local", fail=ProviderError("404", retryable=False)), cloud=Fake("cloud", local=False))
    run(rt.chat({"model": "chat-default", "messages": [{"role": "user", "content": "x"}]}))
    assert len(f["local"].calls) == 1


def test_restricted_is_local_only(monkeypatch):
    install(monkeypatch, local=Fake("local", fail=DOWN), cloud=Fake("cloud", local=False))
    plan = rt.resolve("chat-default", "restricted")
    assert [r.label for r in plan.routes] == ["local/m1"] and "local providers only" in plan.skipped[0]["reason"]
    with pytest.raises(rt.RouteError) as e:
        run(rt.chat({"model": "cloud/gemini-2.5-flash", "messages": [{"role": "user", "content": "x"}]}, classification="restricted"))
    assert e.value.status == 403


def test_budget_cap_skips_provider(monkeypatch):
    f = install(monkeypatch, local=Fake("local"), cloud=Fake("cloud", local=False))
    run(rt.chat({"model": "cloud-first", "messages": [{"role": "user", "content": "x"}]}))   # spends > cap
    res, info = run(rt.chat({"model": "cloud-first", "messages": [{"role": "user", "content": "x"}]}))
    assert info["provider"] == "local" and "budget" in info["attempts"][0]["reason"]


def test_unknown_model_404():
    with pytest.raises(rt.RouteError) as e:
        rt.resolve("nope")
    assert e.value.status == 404


def test_disabled_provider_skipped():
    plan = rt.resolve("anth/c")
    assert plan.routes == [] and plan.skipped[0]["reason"] == "provider disabled"


def test_stream_falls_back_before_first_chunk(monkeypatch):
    install(monkeypatch, local=Fake("local", fail=DOWN), cloud=Fake("cloud", local=False))

    async def go():
        return [c async for c in rt.stream({"model": "chat-default", "messages": [{"role": "user", "content": "x"}]})]
    chunks = run(go())
    assert chunks[0]["route"]["provider"] == "cloud"
    assert "".join(c.get("delta", {}).get("content", "") for c in chunks[1:]) == "hello"


def test_embed_batches(monkeypatch):
    f = install(monkeypatch, local=Fake("local"))
    f["local"].cfg = {}
    monkeypatch.setattr(f["local"], "type", "ollama")
    res, info = run(rt.embed("embed-default", [f"t{i}" for i in range(150)]))
    assert len(res["vectors"]) == 150 and len(f["local"].calls) == 3   # 64 + 64 + 22
