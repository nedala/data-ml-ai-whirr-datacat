"""Paths, service URLs and model constants (plan/architecture.md §1, §3, §6).

Everything reads from the environment so the same code runs inside the
mlflow / model-gateway / mlops containers, the workspace (notebooks) and a
throwaway test network."""

from __future__ import annotations

import os
from pathlib import Path


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


# ── storage (${DATA_ROOT}/mlops on the host → /data/mlops in containers) ──
MLOPS_DIR = Path(env("MLOPS_DATA_DIR", "/data/mlops"))
ARTIFACTS_DIR = MLOPS_DIR / "artifacts"            # MLflow --artifacts-destination
FEATURE_REPO_DIR = MLOPS_DIR / "feature_repo"      # feature store registry + Feast repo
FROZEN_DIR = MLOPS_DIR / "frozen"                  # built bundles (staging area)
ARTIFACTORY_DIR = Path(env("ARTIFACTORY_LOCAL_DIR", str(MLOPS_DIR / "artifactory")))
RUNS_DIR = MLOPS_DIR / "runs"                      # retrain subprocess logs / outcomes

# ── services (internal URLs) ──
MLFLOW_URI = env("MLFLOW_TRACKING_URI", "http://mlflow:5000/mlflow")
GATEWAY_URL = env("MODEL_GATEWAY_URL", "http://model-gateway:8098/serve")
MLOPS_URL = env("MLOPS_URL", "http://mlops:8097/mlops")

# ── model / deployment ──
MODEL_NAME = env("CHURN_MODEL_NAME", "bsp_churn")
DEPLOYMENT = env("CHURN_DEPLOYMENT", "churn")
EXPERIMENT = env("CHURN_EXPERIMENT", "churn-leaderboard")
TUNING_EXPERIMENT = env("CHURN_TUNING_EXPERIMENT", "churn-tuning")
RETRAIN_EXPERIMENT = env("CHURN_RETRAIN_EXPERIMENT", "churn-retrain")
SELECTION_EXPERIMENT = env("CHURN_SELECTION_EXPERIMENT", "churn-feature-selection")
LABEL = "churned_next_90d"
ENTITY = env("CHURN_ENTITY_KEY", "ban")          # BAN — billing account number (10-digit string); see plan/architecture.md §3
TS = "snapshot_month"

# drift thresholds (PSI rule of thumb: <0.1 stable, 0.1–0.25 moderate, >0.25 significant shift)
PSI_WARN = float(env("DRIFT_PSI_WARN", "0.1"))
PSI_BREACH = float(env("DRIFT_PSI_BREACH", "0.25"))
KS_BREACH = float(env("DRIFT_KS_BREACH", "0.1"))
AUC_DROP_BREACH = float(env("DRIFT_AUC_DROP", "0.03"))
MIN_BREACHED_FEATURES = int(env("DRIFT_MIN_FEATURES", "1"))
PROMOTE_MARGIN = float(env("PROMOTE_MARGIN_AUC", "0.002"))


def ensure_dirs() -> None:
    for p in (ARTIFACTS_DIR, FEATURE_REPO_DIR, FROZEN_DIR, ARTIFACTORY_DIR, RUNS_DIR):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            pass


def user() -> str:
    return env("USER_HANDLE", env("USER", "analyst"))
