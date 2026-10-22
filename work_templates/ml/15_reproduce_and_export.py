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
# # 15 · Reproduce a version, delayed-label evaluation, and the export bundle
#
# **Reproducibility.** Every training run records the sha256 of its exact training rows
# (`bsp.data_snapshot_sha256`), the code (`bsp.code_sha256`, + git commit when available), the
# environment lock (`bsp.env_lock_sha256` + `environment/requirements.lock`) and
# `config/training_config.json` (family, params, features, windows, seed). `bsp_mlops.reproduce`
# refits from exactly that, rebuilds the rows through the same point-in-time join, and compares the data
# hash, every metric and every test-window prediction with the registered model.
#
# **Delayed labels.** Churn is known 90 days after the snapshot. Scores logged at serve time are joined
# to the realised label when it arrives: realised AUC per scoring cohort vs the AUC expected at release.
#
# **Export.** One hashed bundle per version: MLflow model, locks, feature definitions, training config,
# lineage, promotion records, Dockerfile + Cloud Run / Vertex AI specs, pipeline source and
# `reproduce.sh` — everything needed to run, audit or rebuild the model outside this appliance.

# %%
import sys, json
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, reproduce, registry, outcomes, lineage, config
nb.setup();
import pandas as pd
champ = registry.aliases().get("champion")
champ

# %%
rc = reproduce.recipe(champ)
{k: rc[k] for k in ("model_kind", "seed", "recency_boost", "recorded")} | {"windows": {k: f"{a:%Y-%m}..{b:%Y-%m}" for k, (a, b) in rc["windows"].items()}}

# %%
r = reproduce.reproduce(champ)
pd.DataFrame(r["metrics"])

# %%
{k: r[k] for k in ("parity", "data_hash_match", "code_match", "env_match")} | {"max_prediction_diff": r["details"]["max_prediction_diff"]}

# %% [markdown]
# The environment check is a real diff of package locks (here: the recorded lock vs this kernel's).

# %%
r["env_diff"]

# %% [markdown]
# ## Delayed-label evaluation

# %%
le = outcomes.delayed_label_eval(source="notebook-15")
pd.DataFrame(le["overall"])[["role", "model_version", "n", "n_labelled", "n_pending", "realised_auc", "expected_auc", "auc_gap", "lift_at_10", "ece"]]

# %%
pd.DataFrame(le["cohorts"]).query("role == 'served'")[["model_version", "cohort", "label_due", "n", "n_labelled", "n_pending", "realised_auc", "realised_rate"]]

# %% [markdown]
# ## Export bundle (runs in the control tower, which owns `<data root>/mlops`)

# %%
fz = nb.tower("POST", "/api/frozen", json={})
{k: fz[k] for k in ("bundle", "model_version", "sha256", "content_digest", "files", "uri", "verify")}

# %%
import httpx
man = httpx.get(f"{config.MLOPS_URL}/api/frozen/{fz['id']}/manifest").json()
print(json.dumps({k: man.get(k) for k in ("lineage", "release", "exportable")}, indent=1, default=str)[:3000])

# %%
sorted(f for f in man["files"] if not f.startswith(("model/", "pipelines/source/")))
