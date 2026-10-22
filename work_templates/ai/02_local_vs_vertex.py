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
# # 02 · Local (Ollama) vs Vertex AI — text and vision
#
# Same prompts against concrete routes: `ollama/...` runs inside the appliance (or on your host),
# `vertex/...` calls Gemini in project `GCP_PROJECT_ID` (needs a Vertex AI identity —
# Studio → Cloud identities; `GCP_SA_JSON` is the legacy fallback).
# Routes that aren't configured are reported, not fatal.

# %%
import base64
import io
import os
import time

from openai import OpenAI

client = OpenAI(base_url=os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1"),
                api_key=os.environ.get("LLM_GATEWAY_TOKEN") or "not-needed")
ROUTES = ["ollama/qwen2.5:7b-instruct", "vertex/gemini-2.5-flash"]
VLM_ROUTES = ["ollama/qwen2.5vl:7b", "vertex/gemini-2.5-flash"]


def ask(route, messages, **kw):
    t0 = time.time()
    try:
        r = client.chat.completions.create(model=route, messages=messages, **kw)
        return {"route": route, "served_by": r.model, "seconds": round(time.time() - t0, 2),
                "tokens": r.usage.total_tokens if r.usage else None, "answer": r.choices[0].message.content}
    except Exception as e:  # noqa: BLE001
        return {"route": route, "error": str(e)[:200]}


# %%
q = [{"role": "user", "content": "Write a SQL query (Postgres) counting cancelled subscriptions per cancel_reason in raw.subscriptions."}]
for row in (ask(r, q, temperature=0) for r in ROUTES):
    print(row, "\n")

# %% [markdown]
# ## Vision: read a bill image
# We draw a small synthetic bill with Pillow, send it as an `image_url` data URI (OpenAI format).

# %%
from PIL import Image, ImageDraw

img = Image.new("RGB", (640, 360), "white")
d = ImageDraw.Draw(img)
lines = ["DATAPLATFORM", "BAN: 3100000016    Invoice: 3100000016-2609", "Bill date: 2026-09-01",
         "Fiber 500 ............................ $69.00", "Taxes & fees ........................... $4.90",
         "Late fee .............................. $10.00", "TOTAL DUE ............................ $83.90"]
for i, line in enumerate(lines):
    d.text((24, 24 + i * 42), line, fill="black")
buf = io.BytesIO()
img.save(buf, format="PNG")
data_uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
img

# %%
vq = [{"role": "user", "content": [
    {"type": "text", "text": "Read this bill. Return JSON: ban, invoice_id, line_items [{name, amount}], total_due."},
    {"type": "image_url", "image_url": {"url": data_uri}}]}]
for row in (ask(r, vq, temperature=0, response_format={"type": "json_object"}) for r in VLM_ROUTES):
    print(row, "\n")

# %% [markdown]
# The `vlm-default` alias tries `ollama/qwen2.5vl:7b` first and falls back to Gemini — check
# *AI Harness → Telemetry* to see latency and tokens per route.
