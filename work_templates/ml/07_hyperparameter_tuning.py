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
# # 07 · Hyper-parameter tuning (Optuna + MLflow nested runs)
#
# TPE search over LightGBM (learning rate, leaves, min child samples, subsampling, regularisation)
# maximising **validation AUC**. Each trial is a nested MLflow run under one parent in experiment
# `churn-tuning`; the best parameters are refit and logged as the parent's model.

# %%
import sys, os
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, pipeline, config
nb.setup();
N_TRIALS = int(os.environ.get("NB_TRIALS", "10"))

# %%
features = pipeline.default_features()
res = pipeline.tune(features, kind="lightgbm", n_trials=N_TRIALS)
print("best params:", res["best_params"])
print(f"best trial valid AUC {res['best_trial_auc']:.4f} → tuned model valid {res['metrics']['valid_auc']:.4f} / test {res['metrics']['test_auc']:.4f}")

# %%
import mlflow, matplotlib.pyplot as plt
trials = mlflow.search_runs(filter_string=f"tags.mlflow.parentRunId = '{res['parent_run_id']}' and tags.stage = 'tuning-trial'",
                            experiment_names=[config.TUNING_EXPERIMENT])
trials = trials.sort_values("start_time")
ax = trials["metrics.valid_auc"].reset_index(drop=True).plot(marker="o", figsize=(8, 3.2), title="validation AUC by trial")
trials["metrics.valid_auc"].cummax().reset_index(drop=True).plot(ax=ax, ls="--")
