"""Fake provider used to exercise routing without network."""
from bsp_harness.providers.base import Provider, ProviderError


class Fake(Provider):
    type = "fake"

    def __init__(self, name, fail=None, text="hello", local=True):
        super().__init__(name, {"local": local})
        self.fail, self.text, self.calls = fail, text, []

    async def chat(self, model, req):
        self.calls.append((model, req))
        if self.fail:
            raise self.fail
        return {"message": {"role": "assistant", "content": self.text}, "finish_reason": "stop",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    async def stream(self, model, req):
        self.calls.append((model, req))
        if self.fail:
            raise self.fail
        yield {"delta": {"role": "assistant", "content": "hel"}, "finish_reason": None, "usage": None}
        yield {"delta": {"content": "lo"}, "finish_reason": "stop", "usage": {"prompt_tokens": 3, "completion_tokens": 2}}

    async def embed(self, model, texts):
        self.calls.append((model, texts))
        return {"vectors": [[float(len(t)), 1.0] for t in texts], "usage": {"prompt_tokens": len(texts)}}


def install(monkeypatch, **fakes):
    from bsp_harness import providers
    monkeypatch.setattr(providers, "get", lambda name: fakes[name])
    return fakes


DOWN = ProviderError("down", retryable=True, status=503)
