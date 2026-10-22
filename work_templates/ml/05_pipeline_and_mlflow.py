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
# # 05 · Transformation pipeline + model, saved together in MLflow
#
# One sklearn `Pipeline`: **ColumnTransformer** (median impute + missing indicators + scaling for
# numerics; constant impute + one-hot for categoricals) → classifier. MLflow receives:
#
# * the **transformation pipeline on its own** (`preprocessor/`) — reusable feature transform,
# * **prep + model as ONE model** (`model/`, sklearn flavour + pyfunc → `predict_proba`) with a
#   **signature** and an **input example**,
# * params, metrics (AUC, PR-AUC, logloss, Brier, lift@10, KS), evaluation plots, gain/calibration
#   tables, SHAP global importance, the drift reference sample and dataset lineage.

# %%
import sys
sys.path.insert(0, "/opt/mlops")
from bsp_mlops import nb, data, pipeline, config
nb.setup();

# %%
features = pipeline.default_features()
pipe = pipeline.build_pipeline("lightgbm", features)
pipe

# %%
res = pipeline.train_and_log("lightgbm", features, experiment=config.EXPERIMENT, run_name="lightgbm (notebook 05)",
                             tags={"stage": "notebook"})
{k: round(v, 4) for k, v in res["metrics"].items() if k.startswith(("valid_auc", "test_"))}

# %% [markdown]
# ## Load it back from MLflow — exactly what the gateway will do

# %%
import mlflow, mlflow.sklearn, numpy as np
from mlflow.models import get_model_info
info = get_model_info(res["model_uri"])
print("signature:", info.signature)
loaded = mlflow.sklearn.load_model(res["model_uri"])
w = data.windows()
te = pipeline.training_set(w["test"], features)
same = np.allclose(loaded.predict_proba(te[features])[:, 1], res["pipeline"].predict_proba(te[features])[:, 1])
print("reloaded model reproduces in-memory scores:", same)

# %%
# the pyfunc flavour answers with probabilities (pyfunc_predict_fn="predict_proba")
pyf = mlflow.pyfunc.load_model(res["model_uri"])
pyf.predict(te[features].head(3))

# %%
prep = mlflow.sklearn.load_model(f"runs:/{res['run_id']}/preprocessor")
prep.transform(te[features].head(3)).shape, list(prep.get_feature_names_out())[:10]

# %%
print(f"Open the run: /mlflow/#/experiments/{mlflow.get_run(res['run_id']).info.experiment_id}/runs/{res['run_id']}")
