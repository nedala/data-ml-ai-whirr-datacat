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
# # 04 · A small agent: gateway + MCP tools
#
# A ~40-line tool loop: the model (any alias — function calling is passed through to Ollama,
# Gemini or Claude) decides which MCP tools to call; we execute them and feed results back.
# The harness *Playground* does the same with a UI (and a live browser view).

# %%
import json
import os

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from openai import OpenAI

llm = OpenAI(base_url=os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1"),
             api_key=os.environ.get("LLM_GATEWAY_TOKEN") or "not-needed")
MCP_URL = os.environ.get("MCP_URL", "http://mcp:8095/mcp")
HEADERS = {"Authorization": f"Bearer {os.environ.get('MCP_TOKEN', '')}"}
MODEL = "chat-default"
GROUPS = {"warehouse", "atlan"}          # which tool groups the agent may use


async def run_agent(question: str, max_steps: int = 6) -> str:
    async with Client(StreamableHttpTransport(MCP_URL, headers=HEADERS)) as mcp:
        tools = [t for t in await mcp.list_tools() if t.name.split("_")[0] in GROUPS]
        oa_tools = [{"type": "function", "function": {"name": t.name, "description": (t.description or "")[:1000],
                                                      "parameters": t.inputSchema}} for t in tools]
        messages = [{"role": "system", "content": "You answer questions about DataPlatform data. Use tools; cite tables."},
                    {"role": "user", "content": question}]
        for step in range(max_steps):
            r = llm.chat.completions.create(model=MODEL, messages=messages, tools=oa_tools, temperature=0)
            msg = r.choices[0].message
            messages.append(msg.model_dump(exclude_none=True))
            if not msg.tool_calls:
                return msg.content
            for tc in msg.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                print(f"[{step}] {r.model} → {tc.function.name}({args})")
                res = await mcp.call_tool(tc.function.name, args, raise_on_error=False)
                text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": text[:15000]})
        return "(stopped: too many steps)"

# %%
print(await run_agent("Which state has the most customers, and how many subscriptions there are cancelled?"))

# %%
print(await run_agent("Find the Atlan glossary definition of churn and the warehouse table that carries the churn label."))
