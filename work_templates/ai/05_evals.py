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
# # 05 · Evals — compare routes on golden sets
#
# The harness ships three small golden sets (dispute extraction, CCAI intent classification,
# warehouse SQL). Run them via the harness API (results land in `ai.eval_runs` and on the
# *Evals* leaderboard), or write your own YAML in `$DATA_ROOT/harness/evals/`.

# %%
import os
import time

import httpx

API = os.environ.get("LLM_GATEWAY_URL", "http://harness:8096/ai/v1").rsplit("/v1", 1)[0] + "/api"
suites = httpx.get(f"{API}/evals/suites", timeout=30).json()["suites"]
for s in suites:
    print(f"{s['suite']:22s} {s['type']:9s} {s['cases']:3d} cases  default routes: {s['default_routes']}")

# %%
ROUTES = ["extract-default", "ollama/qwen2.5:7b-instruct", "vertex/gemini-2.5-flash"]
job = httpx.post(f"{API}/evals/run", json={"suite": "dispute_extraction", "routes": ROUTES}, timeout=30).json()["job_id"]
while (j := httpx.get(f"{API}/evals/jobs/{job}", timeout=30).json())["status"] == "running":
    print(f"\r{j['done']}/{j['total']}", end="")
    time.sleep(2)
print()
for r in j.get("results", []):
    print(f"{r['route']:32s} score={r['score']:.2f}  p50={r['latency_p50_ms']} ms  errors={r['errors']}")

# %% [markdown]
# ## Leaderboard straight from the warehouse

# %%
from bsp_common import db

try:
    display(db.query_df("""select suite, route, round(score::numeric, 3) as score, latency_p50_ms, ts
                           from ai.eval_runs order by ts desc limit 20"""))
except Exception as e:  # noqa: BLE001
    print("warehouse not reachable:", e)

# %% [markdown]
# ## Your own golden set
# Save as `$DATA_ROOT/harness/evals/my_suite.yaml` (visible at `/data/harness/evals` to the harness):
#
# ```yaml
# suite: outage_summary_classify
# type: classify            # extract | classify | sql
# description: Is a ticket note about an outage?
# default_routes: [classify-default]
# labels: {OUTAGE: area outage, NOT_OUTAGE: anything else}
# system: Answer with exactly one label.
# cases:
#   - {id: o1, input: "Whole street down since 6am, ONT red light", expected: OUTAGE}
# ```
