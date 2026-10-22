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
# # 09 · Deployment patterns: single · blue/green · canary · shadow
#
# The **model gateway** (`/serve/`) serves `bsp_churn` from the registry. Routing is changed through
# its admin API (the control tower's *Registry & Deployments* page uses the same calls). Every scored
# instance — served and shadow — lands in `mlops.inference_log`; the counts below come from there.

# %%
import sys, time
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, deploy, simulator, registry, db
nb.setup();
al = registry.aliases(); al

# %%
def counts(since_id):
    return db.query_df("""select model_version, colour, shadow, count(*) n from mlops.inference_log
                          where id > %s group by 1,2,3 order by 1,2,3""", (since_id,))
def last_id():
    return int(db.fetchone("select coalesce(max(id),0) m from mlops.inference_log")["m"])

# %% [markdown]
# ## Single + an id-only request (features from the online store)

# %%
deploy.set_mode("single", shadow_enabled=False, actor="notebook-09")
BAN = db.fetchone("select ban from features.online_features order by ban limit 1")["ban"]   # a BAN in the online store
deploy.predict(BAN)

# %% [markdown]
# ## Blue / green — instant switch, instant rollback

# %%
deploy.set_mode("blue_green", blue_version=al["champion"], green_version=al["challenger"], live_color="blue", actor="notebook-09")
m = last_id(); simulator.burst(200, seed=91); deploy.flush(); print("blue live:"); display(counts(m))
deploy.switch(actor="notebook-09")
m = last_id(); simulator.burst(200, seed=92); deploy.flush(); print("after switch → green live:"); display(counts(m))
deploy.rollback(actor="notebook-09")
m = last_id(); simulator.burst(100, seed=93); deploy.flush(); print("after rollback → blue again:"); display(counts(m))

# %% [markdown]
# ## Canary — 25 % of customers (sticky by BAN hash) to the challenger

# %%
deploy.set_mode("canary", canary_weight=0.25, actor="notebook-09")
m = last_id(); simulator.burst(2000, seed=94); deploy.flush()
c = counts(m); c["share"] = c.n / c.n.sum(); c

# %%
# stickiness: the same customer always lands on the same side
acct = BAN
sides = {deploy.predict(acct)["colour"] for _ in range(5)}
deploy.flush()
print(acct, "always routed to:", sides)

# %% [markdown]
# ## Shadow — champion answers, challenger scored asynchronously and only logged

# %%
deploy.set_mode("shadow", actor="notebook-09")
m = last_id(); simulator.burst(800, seed=95); time.sleep(2); deploy.flush()
counts(m)

# %%
db.query_df("""select count(*) pairs, round(avg(abs(s.score-l.score))::numeric,4) mean_abs_diff, round(corr(s.score,l.score)::numeric,4) corr
               from mlops.inference_log l join mlops.inference_log s on s.request_id=l.request_id and s.shadow and not l.shadow
               where l.id > %s""", (m,))

# %%
deploy.status()["history"][-5:]
