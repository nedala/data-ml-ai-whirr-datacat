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
# # 06 · RAG for front-office automation
#
# The **Knowledge & RAG** service (`rag`, UI at `/rag/`) indexes runbooks, policies and design
# docs into **Qdrant** (or **pgvector** in Postgres), retrieves with **hybrid search**
# (BM25 + dense, reciprocal-rank fusion, rerank, MMR) and answers with **numbered citations** —
# or says *"I don't know"*. This notebook drives it from Python:
#
# 1. list collections and search (see per-stage scores),
# 2. ask a grounded question,
# 3. **agent assist** on a customer e-mail (intent → runbook checklist → drafted reply),
# 4. **runbook copilot** for an outage in a wire center,
# 5. run the golden-set **evals** and compare retrieval configurations,
# 6. call the **gateway** yourself with retrieved context (bring-your-own prompt).
#
# Everything works offline (deterministic embedder + extractive answers); when the LLM gateway is
# up the service uses `chat-default` / `classify-default` / `embed-default` automatically.
# The demo corpus is **synthetic**.

# %%
import json
import os
import sys
import time

import httpx

RAG = os.environ.get("RAG_URL", "http://rag:8101/rag")
API = RAG + "/api"
http = httpx.Client(timeout=300, headers={"X-Auth-User": os.environ.get("USER_HANDLE", "notebook")})

# bsp_rag.tools = the same functions the MCP server registers (needs only httpx)
if os.path.isdir("/opt/rag"):
    sys.path.append("/opt/rag")
try:
    from bsp_rag import tools as rag_tools
except ImportError:
    rag_tools = None
print(json.dumps({k: v for k, v in http.get(API + "/status").json().items() if k in ("ready", "default_backend", "llm_gateway", "embedder_auto")}, indent=2))

# %% [markdown]
# ## 1 · Collections and hybrid search
# Each collection records its backend and embedding model. `frontoffice` (Qdrant) and
# `frontoffice-pg` (pgvector) are seeded with the demo corpus on first start.

# %%
for c in http.get(API + "/collections").json()["collections"]:
    print(f"{c['name']:20s} {c['backend']:9s} {c['embed_model']:40s} docs={c['n_docs']:3d} chunks={c['n_chunks']}")

# %%
res = http.post(API + "/search", json={"query": "ONT red LOS light, customer has no internet", "collection": "frontoffice", "k": 5}).json()
print("rewritten:", res["rewritten"], "| rerank:", res["rerank"], "|", res["timings"])
for r in res["results"]:
    print(f"#{r['rank']} {r['doc_key']:11s} {r['section'][:45]:45s} fused={r['score']:.4f} "
          f"dense#{r.get('dense_rank', '-')} bm25#{r.get('lexical_rank', '-')} rerank={r.get('rerank_score', '-')}")

# %% [markdown]
# ## 2 · Grounded answer with citations (and abstention)

# %%
for q in ["Can an agent reverse a second late fee within 12 months without approval?",
          "What is the dress code for the holiday party?"]:
    a = http.post(API + "/ask", json={"question": q, "collection": "frontoffice"}).json()
    print(f"Q: {q}\nA ({a['mode']}, coverage {a['coverage']}):\n{a['answer']}")
    for c in a["citations"]:
        print(f"   [{c['n']}] {c['title']} › {c['section']}  ({c['doc_key']})")
    print()

# %% [markdown]
# ## 3 · Agent assist on a customer e-mail
# Intent + entities → runbook retrieval → checklist (with citations) → drafted reply for the
# agent to review. Escalation flags (regulator, medical, repeat contact) change the next best action.

# %%
email = """Hello, my bill jumped from $65 to $79 this month on account 3100000032. When I signed up for
Fiber Gig I was promised $65 for 12 months and it has only been 7 months. I want this corrected and
refunded. If this isn't fixed I'll file a complaint with the state PUC.

Regards,
David Chen"""
r = http.post(API + "/frontoffice/agent-assist", json={"text": email, "channel": "email", "collection": "frontoffice"}).json()
print("intent:", r["intent"]["intent"], "via", r["intent"]["method"], "| entities:", r["entities"]["bans"], "| flags:", [f["flag"] for f in r["flags"]])
print("NEXT BEST ACTION:", r["next_best_action"])
for s in r["checklist"]:
    print("  ☐", s["step"][:120], f"[{s['cite']}]")
print("\n--- draft (" + r["draft_mode"] + ") ---\n" + r["draft_reply"])

# %% [markdown]
# ## 4 · Runbook copilot — outage in a wire center

# %%
r = http.post(API + "/frontoffice/runbook", json={"text": "Mass ONT LOS alarms in wire center RLGHNCXA (CLLI), about 6,200 fiber customers offline. What do I do and who do I call?"}).json()
print(r["wire_center"], r["region"], r["severity"])
for s in r["noc_steps"][:8]:
    print("  NOC ☐", s["step"][:110], f"[{s['cite']}]")
for e in r["escalation"]:
    print("  ESC:", e["situation"], "→", e["level_1"], "|", e["level_2"])
print("  LOCAL:", {k: v for k, v in r["local_contacts"].items() if k != "cite"})

# %% [markdown]
# ## 5 · Evals: compare retrieval configurations
# Golden set in `build/rag/evals/golden.yaml` (35 in-scope + 3 out-of-scope questions).
# Results are stored in `rag.eval_runs`; the UI's **Evals** page shows the leaderboard.

# %%
job = http.post(API + "/evals/run", json={"collection": "frontoffice", "presets": ["dense", "lexical", "hybrid", "hybrid+rerank+mmr"]}).json()
while (s := http.get(f"{API}/jobs/{job['id']}").json())["status"] == "running":
    time.sleep(2)
for row in s["results"]:
    print(f"{row['name']:20s} hit@1={row['hit@1']:.3f} hit@5={row['hit@5']:.3f} MRR={row['mrr']:.3f} "
          f"faith={row.get('faithfulness')} cite={row.get('citation_correctness')} abstain={row.get('abstain_accuracy')}")

# %% [markdown]
# ## 6 · Bring your own prompt: retrieved context → gateway
# Use the service for retrieval only and write your own prompt against the gateway alias.

# %%
from openai import OpenAI

hits = http.post(API + "/search", json={"query": "payment arrangement eligibility past due", "k": 4}).json()["results"]
context = "\n\n".join(f"[{i}] {h['title']} › {h['section']}\n{h['text']}" for i, h in enumerate(hits, 1))
client = OpenAI(base_url=os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1"), api_key=os.environ.get("LLM_GATEWAY_TOKEN") or "not-needed")
try:
    resp = client.chat.completions.create(model="chat-default", temperature=0.1, messages=[
        {"role": "system", "content": "Answer only from the context and cite passages like [1]."},
        {"role": "user", "content": f"Context:\n{context}\n\nA customer owes $420 and lost their job. What can we offer?"}])
    print(resp.model, "\n", resp.choices[0].message.content)
except Exception as e:  # gateway down → the service's offline answer still works
    print("gateway unavailable:", e)

# %% [markdown]
# ## 7 · The same tools agents use (MCP / Open WebUI)
# `bsp_rag.tools` is what the MCP server registers (`rag_search`, `rag_answer`,
# `rag_list_collections`, `rag_agent_assist`, `rag_runbook_copilot`) and `/rag/tools` is the
# OpenAPI tool server Open WebUI uses.

# %%
if rag_tools:
    print(json.dumps(rag_tools.rag_answer("What is the unreturned equipment fee for a leased router?"), indent=2)[:800])
else:
    print(http.post(RAG + "/tools/answer", json={"question": "What is the unreturned equipment fee for a leased router?"}).json())
