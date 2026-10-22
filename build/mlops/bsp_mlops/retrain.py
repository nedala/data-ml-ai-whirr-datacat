"""Retrain orchestrator.

    trigger(trigger="drift"|"manual"|"schedule", reason=..., drift_report_id=...)

1. records a `queued` row in mlops.retrain_runs (refused while another
   retrain runs; automatic triggers also respect a cool-down)
2. runs the training pipeline as a SUBPROCESS
       python -m bsp_mlops.pipeline retrain --retrain-id N ...
   (logs to MLflow experiment churn-retrain, registers a new version, moves
   alias `challenger` to it, evaluates champion vs challenger on the newest
   labelled month)
3. with auto-submit on (setting `auto_promote`): submits the new version to the
   release pipeline (bsp_mlops.release) — automated gates → alias `staging` +
   `challenger` in SHADOW → a production request that waits for a human
   approver (four-eyes). Nothing reaches `champion` without that approval.
   With auto-submit off: keep it as challenger and (optionally) start SHADOW.
   The AUC margin on the newest labelled month is still computed and recorded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config, db

_lock = threading.Lock()
COOLDOWN_MIN = float(os.environ.get("RETRAIN_COOLDOWN_MIN", "30"))


def running() -> dict | None:
    return db.fetchone("select * from mlops.retrain_runs where status in ('queued','running') "
                       "and created_at > now() - interval '2 hours' order by id desc limit 1")


def in_cooldown() -> bool:
    if COOLDOWN_MIN <= 0:
        return False
    r = db.fetchone("select 1 from mlops.retrain_runs where trigger <> 'manual' and created_at > now() - %s * interval '1 minute'",
                    (COOLDOWN_MIN,))
    return bool(r)


def trigger(trigger: str = "manual", reason: str = "manual retrain", drift_report_id: int | None = None,
            auto_promote: bool = True, start_shadow: bool = True, margin: float | None = None,
            wait: bool = False, recency_boost: float = 2.0) -> dict:
    db.ensure_schema()
    with _lock:
        cur = running()
        if cur:
            return {"status": "already_running", "retrain_id": cur["id"]}
        if trigger != "manual" and in_cooldown():
            return {"status": "cooldown", "cooldown_min": COOLDOWN_MIN}
        row = db.fetchone("""insert into mlops.retrain_runs (trigger, reason, drift_report_id, status, details)
                             values (%s,%s,%s,'queued',%s) returning id""",
                          (trigger, reason, drift_report_id, json.dumps({"auto_promote": auto_promote, "start_shadow": start_shadow,
                                                                          "margin": margin if margin is not None else config.PROMOTE_MARGIN})))
        rid = row["id"]
        if drift_report_id:
            db.execute("update mlops.drift_reports set triggered_retrain_id=%s where id=%s", (rid, drift_report_id))
    th = threading.Thread(target=_run, args=(rid, trigger, reason, auto_promote, start_shadow,
                                             config.PROMOTE_MARGIN if margin is None else margin, recency_boost),
                          daemon=True, name=f"retrain-{rid}")
    th.start()
    if wait:
        th.join()
        return get(rid)
    return {"status": "queued", "retrain_id": rid}


def get(rid: int) -> dict:
    return db.fetchone("select * from mlops.retrain_runs where id=%s", (rid,))


def _run(rid: int, trigger: str, reason: str, auto_promote: bool, start_shadow: bool, margin: float,
         recency_boost: float) -> None:
    config.ensure_dirs()
    logp = config.RUNS_DIR / f"retrain_{rid}.log"
    outp = config.RUNS_DIR / f"retrain_{rid}.json"
    db.execute("update mlops.retrain_runs set status='running', started_at=now(), log_path=%s where id=%s", (str(logp), rid))
    cmd = [sys.executable, "-m", "bsp_mlops.pipeline", "retrain", "--retrain-id", str(rid), "--reason", reason,
           "--trigger", trigger, "--recency-boost", str(recency_boost), "--out", str(outp)]
    try:
        with open(logp, "w") as fh:
            fh.write(f"$ {' '.join(cmd)}\n")
            fh.flush()
            proc = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, timeout=3600, env=os.environ.copy())
        if proc.returncode != 0 or not outp.exists():
            tail = Path(logp).read_text()[-1500:]
            db.execute("update mlops.retrain_runs set status='failed', finished_at=now(), decision=%s where id=%s",
                       (f"pipeline exited {proc.returncode}: {tail[-400:]}", rid))
            return
        out = json.loads(outp.read_text())
        promoted, shadow, decision = False, False, ""
        release_info: dict = {}
        if auto_promote:
            from . import release

            try:
                sub = release.submit(out["model_version"], requested_by="retrain-orchestrator", role="service", source="automation",
                                     note=f"retrain #{rid} ({trigger}): {reason[:200]}", verbose=True)
                release_info = {"stage_promotion_id": sub["promotion"]["id"], "pending_promotion_id": (sub["pending"] or {}).get("id"),
                                "gates_passed": sub["staged"]}
                if sub["staged"]:
                    shadow = True
                    decision = (f"v{out['model_version']} passed the release gates → staging + challenger in shadow; production "
                                f"request #{release_info['pending_promotion_id']} awaits a human approver "
                                f"(AUC {out['challenger_auc']:.4f} vs champion {out['champion_auc']:.4f}, margin {out['margin']:+.4f})")
                else:
                    failed = [g["name"] for g in sub["gates"]["gates"] if g["blocking"] and g["status"] == "fail"]
                    decision = f"v{out['model_version']} blocked by release gates ({', '.join(failed)}); champion unchanged"
            except Exception as e:
                decision = f"kept v{out['model_version']} as challenger; release submission failed: {e.__class__.__name__}: {str(e)[:200]}"
        else:
            decision = f"kept v{out['model_version']} as challenger (auto-submit off; margin {out['margin']:+.4f})"
            if start_shadow:
                try:
                    from . import deploy

                    deploy.reconcile("shadow", actor=f"retrain-{rid}", reason="new challenger")
                    shadow = True
                    decision += "; shadow deployment started"
                except Exception as e:
                    decision += f"; could not start shadow: {e}"
        out["release"] = release_info
        db.execute("""update mlops.retrain_runs set status='succeeded', finished_at=now(), mlflow_run_id=%s, model_version=%s,
                      champion_version=%s, challenger_auc=%s, champion_auc=%s, margin=%s, promoted=%s, shadow_started=%s,
                      decision=%s, details = coalesce(details,'{}'::jsonb) || %s::jsonb where id=%s""",
                   (out["run_id"], out["model_version"], out["champion_version"], out["challenger_auc"], out["champion_auc"],
                    out["margin"], promoted, shadow, decision, json.dumps(out), rid))
    except Exception as e:
        db.execute("update mlops.retrain_runs set status='failed', finished_at=now(), decision=%s where id=%s",
                   (f"{e.__class__.__name__}: {e}"[:500], rid))


def wait_for(rid: int, timeout: float = 1800, poll: float = 3, verbose: bool = False) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        r = get(rid)
        if verbose and r["status"] != last:
            print(f"    retrain #{rid}: {r['status']}")
            last = r["status"]
        if r["status"] in ("succeeded", "failed"):
            return r
        time.sleep(poll)
    raise TimeoutError(f"retrain #{rid} still running after {timeout}s")


def history(limit: int = 50) -> list[dict]:
    return db.fetchall("select * from mlops.retrain_runs order by id desc limit %s", (limit,))
