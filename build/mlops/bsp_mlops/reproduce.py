"""Reproduce a registered version from its recorded recipe and prove parity.

    python -m bsp_mlops.reproduce 14            # → mlops.reproductions + MLflow run in `churn-reproduce`

What is replayed (all read back from the version's MLflow training run):
model family + hyper-parameters, feature list, train / valid / test windows,
recency weighting, random seed. The training rows are rebuilt through the same
feature-store point-in-time join and hashed — the hash must equal the
`bsp.data_snapshot_sha256` recorded at training time. The refit model then
scores the same test window as the registered model.

Verdict:
    identical         every metric and every test-window prediction equal (|Δ| ≤ 1e-9)
    within_tolerance  metrics within REPRO_TOLERANCE (default 0.002 AUC) — e.g. a library upgrade
    diverged          anything else (the record shows which input moved: data, code, environment)

The environment check diffs the recorded `environment/requirements.lock`
against the running interpreter (added / removed / changed packages); the code
check compares `bsp.code_sha256`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd

from . import config, db, lineage

EXPERIMENT = config.env("CHURN_REPRODUCE_EXPERIMENT", "churn-reproduce")
TOLERANCE = float(config.env("REPRO_TOLERANCE", "0.002"))
METRICS = ("valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_brier", "test_lift_at_10")


def _win(s: str):
    a, b = s.split("..")
    return pd.Timestamp(a + "-01"), pd.Timestamp(b + "-01")


def recipe(version) -> dict:
    from . import registry
    from .pipeline import _coerce_param

    c = registry.client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    run = c.get_run(mv.run_id)
    p = run.data.params
    lock = None
    try:
        with tempfile.TemporaryDirectory() as td:
            lock = Path(c.download_artifacts(mv.run_id, "environment/requirements.lock", td)).read_text()
    except Exception:
        pass
    return {"version": str(version), "run_id": mv.run_id, "experiment_id": run.info.experiment_id, "model_kind": p.get("model_kind"),
            "params": {k[3:]: _coerce_param(v) for k, v in p.items() if k.startswith("hp_")},
            "features": registry.model_features(version),
            "windows": {k: _win(p[f"{k}_window"]) for k in ("train", "valid", "test") if p.get(f"{k}_window")},
            "recency_boost": float(p.get("recency_boost") or 0) or None, "seed": int(p.get("seed") or 7),
            "recorded": {k: run.data.tags.get(f"bsp.{k}") for k in ("data_snapshot_sha256", "code_sha256", "env_lock_sha256", "git_sha")},
            "metrics": {k: run.data.metrics.get(k) for k in METRICS}, "lock": lock}


def reproduce(version, by: str | None = None, verbose: bool = True) -> dict:
    from . import registry
    from .pipeline import train_and_log, training_set

    db.ensure_schema()
    v = str(version)
    rc = recipe(v)
    row = db.fetchone("insert into mlops.reproductions (model_version, source_run_id, status, created_by) values (%s,%s,'running',%s) returning id",
                      (v, rc["run_id"], by or config.user()))
    rid = row["id"]
    t0 = time.time()
    try:
        if verbose:
            print(f"   reproducing v{v}: {rc['model_kind']} · {len(rc['features'])} features · seed {rc['seed']} · windows "
                  + ", ".join(f"{k} {a:%Y-%m}..{b:%Y-%m}" for k, (a, b) in rc["windows"].items()), flush=True)
        res = train_and_log(rc["model_kind"], rc["features"], rc["windows"], params=rc["params"], experiment=EXPERIMENT,
                            run_name=f"reproduce v{v}", tags={"stage": "reproduce", "bsp.reproduces_version": v, "bsp.reproduces_run": rc["run_id"]},
                            log_model=False, log_shap=False, recency_boost=rc["recency_boost"], seed=rc["seed"], verbose=False)
        te = training_set(rc["windows"]["test"], rc["features"])
        p_orig = registry.load_version(v).predict_proba(te[rc["features"]])[:, 1]
        p_new = res["pipeline"].predict_proba(te[rc["features"]])[:, 1]
        pred_diff = float(np.max(np.abs(p_orig - p_new))) if len(te) else None
        mets = []
        for k in METRICS:
            a, b = rc["metrics"].get(k), res["metrics"].get(k)
            mets.append({"metric": k, "recorded": a, "reproduced": b, "abs_diff": None if a is None or b is None else abs(a - b)})
        diffs = [m["abs_diff"] for m in mets if m["abs_diff"] is not None]
        max_diff = max(diffs) if diffs else None
        data_match = rc["recorded"]["data_snapshot_sha256"] == res["data_snapshot_sha256"] if rc["recorded"]["data_snapshot_sha256"] else None
        code_now = lineage.code_fingerprint()["code_sha256"]
        code_match = rc["recorded"]["code_sha256"] == code_now if rc["recorded"]["code_sha256"] else None
        env_now = lineage.env_lock()
        env = lineage.env_diff(rc["lock"], env_now["lock"]) if rc["lock"] else {"identical": None, "note": "no lock recorded with this run"}
        if max_diff is not None and max_diff <= 1e-9 and (pred_diff or 0) <= 1e-9:
            parity = "identical"
        elif max_diff is not None and max_diff <= TOLERANCE:
            parity = "within_tolerance"
        else:
            parity = "diverged"
        details = {"recipe": {k: (str(v_) if k == "windows" else v_) for k, v_ in rc.items() if k not in ("lock", "metrics")},
                   "max_metric_diff": max_diff, "max_prediction_diff": pred_diff, "n_test": int(len(te)), "tolerance": TOLERANCE,
                   "data_snapshot_sha256": {"recorded": rc["recorded"]["data_snapshot_sha256"], "reproduced": res["data_snapshot_sha256"]},
                   "code_sha256": {"recorded": rc["recorded"]["code_sha256"], "current": code_now},
                   "env_lock_sha256": {"recorded": rc["recorded"]["env_lock_sha256"], "current": env_now["env_lock_sha256"]},
                   "seconds": round(time.time() - t0, 1)}
        out = db.fetchone("""update mlops.reproductions set status='succeeded', finished_at=now(), repro_run_id=%s, parity=%s,
                             data_hash_match=%s, code_match=%s, env_match=%s, metrics=%s, env_diff=%s, details=%s where id=%s returning *""",
                          (res["run_id"], parity, data_match, code_match, env.get("identical"), db.jdump(mets), db.jdump(env),
                           db.jdump(details), rid))
        try:
            c = registry.client()
            c.set_tag(res["run_id"], "bsp.parity", parity)
            c.log_dict(res["run_id"], json.loads(json.dumps({"parity": parity, "metrics": mets, "env_diff": env, **details}, default=str)),
                       "reproduce/parity.json")
            c.set_model_version_tag(config.MODEL_NAME, v, "bsp.reproduced", f"{parity} (run {res['run_id'][:8]}, record #{rid})")
        except Exception:
            pass
        db.audit("reproduce", f"{config.MODEL_NAME}:v{v}", by or config.user(), None, "automation", reproduction_id=rid, parity=parity)
        if verbose:
            print(f"   reproduction #{rid}: {parity} — max |Δmetric| {max_diff if max_diff is not None else float('nan'):.2e}, "
                  f"max |Δprediction| {pred_diff if pred_diff is not None else float('nan'):.2e}; data hash match={data_match}, "
                  f"code match={code_match}, env identical={env.get('identical')} ({details['seconds']} s)", flush=True)
        return dict(out)
    except Exception as e:
        db.execute("update mlops.reproductions set status='failed', finished_at=now(), details=%s where id=%s",
                   (db.jdump({"error": f"{e.__class__.__name__}: {str(e)[:500]}"}), rid))
        raise


def history(limit: int = 30) -> list[dict]:
    return db.fetchall("select * from mlops.reproductions order by id desc limit %s", (limit,))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.reproduce", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("version", nargs="?", help="registered version (default: the champion)")
    a = ap.parse_args(argv)
    os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
    v = a.version
    if not v:
        from . import registry

        ch = registry.alias_version("champion")
        v = str(ch.version) if ch else None
    if not v:
        print("no version given and no champion", file=sys.stderr)
        return 2
    out = reproduce(v)
    print(json.dumps({k: out[k] for k in ("id", "model_version", "parity", "data_hash_match", "code_match", "env_match")}, default=str))
    return 0 if out["parity"] != "diverged" else 1


if __name__ == "__main__":
    sys.exit(main())
