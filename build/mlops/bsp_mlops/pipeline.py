"""Training pipeline as code.

    training set (feature store, point-in-time join)
      → split (time-based by default; see splits.py for the alternatives)
      → sklearn Pipeline[ ColumnTransformer(impute / one-hot / scale) → model ]
      → evaluation (AUC, PR-AUC, logloss, Brier, lift@10, KS, calibration, gain, SHAP)
      → MLflow: params, metrics, dataset lineage, plots, the transformation
        pipeline on its own AND prep+model as ONE sklearn/pyfunc model with
        signature + input example
      → (optionally) registry

Model families: logistic regression (baseline), random forest, LightGBM,
XGBoost, HistGradientBoosting. Optuna tuning logs every trial as a nested
MLflow run under one parent.

CLI (used by the retrain orchestrator as a subprocess):
    python -m bsp_mlops.pipeline retrain --retrain-id 7 --reason "drift: 4 features PSI>0.2"
    python -m bsp_mlops.pipeline leaderboard
    python -m bsp_mlops.pipeline tune --trials 20
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import warnings
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .config import ENTITY, LABEL, TS
from .data import CATEGORICAL

warnings.filterwarnings("ignore", message=".*X does not have valid feature names.*")
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", message=".*Inferred schema contains integer column.*")
warnings.filterwarnings("ignore", message=".*dataset source can be interpreted in multiple ways.*")
warnings.filterwarnings("ignore", message=".*shap values output has changed.*")
warnings.filterwarnings("ignore", message=".*'generic' unit for NumPy timedelta.*")

# cloudpickle: LightGBM / XGBoost estimators inside the Pipeline are not on skops' trusted-type list
SER = "cloudpickle"
MODEL_KINDS = ("logreg", "random_forest", "lightgbm", "xgboost", "hist_gb")
DEFAULT_PARAMS = {
    "logreg": {"C": 0.5},
    "random_forest": {"n_estimators": 300, "min_samples_leaf": 25, "max_features": "sqrt"},
    "lightgbm": {"n_estimators": 400, "learning_rate": 0.04, "num_leaves": 31, "min_child_samples": 40,
                 "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8, "reg_lambda": 1.0},
    "xgboost": {"n_estimators": 400, "learning_rate": 0.05, "max_depth": 5, "subsample": 0.8,
                "colsample_bytree": 0.8, "min_child_weight": 5},
    "hist_gb": {"max_iter": 300, "learning_rate": 0.06, "max_leaf_nodes": 31, "l2_regularization": 1.0},
}


# ── building blocks ───────────────────────────────────────────────────────
def build_preprocessor(features: list[str]):
    from sklearn.compose import ColumnTransformer
    from sklearn.impute import SimpleImputer
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import OneHotEncoder, StandardScaler

    cats = [f for f in features if f in CATEGORICAL]
    nums = [f for f in features if f not in CATEGORICAL]
    num = Pipeline([("impute", SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True)),
                    ("scale", StandardScaler())])
    cat = Pipeline([("impute", SimpleImputer(strategy="constant", fill_value="missing")),
                    ("onehot", OneHotEncoder(handle_unknown="ignore", min_frequency=0.005, sparse_output=False))])
    return ColumnTransformer([("num", num, nums), ("cat", cat, cats)], remainder="drop",
                             verbose_feature_names_out=True)


def build_model(kind: str, params: dict | None = None, seed: int = 7):
    p = {**DEFAULT_PARAMS.get(kind, {}), **(params or {})}
    if kind == "logreg":
        from sklearn.linear_model import LogisticRegression

        return LogisticRegression(max_iter=2000, **p)
    if kind == "random_forest":
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(n_jobs=-1, random_state=seed, **p)
    if kind == "lightgbm":
        from lightgbm import LGBMClassifier

        return LGBMClassifier(random_state=seed, verbose=-1, n_jobs=4, deterministic=True, force_col_wise=True, **p)
    if kind == "xgboost":
        from xgboost import XGBClassifier

        return XGBClassifier(random_state=seed, tree_method="hist", eval_metric="logloss", n_jobs=4, **p)
    if kind == "hist_gb":
        from sklearn.ensemble import HistGradientBoostingClassifier

        return HistGradientBoostingClassifier(random_state=seed, **p)
    raise ValueError(f"unknown model kind {kind!r}; choose from {MODEL_KINDS}")


def build_pipeline(kind: str, features: list[str], params: dict | None = None, seed: int = 7):
    from sklearn.pipeline import Pipeline

    return Pipeline([("prep", build_preprocessor(features)), ("model", build_model(kind, params, seed))])


def fit(pipe, X: pd.DataFrame, y, sample_weight=None):
    if sample_weight is not None:
        return pipe.fit(X, y, model__sample_weight=sample_weight)
    return pipe.fit(X, y)


def default_features() -> list[str]:
    from .featurestore import all_features

    return [f for f in all_features() if f not in ("last_survey_score", "charge_x_increase")]


# ── training data ─────────────────────────────────────────────────────────
@lru_cache(maxsize=8)
def _training_set_cached(start: str, end: str, feats: tuple) -> pd.DataFrame:
    from . import db
    from .data import ensure_base
    from .featurestore import get_historical_features

    ensure_base()
    ent = db.query_df(f"select {ENTITY}, {TS}, ({LABEL})::int as {LABEL} from features.churn_base "
                      f"where label_complete and {TS} between %s and %s", (start, end))
    return get_historical_features(ent, list(feats))


def training_set(window: tuple, features: list[str]) -> pd.DataFrame:
    """Point-in-time training rows for a (first_month, last_month) window."""
    a, b = (pd.Timestamp(window[0]).date().isoformat(), pd.Timestamp(window[1]).date().isoformat())
    return _training_set_cached(a, b, tuple(features)).copy()


def default_windows() -> dict:
    from .data import windows

    return windows()


# ── MLflow plumbing ───────────────────────────────────────────────────────
def mlflow_setup(experiment: str | None = None):
    import logging

    import mlflow

    logging.getLogger("mlflow").setLevel(logging.WARNING)
    logging.getLogger("mlflow.sklearn").setLevel(logging.ERROR)   # the pickle caution note (see SER)
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "120")
    os.environ.setdefault("MLFLOW_ENABLE_ARTIFACTS_PROGRESS_BAR", "false")
    os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")
    # MLflow ≥ 3.x refuses to deserialise sklearn models unless this is set. Models here only
    # come from the appliance's own, gated registry, so loading them is trusted.
    os.environ.setdefault("MLFLOW_ALLOW_PICKLE_DESERIALIZATION", "true")      # no git in the image: skip the SHA lookup noise
    logging.getLogger("mlflow.utils.git_utils").setLevel(logging.ERROR)
    mlflow.set_tracking_uri(config.MLFLOW_URI)
    mlflow.set_registry_uri(config.MLFLOW_URI)
    if experiment:
        mlflow.set_experiment(experiment)
    return mlflow


def _wfmt(w) -> str:
    return f"{pd.Timestamp(w[0]):%Y-%m}..{pd.Timestamp(w[1]):%Y-%m}"


def recency_weights(months: pd.Series, boost: float, last_k: int = 3) -> np.ndarray:
    ms = np.sort(months.unique())
    recent = set(ms[-last_k:])
    return np.where(months.isin(recent), boost, 1.0)


def train_and_log(kind: str, features: list[str], windows: dict | None = None, params: dict | None = None,
                  experiment: str | None = None, run_name: str | None = None, tags: dict | None = None,
                  nested: bool = False, log_model: bool = True, log_shap: bool = True,
                  recency_boost: float | None = None, register: bool = False, verbose: bool = True,
                  seed: int = 7, permute_labels: int | None = None) -> dict:
    """Fit one model on the train window, evaluate on valid + test windows,
    log everything to MLflow. Returns {run_id, model_uri, metrics, ...}.

    permute_labels=<seed> shuffles the training labels — a NEGATIVE CONTROL
    used to prove the release gates block a model with no real signal."""
    mlflow = mlflow_setup(experiment or config.EXPERIMENT)
    from mlflow.models import infer_signature

    from .metrics import calibration_table, evaluate, gain_table, plots

    w = windows or default_windows()
    tr, va, te = training_set(w["train"], features), training_set(w["valid"], features), training_set(w["test"], features)
    params = {**DEFAULT_PARAMS.get(kind, {}), **(params or {})}
    pipe = build_pipeline(kind, features, params, seed)
    sw = recency_weights(tr[TS], recency_boost) if recency_boost else None
    t0 = time.time()
    if permute_labels is not None:
        tr = tr.copy()
        tr[LABEL] = np.random.default_rng(permute_labels).permutation(tr[LABEL].to_numpy())
        tags = {**(tags or {}), "bsp.negative_control": "true", "bsp.permute_seed": str(permute_labels)}
    fit(pipe, tr[features], tr[LABEL].astype(int), sw)
    fit_s = time.time() - t0
    p_va = pipe.predict_proba(va[features])[:, 1]
    p_te = pipe.predict_proba(te[features])[:, 1]
    m = {**evaluate(va[LABEL], p_va, "valid_"), **evaluate(te[LABEL], p_te, "test_"), "fit_seconds": fit_s}
    from . import lineage
    from .journey import link_tags

    # reproducibility contract: the exact rows, code and environment behind this run
    data_hash = lineage.data_snapshot_hash(tr[[ENTITY, TS] + features + [LABEL]])
    lin = lineage.run_tags(data_hash)
    with mlflow.start_run(run_name=run_name or kind, nested=nested,
                          tags={"model_kind": kind, **link_tags(), **lin, **(tags or {})}) as run:
        mlflow.log_params({"model_kind": kind, "n_features": len(features), "split_strategy": "time",
                           "train_window": _wfmt(w["train"]), "valid_window": _wfmt(w["valid"]),
                           "test_window": _wfmt(w["test"]), "n_train": len(tr), "n_valid": len(va), "n_test": len(te),
                           "recency_boost": recency_boost or 0, "seed": seed,
                           **{f"hp_{k}": v for k, v in params.items()}})
        try:
            from .featurestore import freshness

            views = [{"name": r["feature_view"], "version": r["version"], "def_hash": r["def_hash"]} for r in freshness()]
        except Exception:
            views = []
        mlflow.log_dict({"model_kind": kind, "params": params, "features": features, "seed": seed, "label": LABEL,
                         "windows": {k: _wfmt(w[k]) for k in ("train", "valid", "test")}, "recency_boost": recency_boost or 0,
                         "split_strategy": "time", "data_snapshot_sha256": data_hash, "n_train": len(tr),
                         "source": "features.churn_base via feature-store point-in-time join", "feature_views": views,
                         **{k.replace("bsp.", ""): v for k, v in lin.items()}},
                        "config/training_config.json")
        mlflow.log_text(lineage.env_lock()["lock"], "environment/requirements.lock")
        mlflow.log_metrics({k: v for k, v in m.items() if np.isfinite(v)})
        try:
            ds = mlflow.data.from_pandas(tr[[ENTITY, TS] + features + [LABEL]].head(50000), name="churn_train",
                                         targets=LABEL, source="features.churn_base (feature store PIT join)")
            mlflow.log_input(ds, context="training")
            mlflow.log_input(mlflow.data.from_pandas(te[[ENTITY, TS] + features + [LABEL]], name="churn_test",
                                                     targets=LABEL, source="features.churn_base"), context="test")
        except Exception:
            pass
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "features.json").write_text(json.dumps({"features": features, "label": LABEL,
                                                         "categorical": [f for f in features if f in CATEGORICAL]}, indent=2))
            gain_table(te[LABEL], p_te).to_csv(d / "gain_test.csv")
            calibration_table(te[LABEL], p_te).to_csv(d / "calibration_test.csv")
            plots(te[LABEL].to_numpy(), p_te, d, title=f"{kind} — test {_wfmt(w['test'])}")
            # drift reference = the release baseline: the most recent data the model was validated
            # on (valid + test windows) — what "normal" looked like when it went live
            rel = pd.concat([va, te], ignore_index=True)
            ref = rel[[TS] + features].sample(min(5000, len(rel)), random_state=7).copy()
            ref["score"] = pipe.predict_proba(ref[features])[:, 1]
            ref.to_parquet(d / "reference_sample.parquet", index=False)
            if log_shap:
                try:
                    from .selection import shap_importance

                    sh = shap_importance(pipe, te[features].sample(min(1000, len(te)), random_state=7))
                    sh.to_csv(d / "shap_importance.csv")
                    _bar(sh.iloc[:, 0].head(15), d / "shap_importance.png", "Global importance (mean |SHAP|)")
                    m["shap_top"] = sh.index[0]
                except Exception as e:
                    (d / "shap_error.txt").write_text(str(e))
            mlflow.log_artifacts(str(d), artifact_path="evaluation")
        model_uri = None
        if log_model:
            example = te[features].head(5)
            sig = infer_signature(example, pipe.predict_proba(example)[:, 1])
            import mlflow.sklearn

            # 1) the transformation pipeline on its own (reusable feature transform)
            # (no input_example: a bare transformer has no pyfunc flavour to validate it against)
            mlflow.sklearn.log_model(pipe.named_steps["prep"], name="preprocessor", serialization_format=SER,
                                     signature=infer_signature(example))
            # 2) prep + model as ONE deployable model (pyfunc → predict_proba)
            info = mlflow.sklearn.log_model(pipe, name="model", signature=sig, input_example=example, serialization_format=SER,
                                            pyfunc_predict_fn="predict_proba",
                                            registered_model_name=config.MODEL_NAME if register else None)
            model_uri = info.model_uri
        run_id = run.info.run_id
    if verbose:
        print(f"  · {run_name or kind:<22} valid AUC {m['valid_auc']:.4f}  test AUC {m['test_auc']:.4f}  "
              f"PR-AUC {m['test_pr_auc']:.4f}  lift@10 {m['test_lift_at_10']:.2f}  ({fit_s:.1f}s)")
    return {"run_id": run_id, "model_uri": model_uri, "metrics": m, "kind": kind, "params": params,
            "features": features, "pipeline": pipe, "data_snapshot_sha256": data_hash}


def _bar(s: pd.Series, path, title):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 0.32 * len(s) + 1))
    s[::-1].plot.barh(ax=ax, color="#0b6e4f")
    ax.set_title(title)
    fig.tight_layout()
    fig.savefig(path, dpi=90)
    plt.close(fig)


# ── leaderboard ───────────────────────────────────────────────────────────
def leaderboard(features: list[str], windows: dict | None = None, kinds=MODEL_KINDS, verbose: bool = True) -> pd.DataFrame:
    w = windows or default_windows()
    rows = []
    for k in kinds:
        r = train_and_log(k, features, w, experiment=config.EXPERIMENT, run_name=k, tags={"stage": "leaderboard"},
                          log_shap=k in ("lightgbm", "xgboost", "random_forest"), verbose=verbose)
        rows.append({"run_id": r["run_id"], "model": k, **{x: r["metrics"][x] for x in
                     ("valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_lift_at_10", "fit_seconds")}})
    return pd.DataFrame(rows).sort_values("valid_auc", ascending=False).reset_index(drop=True)


def search_leaderboard(experiments: list[str] | None = None, max_results: int = 500) -> pd.DataFrame:
    mlflow = mlflow_setup()
    names = experiments or [config.EXPERIMENT, config.TUNING_EXPERIMENT, config.RETRAIN_EXPERIMENT]
    exps = [e for e in (mlflow.get_experiment_by_name(n) for n in names) if e]
    if not exps:
        return pd.DataFrame()
    return mlflow.search_runs([e.experiment_id for e in exps], max_results=max_results,
                              order_by=["metrics.valid_auc DESC"])


# ── hyper-parameter tuning (Optuna, nested MLflow runs) ───────────────────
def _space(trial, kind: str) -> dict:
    if kind == "lightgbm":
        return {"n_estimators": trial.suggest_int("n_estimators", 150, 700, step=50),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.15, log=True),
                "num_leaves": trial.suggest_int("num_leaves", 8, 96, log=True),
                "min_child_samples": trial.suggest_int("min_child_samples", 10, 200, log=True),
                "subsample": trial.suggest_float("subsample", 0.5, 1.0), "subsample_freq": 1,
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 1.0),
                "reg_lambda": trial.suggest_float("reg_lambda", 1e-3, 20, log=True)}
    if kind == "xgboost":
        return {"n_estimators": trial.suggest_int("n_estimators", 150, 700, step=50),
                "learning_rate": trial.suggest_float("learning_rate", 0.01, 0.2, log=True),
                "max_depth": trial.suggest_int("max_depth", 2, 8),
                "min_child_weight": trial.suggest_float("min_child_weight", 1, 30, log=True),
                "subsample": trial.suggest_float("subsample", 0.5, 1.0),
                "colsample_bytree": trial.suggest_float("colsample_bytree", 0.4, 1.0)}
    if kind == "hist_gb":
        return {"max_iter": trial.suggest_int("max_iter", 100, 600, step=50),
                "learning_rate": trial.suggest_float("learning_rate", 0.02, 0.2, log=True),
                "max_leaf_nodes": trial.suggest_int("max_leaf_nodes", 8, 64, log=True),
                "l2_regularization": trial.suggest_float("l2_regularization", 1e-3, 10, log=True)}
    if kind == "random_forest":
        return {"n_estimators": trial.suggest_int("n_estimators", 100, 400, step=50),
                "min_samples_leaf": trial.suggest_int("min_samples_leaf", 5, 100, log=True),
                "max_features": trial.suggest_float("max_features", 0.2, 0.8)}
    return {"C": trial.suggest_float("C", 1e-3, 10, log=True)}


def tune(features: list[str], kind: str = "lightgbm", n_trials: int = 20, windows: dict | None = None,
         timeout: int | None = None, register: bool = False, verbose: bool = True) -> dict:
    """Optuna TPE search on validation AUC; every trial is a nested MLflow
    run under one parent; the best params are refit and logged as the
    parent's model (optionally registered)."""
    import optuna
    from sklearn.metrics import roc_auc_score

    mlflow = mlflow_setup(config.TUNING_EXPERIMENT)
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    w = windows or default_windows()
    tr, va = training_set(w["train"], features), training_set(w["valid"], features)
    from .journey import link_tags

    with mlflow.start_run(run_name=f"optuna-{kind}", tags={"stage": "tuning", "model_kind": kind, **link_tags()}) as parent:
        def objective(trial):
            params = _space(trial, kind)
            pipe = build_pipeline(kind, features, params)
            t0 = time.time()
            fit(pipe, tr[features], tr[LABEL].astype(int))
            auc = roc_auc_score(va[LABEL], pipe.predict_proba(va[features])[:, 1])
            with mlflow.start_run(run_name=f"trial-{trial.number:02d}", nested=True,
                                  tags={"stage": "tuning-trial", "model_kind": kind}):
                mlflow.log_params({f"hp_{k}": v for k, v in params.items()})
                mlflow.log_metrics({"valid_auc": auc, "fit_seconds": time.time() - t0})
            if verbose:
                print(f"    trial {trial.number:02d}  valid AUC {auc:.4f}")
            return auc

        study = optuna.create_study(direction="maximize", sampler=optuna.samplers.TPESampler(seed=7),
                                    study_name=f"churn-{kind}")
        study.optimize(objective, n_trials=n_trials, timeout=timeout)
        mlflow.log_params({f"best_{k}": v for k, v in study.best_params.items()})
        mlflow.log_metric("best_trial_valid_auc", study.best_value)
        mlflow.log_metric("n_trials", len(study.trials))
        try:
            hist = study.trials_dataframe()[["number", "value"] + [c for c in study.trials_dataframe().columns
                                                                   if c.startswith("params_")]]
            with tempfile.TemporaryDirectory() as td:
                hist.to_csv(Path(td) / "optuna_trials.csv", index=False)
                mlflow.log_artifacts(td, artifact_path="tuning")
        except Exception:
            pass
        best = {**_space(optuna.trial.FixedTrial(study.best_params), kind)}
        res = train_and_log(kind, features, w, params=best, experiment=config.TUNING_EXPERIMENT,
                            run_name=f"{kind}-tuned", tags={"stage": "tuned", "parent_run": parent.info.run_id},
                            nested=True, register=register, verbose=verbose)
        mlflow.log_metrics({"tuned_valid_auc": res["metrics"]["valid_auc"], "tuned_test_auc": res["metrics"]["test_auc"]})
    res.update(best_params=best, best_trial_auc=study.best_value, parent_run_id=parent.info.run_id,
               n_trials=len(study.trials))
    return res


# ── retrain (run by the orchestrator as a subprocess) ─────────────────────
def retrain(retrain_id: int | None = None, reason: str = "manual", trigger: str = "manual",
            recency_boost: float = 2.0, verbose: bool = True) -> dict:
    """Refit the champion's recipe (model kind, params, features) on the most
    recent labelled data, register it as a new version with alias
    `challenger`, and evaluate champion vs challenger on the same newest
    hold-out month. Promotion is decided by the orchestrator.

    Every step is recorded as a model journey (bsp_mlops.journey) whose
    parent is the champion's journey, so the control tower can show the
    retrain's progress live and its evidence afterwards."""
    from mlflow import MlflowClient

    from . import journey as JY
    from . import registry
    from .metrics import evaluate

    mlflow = mlflow_setup(config.RETRAIN_EXPERIMENT)
    client = MlflowClient()
    champ = registry.alias_version("champion")
    if champ is None:
        raise RuntimeError("no champion registered — run the leaderboard / registration first")
    crun = client.get_run(champ.run_id)
    kind = crun.data.params.get("model_kind", "lightgbm")
    params = {k[3:]: _coerce_param(v) for k, v in crun.data.params.items() if k.startswith("hp_")}
    features = registry.model_features(champ.version)
    parent_jid = (champ.tags or {}).get("bsp.journey_id")
    J = None
    try:
        J = JY.Journey.start("retrain", title=f"retrain #{retrain_id or 'manual'} of v{champ.version} ({trigger})",
                             parent_journey=parent_jid, extra_tags={"bsp.retrain_id": str(retrain_id), "bsp.trigger": trigger,
                                                                    "bsp.reason": reason[:250]})
    except Exception as e:   # the journey is evidence, never a reason to fail a retrain
        print(f"  (journey disabled: {e})")

    def _stage(key):
        from contextlib import nullcontext

        return J.stage(key, swallow=True) if J is not None else nullcontext(None)

    wd = default_windows()
    labelled = wd["labelled"]
    # newest labelled month is the shared hold-out for champion vs challenger;
    # no tuning happens in a retrain, so validation == that hold-out
    w = {"train": (labelled[0], labelled[-2]), "valid": (labelled[-1], labelled[-1]), "test": (labelled[-1], labelled[-1])}
    if verbose:
        print(f"  retrain #{retrain_id}: {kind} recipe of champion v{champ.version}, train {_wfmt(w['train'])}, "
              f"valid {_wfmt(w['valid'])}, test {_wfmt(w['test'])} (recency boost {recency_boost})")
    ok = False
    try:
        with _stage("integration") as st:
            if st:
                JY.integration_evidence(st, reused=True)
        with _stage("eda") as st:
            if st:
                JY.eda_evidence(st, window=w["train"])
        with _stage("notebook") as st:
            if st:
                JY.notebook_evidence(st, ["11_retrain_trigger"])
                st.note(f"Triggered by {trigger}: {reason[:300]}")
                st.headline(f"{trigger} trigger · nb 11")
        with _stage("selection") as st:
            if st:
                st.status("skipped").headline(f"{len(features)} features inherited")
                st.summary(f"Feature list inherited from champion v{champ.version} — the retrain isolates fresher data.")
                st.table("features", "Features (inherited)", [{"feature": f} for f in features], columns=["feature"])
        with _stage("holdout") as st:
            if st:
                JY.holdout_evidence(st, None, w, chosen="time")
                st.note("Retrain hold-out: the newest labelled month is the shared test set for champion and challenger.")
        with _stage("feature_store") as st:
            if st:
                st.status("skipped").headline("views inherited").summary(
                    "Same versioned feature views as the champion; training rows rebuilt through the point-in-time join.")
        with _stage("algo_selection") as st:
            if st:
                st.status("skipped").headline(f"{kind} inherited").summary(
                    f"Recipe inherited from champion v{champ.version}: {kind}. The full leaderboard lives in the parent journey.")
                st.source_run(champ.run_id, f"champion v{champ.version} training run")
        res = train_and_log(kind, features, w, params=params, experiment=config.RETRAIN_EXPERIMENT,
                            run_name=f"retrain-{retrain_id or 'manual'}",
                            tags={"stage": "retrain", "retrain_id": str(retrain_id), "trigger": trigger, "reason": reason[:250],
                                  "parent_champion_version": str(champ.version)},
                            recency_boost=recency_boost, register=True, verbose=verbose)
        mv = registry.version_for_run(res["run_id"])
        try:
            from .release import register_dev

            register_dev(mv.version, note=f"retrain #{retrain_id}")      # stage dev, artifact hash pinned, lineage on the version
        except Exception as e:
            print(f"  (release: could not register v{mv.version} as dev: {e})")
        client.set_registered_model_alias(config.MODEL_NAME, "challenger", mv.version)
        client.set_model_version_tag(config.MODEL_NAME, mv.version, "retrain_id", str(retrain_id))
        client.set_model_version_tag(config.MODEL_NAME, mv.version, "trigger", trigger)
        # champion on the same newest hold-out
        te = training_set(w["test"], features)
        champ_model = registry.load_version(champ.version)
        champ_m = evaluate(te[LABEL], champ_model.predict_proba(te[features])[:, 1], "test_")
        out = {"retrain_id": retrain_id, "run_id": res["run_id"], "model_version": str(mv.version),
               "champion_version": str(champ.version), "challenger_auc": res["metrics"]["test_auc"],
               "champion_auc": champ_m["test_auc"], "margin": res["metrics"]["test_auc"] - champ_m["test_auc"],
               "challenger_pr_auc": res["metrics"]["test_pr_auc"], "champion_pr_auc": champ_m["test_pr_auc"],
               "test_window": _wfmt(w["test"]), "train_window": _wfmt(w["train"]), "model_kind": kind}
        p_te = res["pipeline"].predict_proba(te[features])[:, 1]
        with _stage("roc_auc") as st:
            if st:
                JY.roc_evidence(st, te[LABEL].to_numpy(), p_te, f"v{mv.version} {kind} (retrained)", out["test_window"])
        with _stage("tuning") as st:
            if st:
                JY.inherited_params_evidence(st, res["run_id"], str(champ.version))
        with _stage("evaluation") as st:
            if st:
                cmp = {f"challenger v{mv.version}": {"auc": out["challenger_auc"], "pr_auc": out["challenger_pr_auc"]},
                       f"champion v{champ.version}": {"auc": out["champion_auc"], "pr_auc": out["champion_pr_auc"]}}
                JY.evaluation_evidence(st, te[LABEL].to_numpy(), p_te, res["run_id"], f"v{mv.version} {kind}", compare=cmp)
                st.metric(margin_vs_champion=out["margin"])
        with _stage("registration") as st:
            if st:
                JY.registration_evidence(st, str(mv.version), res["run_id"], "challenger",
                                         f"retrain #{retrain_id}, margin {out['margin']:+.4f} vs champion v{champ.version}")
        if J:
            try:
                J.attach_version(str(mv.version))
            except Exception as e:
                print(f"  (journey: could not link version: {e})")
        ok = True
    finally:
        if J:
            J.finish(ok)
    if verbose:
        print(f"  challenger v{mv.version} AUC {out['challenger_auc']:.4f} vs champion v{champ.version} "
              f"AUC {out['champion_auc']:.4f} on {out['test_window']} (margin {out['margin']:+.4f})")
    return out


def _coerce_param(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            pass
    if v in ("True", "False"):
        return v == "True"
    if v == "None":
        return None
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("retrain")
    r.add_argument("--retrain-id", type=int)
    r.add_argument("--reason", default="manual")
    r.add_argument("--trigger", default="manual")
    r.add_argument("--recency-boost", type=float, default=2.0)
    r.add_argument("--out", help="write the outcome JSON here")
    lb = sub.add_parser("leaderboard")
    lb.add_argument("--features", help="comma-separated (default: feature store set)")
    t = sub.add_parser("tune")
    t.add_argument("--kind", default="lightgbm")
    t.add_argument("--trials", type=int, default=20)
    a = ap.parse_args(argv)
    if a.cmd == "retrain":
        out = retrain(a.retrain_id, a.reason, a.trigger, a.recency_boost)
        if a.out:
            Path(a.out).write_text(json.dumps(out, indent=2))
        print("RESULT " + json.dumps(out))
    elif a.cmd == "leaderboard":
        feats = a.features.split(",") if a.features else default_features()
        print(leaderboard(feats).to_string())
    elif a.cmd == "tune":
        res = tune(default_features(), a.kind, a.trials)
        print(json.dumps({k: res[k] for k in ("run_id", "best_params", "best_trial_auc")}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
