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
# # 02 · Feature sifting & selection
#
# A staged funnel over **27 candidates** (core snapshot features + integrated / engineered ones,
# including deliberately redundant columns, a near-constant column and a seeded **random probe**):
#
# 1. missingness filter · 2. variance filter · 3. correlation + VIF pruning ·
# 4. mutual information · 5. permutation importance · 6. SHAP · 7. stability selection (L1 on bootstrap halves)
# + the probe rule: anything indistinguishable from random noise is dropped.
#
# Runs on the **training window only** (never the hold-out). The report is logged to MLflow.

# %%
import sys, tempfile
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, data, selection, pipeline, config
nb.setup();

# %%
w = data.windows()
df = data.load(months=w["train"], labelled_only=True)
print(f"training window {w['train'][0]:%Y-%m} → {w['train'][1]:%Y-%m}: {len(df):,} rows")
pd_desc = {k: v for k, v in data.DERIVED.items()}
pd_desc

# %%
rep = selection.sift(df, data.CANDIDATES, sample=20000)
rep.summary().round(4)

# %% [markdown]
# ### Why each feature was dropped

# %%
for f, why in rep.dropped.items():
    print(f"{f:<28} {why}")

# %%
import matplotlib.pyplot as plt
fig, ax = plt.subplots(1, 3, figsize=(16, 4.5))
rep.tables["mutual_info"].head(15).iloc[::-1].plot.barh(ax=ax[0], legend=False, title="mutual information")
rep.tables["permutation"].perm_auc_drop.head(15).iloc[::-1].plot.barh(ax=ax[1], title="permutation AUC drop")
if "shap" in rep.tables:
    rep.tables["shap"].head(15).iloc[::-1].plot.barh(ax=ax[2], legend=False, title="mean |SHAP|")
plt.tight_layout()

# %%
rep.tables["stability"].round(2).T

# %% [markdown]
# ### Log the report to MLflow (experiment `churn-feature-selection`)

# %%
mlflow = pipeline.mlflow_setup(config.SELECTION_EXPERIMENT)
with mlflow.start_run(run_name="feature-sifting (notebook)", tags={"stage": "selection"}) as run:
    with tempfile.TemporaryDirectory() as td:
        rep.write(td)
        mlflow.log_artifacts(td, artifact_path="selection")
    mlflow.log_params({"n_candidates": len(rep.candidates), "n_kept": len(rep.kept)})
    mlflow.log_dict({"kept": rep.kept, "dropped": rep.dropped}, "selection/decision.json")
print("kept:", rep.kept)
print("MLflow run:", run.info.run_id)
