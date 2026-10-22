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
# # 03 · Train / validation / test hold-outs: random vs stratified vs time vs group
#
# The churn data is a **panel**: each customer appears once per month. A random split puts the same
# customer in train *and* test, and lets the model learn from the future. We train the same LightGBM
# under four strategies and score each twice: on its **own test split**, and on a truly **future,
# untouched window**. The difference ("optimism") shows which hold-out flatters the model.

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, data, splits, featurestore, pipeline, config
nb.setup();

# %%
w = data.windows()
df = data.load(labelled_only=True)
pre = df[(df.snapshot_month >= w["train"][0]) & (df.snapshot_month <= w["valid"][1])]
future = df[(df.snapshot_month >= w["test"][0]) & (df.snapshot_month <= w["test"][1])]
features = pipeline.default_features()
print(f"development {len(pre):,} rows · future {len(future):,} rows ({w['test'][0]:%Y-%m} → {w['test'][1]:%Y-%m}) · {len(features)} features")

# %%
for s in splits.STRATEGIES:
    print(s, splits.describe(splits.split(pre, s)))

# %%
cmp = splits.compare(pre, features, future)
cmp[["n_train", "n_test", "churn_train", "churn_test", "accounts_in_train_and_test", "test_months_seen_in_train",
     "test_auc", "future_auc", "optimism"]].round(4)

# %%
import matplotlib.pyplot as plt
ax = cmp[["test_auc", "future_auc"]].plot.bar(figsize=(8, 3.5), ylim=(cmp.future_auc.min() - 0.03, cmp.test_auc.max() + 0.01),
                                              title="own test split vs truly future window")
ax.set_xlabel("")

# %% [markdown]
# **Reading it:** random / stratified splits share thousands of customers between train and test and
# see the test months during training → their test AUC is the most optimistic. The **time-based**
# split mirrors production (train on the past, score the future) and is the default for every model
# in this appliance; the group split removes customer leakage but still mixes time periods.

# %%
mlflow = pipeline.mlflow_setup(config.SELECTION_EXPERIMENT)
with mlflow.start_run(run_name="holdout-strategies (notebook)", tags={"stage": "splits"}):
    mlflow.log_table(cmp.reset_index(), "splits/holdout_comparison.json")
    for st, r in cmp.iterrows():
        mlflow.log_metrics({f"{st}_test_auc": r.test_auc, f"{st}_future_auc": r.future_auc, f"{st}_optimism": r.optimism})
