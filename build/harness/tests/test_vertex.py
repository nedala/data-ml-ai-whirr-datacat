"""Vertex (Gemini) adapter against a mocked google-genai client — no credentials needed."""
import asyncio
import base64
import json
from types import SimpleNamespace as NS

import pytest

from bsp_harness import router as rt
from bsp_harness.providers import vertex

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake").decode()


class FakeModels:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    async def generate_content(self, model, contents, config):
        self.calls.append(("generate", model, contents, config))
        return self.responses.pop(0)

    async def generate_content_stream(self, model, contents, config):
        self.calls.append(("stream", model, contents, config))
        chunks = self.responses.pop(0)

        async def it():
            for c in chunks:
                yield c
        return it()

    async def embed_content(self, model, contents):
        self.calls.append(("embed", model, contents))
        return NS(embeddings=[NS(values=[0.1, 0.2, 0.3], statistics=NS(token_count=4)) for _ in contents])


def resp(parts, finish="STOP", pt=11, ct=7):
    return NS(candidates=[NS(content=NS(parts=parts), finish_reason=finish)],
              usage_metadata=NS(prompt_token_count=pt, candidates_token_count=ct))


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def factory(project, location, creds):
        holder["args"] = (project, location)
        return NS(aio=NS(models=holder["models"]))
    monkeypatch.setattr(vertex, "_client_factory", factory)
    return holder


@pytest.fixture(autouse=True)
def _no_studio_vertex_project(monkeypatch):
    monkeypatch.delenv("GCP_VERTEX_PROJECT", raising=False)   # Studio-injected project would override the test config


def test_mapping_system_images_tools_json(fake):
    fake["models"] = FakeModels([resp([NS(text='{"ok": true}', thought=False, function_call=None)])])
    p = vertex.VertexProvider("vertex", {"project": "proj", "region": "us-central1"})
    req = {"messages": [
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": [{"type": "text", "text": "What is on this bill?"},
                                     {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{PNG}"}}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                "function": {"name": "lookup", "arguments": '{"ban": "3100000016"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"balance": 79.0}'},
        {"role": "user", "content": "Answer in JSON"}],
        "tools": [{"type": "function", "function": {"name": "lookup", "description": "acct lookup", "parameters": {
            "type": "object", "additionalProperties": False, "title": "X",
            "properties": {"acct": {"type": "string", "title": "Acct"}, "n": {"anyOf": [{"type": "integer"}, {"type": "null"}]}}}}}],
        "response_format": {"type": "json_object"}, "temperature": 0, "max_tokens": 50}
    out = asyncio.run(p.chat("gemini-2.5-flash", req))
    _, model, contents, cfg = fake["models"].calls[0]
    assert fake["args"] == ("proj", "us-central1") and model == "gemini-2.5-flash"
    assert [c.role for c in contents] == ["user", "model", "user"]       # tool result + next user turn merged
    assert contents[0].parts[1].inline_data.mime_type == "image/png"
    assert contents[1].parts[0].function_call.name == "lookup" and contents[1].parts[0].function_call.args == {"ban": "3100000016"}
    assert contents[2].parts[0].function_response.name == "lookup"
    assert contents[2].parts[0].function_response.response == {"balance": 79.0}
    assert cfg.response_mime_type == "application/json" and "Be terse." in cfg.system_instruction
    assert cfg.temperature == 0 and cfg.max_output_tokens == 50
    decl = cfg.tools[0].function_declarations[0]
    assert decl.name == "lookup" and decl.parameters.properties["n"].nullable is True
    assert out["message"]["content"] == '{"ok": true}' and out["usage"] == {"prompt_tokens": 11, "completion_tokens": 7}


def test_function_call_response(fake):
    fake["models"] = FakeModels([resp([NS(text=None, thought=False, function_call=NS(name="lookup", args={"acct": "X"}))])])
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r"})
    out = asyncio.run(p.chat("gemini-2.5-pro", {"messages": [{"role": "user", "content": "hi"}]}))
    assert out["finish_reason"] == "tool_calls"
    tc = out["message"]["tool_calls"][0]
    assert tc["function"]["name"] == "lookup" and json.loads(tc["function"]["arguments"]) == {"acct": "X"}


def test_thought_parts_skipped_and_max_tokens(fake):
    fake["models"] = FakeModels([resp([NS(text="thinking…", thought=True, function_call=None),
                                       NS(text="answer", thought=False, function_call=None)], finish="FinishReason.MAX_TOKENS")])
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r", "models": [{"id": "gemini-2.5-flash", "thinking_budget": 0}]})
    out = asyncio.run(p.chat("gemini-2.5-flash", {"messages": [{"role": "user", "content": "hi"}]}))
    assert out["message"]["content"] == "answer" and out["finish_reason"] == "length"
    assert fake["models"].calls[0][3].thinking_config.thinking_budget == 0


def test_stream(fake):
    fake["models"] = FakeModels([[resp([NS(text="Hel", thought=False, function_call=None)], pt=0, ct=0),
                                  resp([NS(text="lo", thought=False, function_call=None)], pt=5, ct=2)]])
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r"})

    async def go():
        return [c async for c in p.stream("gemini-2.5-flash", {"messages": [{"role": "user", "content": "hi"}]})]
    chunks = asyncio.run(go())
    assert "".join(c["delta"].get("content", "") for c in chunks) == "Hello"
    assert chunks[-1]["finish_reason"] == "stop" and chunks[-1]["usage"] == {"prompt_tokens": 5, "completion_tokens": 2}


def test_embed(fake):
    fake["models"] = FakeModels([])
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r"})
    out = asyncio.run(p.embed("text-embedding-005", ["a", "b"]))
    assert out["vectors"] == [[0.1, 0.2, 0.3]] * 2 and out["usage"]["prompt_tokens"] == 8


def test_errors_map_retryable(fake):
    class Boom(Exception):
        code = 429

    class M(FakeModels):
        async def generate_content(self, **kw):
            raise Boom("quota")
    fake["models"] = M([])
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r"})
    with pytest.raises(vertex.ProviderError) as e:
        asyncio.run(p.chat("gemini-2.5-flash", {"messages": [{"role": "user", "content": "hi"}]}))
    assert e.value.retryable and e.value.status == 429


def test_vertex_via_router(fake, monkeypatch):
    fake["models"] = FakeModels([resp([NS(text="from gemini", thought=False, function_call=None)])])
    res, info = asyncio.run(rt.chat({"model": "cloud/gemini-2.5-flash", "messages": [{"role": "user", "content": "hi"}]}))
    assert info["provider"] == "cloud" and res["message"]["content"] == "from gemini"


def test_unconfigured_without_credentials(monkeypatch):
    monkeypatch.setattr(vertex, "_client_factory", None)
    monkeypatch.delenv("GCP_SA_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    monkeypatch.delenv("GCP_VERTEX_JSON", raising=False)   # Studio → Cloud identities
    p = vertex.VertexProvider("vertex", {"project": "p", "region": "r"})
    ok, why = p.configured()
    assert not ok and "GCP_SA_JSON" in why
