"""Unit tests for the release / governance layer (no services: MLflow and Postgres are not touched).

    cd build/mlops && python -m pytest -q -p no:cacheprovider tests
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
os.environ.setdefault("TOWER_SCHEDULER", "off")

from bsp_mlops import config, lineage, release, stats  # noqa: E402


# ── statistics ────────────────────────────────────────────────────────────
def _data(n=4000, seed=0, s1=0.3, s2=0.25):
    rng = np.random.default_rng(seed)
    y = (rng.random(n) < 0.08).astype(int)
    return y, s1 * y + rng.random(n), s2 * y + rng.random(n)


def test_auc_matches_sklearn_with_ties():
    from sklearn.metrics import roc_auc_score

    y, p1, _ = _data()
    p = np.round(p1, 1)                       # many ties
    assert stats.auc(y, p) == pytest.approx(roc_auc_score(y, p), abs=1e-12)


def test_delong_and_paired_bootstrap_agree():
    y, p1, p2 = _data()
    dl = stats.delong(y, p1, p2)
    bs = stats.paired_bootstrap_auc(y, p1, p2, n_boot=400)
    assert dl["diff"] == pytest.approx(stats.auc(y, p1) - stats.auc(y, p2))
    assert bs["ci_low"] < dl["diff"] < bs["ci_high"]
    assert abs(bs["boot_se"] - dl["se"]) < 0.5 * dl["se"]       # same uncertainty, two methods
    same = stats.delong(y, p1, p1)
    assert same["diff"] == 0 and same["p_value"] == 1.0


def test_delong_detects_a_clearly_better_model():
    y, p1, p2 = _data(s1=0.8, s2=0.1)
    assert stats.delong(y, p1, p2)["p_value"] < 1e-6
    assert stats.paired_bootstrap_auc(y, p1, p2, n_boot=200)["ci_low"] > 0


def test_ece_brier_and_proportions():
    rng = np.random.default_rng(3)
    p = rng.random(20000) * 0.3
    y = (rng.random(20000) < p).astype(int)     # perfectly calibrated by construction
    assert stats.ece(y, p) < 0.01
    assert stats.ece(y, np.clip(p + 0.1, 0, 1)) > 0.08
    assert stats.brier(y, p) < stats.brier(y, 1 - p)
    tp = stats.two_proportion(80, 1000, 120, 1000)
    assert tp["diff"] == pytest.approx(-0.04) and tp["p_value"] < 0.01 and tp["ci_low"] < -0.04 < tp["ci_high"]
    assert 500 < stats.power_two_proportion(0.2, -0.25) < 1500
    assert stats.lift_at(np.r_[np.ones(10), np.zeros(90)], np.r_[np.ones(10), np.zeros(90)]) == pytest.approx(10.0)


# ── lineage ───────────────────────────────────────────────────────────────
def test_data_snapshot_hash_is_order_independent_and_content_sensitive():
    df = pd.DataFrame({"ban": ["3100000001", "3100000002", "3100000003"], "snapshot_month": pd.to_datetime(["2026-01-01"] * 3),
                       "x": [1.0, 2.0, np.nan], "technology": ["fiber", "copper", "fiber"]})
    h = lineage.data_snapshot_hash(df)
    assert h == lineage.data_snapshot_hash(df.sample(frac=1, random_state=1))
    assert h == lineage.data_snapshot_hash(df[["x", "technology", "ban", "snapshot_month"]])
    d2 = df.copy()
    d2.loc[1, "x"] = 2.0001
    assert h != lineage.data_snapshot_hash(d2)


def test_env_diff_and_fingerprints():
    d = lineage.env_diff("a==1\nb==2\nc==3\n", "a==1\nb==2.1\nd==4\n")
    assert not d["identical"] and d["changed"] == [{"package": "b", "recorded": "2", "current": "2.1"}]
    assert d["added"] == ["d"] and d["removed"] == ["c"]
    assert lineage.env_diff("a==1\n", "A==1\n")["identical"]
    fp = lineage.code_fingerprint()
    assert len(fp["code_sha256"]) == 64 and fp["files"] > 20
    tags = lineage.run_tags("f" * 64)
    assert set(tags) >= {"bsp.code_sha256", "bsp.env_lock_sha256", "bsp.data_snapshot_sha256", "bsp.git_sha"}


# ── release gates (pure parts) ────────────────────────────────────────────
def _run(train, valid, test, tags=None):
    return SimpleNamespace(data=SimpleNamespace(params={"train_window": train, "valid_window": valid, "test_window": test}, tags=tags or {}))


MONTHS = [pd.Timestamp(f"2025-{m:02d}-01") for m in range(1, 13)] + [pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01")]


def test_holdout_is_unseen_by_both_models():
    cand = _run("2025-01..2025-06", "2025-07..2025-08", "2025-09..2025-10")       # tuned on valid → valid is "seen"
    base = _run("2025-01..2025-11", "2025-12..2025-12", "2025-12..2025-12")       # retrain: valid == test → only train seen
    ho = release.choose_holdout(cand, base, labelled=MONTHS, n_months=2)
    assert ho["months"] == ["2025-12-01", "2026-01-01"][-2:] or ho["months"] == ["2026-01-01", "2026-02-01"]
    assert all(pd.Timestamp(m) > pd.Timestamp("2025-11-01") for m in ho["months"]) and not ho["flags"]
    first = release.choose_holdout(cand, None, labelled=MONTHS, n_months=2)
    assert first["months"] == ["2026-01-01", "2026-02-01"]
    late = _run("2025-01..2026-02", "2026-02..2026-02", "2026-02..2026-02")
    fb = release.choose_holdout(cand, late, labelled=MONTHS)
    assert fb["flags"] and fb["months"] == ["2025-09-01", "2025-10-01"]


def test_four_eyes_rule():
    rec = {"requested_by": "data.scientist"}
    release.check_approver(rec, "release.manager", "admin", "sso-gate")
    release.check_approver(rec, "release.manager", "admin", "cli")
    for who, role, src in (("data.scientist", "admin", "sso-gate"), ("Data.Scientist ", "admin", "cli"),
                           ("release.manager", "modeler", "sso-gate"), ("release.manager", "admin", "automation"),
                           ("release.manager", "admin", "token"), ("", "admin", "sso-gate")):
        with pytest.raises(release.ReleaseError):
            release.check_approver(rec, who, role, src)


def test_requester_needs_an_identity_and_a_role():
    release._check_requester("modeler", "sso-gate")
    release._check_requester("service", "automation")
    with pytest.raises(release.ReleaseError):
        release._check_requester("viewer", "sso-gate")
    with pytest.raises(release.ReleaseError):
        release._check_requester(None, "anonymous")


def test_metric_gates():
    y, p1, p2 = _data(s1=0.5, s2=0.5, seed=4)
    assert release.gate_auc_floor(y, p1)["status"] == "pass"
    assert release.gate_auc_floor(y, np.random.default_rng(1).random(len(y)))["status"] == "fail"
    eq = release.gate_non_regression(y, p1, p1, "v1")
    assert eq["status"] == "pass" and eq["data"]["delta_auc"] == 0
    worse = release.gate_non_regression(y, np.random.default_rng(2).random(len(y)), p1, "v1")
    assert worse["status"] == "fail" and worse["data"]["ci_high"] < 0
    cal = release.gate_calibration(y, np.clip(p1 / 4, 0, 1), np.clip(p1 / 4, 0, 1))
    assert cal["key"] == "calibration" and cal["data"]["ece"] is not None
    assert release.gate_calibration(y, np.full(len(y), 0.9))["status"] == "fail"


def test_segment_gate_judges_copper_fiber_and_advises_on_states():
    rng = np.random.default_rng(5)
    n = 6000
    tech = np.where(rng.random(n) < 0.5, "fiber", "copper")
    y = (rng.random(n) < 0.1).astype(int)
    good = 0.5 * y + rng.random(n)
    blind_on_copper = np.where(tech == "copper", rng.random(n), good)
    df = pd.DataFrame({config.LABEL: y, "technology": tech, "state": np.where(rng.random(n) < 0.5, "NC", "OH"), "p_1": good, "p_2": good,
                       "p_3": blind_on_copper})
    assert release.gate_segments(df, "p_1", "p_2")["status"] == "pass"
    bad = release.gate_segments(df, "p_3", "p_1")
    assert bad["status"] == "fail" and "technology" in bad["detail"]


def test_lineage_and_leakage_gates():
    full = _run("a", "b", "c", {"bsp.data_snapshot_sha256": "d" * 64, "bsp.code_sha256": "c" * 64, "bsp.env_lock_sha256": "e" * 64})
    assert release.gate_lineage(full)["status"] == "pass"
    assert release.gate_lineage(_run("a", "b", "c"))["status"] == "fail"
    from bsp_mlops.featurestore import all_features

    feats = all_features()[:5]
    assert release.gate_leakage(feats)["status"] == "pass"
    g = release.gate_leakage(feats + [config.LABEL, "made_up_feature"])
    assert g["status"] == "fail" and config.LABEL in g["data"]["forbidden"] and "made_up_feature" in g["data"]["ungoverned"]


# ── gateway: serving derived from the registry, auth ─────────────────────
def test_divergence_is_loud_and_reconciled_state_is_clean():
    from bsp_mlops.gateway import app as gw

    reg = {"champion": "12", "challenger": "13"}
    # the situation observed live: shadow mode with stale blue/green pins
    stale = {"mode": "shadow", "blue_version": "10", "green_version": "11", "live_color": "blue", "shadow_enabled": False, "canary_weight": 0.2}
    d = gw.divergence(stale, reg, reg)
    assert [x["code"] for x in d] == ["stale_pins"]
    eff = gw.effective(stale, reg)
    assert eff["serving"][0]["version"] == "12" and eff["shadow"] == "13"
    bg = {**stale, "mode": "blue_green"}
    codes = {x["code"]: x["level"] for x in gw.divergence(bg, reg, reg)}
    assert codes["serving_not_champion"] == "error" and codes["standby_not_challenger"] == "warning"
    idle = {"mode": "single", "blue_version": None, "green_version": None, "live_color": "blue", "shadow_enabled": False}
    assert [x["code"] for x in gw.divergence(idle, reg, reg)] == ["challenger_idle"]
    clean = {**idle, "mode": "shadow", "shadow_enabled": True}
    assert gw.divergence(clean, reg, reg) == []
    lag = gw.divergence(clean, {"champion": "11", "challenger": "13"}, reg)
    assert lag and lag[0]["code"] == "hot_reload_lag"
    assert gw.divergence(clean, {}, {})[0]["code"] == "no_champion"


def test_gateway_requires_identity_or_token(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from bsp_mlops import auth
    from bsp_mlops.gateway import app as gw

    monkeypatch.setattr(auth, "TOKENS_FILE", tmp_path / "tokens.json")
    monkeypatch.setattr(auth, "SERVICE_TOKEN_FILE", tmp_path / "svc.token")
    monkeypatch.setattr(gw, "AUTH_MODE", "enforce")
    monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)
    c = TestClient(gw.app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/admin/tokens").status_code == 401
    assert c.post("/v1/predict", json={"ban": "3100000008"}).status_code == 401
    auth.ensure_service_token()
    tok = auth.service_token()
    assert (tmp_path / "svc.token").stat().st_mode & 0o077 == 0              # 0600
    assert tok not in (tmp_path / "tokens.json").read_text()                # only digests stored
    ok = c.get("/admin/tokens", headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200 and ok.json()["tokens"][0]["name"] == "service"
    assert c.get("/admin/tokens", headers={"Authorization": "Bearer nope"}).status_code == 401
    # admin writes: CSRF header + (service token or modeler/admin)
    assert c.put("/admin/deployments/churn", json={"mode": "single"}, headers={"Authorization": f"Bearer {tok}"}).status_code == 403
    client_tok = auth.issue_token("crm-batch", "admin")
    assert auth.verify_token(client_tok) == "crm-batch"
    r = c.post("/admin/reload", headers={"Authorization": f"Bearer {client_tok}", "X-Studio": "1"})
    assert r.status_code == 403                                             # a scoring client cannot change routing
    assert auth.revoke_token("crm-batch", "admin") and auth.verify_token(client_tok) is None


def test_identity_trusts_gate_headers_only_from_configured_peers(monkeypatch):
    from bsp_mlops import auth

    req = SimpleNamespace(client=SimpleNamespace(host="10.0.0.9"), headers={"x-auth-user": "alice", "x-auth-role": "admin"})
    monkeypatch.delenv("MLOPS_TRUSTED_PEERS", raising=False)
    assert auth.identity(req) == {"user": "alice", "role": "admin", "source": "sso-gate"}
    monkeypatch.setenv("MLOPS_TRUSTED_PEERS", "10.0.0.1")
    auth._facade["at"] = 0
    assert auth.identity(req)["source"] == "anonymous"


# ── tower: release endpoints refuse anonymous callers ─────────────────────
def test_tower_release_routes_need_a_person():
    from fastapi.testclient import TestClient

    from bsp_mlops.tower import app as tower

    paths = {r.path for r in tower.app.routes}
    for p in ("/api/release", "/api/release/submit", "/api/release/{pid}/approve", "/api/release/{pid}/reject", "/api/release/rollback",
              "/api/release/reconcile", "/api/reproduce", "/api/outcomes", "/api/slo", "/api/evidence", "/api/code", "/api/lifecycle",
              "/api/audit", "/api/me"):
        assert p in paths, p
    c = TestClient(tower.app)
    h = {"X-Studio": "1"}
    assert c.post("/api/release/submit", json={"version": "1"}, headers=h).status_code == 403
    assert c.post("/api/release/7/approve", json={"note": "x"}, headers=h).status_code == 403
    assert c.post("/api/release/rollback", json={}, headers=h).status_code == 403
    assert c.post("/api/registry/alias", json={"alias": "champion", "version": "1"}, headers=h).status_code == 400
    assert c.post("/api/release/submit", json={"version": "1"}).status_code == 403          # CSRF guard
    assert c.get("/api/code", params={"ref": "release:check_approver"}).json()["line"] > 0
    assert c.get("/api/code", params={"ref": "../etc/passwd"}).status_code == 400


# ── capability evidence + journey teaching ────────────────────────────────
def test_every_capability_item_is_honest_and_its_code_resolves():
    from bsp_mlops import evidence

    c = evidence.checklist(with_live=False)
    assert set(i["area"] for i in c["items"]) == set("ABCDEFGH")
    for it in c["items"]:
        assert it["status"] in evidence.STATUS_LABEL
        assert it["how"] and it["gcp"]
        for r in it["code"]:
            if r["file"].startswith("build/mlops"):
                assert r["line"], (it["id"], r)
        if it["status"] != "implemented":
            assert it.get("limits") or it["status"] == "delegated", it["id"]
    assert {o["status"] for o in c["ownership"]} <= set(evidence.STATUS_LABEL)
    md = evidence.markdown()
    assert "| C2 |" in md and "Integration boundary" in md


def test_journey_has_a_release_stage_with_teaching():
    from bsp_mlops import evidence, journey

    k = journey.STAGE_KEYS
    assert k.index("registration") < k.index("release") < k.index("deployment")
    assert "release" in journey.OPS_STAGES
    for key in k:
        t = evidence.teach(key)
        assert t and t["why"] and t["gcp"] and all(r["line"] for r in t["code"]), key


def test_retention_ab_randomisation_is_deterministic_and_balanced():
    from bsp_mlops.outcomes import _arm

    bans = [f"31{i:08d}" for i in range(4000)]
    arms = [_arm(b, "offer-202607") for b in bans]
    assert arms == [_arm(b, "offer-202607") for b in bans]
    assert 0.45 < arms.count("treatment") / len(arms) < 0.55


def test_lifecycle_cli_refuses_self_approval():
    from bsp_mlops import demo

    with pytest.raises(SystemExit):
        demo.main(["--requester", "alice", "--approver", "Alice"])


def test_export_bundle_documents_its_contents():
    src = Path(__file__).resolve().parents[1] / "bsp_mlops" / "freeze.py"
    text = src.read_text()
    for part in ("lineage/lineage.json", "release/promotions.json", "deploy/Dockerfile", "cloudrun-service.yaml", "pipelines/reproduce.sh",
                 "config/training_config.json"):
        assert part in text


def test_stratified_assignment_balances_risk():
    from bsp_mlops.outcomes import assign_arms

    rng = np.random.default_rng(9)
    df = pd.DataFrame({config.ENTITY: [f"31{i:08d}" for i in range(2000)], "score": rng.random(2000)})
    a = assign_arms(df, "offer-202607")
    assert (a == assign_arms(df, "offer-202607")).all()
    assert abs((a == "treatment").mean() - 0.5) <= 0.01
    assert abs(df.score[a == "treatment"].mean() - df.score[a == "control"].mean()) < 0.01
