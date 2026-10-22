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
# # 13 · Artifact freezing → artifactory
#
# An immutable, hashed bundle of everything needed to reproduce or audit the champion:
# **model** (MLflow format) · **training-data snapshot** (parquet) · **feature definitions** (+ registry
# hashes) · **metrics/params** · **environment lock** (pip freeze) · **provenance manifest** (sha256 of
# every file, MLflow run id, registry version, dataset windows) · deterministic **tar.gz** · optional
# **OCI image layout**. Pushed to the local file artifactory (`<data root>/mlops/artifactory`) or to JFrog
# Artifactory when `ARTIFACTORY_URL` + `ARTIFACTORY_TOKEN` are set. The registry version is tagged
# `frozen=true` and the bundle recorded in `mlops.frozen_artifacts`.

# %%
import sys, json
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, registry, config
nb.setup();
import pandas as pd

# %%
# runs in the control tower (it owns <data root>/mlops); the library call is bsp_mlops.freeze.freeze_and_push()
fz = nb.tower("POST", "/api/frozen", json={})
{k: fz[k] for k in ("bundle", "model_version", "sha256", "content_digest", "files", "size_bytes", "repository", "uri", "verify")}

# %%
tags = registry.client().get_model_version(config.MODEL_NAME, fz["model_version"]).tags
{k: v for k, v in tags.items() if k.startswith("frozen")}

# %%
import httpx
man = httpx.get(f"{config.MLOPS_URL}/api/frozen/{fz['id']}/manifest").json()
print(json.dumps({k: man[k] for k in ("schema", "bundle", "model", "datasets", "feature_store", "environment", "content_digest")}, indent=2)[:3000])

# %%
pd.DataFrame([{"file": k, **v} for k, v in man["files"].items()]).sort_values("size", ascending=False).head(15)

# %%
pd.DataFrame(nb.tower("GET", "/api/frozen")["bundles"])[["id", "created_at", "model_version", "bundle_name", "sha256", "repository"]]
