# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # 01 · LLM gateway basics
#
# The AI harness exposes an **OpenAI-compatible** API at `http://harness:8096/ai/v1`
# (env `LLM_GATEWAY_URL`). You ask for a **model alias** — the harness routes it to Ollama,
# Vertex AI or Anthropic per `config/ai/providers.yaml`, with fallbacks, and logs every call
# to `ai.llm_calls`. Any OpenAI SDK works.

# %%
import json
import os

from openai import OpenAI

BASE = os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1")
client = OpenAI(base_url=BASE, api_key=os.environ.get("LLM_GATEWAY_TOKEN") or "not-needed")
for m in client.models.list().data:
    print(f"{m.id:40s}", getattr(m, "kind", ""), getattr(m, "name", ""))

# %% [markdown]
# ## Chat with an alias
# `resp.model` tells you which route actually answered (e.g. `ollama/qwen2.5:7b-instruct`).

# %%
resp = client.chat.completions.create(
    model="chat-default",
    messages=[{"role": "system", "content": "You are a concise analyst at a US internet provider."},
              {"role": "user", "content": "In two sentences: why do fiber (FTTH) customers churn less than copper DSL customers?"}],
)
print(resp.model, resp.usage)
print(resp.choices[0].message.content)

# %% [markdown]
# ## JSON mode — structured extraction
# `response_format={"type": "json_object"}` works on every provider (Ollama `format=json`,
# Gemini `response_mime_type`, Claude via instruction). `extract-default` turns it on by default.

# %%
email = """Subject: Wrong charge
My BAN is 3100034518. Invoice 3100034518-2603 has a $40.81 equipment fee — I returned the modem."""
resp = client.chat.completions.create(
    model="extract-default",
    response_format={"type": "json_object"},
    temperature=0,
    messages=[{"role": "system", "content": "Extract ban (10 digits), invoice_id (<ban>-<yymm>), disputed_amount (number), reason_category. JSON only."},
              {"role": "user", "content": email}],
)
print(json.loads(resp.choices[0].message.content))

# %% [markdown]
# ## Streaming

# %%
stream = client.chat.completions.create(model="chat-default", stream=True,
                                        messages=[{"role": "user", "content": "List three causes of slow Wi-Fi."}])
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)

# %% [markdown]
# ## Embeddings (batched automatically)

# %%
emb = client.embeddings.create(model="embed-default", input=["fiber outage in Raleigh", "late fee dispute", "router reboot"])
print(emb.model, len(emb.data), "vectors of", len(emb.data[0].embedding), "dims")

# %% [markdown]
# ## Data classification policy
# Restricted data is only ever routed to **local** providers (Ollama). Tag the request:

# %%
resp = client.chat.completions.create(
    model="chat-default", extra_headers={"X-Data-Classification": "restricted"},
    messages=[{"role": "user", "content": "Summarise: BAN 3100008373 called about a late fee charged despite autopay."}])
print(resp.model)   # always an ollama/... route

# %% [markdown]
# ## The shared helper used by appliance services
# `bsp_common.llm` wraps the same API and lets code fall back to rules when no gateway runs.

# %%
from bsp_common import llm

if llm.available():
    print(llm.extract_json(email, {"type": "object", "properties": {"ban": {"type": "string"}}}))
