"""MLflow Model Registry helpers — champion / challenger aliases.

Conventions (registered model `bsp_churn`):
  alias `champion`    serves production traffic
  alias `challenger`  candidate (shadow / canary / green)
  tag   `previous_champion` on the model → enables one-click rollback
  tag   `frozen=true` (+ `frozen_sha256`) on a version once bundled
"""

from __future__ import annotations

import json
import tempfile
import time
from functools import lru_cache
from pathlib import Path

from . import config


def client():
    from mlflow import MlflowClient

    from .pipeline import mlflow_setup

    mlflow_setup()
    return MlflowClient()


def ensure_model() -> None:
    c = client()
    try:
        c.get_registered_model(config.MODEL_NAME)
    except Exception:
        c.create_registered_model(config.MODEL_NAME, description="DataPlatform 90-day churn propensity "
                                  "(sklearn Pipeline: ColumnTransformer + classifier).")


def register_run(run_id: str, alias: str | None = None, description: str = "") -> str:
    """Register runs:/<run_id>/model as a new version; optionally set alias."""
    import mlflow

    ensure_model()
    mv = mlflow.register_model(f"runs:/{run_id}/model", config.MODEL_NAME)
    c = client()
    _wait_ready(mv.version)
    if description:
        c.update_model_version(config.MODEL_NAME, mv.version, description=description)
    if alias:
        set_alias(alias, mv.version)
    return str(mv.version)


def _wait_ready(version, timeout: float = 60) -> None:
    c = client()
    t0 = time.time()
    while time.time() - t0 < timeout:
        if c.get_model_version(config.MODEL_NAME, str(version)).status == "READY":
            return
        time.sleep(0.5)


def alias_version(alias: str):
    try:
        return client().get_model_version_by_alias(config.MODEL_NAME, alias)
    except Exception:
        return None


def aliases() -> dict[str, str]:
    try:
        m = client().get_registered_model(config.MODEL_NAME)
        return {k: str(v) for k, v in (m.aliases or {}).items()}
    except Exception:
        return {}


def set_alias(alias: str, version) -> None:
    client().set_registered_model_alias(config.MODEL_NAME, alias, str(version))


def version_for_run(run_id: str):
    c = client()
    for _ in range(30):
        vs = c.search_model_versions(f"name='{config.MODEL_NAME}' and run_id='{run_id}'")
        if vs:
            _wait_ready(vs[0].version)
            return vs[0]
        time.sleep(1)
    raise RuntimeError(f"no registered version for run {run_id}")


def promote(version, reason: str = "") -> dict:
    """challenger → champion (old champion remembered for rollback).

    LOW-LEVEL and UNGATED: production promotions go through
    bsp_mlops.release (gates → staging → four-eyes approval). A direct call is
    recorded in mlops.audit_log as `registry.promote (ungated)`, and the release
    status flags a champion that has no approval record."""
    c = client()
    try:
        from . import db

        db.audit("registry.promote (ungated)", f"{config.MODEL_NAME}:v{version}", config.user(), None, "library", reason=reason[:300])
    except Exception:
        pass
    old = alias_version("champion")
    if old is not None and str(old.version) == str(version):
        return {"champion": str(version), "previous": None, "changed": False}
    if old is not None:
        c.set_registered_model_tag(config.MODEL_NAME, "previous_champion", str(old.version))
    set_alias("champion", version)
    c.set_model_version_tag(config.MODEL_NAME, str(version), "promoted_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if reason:
        c.set_model_version_tag(config.MODEL_NAME, str(version), "promotion_reason", reason[:500])
    ch = alias_version("challenger")
    if ch is not None and str(ch.version) == str(version):
        try:
            c.delete_registered_model_alias(config.MODEL_NAME, "challenger")
        except Exception:
            pass
    return {"champion": str(version), "previous": str(old.version) if old else None, "changed": True}


def rollback() -> dict:
    """LOW-LEVEL alias swap (ungated, audited); bsp_mlops.release.rollback is the recorded path."""
    c = client()
    try:
        from . import db

        db.audit("registry.rollback (ungated)", config.MODEL_NAME, config.user(), None, "library")
    except Exception:
        pass
    m = c.get_registered_model(config.MODEL_NAME)
    prev = (m.tags or {}).get("previous_champion")
    if not prev:
        raise RuntimeError("no previous champion recorded")
    cur = alias_version("champion")
    set_alias("champion", prev)
    if cur is not None:
        c.set_registered_model_tag(config.MODEL_NAME, "previous_champion", str(cur.version))
        set_alias("challenger", cur.version)
    return {"champion": prev, "demoted": str(cur.version) if cur else None}


def list_versions() -> list[dict]:
    c = client()
    try:
        m = c.get_registered_model(config.MODEL_NAME)
    except Exception:
        return []
    by_ver: dict[str, list[str]] = {}
    for a, v in (m.aliases or {}).items():
        by_ver.setdefault(str(v), []).append(a)
    out = []
    for v in c.search_model_versions(f"name='{config.MODEL_NAME}'"):
        row = {"version": str(v.version), "run_id": v.run_id, "status": v.status, "aliases": by_ver.get(str(v.version), []),
               "created": v.creation_timestamp, "tags": dict(v.tags or {}), "description": v.description or ""}
        try:
            r = c.get_run(v.run_id)
            row["metrics"] = {k: r.data.metrics[k] for k in ("valid_auc", "test_auc", "test_pr_auc", "test_logloss",
                                                              "test_lift_at_10") if k in r.data.metrics}
            row["model_kind"] = r.data.params.get("model_kind")
            row["train_window"] = r.data.params.get("train_window")
            row["experiment_id"] = r.info.experiment_id
            row["run_name"] = r.info.run_name
        except Exception:
            row["metrics"] = {}
        out.append(row)
    out.sort(key=lambda r: int(r["version"]), reverse=True)
    return out


def model_tags() -> dict:
    try:
        return dict(client().get_registered_model(config.MODEL_NAME).tags or {})
    except Exception:
        return {}


@lru_cache(maxsize=32)
def model_features(version) -> list[str]:
    c = client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    with tempfile.TemporaryDirectory() as td:
        p = c.download_artifacts(mv.run_id, "evaluation/features.json", td)
        return json.loads(Path(p).read_text())["features"]


@lru_cache(maxsize=16)
def load_version(version):
    import mlflow.sklearn

    client()
    return mlflow.sklearn.load_model(f"models:/{config.MODEL_NAME}/{version}")


def reference_sample(version):
    """Training-time feature/score sample logged with the run (drift reference)."""
    import pandas as pd

    c = client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    with tempfile.TemporaryDirectory() as td:
        p = c.download_artifacts(mv.run_id, "evaluation/reference_sample.parquet", td)
        return pd.read_parquet(p)
