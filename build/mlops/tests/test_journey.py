"""Unit tests for the model journey (no services needed: MLflow / Postgres are not touched).

    cd build/mlops && python -m pytest -q -p no:cacheprovider tests
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
os.environ.setdefault("TOWER_SCHEDULER", "off")

from bsp_mlops import config, journey as JY  # noqa: E402


def stage(key="roc_auc"):
    d = Path(tempfile.mkdtemp())
    return JY.StageLog(key, "0" * 32, d)


def test_stage_catalogue_is_ordered_and_complete():
    keys = JY.STAGE_KEYS
    assert len(keys) == len(set(keys))
    for k in ("integration", "eda", "notebook", "selection", "holdout", "algo_selection", "roc_auc", "tuning",
              "evaluation", "registration", "deployment", "monitoring", "retrain", "freeze"):
        assert k in keys
    assert keys.index("integration") < keys.index("eda") < keys.index("algo_selection") < keys.index("roc_auc") < keys.index("tuning")
    assert set(JY.OPS_STAGES) <= set(keys)


def test_entity_key_is_ban():
    assert config.ENTITY == "ban"


def test_link_tags_follow_the_active_journey(monkeypatch):
    monkeypatch.delenv(JY.ENV_KEY, raising=False)
    assert JY.link_tags() == {}
    monkeypatch.setenv(JY.ENV_KEY, "j-test")
    assert JY.link_tags() == {"bsp.journey_id": "j-test"}


def test_curve_points_downsample_keeps_ends():
    x = np.linspace(0, 1, 5000)
    pts = JY.curve_points(x, x ** 0.5, n=100)
    assert len(pts) <= 100
    assert pts[0] == [0.0, 0.0] and pts[-1] == [1.0, 1.0]


def test_roc_evidence_matches_sklearn():
    from sklearn.metrics import roc_auc_score

    rng = np.random.default_rng(1)
    y = rng.random(3000) < 0.1
    p = np.clip(0.1 + 0.25 * y + rng.normal(0, 0.15, 3000), 0, 1)
    st = stage()
    out = JY.roc_evidence(st, y, p, "toy", "2026-04..2026-05")
    assert out["auc"] == pytest.approx(roc_auc_score(y, p))
    assert st.doc["curves"]["roc"]["points"][0] == [0.0, 0.0]
    assert st.doc["headline"].startswith("AUC ")
    assert (st.dir / "roc_curve.png").exists() and (st.dir / "pr_curve.png").exists()
    assert json.loads((st.dir / "roc_points.json").read_text())["auc"] == pytest.approx(out["auc"])
    assert st.metrics["n_test"] == 3000


def test_evaluation_evidence_tables_and_charts():
    rng = np.random.default_rng(2)
    y = rng.random(2000) < 0.12
    p = np.clip(0.1 + 0.3 * y + rng.normal(0, 0.12, 2000), 0.001, 0.999)
    st = stage("evaluation")
    JY.evaluation_evidence(st, y, p, None, "toy", compare={"challenger": {"auc": 0.76}, "champion": {"auc": 0.74}})
    names = {t["name"] for t in st.doc["tables"]}
    assert {"calibration", "gain", "champion_vs_challenger"} <= names
    assert {c["path"] for c in st.doc["charts"]} >= {"calibration.png", "lift_gain.png"}
    assert "lift@10" in st.doc["headline"]


def test_selection_evidence_ranks_and_flags():
    sel = {"candidates": ["a", "b", "probe_random"], "kept": ["a", "b"], "dropped": {"probe_random": "below the random probe"},
           "tables": {"mutual_info": [{"index": "a", "mutual_info": 0.03}, {"index": "b", "mutual_info": 0.01},
                                      {"index": "probe_random", "mutual_info": 0.0}],
                      "permutation": [{"index": "a", "importance_mean": 0.02}, {"index": "b", "importance_mean": 0.004},
                                      {"index": "probe_random", "importance_mean": -0.001}]}}
    st = stage("selection")
    JY.selection_evidence(st, sel, used=["a"])
    t = st.doc["tables"][0]
    assert t["rows"][0]["feature"] == "a" and t["rows"][0]["decision"] == "keep"
    drop = next(r for r in t["rows"] if r["feature"] == "probe_random")
    assert drop["decision"] == "drop" and "probe" in drop["reason"]
    assert st.doc["headline"] == "2 kept / 3"


def test_holdout_evidence_leakage_checks():
    ts = pd.Timestamp
    w = {"train": (ts("2024-10-01"), ts("2025-12-01")), "valid": (ts("2026-01-01"), ts("2026-02-01")),
         "test": (ts("2026-03-01"), ts("2026-04-01"))}
    cmp = pd.DataFrame([
        {"strategy": "random", "n_train": 10, "n_valid": 2, "n_test": 2, "accounts_in_train_and_test": 120,
         "test_months_seen_in_train": 18, "valid_auc": .8, "test_auc": .8, "future_auc": .7, "optimism": .1},
        {"strategy": "time", "n_train": 10, "n_valid": 2, "n_test": 2, "accounts_in_train_and_test": 90,
         "test_months_seen_in_train": 0, "valid_auc": .74, "test_auc": .73, "future_auc": .72, "optimism": .01}]).set_index("strategy")
    st = stage("holdout")
    JY.holdout_evidence(st, cmp, w, pit_exact=True)
    checks = next(t for t in st.doc["tables"] if t["name"] == "leakage_checks")["rows"]
    assert all(c["result"] in ("pass", "avoided") for c in checks)
    assert st.metrics["leakage_checks_passed"] == 1.0
    assert st.params["train_to_test_gap_months"] == 3
    assert "pass" in st.doc["headline"]


def test_status_mapping_for_a_stage_left_open():
    class Info:  # minimal stand-in for an MLflow run
        status, start_time, end_time = "RUNNING", 0, None

    class Run:
        info = Info()

    assert JY._status_of_run(Run(), {"status": "done"}) == "running"
    Info.status = "FINISHED"
    assert JY._status_of_run(Run(), {"status": "skipped"}) == "skipped"
    Info.status = "FAILED"
    assert JY._status_of_run(Run(), {}) == "failed"


def test_tower_exposes_journey_routes_and_guards_artifacts():
    from fastapi.testclient import TestClient

    from bsp_mlops.tower import app as tower

    paths = {r.path for r in tower.app.routes}
    for p in ("/api/journeys", "/api/journeys/id/{journey_id}", "/api/journeys/{model}/{version}",
              "/api/journeys/artifact/{run_id}/{path:path}"):
        assert p in paths
    c = TestClient(tower.app)
    assert c.get("/api/journeys/artifact/" + "a" * 32 + "/../../etc/passwd.png").status_code in (400, 404)
    assert c.get("/api/journeys/artifact/not-a-run/x.png").status_code == 400
    assert c.get("/api/journeys/artifact/" + "a" * 32 + "/model.pkl").status_code == 415
    assert c.get("/api/journeys/other_model/1").status_code == 404
    assert c.get("/api/journeys/id/bad;id").status_code == 400


def test_missing_indicator_columns_belong_to_their_feature():
    from bsp_mlops.selection import _owner

    feats = ["dsl_retrains", "dsl_snr_margin_db", "technology"]
    assert _owner("num__missingindicator_dsl_retrains", feats) == "dsl_retrains"
    assert _owner("cat__technology_fiber", feats) == "technology"


def test_candidate_list_follows_the_two_technology_contract():
    from bsp_mlops import data

    assert set(data.TECHNOLOGIES) == {"copper", "fiber"}
    assert "line_quality_bad" in data.CANDIDATES
    for cols in data.TECH_SPECIFIC.values():
        assert set(cols) <= set(data.CANDIDATES)
    assert config.ENTITY in data.REQUIRED and "account_id" not in data.REQUIRED
