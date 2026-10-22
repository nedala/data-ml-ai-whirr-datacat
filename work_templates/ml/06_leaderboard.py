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
# # 06 · Experimentation leaderboard
#
# Five model families on identical, time-based windows: logistic regression (baseline), random forest,
# LightGBM, XGBoost and HistGradientBoosting. **Selected on validation AUC, reported on the untouched
# test window.** All runs land in MLflow experiment `churn-leaderboard`; the control tower's
# *Leaderboard* page reads the same runs (sortable by AUC / PR-AUC / logloss / lift@10).

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, data, pipeline, config
nb.setup();

# %%
features = pipeline.default_features()
lb = pipeline.leaderboard(features)
lb.round(4)

# %%
import matplotlib.pyplot as plt
ax = lb.set_index("model")[["valid_auc", "test_auc"]].plot.bar(figsize=(8, 3.5), ylim=(lb.test_auc.min() - 0.03, lb.valid_auc.max() + 0.01))
ax.set_title("validation vs test AUC"); ax.set_xlabel("")

# %%
# everything MLflow knows, across experiments (what the tower shows)
runs = pipeline.search_leaderboard()
cols = [c for c in ["tags.mlflow.runName", "params.model_kind", "tags.stage", "metrics.valid_auc", "metrics.test_auc",
                    "metrics.test_pr_auc", "metrics.test_logloss", "metrics.test_lift_at_10"] if c in runs]
runs[cols].head(15)
