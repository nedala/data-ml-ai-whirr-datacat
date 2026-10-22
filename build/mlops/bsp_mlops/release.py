"""Release pipeline — Dev → Staging → Production for registered model versions.

Stages are emulated with MLflow model-version aliases + tags (registry stages
are deprecated in MLflow 3):

    dev          every registered version (tag bsp.stage=dev, artifact hash pinned)
    staging      alias `staging` — passed every automated release gate; also made the
                 `challenger` so it collects live shadow evidence at zero customer risk
    production   alias `champion` — needs a HUMAN approval by a user with an approver
                 role who is not the requester (four-eyes)
    archived / rejected / rolled_back   tag only

Automated gates (evaluate_gates) — each records value, threshold, pass / fail:

    artifact_integrity  sha256 of the registered model files == hash pinned at registration
    lineage             data snapshot, code and environment-lock hashes recorded on the run
    leakage_guard       no label-derived columns; every feature is a governed feature-store feature
    schema_contract     model signature == feature list == feature-store dtypes; live scoring
                        payloads: required columns, technology-aware null rates, domains
                        (technology ∈ copper/fiber, 10-digit BAN), ranges and categories vs training
    auc_floor           hold-out AUC ≥ floor
    non_regression      paired bootstrap CI (and DeLong) of AUC(candidate) − AUC(champion) on the
                        SAME hold-out rows: lower 95 % bound ≥ −margin (non-inferiority)
    calibration         ECE ≤ bound and Brier no worse than the champion by more than a bound
    segment_parity      AUC gap copper vs fiber (and across states) ≤ bound; no segment regresses
    latency_smoke       p95 single-row latency of THIS version inside the model gateway ≤ bound

The hold-out is chosen to be unseen by BOTH models: the latest labelled months
after either model's training (and the candidate's validation) window.

Every move is a row in mlops.promotions (gate results, requester, approver,
git / code / data / environment hashes, artifact hash, evidence run) and the
same record is attached to an MLflow run in experiment `churn-release`
(+ tags on the model version). Rollback re-points `champion` to the previous
APPROVED production version and is recorded the same way.

CLI (no UI needed):
    python -m bsp_mlops.release status
    python -m bsp_mlops.release gates 14 [--baseline 12]
    python -m bsp_mlops.release submit 14 --as alice --role modeler
    python -m bsp_mlops.release approve 7 --as bob --role admin --note "reviewed gates"
    python -m bsp_mlops.release reject 7 --as bob --role admin --note "calibration drifted"
    python -m bsp_mlops.release rollback --as bob --role admin --reason "incident 42"
    python -m bsp_mlops.release reconcile [--policy shadow|canary]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, db
from .config import ENTITY, LABEL, TS

RELEASE_EXPERIMENT = config.env("CHURN_RELEASE_EXPERIMENT", "churn-release")
STAGE_TAG = "bsp.stage"
THRESHOLDS = {
    "auc_floor": float(config.env("GATE_AUC_FLOOR", "0.65")),
    "noninferiority_margin": float(config.env("GATE_NONINF_MARGIN", "0.02")),
    "ece_max": float(config.env("GATE_ECE_MAX", "0.05")),
    "brier_regress_max": float(config.env("GATE_BRIER_REGRESS", "0.01")),
    "segment_gap_max": float(config.env("GATE_SEGMENT_GAP", "0.10")),
    "segment_drop_max": float(config.env("GATE_SEGMENT_DROP", "0.05")),
    "segment_min_positives": int(config.env("GATE_SEGMENT_MIN_POS", "25")),
    "latency_p95_ms": float(config.env("GATE_LATENCY_P95_MS", "100")),
    "null_rate_max": float(config.env("GATE_NULL_RATE_MAX", "0.20")),
    "out_of_range_max": float(config.env("GATE_OUT_OF_RANGE_MAX", "0.05")),
    "unseen_category_max": float(config.env("GATE_UNSEEN_CAT_MAX", "0.02")),
    "n_boot": int(config.env("GATE_N_BOOT", "1000")),
    "holdout_months": int(config.env("GATE_HOLDOUT_MONTHS", "2")),
}
APPROVER_ROLES = {r.strip() for r in config.env("MLOPS_APPROVER_ROLES", "admin").split(",") if r.strip()}
REQUESTER_MIN_ROLE = config.env("MLOPS_REQUESTER_MIN_ROLE", "analyst")
TRUSTED_SOURCES = ("sso-gate", "cli", "notebook", "automation")
FORBIDDEN_FEATURES = {LABEL, "label_complete", "churned", "churn_date", "disconnect_date", "outcome_known"}


class ReleaseError(RuntimeError):
    pass


# ═══════════════════════════════════════════════════════════════════════════
#  helpers
# ═══════════════════════════════════════════════════════════════════════════
def _client():
    from . import registry

    return registry.client()


def _mv(version):
    return _client().get_model_version(config.MODEL_NAME, str(version))


def _run_of(version):
    mv = _mv(version)
    return mv, _client().get_run(mv.run_id)


def _window(s: str | None):
    if not s or ".." not in s:
        return None
    a, b = s.split("..")
    return pd.Timestamp(a + "-01"), pd.Timestamp(b + "-01")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _gate(key: str, name: str, status: str, value=None, threshold=None, detail: str = "", blocking: bool = True, **data) -> dict:
    return {"key": key, "name": name, "status": status, "value": value, "threshold": threshold, "detail": detail,
            "blocking": blocking, "data": data}


def _f(x, nd=4):
    try:
        return None if x is None or not np.isfinite(float(x)) else round(float(x), nd)
    except (TypeError, ValueError):
        return None


def set_stage(version, stage: str) -> None:
    try:
        _client().set_model_version_tag(config.MODEL_NAME, str(version), STAGE_TAG, stage)
    except Exception:
        pass


def stage_of(mv) -> str:
    return (mv.tags or {}).get(STAGE_TAG) or "dev"


# ═══════════════════════════════════════════════════════════════════════════
#  artifact integrity
# ═══════════════════════════════════════════════════════════════════════════
@lru_cache(maxsize=32)
def artifact_sha256(version: str) -> str:
    """sha256 over every file of the registered model (relative path + bytes, sorted)."""
    import mlflow

    from .pipeline import mlflow_setup

    mlflow_setup()
    with tempfile.TemporaryDirectory() as td:
        local = Path(mlflow.artifacts.download_artifacts(f"models:/{config.MODEL_NAME}/{version}", dst_path=td))
        h = hashlib.sha256()
        for p in sorted(x for x in local.rglob("*") if x.is_file()):
            h.update(str(p.relative_to(local)).encode())
            h.update(b"\0")
            h.update(p.read_bytes())
        return h.hexdigest()


def pin_artifact(version) -> str:
    """Record the model's artifact hash on the version (done at registration)."""
    v = str(version)
    tags = _mv(v).tags or {}
    if tags.get("bsp.artifact_sha256"):
        return tags["bsp.artifact_sha256"]
    digest = artifact_sha256(v)
    _client().set_model_version_tag(config.MODEL_NAME, v, "bsp.artifact_sha256", digest)
    return digest


def register_dev(version, note: str = "") -> dict:
    """A freshly registered version enters the release process: stage dev,
    artifact hash pinned, the run's lineage hashes copied onto the version."""
    v = str(version)
    mv, run = _run_of(v)
    c = _client()
    set_stage(v, "dev")
    digest = pin_artifact(v)
    for k in ("bsp.data_snapshot_sha256", "bsp.code_sha256", "bsp.env_lock_sha256", "bsp.git_sha"):
        if run.data.tags.get(k):
            c.set_model_version_tag(config.MODEL_NAME, v, k, run.data.tags[k][:250])
    db.audit("release.register_dev", f"{config.MODEL_NAME}:v{v}", config.user(), None, "automation", artifact_sha256=digest, note=note)
    return {"version": v, "stage": "dev", "artifact_sha256": digest}


# ═══════════════════════════════════════════════════════════════════════════
#  hold-out + scoring
# ═══════════════════════════════════════════════════════════════════════════
def choose_holdout(cand_run, base_run=None, labelled: list | None = None, n_months: int | None = None) -> dict:
    """Latest labelled months unseen by BOTH models (training windows, plus the
    validation window of a model that was selected on it)."""
    from .data import windows

    n_months = n_months or THRESHOLDS["holdout_months"]
    labelled = labelled if labelled is not None else windows()["labelled"]
    labelled = [pd.Timestamp(m) for m in labelled]

    def seen_end(run):
        if run is None:
            return None
        p = run.data.params
        tr, va, te = _window(p.get("train_window")), _window(p.get("valid_window")), _window(p.get("test_window"))
        ends = [tr[1]] if tr else []
        if va and te and va != te:             # selected on validation → validation is "seen" too
            ends.append(va[1])
        return max(ends) if ends else None

    ends = [e for e in (seen_end(cand_run), seen_end(base_run)) if e is not None]
    cutoff = max(ends) if ends else None
    unseen = [m for m in labelled if cutoff is None or m > cutoff]
    flags = []
    if unseen:
        months = unseen[-n_months:]
    else:
        te = _window(cand_run.data.params.get("test_window"))
        months = [m for m in labelled if te and te[0] <= m <= te[1]] or labelled[-1:]
        flags.append("no month is unseen by both models — fell back to the candidate's test window; "
                     "the baseline may have trained on it (comparison biased towards the baseline)")
    return {"months": [m.date().isoformat() for m in months], "window": (months[0].date().isoformat(), months[-1].date().isoformat()),
            "cutoff": cutoff.date().isoformat() if cutoff is not None else None, "flags": flags}


def holdout_frame(window: tuple, features_by_version: dict[str, list[str]]) -> pd.DataFrame:
    """Hold-out rows with every model's score, plus segment columns."""
    from . import registry
    from .pipeline import training_set

    frames = []
    for v, feats in features_by_version.items():
        df = training_set(window, feats)
        pipe = registry.load_version(v)
        frames.append(df[[ENTITY, TS, LABEL]].assign(**{f"p_{v}": pipe.predict_proba(df[feats])[:, 1]}))
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f.drop(columns=[LABEL]), on=[ENTITY, TS], how="inner")
    seg = db.query_df(f"select {ENTITY}, {TS}, technology, state from features.churn_base where {TS} between %s and %s",
                      (window[0], window[1]))
    seg[TS] = pd.to_datetime(seg[TS])
    out[TS] = pd.to_datetime(out[TS])
    return out.merge(seg, on=[ENTITY, TS], how="left")


# ═══════════════════════════════════════════════════════════════════════════
#  the gates
# ═══════════════════════════════════════════════════════════════════════════
def gate_artifact(version: str, fresh: bool = False) -> dict:
    tags = _mv(version).tags or {}
    pinned = tags.get("bsp.artifact_sha256")
    now = artifact_sha256.__wrapped__(version) if fresh else artifact_sha256(version)
    if not pinned:
        _client().set_model_version_tag(config.MODEL_NAME, version, "bsp.artifact_sha256", now)
        return _gate("artifact_integrity", "Artifact integrity", "pass", now[:16], "pinned hash",
                     "No hash was pinned at registration (version predates the release process) — pinned now; later moves verify it.",
                     sha256=now, pinned_now=True)
    ok = pinned == now
    return _gate("artifact_integrity", "Artifact integrity", "pass" if ok else "fail", now[:16], pinned[:16],
                 "Registered model files hash to the value pinned at registration." if ok else
                 "MODEL FILES CHANGED since registration — refusing to promote.", sha256=now, pinned=pinned)


def gate_lineage(run) -> dict:
    t = run.data.tags
    need = {"data snapshot": t.get("bsp.data_snapshot_sha256"), "code": t.get("bsp.code_sha256"), "environment lock": t.get("bsp.env_lock_sha256")}
    missing = [k for k, v in need.items() if not v]
    return _gate("lineage", "Lineage recorded", "fail" if missing else "pass", f"{3 - len(missing)}/3", "3/3",
                 ("Missing: " + ", ".join(missing) + " — the run cannot be reproduced exactly (trained before lineage capture).")
                 if missing else "Data snapshot, code and environment-lock hashes are on the training run.",
                 **{k.replace(" ", "_"): (v or "")[:16] for k, v in need.items()}, git_sha=t.get("bsp.git_sha", "")[:40])


def gate_leakage(features: list[str]) -> dict:
    from .featurestore import all_features

    bad = sorted(f for f in features if f in FORBIDDEN_FEATURES or f.startswith("churn"))
    governed = set(all_features())
    ungoverned = sorted(f for f in features if f not in governed)
    status = "fail" if bad or ungoverned else "pass"
    detail = []
    if bad:
        detail.append(f"label-derived columns used: {', '.join(bad)}")
    if ungoverned:
        detail.append(f"features not defined in the feature store: {', '.join(ungoverned)}")
    return _gate("leakage_guard", "Leakage guard", status, len(bad) + len(ungoverned), 0,
                 "; ".join(detail) or f"All {len(features)} features are governed feature-store features built point-in-time "
                 "(as of the snapshot month, label window strictly after); no label-derived column.",
                 forbidden=bad, ungoverned=ungoverned)


def _payload_sample(n: int = 1000) -> tuple[pd.DataFrame, str]:
    rows = db.fetchall("select ban, features from mlops.inference_log where not shadow and features is not null "
                       "order by id desc limit %s", (n,))
    if len(rows) >= 50:
        df = pd.DataFrame([{**(r["features"] or {}), "ban": r["ban"]} for r in rows])
        return df, f"last {len(df)} live scoring payloads (mlops.inference_log)"
    from .featurestore import get_online_features

    ids = [r["ban"] for r in db.fetchall("select ban from features.online_features order by md5(ban) limit %s", (n,))]
    onl = get_online_features(ids)
    df = pd.DataFrame([{**{k: v for k, v in f.items() if k != "_event_ts"}, "ban": b} for b, f in onl.items()])
    return df, f"{len(df)} online-store rows (no live traffic yet)"


def gate_schema(version: str, features: list[str], reference: pd.DataFrame | None = None) -> dict:
    from mlflow.models import get_model_info

    from .data import TECH_SPECIFIC, TECHNOLOGIES
    from .featurestore import load_definitions

    problems, checks = [], []
    info = get_model_info(f"models:/{config.MODEL_NAME}/{version}")
    sig = {c["name"]: c.get("type") for c in (info.signature.inputs.to_dict() if info.signature else [])}
    if set(sig) != set(features):
        problems.append(f"signature ≠ feature list ({sorted(set(sig) ^ set(features))})")
    checks.append({"check": "signature == model feature list", "result": "pass" if set(sig) == set(features) else "fail"})
    dtypes = {f["name"]: f["dtype"] for v in load_definitions()["feature_views"] for f in v["features"]}
    mism = [f for f in features if f in sig and f in dtypes and (dtypes[f] == "string") != (sig[f] == "string")]
    if mism:
        problems.append(f"signature dtype ≠ feature-store dtype: {mism}")
    checks.append({"check": "signature dtypes == feature-store dtypes", "result": "fail" if mism else "pass", "detail": ", ".join(mism)})
    df, src = _payload_sample()
    missing = [f for f in features if f not in df.columns]
    if missing:
        problems.append(f"payload lacks {missing}")
    checks.append({"check": "payload carries every model feature", "result": "fail" if missing else "pass", "detail": ", ".join(missing)})
    tech_of = {f: t for t, fs in TECH_SPECIFIC.items() for f in fs}
    nulls = {}
    for f in features:
        if f not in df.columns:
            continue
        col = df[f]
        if f in tech_of and "technology" in df.columns:
            col = col[df["technology"] == tech_of[f]]
        nulls[f] = float(col.isna().mean()) if len(col) else 0.0
    high = {f: round(r, 3) for f, r in nulls.items() if r > THRESHOLDS["null_rate_max"] and f != "nps_last"}
    if high:
        problems.append(f"null rate > {THRESHOLDS['null_rate_max']}: {high}")
    checks.append({"check": f"null rate ≤ {THRESHOLDS['null_rate_max']} (technology-aware: ONT fields on fiber, DSL fields on copper)",
                   "result": "fail" if high else "pass", "detail": ", ".join(f"{k} {v}" for k, v in high.items())})
    if "technology" in df.columns:
        bad_t = sorted(set(df["technology"].dropna().astype(str)) - set(TECHNOLOGIES))
        if bad_t:
            problems.append(f"technology outside copper/fiber: {bad_t}")
        checks.append({"check": "technology ∈ {copper, fiber}", "result": "fail" if bad_t else "pass", "detail": ", ".join(bad_t)})
    bad_ban = int((~df["ban"].astype(str).str.fullmatch(r"\d{10}")).sum()) if "ban" in df.columns else 0
    if bad_ban:
        problems.append(f"{bad_ban} BANs not 10 digits")
    checks.append({"check": "BAN is a 10-digit string", "result": "fail" if bad_ban else "pass", "detail": str(bad_ban or "")})
    oor, unseen = {}, {}
    if reference is not None and len(reference):
        for f in features:
            if f not in df.columns or f not in reference.columns:
                continue
            if reference[f].dtype == object:
                known = set(reference[f].dropna().astype(str))
                cur = df[f].dropna().astype(str)
                r = float((~cur.isin(known)).mean()) if len(cur) else 0.0
                if r > THRESHOLDS["unseen_category_max"]:
                    unseen[f] = round(r, 3)
            else:
                lo, hi = pd.to_numeric(reference[f], errors="coerce").quantile([0.0, 1.0])
                span = (hi - lo) or 1.0
                cur = pd.to_numeric(df[f], errors="coerce").dropna()
                r = float(((cur < lo - 0.05 * span) | (cur > hi + 0.05 * span)).mean()) if len(cur) else 0.0
                if r > THRESHOLDS["out_of_range_max"]:
                    oor[f] = round(r, 3)
        checks.append({"check": f"numeric values inside the training range (± 5 %) for ≥ {1 - THRESHOLDS['out_of_range_max']:.0%} of rows",
                       "result": "warn" if oor else "pass", "detail": ", ".join(f"{k} {v}" for k, v in oor.items())})
        checks.append({"check": f"unseen categories ≤ {THRESHOLDS['unseen_category_max']:.0%}", "result": "fail" if unseen else "pass",
                       "detail": ", ".join(f"{k} {v}" for k, v in unseen.items())})
        if unseen:
            problems.append(f"unseen categories: {unseen}")
    status = "fail" if problems else "pass"
    return _gate("schema_contract", "Schema & data contract", status, f"{sum(1 for c in checks if c['result'] == 'pass')}/{len(checks)}",
                 "all checks", ("; ".join(problems) if problems else f"Scoring input contract holds on {src}.")
                 + (f" Range warnings (non-blocking): {oor}." if oor else ""), checks=checks, source=src, null_rates=nulls)


def gate_auc_floor(y, p) -> dict:
    from .stats import auc

    a = auc(y, p)
    return _gate("auc_floor", "AUC floor", "pass" if a >= THRESHOLDS["auc_floor"] else "fail", _f(a), THRESHOLDS["auc_floor"],
                 f"Hold-out AUC {a:.4f} on {len(y):,} rows ({int(np.sum(y)):,} churners).", auc=_f(a), n=int(len(y)), positives=int(np.sum(y)))


def gate_non_regression(y, p_c, p_b, base_label: str) -> dict:
    from .stats import delong, paired_bootstrap_auc

    bs = paired_bootstrap_auc(y, p_c, p_b, n_boot=THRESHOLDS["n_boot"])
    dl = delong(y, p_c, p_b)
    m = THRESHOLDS["noninferiority_margin"]
    ok = bs["ci_low"] >= -m
    verdict = "superior" if bs["ci_low"] > 0 else "non-inferior" if ok else "worse"
    return _gate("non_regression", f"Non-regression vs {base_label}", "pass" if ok else "fail", _f(bs["diff"]), f"CI low ≥ −{m}",
                 f"ΔAUC {bs['diff']:+.4f} (95 % paired bootstrap CI [{bs['ci_low']:+.4f}, {bs['ci_high']:+.4f}], {bs['n_boot']} resamples); "
                 f"DeLong z={dl['z']:.2f}, p={dl['p_value']:.3g} → {verdict}.",
                 delta_auc=_f(bs["diff"]), ci_low=_f(bs["ci_low"]), ci_high=_f(bs["ci_high"]), delong_p=_f(dl["p_value"], 6),
                 delong_z=_f(dl["z"], 3), auc_candidate=_f(dl["auc_1"]), auc_baseline=_f(dl["auc_2"]), verdict=verdict, n_boot=bs["n_boot"])


def gate_calibration(y, p_c, p_b=None) -> dict:
    from .stats import brier, ece

    e, b = ece(y, p_c), brier(y, p_c)
    eb, bb = (ece(y, p_b), brier(y, p_b)) if p_b is not None else (None, None)
    fails = []
    if e > THRESHOLDS["ece_max"]:
        fails.append(f"ECE {e:.4f} > {THRESHOLDS['ece_max']}")
    if bb is not None and b - bb > THRESHOLDS["brier_regress_max"]:
        fails.append(f"Brier {b:.4f} worse than baseline {bb:.4f} by > {THRESHOLDS['brier_regress_max']}")
    return _gate("calibration", "Calibration", "fail" if fails else "pass", _f(e), f"ECE ≤ {THRESHOLDS['ece_max']}",
                 "; ".join(fails) or f"ECE {e:.4f}, Brier {b:.4f}" + (f" (baseline ECE {eb:.4f}, Brier {bb:.4f})" if bb is not None else "") + ".",
                 ece=_f(e), brier=_f(b), ece_baseline=_f(eb), brier_baseline=_f(bb), mean_score=_f(float(np.mean(p_c))), base_rate=_f(float(np.mean(y))))


def gate_segments(df: pd.DataFrame, pc: str, pb: str | None) -> dict:
    from .stats import auc

    rows, fails, warns = [], [], []
    mp = THRESHOLDS["segment_min_positives"]
    for col in ("technology", "state"):
        if col not in df.columns:
            continue
        for seg, g in df.groupby(col):
            pos = int(g[LABEL].sum())
            r = {"segment": f"{col}={seg}", "n": len(g), "positives": pos,
                 "auc": _f(auc(g[LABEL], g[pc])) if pos >= 5 and pos < len(g) else None,
                 "auc_baseline": _f(auc(g[LABEL], g[pb])) if pb and pos >= 5 and pos < len(g) else None, "evaluated": pos >= mp}
            if r["auc"] is not None and r["auc_baseline"] is not None:
                r["delta"] = _f(r["auc"] - r["auc_baseline"])
            rows.append(r)
    for col, blocking in (("technology", True), ("state", False)):
        ev = [r for r in rows if r["segment"].startswith(col + "=") and r["evaluated"] and r["auc"] is not None]
        if len(ev) >= 2:
            gap = max(r["auc"] for r in ev) - min(r["auc"] for r in ev)
            msg = f"{col} AUC gap {gap:.3f} > {THRESHOLDS['segment_gap_max']}"
            if gap > THRESHOLDS["segment_gap_max"]:
                (fails if blocking else warns).append(msg)
        for r in ev:
            if r.get("delta") is not None and r["delta"] < -THRESHOLDS["segment_drop_max"]:
                (fails if blocking else warns).append(f"{r['segment']} regresses {r['delta']:+.3f} vs baseline")
    tech = [r for r in rows if r["segment"].startswith("technology=") and r["auc"] is not None]
    gap_t = (max(r["auc"] for r in tech) - min(r["auc"] for r in tech)) if len(tech) >= 2 else None
    status = "fail" if fails else "warn" if warns else "pass"
    return _gate("segment_parity", "Segment parity (copper / fiber, state)", status, _f(gap_t), f"gap ≤ {THRESHOLDS['segment_gap_max']}",
                 "; ".join(fails + [w + " (state: advisory)" for w in warns]) or
                 "Copper and fiber AUC within bound; no evaluated segment regresses vs the baseline "
                 f"(segments with < {mp} churners are reported, not judged).", segments=rows)


def gate_latency(version: str) -> dict:
    from . import deploy

    try:
        s = deploy.smoke(version, n=60)
    except Exception as e:
        return _gate("latency_smoke", "Latency smoke test (gateway)", "fail", None, f"p95 ≤ {THRESHOLDS['latency_p95_ms']} ms",
                     f"Smoke test failed: {str(e)[:300]}")
    ok = s["errors"] == 0 and s["p95_ms"] <= THRESHOLDS["latency_p95_ms"]
    return _gate("latency_smoke", "Latency smoke test (gateway)", "pass" if ok else "fail", s["p95_ms"], THRESHOLDS["latency_p95_ms"],
                 f"v{version} loaded in the gateway in {s['load_seconds']} s; {s['n']} single-row calls p50 {s['p50_ms']} ms / "
                 f"p95 {s['p95_ms']} ms; 200-row batch {s['batch_200_ms']} ms; {s['errors']} errors.", **s)


def evaluate_gates(version, baseline=None, log: bool = True, verbose: bool = False) -> dict:
    """Run every gate for `version` against `baseline` (default: the champion)."""
    from . import registry

    v = str(version)
    mv, run = _run_of(v)
    if baseline is None:
        ch = registry.alias_version("champion")
        baseline = str(ch.version) if ch is not None and str(ch.version) != v else None
    base = str(baseline) if baseline else None
    base_run = _run_of(base)[1] if base else None
    t0 = time.time()
    feats = {v: registry.model_features(v)}
    if base:
        feats[base] = registry.model_features(base)
    ho = choose_holdout(run, base_run)
    df = holdout_frame(ho["window"], feats)
    y = df[LABEL].astype(int).to_numpy()
    pc = df[f"p_{v}"].to_numpy()
    pb = df[f"p_{base}"].to_numpy() if base else None
    try:
        ref = registry.reference_sample(v)
    except Exception:
        ref = None
    gates = []

    def add(fn, *a):
        try:
            g = fn(*a)
        except Exception as e:
            g = _gate(fn.__name__.replace("gate_", ""), fn.__name__.replace("gate_", "").replace("_", " "), "fail", None, None,
                      f"gate errored: {e.__class__.__name__}: {str(e)[:300]}")
        gates.append(g)
        if verbose:
            print(f"     {'✔' if g['status'] in ('pass', 'skip') else '!' if g['status'] == 'warn' else '✘'} {g['name']:<40} {g['detail'][:150]}", flush=True)

    add(gate_artifact, v)
    add(gate_lineage, run)
    add(gate_leakage, feats[v])
    add(gate_schema, v, feats[v], ref)
    add(gate_auc_floor, y, pc)
    if base:
        add(gate_non_regression, y, pc, pb, f"v{base}")
    else:
        gates.append(_gate("non_regression", "Non-regression", "skip", None, None,
                           "No incumbent and no baseline given — first release; nothing to regress against.", blocking=False))
    add(gate_calibration, y, pc, pb)
    add(gate_segments, df, f"p_{v}", f"p_{base}" if base else None)
    add(gate_latency, v)
    passed = all(g["status"] in ("pass", "warn", "skip") for g in gates if g["blocking"])
    res = {"version": v, "baseline": base, "passed": passed, "gates": gates, "holdout": {**ho, "rows": int(len(df)), "positives": int(y.sum())},
           "thresholds": THRESHOLDS, "seconds": round(time.time() - t0, 1), "evaluated_at": _now(),
           "lineage": {k: run.data.tags.get(f"bsp.{k}") for k in ("data_snapshot_sha256", "code_sha256", "env_lock_sha256", "git_sha")},
           "run_id": mv.run_id}
    if log:
        try:
            res["evidence_run_id"] = _log_gate_run(res, df[[ENTITY, TS, LABEL, "technology", "state"] + [c for c in df.columns if c.startswith("p_")]])
        except Exception as e:
            res["evidence_error"] = str(e)[:300]
    return res


def _log_gate_run(res: dict, scores: pd.DataFrame) -> str:
    from .journey import link_tags
    from .pipeline import mlflow_setup

    mlflow = mlflow_setup(RELEASE_EXPERIMENT)
    jid = (_mv(res["version"]).tags or {}).get("bsp.journey_id")
    tags = {"bsp.role": "release-gates", "bsp.model_version": res["version"], "bsp.baseline_version": res["baseline"] or "none",
            "bsp.gates_passed": str(res["passed"]).lower(), **link_tags(), **({"bsp.journey_id": jid} if jid else {})}
    with mlflow.start_run(run_name=f"gates v{res['version']} vs {('v' + res['baseline']) if res['baseline'] else 'none'}", tags=tags) as r:
        mlflow.log_params({"holdout_window": f"{res['holdout']['window'][0]}..{res['holdout']['window'][1]}", "holdout_rows": res["holdout"]["rows"],
                           **{f"th_{k}": v for k, v in THRESHOLDS.items()}})
        mets = {}
        for g in res["gates"]:
            mets[f"gate_{g['key']}_pass"] = 1.0 if g["status"] in ("pass", "warn", "skip") else 0.0
            for k, val in (g.get("data") or {}).items():
                if isinstance(val, (int, float)) and not isinstance(val, bool) and np.isfinite(val):
                    mets[f"{g['key']}__{k}"[:250]] = float(val)
        mlflow.log_metrics(mets)
        mlflow.log_dict(json.loads(json.dumps(res, default=str)), "release/gates.json")
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "holdout_scores.parquet"
            scores.to_parquet(p, index=False)
            mlflow.log_artifact(str(p), "release")
        return r.info.run_id


# ═══════════════════════════════════════════════════════════════════════════
#  promotion records
# ═══════════════════════════════════════════════════════════════════════════
def _insert(rec: dict) -> dict:
    db.ensure_schema()
    cols = [c for c in rec if rec[c] is not None]
    vals = [db.jdump(rec[c]) if c in ("gates", "holdout", "details") else rec[c] for c in cols]
    return db.fetchone(f"insert into mlops.promotions ({', '.join(cols)}) values ({', '.join(['%s'] * len(cols))}) returning *", vals)


def _update(pid: int, **kw) -> dict:
    sets = ", ".join(f"{k}=%s" for k in kw) + ", updated_at=now()"
    vals = [db.jdump(v) if k in ("gates", "holdout", "details") else v for k, v in kw.items()]
    return db.fetchone(f"update mlops.promotions set {sets} where id=%s returning *", vals + [pid])


def get(pid: int) -> dict | None:
    return db.fetchone("select * from mlops.promotions where id=%s", (pid,))


def history(limit: int = 100, version: str | None = None) -> list[dict]:
    if version:
        return db.fetchall("select * from mlops.promotions where model_name=%s and model_version=%s order by id desc limit %s",
                           (config.MODEL_NAME, str(version), limit))
    return db.fetchall("select * from mlops.promotions where model_name=%s order by id desc limit %s", (config.MODEL_NAME, limit))


def pending() -> list[dict]:
    return db.fetchall("select * from mlops.promotions where model_name=%s and status='pending_approval' order by id desc",
                       (config.MODEL_NAME,))


def _attach(rec: dict) -> None:
    """Promotion record → MLflow (artifact on the gate run + tags on the version)."""
    try:
        c = _client()
        if rec.get("evidence_run_id"):
            c.log_dict(rec["evidence_run_id"], json.loads(json.dumps(rec, default=str)), f"release/promotion_{rec['id']}_{rec['status']}.json")
            c.set_tag(rec["evidence_run_id"], f"bsp.promotion_{rec['id']}", f"{rec['action']}→{rec['to_stage']}:{rec['status']}")
        v = rec["model_version"]
        c.set_model_version_tag(config.MODEL_NAME, v, "bsp.promotion_id", str(rec["id"]))
        c.set_model_version_tag(config.MODEL_NAME, v, "bsp.promotion_status", f"{rec['to_stage']}:{rec['status']}")
        if rec.get("approved_by") and rec["status"] == "approved":
            c.set_model_version_tag(config.MODEL_NAME, v, "bsp.approved_by", rec["approved_by"])
            c.set_model_version_tag(config.MODEL_NAME, v, "bsp.requested_by", rec.get("requested_by") or "")
    except Exception as e:
        print(f"   (release: could not attach record {rec.get('id')} to MLflow: {e})", flush=True)


def _check_requester(role: str | None, source: str) -> None:
    from .auth import has_role

    if source not in TRUSTED_SOURCES:
        raise ReleaseError(f"release requests need an identified caller (got source {source!r})")
    if source == "sso-gate" and not has_role(role, REQUESTER_MIN_ROLE):
        raise ReleaseError(f"requesting a release needs the {REQUESTER_MIN_ROLE} role or higher (you are {role or 'unknown'})")


def check_approver(rec: dict, approver: str, role: str | None, source: str) -> None:
    """Four-eyes: an approver role AND a different person from the requester."""
    if not approver:
        raise ReleaseError("approval needs an identified approver")
    if source not in ("sso-gate", "cli"):
        raise ReleaseError(f"production approval must come from a signed-in person (Studio SSO) or the operator CLI, not {source!r}")
    if (role or "") not in APPROVER_ROLES:
        raise ReleaseError(f"approving production needs one of the roles {sorted(APPROVER_ROLES)} (you are {role or 'unknown'})")
    if approver.strip().lower() == (rec.get("requested_by") or "").strip().lower():
        raise ReleaseError("four-eyes rule: the approver must be a different person from the requester "
                           f"({rec.get('requested_by')})")


def _lineage_cols(run) -> dict:
    t = run.data.tags
    return {"git_sha": t.get("bsp.git_sha"), "code_sha256": t.get("bsp.code_sha256"), "data_snapshot_sha256": t.get("bsp.data_snapshot_sha256"),
            "env_lock_sha256": t.get("bsp.env_lock_sha256")}


def submit(version, requested_by: str, role: str | None = None, source: str = "cli", note: str = "", baseline=None,
           request_production: bool = True, make_challenger: bool = True, verbose: bool = True) -> dict:
    """dev → staging (automated gates) → production request (pending approval)."""
    from . import registry

    _check_requester(role, source)
    v = str(version)
    mv, run = _run_of(v)
    champ = registry.alias_version("champion")
    if champ is not None and str(champ.version) == v:
        raise ReleaseError(f"v{v} is already the champion")
    if verbose:
        print(f"   release gates for v{v} (baseline {('v' + str(baseline)) if baseline else ('champion v' + champ.version) if champ else 'none'}):", flush=True)
    res = evaluate_gates(v, baseline, verbose=verbose)
    art = next((g for g in res["gates"] if g["key"] == "artifact_integrity"), {}).get("data", {}).get("sha256")
    base_rec = {"model_name": config.MODEL_NAME, "model_version": v, "action": "promote", "from_stage": stage_of(mv),
                "to_stage": "staging", "requested_by": requested_by, "requested_role": role, "requested_source": source,
                "baseline_version": res["baseline"], "champion_before": champ.version if champ else None,
                "gates": res["gates"], "gates_passed": res["passed"], "holdout": res["holdout"], "artifact_sha256": art,
                "mlflow_run_id": mv.run_id, "evidence_run_id": res.get("evidence_run_id"), "decision_note": note or None,
                "details": {"thresholds": THRESHOLDS, "seconds": res["seconds"]}, **_lineage_cols(run)}
    if not res["passed"]:
        failed = [g["name"] for g in res["gates"] if g["blocking"] and g["status"] == "fail"]
        rec = _insert({**base_rec, "status": "gates_failed", "approved_by": "release-gates (automated)", "approved_source": "automation",
                       "approved_at": _now()})
        set_stage(v, "rejected")
        _client().set_model_version_tag(config.MODEL_NAME, v, "bsp.gates", "failed")
        _attach(rec)
        from .alerts import raise_alert

        raise_alert("gate_failure", f"v{v} blocked by release gates: {', '.join(failed)}", "info",
                    {"promotion_id": rec["id"], "failed": failed}, dedupe_key=f"gates:v{v}")
        db.audit("release.gates_failed", f"{config.MODEL_NAME}:v{v}", requested_by, role, source, promotion_id=rec["id"], failed=failed)
        if verbose:
            print(f"   ✘ v{v} BLOCKED by: {', '.join(failed)} (promotion #{rec['id']})", flush=True)
        return {"promotion": rec, "gates": res, "staged": False, "pending": None}
    rec = _insert({**base_rec, "status": "completed", "approved_by": "release-gates (automated)", "approved_source": "automation",
                   "approved_at": _now()})
    c = _client()
    prev_staging = registry.alias_version("staging")
    if prev_staging is not None and str(prev_staging.version) != v:
        for p in db.fetchall("select id from mlops.promotions where model_name=%s and model_version=%s and status='pending_approval'",
                             (config.MODEL_NAME, str(prev_staging.version))):
            _update(p["id"], status="superseded", decision_note=f"superseded by v{v} (promotion #{rec['id']})")
        set_stage(prev_staging.version, "dev")
    registry.set_alias("staging", v)
    set_stage(v, "staging")
    c.set_model_version_tag(config.MODEL_NAME, v, "bsp.gates", "passed")
    if make_challenger:
        registry.set_alias("challenger", v)
    _attach(rec)
    db.audit("release.staged", f"{config.MODEL_NAME}:v{v}", requested_by, role, source, promotion_id=rec["id"])
    pend = None
    if request_production:
        pend = _insert({**{k: base_rec[k] for k in ("model_name", "model_version", "requested_by", "requested_role", "requested_source",
                                                      "baseline_version", "champion_before", "gates", "gates_passed", "holdout",
                                                      "artifact_sha256", "mlflow_run_id", "evidence_run_id", "git_sha", "code_sha256",
                                                      "data_snapshot_sha256", "env_lock_sha256")},
                        "action": "promote", "from_stage": "staging", "to_stage": "production", "status": "pending_approval",
                        "parent_id": rec["id"], "decision_note": note or None})
        _attach(pend)
        db.audit("release.requested", f"{config.MODEL_NAME}:v{v}", requested_by, role, source, promotion_id=pend["id"])
    if make_challenger:
        _reconcile(f"release: v{v} staged → challenger in shadow", actor=requested_by)
    if verbose:
        print(f"   ✔ v{v} → staging (promotion #{rec['id']}); production request #{pend['id'] if pend else '—'} awaits approval", flush=True)
    return {"promotion": rec, "gates": res, "staged": True, "pending": pend}


def approve(pid: int, approver: str, role: str | None, source: str = "cli", note: str = "", next_challenger=None,
            verbose: bool = True) -> dict:
    """staging → production. Four-eyes, re-verifies the artifact, moves `champion`,
    re-derives serving from the registry."""
    from . import registry

    rec = get(pid)
    if not rec:
        raise ReleaseError(f"no promotion #{pid}")
    if rec["status"] != "pending_approval":
        raise ReleaseError(f"promotion #{pid} is {rec['status']}, not pending approval")
    check_approver(rec, approver, role, source)
    v = rec["model_version"]
    st = registry.alias_version("staging")
    if st is None or str(st.version) != v:
        raise ReleaseError(f"v{v} is no longer the staging version (staging = {st.version if st else 'none'})")
    g = gate_artifact(v, fresh=True)
    if g["status"] != "pass":
        _update(pid, status="rejected", approved_by=approver, approved_role=role, approved_source=source, approved_at=_now(),
                decision_note="artifact hash changed between staging and approval")
        raise ReleaseError(f"artifact integrity failed at approval: {g['detail']}")
    champ = registry.alias_version("champion")
    before = champ.version if champ else None
    c = _client()
    if before:
        c.set_registered_model_tag(config.MODEL_NAME, "previous_champion", str(before))
        set_stage(before, "archived")
    registry.set_alias("champion", v)
    for a in ("staging", "challenger"):
        cur = registry.alias_version(a)
        if cur is not None and str(cur.version) == v:
            try:
                c.delete_registered_model_alias(config.MODEL_NAME, a)
            except Exception:
                pass
    if next_challenger:
        registry.set_alias("challenger", str(next_challenger))
    set_stage(v, "production")
    c.set_model_version_tag(config.MODEL_NAME, v, "promoted_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    c.set_model_version_tag(config.MODEL_NAME, v, "promotion_reason", f"approved by {approver}: {note}"[:500])
    out = _update(pid, status="approved", approved_by=approver, approved_role=role, approved_source=source, approved_at=_now(),
                  decision_note=note or rec.get("decision_note"), champion_before=before, champion_after=v)
    _attach(out)
    db.audit("release.approved", f"{config.MODEL_NAME}:v{v}", approver, role, source, promotion_id=pid, requested_by=rec["requested_by"],
             champion_before=before, champion_after=v, note=note)
    serving = _reconcile(f"release #{pid}: v{v} approved to production", actor=approver)
    if verbose:
        print(f"   ✔ promotion #{pid}: v{v} → production (champion), approved by {approver} ({role}, {source}); "
              f"requested by {rec['requested_by']}; previous champion v{before}", flush=True)
    return {"promotion": out, "serving": serving}


def reject(pid: int, approver: str, role: str | None, source: str = "cli", note: str = "") -> dict:
    from . import registry

    rec = get(pid)
    if not rec or rec["status"] != "pending_approval":
        raise ReleaseError(f"promotion #{pid} is not pending approval")
    check_approver(rec, approver, role, source)
    v = rec["model_version"]
    c = _client()
    for a in ("staging", "challenger"):
        cur = registry.alias_version(a)
        if cur is not None and str(cur.version) == v:
            try:
                c.delete_registered_model_alias(config.MODEL_NAME, a)
            except Exception:
                pass
    set_stage(v, "rejected")
    out = _update(pid, status="rejected", approved_by=approver, approved_role=role, approved_source=source, approved_at=_now(),
                  decision_note=note or "rejected")
    _attach(out)
    db.audit("release.rejected", f"{config.MODEL_NAME}:v{v}", approver, role, source, promotion_id=pid, note=note)
    _reconcile(f"release #{pid}: v{v} rejected", actor=approver)
    return {"promotion": out}


def previous_approved() -> tuple[str | None, dict | None]:
    """The production version before the current champion, if it was approved
    through the release process (or the registry's previous_champion tag)."""
    from . import registry

    champ = registry.alias_version("champion")
    cur = str(champ.version) if champ else None
    rows = db.fetchall("""select * from mlops.promotions where model_name=%s and to_stage='production'
                          and status in ('approved','completed') and champion_after=%s order by id desc limit 1""",
                       (config.MODEL_NAME, cur))
    if rows and rows[0].get("champion_before"):
        return rows[0]["champion_before"], rows[0]
    prev = registry.model_tags().get("previous_champion")
    return (str(prev) if prev and prev != cur else None), None


def rollback(actor: str, role: str | None = None, source: str = "cli", reason: str = "", verbose: bool = True) -> dict:
    """Re-point `champion` to the previous approved production version; the
    demoted version becomes the challenger (shadow) for diagnosis."""
    from . import registry

    if source == "sso-gate" and (role or "") not in APPROVER_ROLES:
        raise ReleaseError(f"rollback needs one of the roles {sorted(APPROVER_ROLES)}")
    champ = registry.alias_version("champion")
    if champ is None:
        raise ReleaseError("no champion to roll back")
    target, made_by = previous_approved()
    if not target:
        raise ReleaseError("no previous production version recorded")
    approved_before = bool(db.fetchone("""select 1 from mlops.promotions where model_name=%s and model_version=%s
                                          and to_stage='production' and status in ('approved','completed')""", (config.MODEL_NAME, target)))
    cur = str(champ.version)
    c = _client()
    registry.set_alias("champion", target)
    registry.set_alias("challenger", cur)
    c.set_registered_model_tag(config.MODEL_NAME, "previous_champion", cur)
    set_stage(target, "production")
    set_stage(cur, "rolled_back")
    _, run = _run_of(target)
    rec = _insert({"model_name": config.MODEL_NAME, "model_version": target, "action": "rollback", "from_stage": "archived",
                   "to_stage": "production", "status": "completed", "requested_by": actor, "requested_role": role,
                   "requested_source": source, "approved_by": actor, "approved_role": role, "approved_source": source,
                   "approved_at": _now(), "decision_note": reason or "rollback", "champion_before": cur, "champion_after": target,
                   "mlflow_run_id": run.info.run_id, "parent_id": made_by["id"] if made_by else None,
                   "artifact_sha256": (_mv(target).tags or {}).get("bsp.artifact_sha256"),
                   "details": {"target_was_approved": approved_before, "undoes_promotion": made_by["id"] if made_by else None},
                   **_lineage_cols(run)})
    _attach(rec)
    db.audit("release.rollback", f"{config.MODEL_NAME}:v{target}", actor, role, source, promotion_id=rec["id"], from_version=cur,
             reason=reason, target_was_approved=approved_before)
    from .alerts import raise_alert

    raise_alert("serving_divergence", f"Rollback: champion v{cur} → v{target} ({reason or 'no reason given'})", "warning",
                {"promotion_id": rec["id"]})
    serving = _reconcile(f"rollback #{rec['id']}: v{cur} → v{target}", actor=actor)
    if verbose:
        print(f"   ↩ rollback #{rec['id']}: champion v{cur} → v{target} (previous approved: {approved_before}); v{cur} kept as "
              f"challenger in shadow", flush=True)
    return {"promotion": rec, "serving": serving}


def retire_all(actor: str, reason: str, source: str = "cli") -> list[dict]:
    """Start a fresh release train: clear champion / challenger / staging aliases
    (each recorded as a `retire` promotion row — nothing is deleted)."""
    from . import registry

    out = []
    c = _client()
    for alias in ("champion", "challenger", "staging"):
        mv = registry.alias_version(alias)
        if mv is None:
            continue
        try:
            c.delete_registered_model_alias(config.MODEL_NAME, alias)
        except Exception:
            pass
        set_stage(mv.version, "archived")
        out.append(_insert({"model_name": config.MODEL_NAME, "model_version": str(mv.version), "action": "retire", "from_stage": alias,
                            "to_stage": "archived", "status": "completed", "requested_by": actor, "requested_source": source,
                            "approved_by": actor, "approved_source": source, "approved_at": _now(), "decision_note": reason,
                            "champion_before": str(mv.version) if alias == "champion" else None}))
        if alias == "champion":
            c.set_registered_model_tag(config.MODEL_NAME, "previous_champion", str(mv.version))
    for p in pending():
        _update(p["id"], status="superseded", decision_note=f"release train reset: {reason}")
    db.audit("release.retire_all", config.MODEL_NAME, actor, None, source, reason=reason, retired=[r["model_version"] for r in out])
    return out


def _reconcile(reason: str, actor: str = "release", policy: str = "shadow") -> dict | None:
    from . import deploy

    try:
        deploy.wait_alias("champion", _champion_version() or "", timeout=30)
        d = deploy.reconcile(policy, actor=actor, reason=reason)
        return {"mode": d.get("mode"), "effective": d.get("effective"), "divergence": d.get("divergence")}
    except Exception as e:
        print(f"   (release: gateway reconcile failed: {e})", flush=True)
        return None


def _champion_version() -> str | None:
    from . import registry

    ch = registry.alias_version("champion")
    return str(ch.version) if ch else None


def status() -> dict:
    from . import registry

    vers = []
    for r in registry.list_versions():
        t = r.get("tags") or {}
        vers.append({"version": r["version"], "aliases": r["aliases"], "stage": t.get(STAGE_TAG) or ("production" if "champion" in r["aliases"] else "dev"),
                     "gates": t.get("bsp.gates"), "approved_by": t.get("bsp.approved_by"), "promotion_id": t.get("bsp.promotion_id"),
                     "artifact_sha256": t.get("bsp.artifact_sha256"), "data_snapshot_sha256": t.get("bsp.data_snapshot_sha256"),
                     "model_kind": r.get("model_kind"), "test_auc": (r.get("metrics") or {}).get("test_auc"), "run_id": r["run_id"],
                     "experiment_id": r.get("experiment_id"), "frozen": t.get("frozen") == "true", "journey": bool(t.get("bsp.journey_id")),
                     "negative_control": t.get("bsp.negative_control") == "true"})
    al = registry.aliases()
    warnings = []
    if al.get("champion") and not db.fetchone("""select 1 from mlops.promotions where model_name=%s and model_version=%s
                                                 and to_stage='production' and status in ('approved','completed')""",
                                              (config.MODEL_NAME, al["champion"])):
        warnings.append(f"Champion v{al['champion']} has no approval record — it was set outside the release pipeline "
                        "(e.g. registry.promote or the MLflow UI). Submit it, or roll back.")
    if al.get("staging") and al.get("staging") == al.get("champion"):
        warnings.append("`staging` and `champion` point at the same version.")
    return {"model": config.MODEL_NAME, "aliases": al, "versions": vers, "pending": pending(), "history": history(60),
            "thresholds": THRESHOLDS, "approver_roles": sorted(APPROVER_ROLES), "requester_min_role": REQUESTER_MIN_ROLE,
            "governance_warnings": warnings}


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.release", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    g = sub.add_parser("gates")
    g.add_argument("version")
    g.add_argument("--baseline")
    s = sub.add_parser("submit")
    s.add_argument("version")
    s.add_argument("--as", dest="who", default=config.user())
    s.add_argument("--role", default="modeler")
    s.add_argument("--baseline")
    s.add_argument("--note", default="")
    for name in ("approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("promotion_id", type=int)
        p.add_argument("--as", dest="who", required=True)
        p.add_argument("--role", required=True)
        p.add_argument("--note", default="")
    r = sub.add_parser("rollback")
    r.add_argument("--as", dest="who", required=True)
    r.add_argument("--role", default="admin")
    r.add_argument("--reason", default="")
    rc = sub.add_parser("reconcile")
    rc.add_argument("--policy", default="shadow")
    a = ap.parse_args(argv)
    os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
    db.ensure_schema()
    try:
        if a.cmd == "status":
            st = status()
            print(json.dumps({"aliases": st["aliases"], "pending": [{k: p[k] for k in ("id", "model_version", "requested_by", "created_at")}
                                                                     for p in st["pending"]],
                              "versions": [{k: v[k] for k in ("version", "aliases", "stage", "gates", "approved_by")} for v in st["versions"][:15]]},
                             indent=1, default=str))
        elif a.cmd == "gates":
            res = evaluate_gates(a.version, a.baseline, verbose=True)
            print(f"passed={res['passed']} holdout={res['holdout']['window']} evidence run={res.get('evidence_run_id')}")
        elif a.cmd == "submit":
            out = submit(a.version, a.who, a.role, "cli", a.note, a.baseline)
            print(json.dumps({"promotion": out["promotion"]["id"], "staged": out["staged"], "pending": out["pending"] and out["pending"]["id"]}))
        elif a.cmd == "approve":
            out = approve(a.promotion_id, a.who, a.role, "cli", a.note)
            print(json.dumps({"promotion": out["promotion"]["id"], "status": out["promotion"]["status"]}))
        elif a.cmd == "reject":
            reject(a.promotion_id, a.who, a.role, "cli", a.note)
        elif a.cmd == "rollback":
            out = rollback(a.who, a.role, "cli", a.reason)
            print(json.dumps({"promotion": out["promotion"]["id"], "champion": out["promotion"]["champion_after"]}))
        elif a.cmd == "reconcile":
            print(json.dumps(_reconcile("manual reconcile (CLI)", policy=a.policy), indent=1, default=str))
    except ReleaseError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
