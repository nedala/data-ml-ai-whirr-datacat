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
# # 01 · Integrate the churn data and explore it
#
# **Goal:** one leakage-safe, feature-ready table for the DataPlatform 90-day churn model, and a first
# look at what drives churn — and at the distribution shift hiding in the most recent months.
#
# * Source: `marts.mart_churn_features` (dbt) when it is built, else `raw.customer_monthly` (seed).
# * Integration: as-of join with **NPS surveys** (latest survey strictly *before* the snapshot) and
#   **field dispatches** (repair truck rolls in the 90 days *before* the snapshot) → `features.churn_base`.
# * Key: every account table is keyed by **`ban`** (billing account number, 10-digit string) — `config.ENTITY`.
# * DataPlatform sells two technologies: **copper** (DSL) and **fiber** (FTTH). Plant quality is technology-specific:
#   ONT optics for fiber (`ont_rx_power_dbm`, `ont_los_events`), line quality for copper (`dsl_snr_margin_db`,
#   `dsl_retrains`); integration derives one technology-neutral flag, `line_quality_bad`.
# * Label: `churned_next_90d`; only `label_complete` rows are used for training.
#
# Library: `bsp_mlops.data`, `bsp_mlops.eda` (notebooks stay thin).

# %%
import sys
sys.path.insert(0, "/opt/mlops")          # bsp_mlops — mounted from build/mlops
from bsp_mlops import nb, data, eda, config
nb.setup();

# %% [markdown]
# ## 1 · Integrate

# %%
summary = data.integrate()
summary

# %%
w = data.windows()
{k: (f"{v[0]:%Y-%m} → {v[1]:%Y-%m}" if isinstance(v, tuple) else v) for k, v in w.items() if k not in ("all", "labelled", "shift")}

# %% [markdown]
# ## 2 · Profile

# %%
df = data.load()
lab = df[df.label_complete == 1]
print(f"{len(df):,} snapshot rows · {df[config.ENTITY].nunique():,} billing accounts (BAN) · churn rate {lab[config.LABEL].mean():.2%}")
eda.profile(lab, data.CANDIDATES).round(3)

# %% [markdown]
# ## 3 · What drives churn?

# %%
import matplotlib.pyplot as plt
fig, ax = plt.subplots(1, 3, figsize=(15, 3.8))
for a, col in zip(ax, ["technology", "credit_band", "recent_price_increase"]):
    t = eda.churn_by(lab, col)
    t.churn_rate.plot.bar(ax=a, color="#0b6e79"); a.set_title(f"churn rate by {col}"); a.set_xlabel("")
plt.tight_layout()

# %%
eda.churn_by(lab, "outage_minutes", bins=5).round(3)

# %% [markdown]
# ### Plant quality: copper vs fiber
# ONT columns are empty on copper lines and DSL columns are empty on fiber — by design, not a data-quality
# problem. Feature sifting will drop the sparser pair; `line_quality_bad` carries the signal for both.

# %%
print(lab.groupby("technology")[["ont_rx_power_dbm", "dsl_snr_margin_db"]].apply(lambda g: g.isna().mean()).round(3))
lab.groupby(["technology", "line_quality_bad"])[config.LABEL].agg(["count", "mean"]).round(3)

# %% [markdown]
# ## 4 · The shift: a price-increase wave and an outage cluster in the last four months
# The drift monitor will later find exactly this on live traffic.

# %%
monthly = data.summary_table()
fig, ax = plt.subplots(1, 3, figsize=(15, 3.6))
monthly.plot(x=config.TS, y="price_increase_share", ax=ax[0], legend=False, title="share with a recent price increase")
monthly.plot(x=config.TS, y="avg_outage_min", ax=ax[1], legend=False, title="avg outage minutes")
monthly.plot(x=config.TS, y="churn_rate", ax=ax[2], legend=False, title="90-day churn rate (labelled months)")
for a in ax: a.axvline(w["shift"][0], color="#ff8205", ls="--")
plt.tight_layout()

# %%
eda.shift_preview(df, w["shift"][0]).round(3).head(8)
