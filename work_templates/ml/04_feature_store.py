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
# # 04 · Feature store: definitions → offline store → point-in-time training sets → online store
#
# * **Definitions**: `feature_repo/features.yaml` — four versioned feature views (customer profile,
#   billing behaviour, network experience, sentiment) with TTLs and owners.
# * **Offline store**: Postgres `features.fv_<view>_v<version>` (+ `features.feature_registry`).
# * **Point-in-time join**: each (account, timestamp) gets the latest feature row *at or before* that
#   timestamp, within the view's TTL — no leakage from the future.
# * **Online store**: `features.online_features` — what the model gateway reads when a request carries
#   only a `ban` (billing account number).
# * **Feast**: the same definitions are mirrored into a Feast repo (file offline store, sqlite online store).

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, data, featurestore, config
nb.setup();
import pandas as pd

# %%
defs = featurestore.load_definitions()
pd.DataFrame([{"view": f"{v['name']}_v{v['version']}", "ttl_days": v["ttl_days"], "features": ", ".join(f["name"] for f in v["features"]),
               "description": v["description"]} for v in defs["feature_views"]])

# %%
featurestore.apply()

# %%
pd.DataFrame(featurestore.freshness())[["feature_view", "version", "def_hash", "rows", "first_event", "last_event", "offline_age_min"]]

# %% [markdown]
# ## Point-in-time correctness
# Ask for features of a customer *as of* three dates. Between snapshots the join carries the latest
# known value forward; it never reaches into a later month.

# %%
base = data.load(columns=["monthly_charge"])
acct = base.groupby("ban").size().idxmax()       # a customer present in every month
ent = pd.DataFrame({"ban": [acct] * 3, "snapshot_month": ["2025-03-01", "2025-03-20", "2025-04-01"]})
featurestore.get_historical_features(ent, ["monthly_charge", "recent_price_increase", "outage_minutes", "nps_last"])

# %%
# a whole training set for one window
w = data.windows()
ts = featurestore.get_historical_features(
    data.load(months=w["valid"], labelled_only=True)[["ban", "snapshot_month", config.LABEL]],
    featurestore.all_features())
print(ts.shape); ts.head()

# %% [markdown]
# ## Online store + gateway lookup

# %%
featurestore.materialize_online()
featurestore.get_online_features([acct])

# %% [markdown]
# ## Feast mirror (optional)

# %%
res = featurestore.feast_sync()
res

# %%
if res.get("status") == "applied":
    print(featurestore.feast_online([acct], ["monthly_charge", "outage_minutes", "nps_last"]))
