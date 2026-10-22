import asyncio
import base64
from types import SimpleNamespace as NS

from bsp_harness.providers import anthropic_ as A


def test_to_anthropic_mapping():
    png = base64.b64encode(b"img").decode()
    system, msgs = A.to_anthropic({"messages": [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}}]},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "f", "arguments": '{"a":1}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "42"},
        {"role": "user", "content": "thanks"}], "response_format": {"type": "json_object"}})
    assert "sys" in system and "JSON" in system
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[0]["content"][0]["source"]["media_type"] == "image/png"
    assert msgs[1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}}
    assert msgs[2]["content"][0]["type"] == "tool_result" and msgs[2]["content"][1]["text"] == "thanks"


def test_chat_parses_tool_use(monkeypatch):
    class Msgs:
        async def create(self, **kw):
            self.kw = kw
            return NS(content=[NS(type="text", text="ok "), NS(type="tool_use", id="x", name="f", input={"q": 1})],
                      stop_reason="tool_use", usage=NS(input_tokens=9, output_tokens=4))
    m = Msgs()
    monkeypatch.setattr(A, "_client_factory", lambda p: NS(messages=m))
    p = A.AnthropicProvider("anthropic", {})
    out = asyncio.run(p.chat("claude-sonnet-4-5", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 10,
                                                   "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]}))
    assert m.kw["tools"][0]["input_schema"] == {"type": "object"} and m.kw["max_tokens"] == 10
    assert out["finish_reason"] == "tool_calls" and out["usage"] == {"prompt_tokens": 9, "completion_tokens": 4}


def test_missing_key_unconfigured(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, why = A.AnthropicProvider("anthropic", {}).configured()
    assert not ok and "ANTHROPIC_API_KEY" in why
