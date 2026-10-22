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
# # 10 · Telemetry and drift
#
# * **Telemetry**: requests / min, latency p50/p95, score distribution by version and colour — from
#   `mlops.inference_log` (and Prometheus metrics at `/serve/metrics`).
# * **Drift**: live served traffic vs the champion's training reference sample — PSI + KS per feature,
#   prediction PSI, and AUC against realised churn once outcomes are known. The reference is the champion's
#   release baseline (its validation + test windows). Thresholds: PSI > 0.1 warn, > 0.25 breach; a report
#   breaches when ≥ 1 feature breaches (or prediction PSI / AUC drop do) — all editable in the Drift page.

# %%
import sys, time
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, deploy, simulator, drift, db, config
nb.setup();
import pandas as pd, matplotlib.pyplot as plt

# %%
deploy.set_mode("single", shadow_enabled=False, actor="notebook-10")
tel = nb.tower("GET", "/api/telemetry?minutes=60")
pd.DataFrame(tel["split"])

# %%
s = pd.DataFrame(tel["series"]); s["minute"] = pd.to_datetime(s["minute"])
fig, ax = plt.subplots(1, 2, figsize=(14, 3.5))
s[~s.shadow].groupby("minute").n.sum().plot(ax=ax[0], title="served requests / minute")
s[~s.shadow].groupby("minute")[["p50", "p95"]].mean().plot(ax=ax[1], title="latency ms (per instance)")
plt.tight_layout()

# %%
print("\n".join(l for l in __import__("httpx").get(config.GATEWAY_URL + "/metrics").text.splitlines()
                if l.startswith("bsp_gateway_predictions_total{"))[:1500])

# %% [markdown]
# ## Two replays against the current champion's baseline
# *Which* window drifts depends on the champion. A champion released before the shift (as in the
# demo's first run) sees the **shifted** months drift (monthly charge, price-increase share). After a
# drift-triggered retrain is promoted, the new champion's baseline *is* the shifted period — then the
# older "normal" months are the ones that look different. Both are correct: the monitor compares live
# traffic with what the serving model was signed off on.
#
# ### Replay 1 — the months just before the shift

# %%
m = int(db.fetchone("select coalesce(max(id),0) m from mlops.inference_log")["m"])
simulator.burst(1200, shift=False, seed=101); deploy.flush()
since = db.fetchone("select ts from mlops.inference_log where id=%s", (m,))["ts"] if m else None
ok = drift.run_check(since=since, last_n=1200, source="notebook")
print("breached:", ok["breached"], "· max PSI", ok["max_psi"], "· prediction PSI", ok["prediction_psi"])
pd.DataFrame(ok["feature_drift"]).head(8)

# %% [markdown]
# ### Replay 2 — the shifted months (price-increase wave + outage cluster)

# %%
m = int(db.fetchone("select coalesce(max(id),0) m from mlops.inference_log")["m"])
simulator.burst(1200, shift=True, seed=102); deploy.flush()
since = db.fetchone("select ts from mlops.inference_log where id=%s", (m,))["ts"]
bad = drift.run_check(since=since, last_n=1200, source="notebook")
print("breached:", bad["breached"], "·", bad["breach_reasons"])
fd = pd.DataFrame(bad["feature_drift"])
fd.head(10)

# %%
ax = fd.set_index("feature").psi.sort_values().plot.barh(figsize=(8, 5), title="PSI by feature (shifted traffic)")
ax.axvline(config.PSI_BREACH, color="#c0392b", ls="--"); ax.axvline(config.PSI_WARN, color="#ff8205", ls=":")

# %%
pd.DataFrame(drift.recent_reports(10))[["id", "created_at", "source", "n_current", "max_psi", "prediction_psi", "breached"]]
