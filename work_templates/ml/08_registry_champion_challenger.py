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
# # 08 · Model registry: champion / challenger
#
# Registered model **`bsp_churn`** in MLflow. Two aliases drive everything downstream:
#
# * `champion` — answers production traffic in the model gateway
# * `challenger` — the candidate (shadow, canary or the green colour)
#
# Promotion moves `champion` and remembers the previous one (registered-model tag
# `previous_champion`) so rollback is one call. The gateway polls aliases and hot-reloads.
#
# > **This notebook shows the raw registry mechanics.** `registry.promote()` is UNGATED — it is recorded
# > in `mlops.audit_log` as `registry.promote (ungated)` and the Release page flags a champion without an
# > approval record. Production promotions go through the release pipeline: gates → staging → four-eyes
# > approval → champion (notebook 14, control tower → Release & approvals).

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, pipeline, registry, config
nb.setup();
import pandas as pd

# %% [markdown]
# ## Pick candidates from the leaderboard (validation AUC — never test)

# %%
runs = pipeline.search_leaderboard([config.EXPERIMENT, config.TUNING_EXPERIMENT])
runs = runs[runs["tags.stage"].isin(["leaderboard", "tuned", "notebook"]) & runs["metrics.valid_auc"].notna()]
runs = runs.sort_values("metrics.valid_auc", ascending=False)
runs[["run_id", "tags.mlflow.runName", "params.model_kind", "metrics.valid_auc", "metrics.test_auc"]].head(8)

# %%
best = runs.iloc[0]
other = runs[runs["params.model_kind"] != best["params.model_kind"]].iloc[0]
current = registry.aliases()
print("aliases before:", current)

# %%
v_best = registry.register_run(best.run_id, description=f"{best['params.model_kind']} valid AUC {best['metrics.valid_auc']:.4f} (notebook 08)")
v_other = registry.register_run(other.run_id, alias="challenger",
                                description=f"{other['params.model_kind']} valid AUC {other['metrics.valid_auc']:.4f} (notebook 08)")
if "champion" not in current:
    registry.promote(v_best, reason="first champion (notebook 08)")
print("aliases after:", registry.aliases())

# %%
pd.DataFrame(registry.list_versions())[["version", "aliases", "model_kind", "train_window", "metrics", "run_name"]].head(10)

# %% [markdown]
# ## Promote and roll back (the same calls the control tower makes)
# Only run the promotion demo when the challenger is a *new* version we just registered.

# %%
champ_before = registry.aliases()["champion"]
r = registry.promote(v_other, reason="notebook 08: promotion demo")
print("promoted:", r)
print("rollback:", registry.rollback())
print("aliases now:", registry.aliases(), "· champion restored:", registry.aliases()["champion"] == champ_before)
