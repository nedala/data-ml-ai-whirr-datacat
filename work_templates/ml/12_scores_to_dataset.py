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
# # 12 · Inference scoring → dataset shaping
#
# The champion batch-scores every customer snapshot of the last six months and writes the result back
# to the warehouse: **`mlops.scored_customers`** (ban, snapshot_month, score, decile, risk band,
# model version, top-3 reason codes). **`mlops.v_score_outcomes`** joins those scores with realised
# churn once the 90-day window closes — the table Superset / Power BI and the performance monitor use.

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, scoring, db
nb.setup();
import matplotlib.pyplot as plt

# %%
res = scoring.batch_score(last_k=6)
res

# %%
db.query_df("""select ban, snapshot_month, round(score::numeric,4) score, decile, risk_band, reason_1, reason_2, reason_3
               from mlops.scored_customers where model_version=%s and snapshot_month=(select max(snapshot_month) from mlops.scored_customers)
               order by score desc limit 10""", (res["model_version"],))

# %% [markdown]
# ## Did the ranking hold up? Realised churn by score decile (months with known outcomes)

# %%
mon = scoring.outcome_monitor(res["model_version"])
ax = mon.set_index("decile").realised_churn.plot.bar(figsize=(8, 3.5), color="#0b6e79", title="realised 90-day churn by score decile")
ax.axhline((mon.realised_churn * mon.customers).sum() / mon.customers.sum(), color="#ff8205", ls="--")
mon.round(4)

# %%
db.query_df("""select technology, count(*) customers, round(avg(score)::numeric,4) mean_score,
                      round(avg(churned_next_90d::int)::numeric,4) realised
               from mlops.v_score_outcomes where outcome_known and model_version=%s group by 1 order by 3 desc""", (res["model_version"],))
