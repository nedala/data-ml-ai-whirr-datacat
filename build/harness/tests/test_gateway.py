import base64
import json
import struct

from fastapi.testclient import TestClient

from bsp_harness.app import create_app
from fakes import DOWN, Fake, install


def client():
    return TestClient(create_app())


def test_models_and_health():
    c = client()
    assert c.get("/ai/healthz").json()["status"] == "ok"
    ids = {m["id"]: m for m in c.get("/ai/v1/models").json()["data"]}
    assert ids["chat-default"]["alias"] and ids["chat-default"]["name"].startswith("DataPlatform")
    assert "local/m1" in ids and "anth/c" not in ids
    assert c.get("/v1/models").status_code == 200


def test_chat_completion_and_fallback(monkeypatch):
    install(monkeypatch, local=Fake("local", fail=DOWN), cloud=Fake("cloud", text="hi there", local=False))
    r = client().post("/ai/v1/chat/completions", json={"model": "chat-default", "messages": [{"role": "user", "content": "x"}]})
    d = r.json()
    assert r.status_code == 200 and d["object"] == "chat.completion" and d["model"] == "cloud/gemini-2.5-flash"
    assert d["choices"][0]["message"]["content"] == "hi there" and d["usage"]["total_tokens"] == 5
    assert r.headers["x-route"] == "cloud/gemini-2.5-flash"


def test_streaming_sse(monkeypatch):
    install(monkeypatch, local=Fake("local"), cloud=Fake("cloud"))
    with client().stream("POST", "/ai/v1/chat/completions", json={"model": "chat-default", "stream": True,
                         "stream_options": {"include_usage": True}, "messages": [{"role": "user", "content": "x"}]}) as r:
        lines = [l for l in r.iter_lines() if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[6:]) for l in lines[:-1]]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"]) == "hello"
    assert chunks[-1]["usage"]["total_tokens"] == 5


def test_policy_error_is_http_403(monkeypatch):
    install(monkeypatch, local=Fake("local"), cloud=Fake("cloud"))
    r = client().post("/ai/v1/chat/completions", headers={"X-Data-Classification": "restricted"},
                      json={"model": "cloud/gemini-2.5-flash", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 403 and "local providers only" in r.json()["error"]["message"]


def test_bearer_token(monkeypatch):
    install(monkeypatch, local=Fake("local"), cloud=Fake("cloud"))
    monkeypatch.setenv("LLM_GATEWAY_TOKEN", "s3cret")
    c = client()
    assert c.get("/ai/v1/models").status_code == 401
    assert c.get("/ai/v1/models", headers={"Authorization": "Bearer s3cret"}).status_code == 200


def test_embeddings_batch_and_base64(monkeypatch):
    install(monkeypatch, local=Fake("local"))
    c = client()
    d = c.post("/ai/v1/embeddings", json={"model": "embed-default", "input": ["ab", "abc"]}).json()
    assert [x["embedding"] for x in d["data"]] == [[2.0, 1.0], [3.0, 1.0]]
    d = c.post("/ai/v1/embeddings", json={"input": "abcd", "encoding_format": "base64"}).json()
    assert list(struct.unpack("<2f", base64.b64decode(d["data"][0]["embedding"]))) == [4.0, 1.0]
    assert c.post("/ai/v1/embeddings", json={"input": [1, 2]}).status_code == 400


def test_bsp_common_llm_client_contract(monkeypatch):
    """bsp_common.llm (the client disputes/ccai use) against the gateway app in-process."""
    import httpx
    from bsp_common import llm
    install(monkeypatch, local=Fake("local", text='Sure: {"ban": "3100000016"}'), cloud=Fake("cloud"))
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    real_post, real_get = httpx.post, httpx.get

    def via_app(method):
        def call(url, **kw):
            kw.pop("timeout", None)
            async def go():
                async with httpx.AsyncClient(transport=transport, base_url="http://harness:8096") as c:
                    return await c.request(method, url, **kw)
            import asyncio
            return asyncio.run(go())
        return call
    monkeypatch.setattr(httpx, "post", via_app("POST"))
    monkeypatch.setattr(httpx, "get", via_app("GET"))
    monkeypatch.setenv("LLM_GATEWAY_URL", "http://harness:8096/ai/v1")
    assert llm.available()
    assert llm.extract_json("x", {"type": "object"}) == {"ban": "3100000016"}
    assert llm.embed(["abc"], model="embed-default") == [[3.0, 1.0]]
    monkeypatch.setattr(httpx, "post", real_post); monkeypatch.setattr(httpx, "get", real_get)
