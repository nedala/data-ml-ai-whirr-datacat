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
# # 14 · Release gates, four-eyes approval, rollback — and outcomes with statistics
#
# A registered version is **not** a released one. `bsp_mlops.release` moves versions
# **dev → staging → production** with MLflow aliases (`staging`, `champion`) and records every move in
# `mlops.promotions` (+ an MLflow run in experiment `churn-release`):
#
# | gate | what it proves |
# |---|---|
# | artifact integrity | the model files still hash to the sha256 pinned at registration |
# | lineage | data-snapshot, code and environment-lock hashes are on the training run |
# | leakage guard | no label-derived column; every feature is a governed feature-store feature |
# | schema & data contract | signature = feature list = feature-store dtypes; live payloads: columns, technology-aware nulls, domains, ranges |
# | AUC floor | hold-out AUC ≥ floor |
# | non-regression | paired bootstrap CI + DeLong of ΔAUC vs the champion on the **same** hold-out rows; CI lower bound ≥ −margin |
# | calibration | ECE ≤ bound, Brier not worse than the champion by more than a bound |
# | segment parity | copper vs fiber AUC gap ≤ bound; no segment regresses (states: advisory) |
# | latency smoke | p95 single-row latency of this version inside the model gateway ≤ bound |
#
# Production then needs **a person with an approver role who is not the requester** (four-eyes). In the
# control tower that person is the signed-in Studio user; from a notebook or the CLI the identities are
# *declared* and recorded with source `notebook` / `cli` — the record says so.

# %%
import sys, json
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, release, registry, outcomes, stats, config
nb.setup();
import pandas as pd
print("aliases:", registry.aliases())
pd.DataFrame(release.THRESHOLDS.items(), columns=["threshold", "value"])

# %% [markdown]
# ## The statistics behind "not worse than the champion"
# Paired comparison: both models score the SAME hold-out rows, so the noise of the sample cancels.
# DeLong gives an analytic standard error of ΔAUC; the stratified paired bootstrap gives a CI without
# distributional assumptions. They should agree.

# %%
import numpy as np
rng = np.random.default_rng(0)
y = (rng.random(4000) < 0.08).astype(int)
a, b = 0.30 * y + rng.random(4000), 0.25 * y + rng.random(4000)
print(json.dumps(stats.delong(y, a, b), indent=1))
print(json.dumps(stats.paired_bootstrap_auc(y, a, b, n_boot=500), indent=1))

# %% [markdown]
# ## Run the gates on a version (no promotion — just the evidence)

# %%
versions = pd.DataFrame(release.status()["versions"])
versions[["version", "aliases", "stage", "gates", "model_kind", "test_auc", "approved_by"]].head(12)

# %%
cand = next((v["version"] for v in release.status()["versions"] if not v["aliases"] and not v["negative_control"]), None)
res = release.evaluate_gates(cand, verbose=True) if cand else None
pd.DataFrame(res["gates"])[["name", "status", "value", "threshold", "detail"]] if res else "no candidate version"

# %% [markdown]
# ## Submit → staging → production request
# `submit` runs the gates; on pass the version becomes `staging` **and** `challenger` (so it is
# shadow-scored on live traffic at zero customer risk) and a production request waits for approval.

# %%
REQUESTER = config.user()          # you
RUN_SUBMIT = False                 # True = really submit: moves `staging` + `challenger` and supersedes any pending request
sub = release.submit(cand, REQUESTER, "modeler", "notebook", note="notebook 14") if (cand and RUN_SUBMIT) else None
{k: (sub[k]["id"] if isinstance(sub[k], dict) else sub[k]) for k in ("promotion", "staged", "pending")} if sub else "dry run — set RUN_SUBMIT = True"

# %% [markdown]
# ## Four-eyes: the requester cannot approve their own request

# %%
pid = (sub and sub["pending"] and sub["pending"]["id"]) or next((p["id"] for p in release.pending()), None)
if pid:
    try:
        release.check_approver(release.get(pid), release.get(pid)["requested_by"], "admin", "cli")
    except release.ReleaseError as e:
        print("refused:", e)

# %% [markdown]
# Approve in the control tower (**Release & approvals**) as another signed-in person with the approver
# role — or, as an operator, declare the approver explicitly (recorded as source `cli`):
#
# ```python
# release.approve(pid, "release.manager", "admin", "cli", note="reviewed gate evidence")
# release.rollback("release.manager", "admin", "cli", reason="incident drill")   # back to the previous APPROVED version
# ```

# %%
pd.DataFrame(release.history(15))[["id", "model_version", "action", "to_stage", "status", "requested_by", "approved_by", "approved_source", "gates_passed"]]

# %% [markdown]
# ## Champion vs challenger on live traffic, and a business A/B
# * **Shadow** — both models scored every request → paired bootstrap CI + DeLong once the 90-day labels arrive.
# * **Retention offer A/B** — the champion's top-risk customers, hash-randomised into offer vs control;
#   two-proportion z-test, CI, power. Customers, scores and labels are real warehouse rows; the offer's
#   **effect is simulated** (`effect`), and the readout says so.

# %%
sh = outcomes.champion_vs_challenger(write=False)
{k: sh.get(k) for k in ("champion_version", "challenger_version", "n_pairs", "n_labelled", "delta_auc", "ci_low", "ci_high", "delong_p", "verdict")}

# %%
ab = outcomes.retention_offer_ab(effect=0.25, write=False)
{k: ab.get(k) for k in ("month", "eligible", "control", "treatment", "abs_uplift", "ci_low", "ci_high", "p_value", "balance_check", "n_per_arm_for_80pct_power", "note")}
