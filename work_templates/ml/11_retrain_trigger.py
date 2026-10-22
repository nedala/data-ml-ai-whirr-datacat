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
# # 11 · Automatic retrain triggering
#
# The control tower's drift monitor runs on a schedule (and on demand). On a breach it asks the
# **retrain orchestrator** to:
#
# 1. run the training pipeline as a subprocess (`python -m bsp_mlops.pipeline retrain …`) on the most
#    recent labelled data with the champion's recipe (model family, hyper-parameters, features),
# 2. register the result as a new version with alias `challenger`,
# 3. compare champion vs challenger on the **same newest labelled month** (the AUC margin is recorded), and
# 4. with auto-submit on, hand the new version to the **release pipeline** (notebook 14): automated gates →
#    `staging` + `challenger` in **shadow** → a production request that a second person with an approver
#    role must approve. A retrain never moves `champion` by itself; a drift breach also raises an alert.

# %%
import sys, time
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, deploy, simulator, registry
nb.setup();
import pandas as pd
print("aliases:", registry.aliases())
nb.tower("GET", "/api/settings")

# %% [markdown]
# ## Drift → retrain, end to end through the control tower

# %%
deploy.set_mode("single", shadow_enabled=False, actor="notebook-11")
simulator.burst(1500, shift=True, seed=111); deploy.flush()
rep = nb.tower("POST", "/api/drift/check", json={"minutes": 15, "last_n": 1500, "retrain_on_breach": True})
print("breached:", rep["breached"], rep.get("breach_reasons"))
print("retrain:", rep.get("retrain"))

# %%
rid = (rep.get("retrain") or {}).get("retrain_id")
if not rid:   # cooldown / already running → manual trigger shows the same machinery
    r = nb.tower("POST", "/api/retrains", json={"reason": "notebook 11: manual retrain"})
    rid = r.get("retrain_id") or r
print("retrain id:", rid)
while True:
    r = nb.tower("GET", f"/api/retrains/{rid}")
    if r["status"] in ("succeeded", "failed"):
        break
    time.sleep(5)
{k: r[k] for k in ("id", "trigger", "status", "model_version", "champion_version", "challenger_auc", "champion_auc", "margin", "promoted", "shadow_started", "decision")}

# %%
print(nb.tower("GET", f"/api/retrains/{rid}/log")[-2500:])

# %%
pd.DataFrame(nb.tower("GET", "/api/retrains")["runs"])[["id", "created_at", "trigger", "status", "model_version", "promoted", "decision"]].head(10)

# %%
print("aliases now:", registry.aliases(), "· gateway mode:", deploy.status()["mode"])
