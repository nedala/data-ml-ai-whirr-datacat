"""MLOps control tower (/mlops/) — UI + API + background jobs.

    uvicorn bsp_mlops.tower.app:app --host 0.0.0.0 --port 8097 --proxy-headers

Pages: Model journey · Overview · Leaderboard · Registry & Deployments ·
Release & approvals · Telemetry · Drift · Outcomes & experiments · SLO, access
& alerts · Retrains · Feature store · Scores · Frozen & export · Teach
(capability evidence + OSS / GCP ownership) — static SPA in ./static on the
shared UI kit.

Identity: the Studio gate (nginx) forwards X-Auth-User / X-Auth-Role; release
requests, approvals, rollbacks and token issuance require that identity
(bsp_mlops.auth.identity). Approval = approver role + a different person
than the requester (four-eyes, bsp_mlops.release.check_approver).

Background jobs (APScheduler):
  drift-monitor   every DRIFT_INTERVAL_MIN (default 5): PSI/KS check over the
                  recent served traffic; a breach triggers a retrain when
                  auto-retrain is on (cool-down applies)
  traffic sim     on demand (start/stop/rate/shift) — see simulator.py
  retrains        run as subprocesses by retrain.py
  label-eval      every LABEL_EVAL_MIN (default 60): delayed-label evaluation
  lifecycle       on demand: `python -m bsp_mlops.demo` as a subprocess
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, db

log = logging.getLogger("bsp.tower")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

STATIC = Path(__file__).resolve().parent / "static"
UI_KIT = Path(os.environ.get("UI_KIT_DIR", "/opt/ui-kit"))
SETTINGS_FILE = config.MLOPS_DIR / "tower_settings.json"
DEFAULT_SETTINGS = {
    "drift_interval_min": float(os.environ.get("DRIFT_INTERVAL_MIN", "5")),
    "drift_window_min": float(os.environ.get("DRIFT_WINDOW_MIN", "60")),
    "drift_last_n": int(os.environ.get("DRIFT_LAST_N", "2000")),
    "auto_retrain": os.environ.get("AUTO_RETRAIN", "true").lower() == "true",
    "auto_promote": os.environ.get("AUTO_PROMOTE", "true").lower() == "true",
    "promote_margin": config.PROMOTE_MARGIN,
    "start_shadow_on_retrain": True,
    "psi_warn": config.PSI_WARN, "psi_breach": config.PSI_BREACH, "ks_breach": config.KS_BREACH,
    "auc_drop_breach": config.AUC_DROP_BREACH, "min_breached_features": config.MIN_BREACHED_FEATURES,
}


def load_settings() -> dict:
    try:
        return {**DEFAULT_SETTINGS, **json.loads(SETTINGS_FILE.read_text())}
    except Exception:
        return dict(DEFAULT_SETTINGS)


SETTINGS = load_settings()


def save_settings() -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(SETTINGS, indent=2))
    except OSError:
        pass
    config.PSI_WARN, config.PSI_BREACH = float(SETTINGS["psi_warn"]), float(SETTINGS["psi_breach"])
    config.KS_BREACH, config.AUC_DROP_BREACH = float(SETTINGS["ks_breach"]), float(SETTINGS["auc_drop_breach"])
    config.MIN_BREACHED_FEATURES = int(SETTINGS["min_breached_features"])
    config.PROMOTE_MARGIN = float(SETTINGS["promote_margin"])


def clean(o: Any) -> Any:
    """JSON-safe: NaN/inf → None, datetimes → ISO, numpy → python."""
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, float):
        return None if math.isnan(o) or math.isinf(o) else o
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if hasattr(o, "item") and not isinstance(o, (str, bytes)):
        try:
            return clean(o.item())
        except Exception:
            return str(o)
    return o


def J(o: Any) -> JSONResponse:
    return JSONResponse(clean(o))


app = FastAPI(title="DataPlatform MLOps Control Tower", root_path="/mlops", version="1.0",
              docs_url="/api/docs", openapi_url="/api/openapi.json")
if UI_KIT.is_dir():
    app.mount("/ui-kit", StaticFiles(directory=str(UI_KIT)), name="ui-kit")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.middleware("http")
async def guard(request: Request, call_next):
    path = request.url.path
    rel = path.split("/mlops", 1)[-1] if path.startswith("/mlops") else path
    if rel.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-studio") != "1":
        return JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("x-content-type-options", "nosniff")
    resp.headers.setdefault("referrer-policy", "same-origin")
    if rel.startswith("/api/"):
        resp.headers.setdefault("cache-control", "no-store")
    elif "/static/" in rel or "/ui-kit/" in rel:
        resp.headers.setdefault("cache-control", "no-cache")
    return resp


# ── background jobs ───────────────────────────────────────────────────────
SCHED = None
JOBS: dict[str, dict] = {"drift": {"last_run": None, "last_result": None}}


def drift_job(source: str = "schedule") -> dict:
    from .. import drift, retrain

    try:
        rep = drift.run_check(minutes=SETTINGS["drift_window_min"], last_n=int(SETTINGS["drift_last_n"]), source=source)
    except Exception as e:
        rep = {"status": "error", "error": str(e)[:300]}
    JOBS["drift"].update(last_run=datetime.now(timezone.utc).isoformat(),
                         last_result={k: rep.get(k) for k in ("status", "id", "max_psi", "n_features_breached",
                                                              "prediction_psi", "breach_reasons", "n_current", "error")})
    if rep.get("breached"):
        from ..alerts import raise_alert

        raise_alert("drift", f"Drift breach on v{rep.get('model_version')}: " + "; ".join(rep["breach_reasons"])[:300], "warning",
                    {"drift_report_id": rep.get("id")}, dedupe_key=f"drift:v{rep.get('model_version')}")
    if rep.get("breached") and SETTINGS["auto_retrain"]:
        r = retrain.trigger("drift", reason="; ".join(rep["breach_reasons"]), drift_report_id=rep.get("id"),
                            auto_promote=SETTINGS["auto_promote"], start_shadow=SETTINGS["start_shadow_on_retrain"],
                            margin=SETTINGS["promote_margin"])
        rep["retrain"] = r
        log.info("drift breach → retrain %s", r)
    return rep


def label_job() -> None:
    from .. import outcomes

    try:
        outcomes.delayed_label_eval(source="schedule")
    except Exception as e:
        log.warning("delayed-label evaluation failed: %s", e)


def reschedule() -> None:
    if SCHED is None:
        return
    SCHED.add_job(drift_job, "interval", minutes=float(SETTINGS["drift_interval_min"]), id="drift-monitor",
                  replace_existing=True, max_instances=1, coalesce=True)
    SCHED.add_job(label_job, "interval", minutes=float(os.environ.get("LABEL_EVAL_MIN", "60")), id="label-eval",
                  replace_existing=True, max_instances=1, coalesce=True)


@app.on_event("startup")
def startup() -> None:
    global SCHED
    config.ensure_dirs()
    save_settings()
    for _ in range(30):
        try:
            db.ensure_schema()
            break
        except Exception as e:
            log.warning("waiting for postgres (%s)", e)
            time.sleep(2)
    try:
        db.execute("update mlops.retrain_runs set status='failed', decision='interrupted (service restart)' "
                   "where status in ('queued','running')")
        db.execute("update mlops.reproductions set status='failed', finished_at=now() where status='running'")
        db.execute("update mlops.lifecycle_runs set status='failed', finished_at=now() where status='running'")
    except Exception:
        pass
    if os.environ.get("TOWER_SCHEDULER", "on") != "off":
        from apscheduler.schedulers.background import BackgroundScheduler

        SCHED = BackgroundScheduler(timezone="UTC")
        SCHED.start()
        reschedule()


@app.on_event("shutdown")
def shutdown() -> None:
    from ..simulator import SIM

    SIM.stop()
    if SCHED:
        SCHED.shutdown(wait=False)


# ── pages ─────────────────────────────────────────────────────────────────
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html", headers={"cache-control": "no-cache"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ── gateway proxy helpers ─────────────────────────────────────────────────
def gw(method: str, path: str, **kw) -> Any:
    try:
        from ..auth import client_headers

        r = httpx.request(method, config.GATEWAY_URL.rstrip("/") + path, headers=client_headers(), timeout=kw.pop("timeout", 60), **kw)
    except Exception as e:
        raise HTTPException(502, f"model gateway unreachable: {e}")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail") if "json" in r.headers.get("content-type", "") else r.text)
    return r.json()


def actor(request: Request) -> str:
    return request.headers.get("x-auth-user") or config.user()


UNGATED_OK = os.environ.get("MLOPS_ALLOW_UNGATED_RELEASE", "").lower() == "true"     # tests / break-glass only


def who(request: Request, need: str | None = None) -> dict:
    """The caller's identity; `need` = 'person' refuses anything but a Studio-gated person."""
    from ..auth import identity

    ident = identity(request)
    if need == "person" and ident["source"] != "sso-gate":
        if UNGATED_OK and request.headers.get("x-auth-user"):
            return {"user": request.headers["x-auth-user"], "role": request.headers.get("x-auth-role") or "analyst", "source": "sso-gate"}
        raise HTTPException(403, "This action needs a signed-in person (open the control tower through Studio).")
    return ident


def _audit(request: Request, action: str, target: str = "", **details) -> None:
    ident = who(request)
    db.audit(action, target, ident.get("user") or actor(request), ident.get("role"), ident.get("source"), **details)


# ── overview ──────────────────────────────────────────────────────────────
@app.get("/api/overview")
def overview():
    from .. import registry
    from ..simulator import SIM

    out: dict[str, Any] = {"model": config.MODEL_NAME, "deployment": config.DEPLOYMENT, "mlflow": "/mlflow/"}
    try:
        out["aliases"] = registry.aliases()
    except Exception as e:
        out["aliases"], out["mlflow_error"] = {}, str(e)
    try:
        out["gateway"] = gw("GET", f"/admin/deployments/{config.DEPLOYMENT}")
    except HTTPException as e:
        out["gateway_error"] = e.detail
    out["traffic"] = db.fetchone("""select count(*) filter (where ts > now() - interval '5 minutes' and not shadow) req_5m,
        count(*) filter (where ts > now() - interval '1 minute' and not shadow) req_1m,
        count(*) filter (where shadow and ts > now() - interval '5 minutes') shadow_5m, count(*) total,
        percentile_cont(0.95) within group (order by latency_ms) filter (where ts > now() - interval '5 minutes') p95_5m
        from mlops.inference_log""")
    out["drift"] = db.fetchone("select id, created_at, breached, max_psi, prediction_psi, n_features_breached, breach_reasons "
                               "from mlops.drift_reports order by id desc limit 1")
    out["retrain"] = db.fetchone("select id, created_at, status, trigger, model_version, promoted, decision "
                                 "from mlops.retrain_runs order by id desc limit 1")
    out["frozen"] = db.fetchone("select count(*) n, max(created_at) last from mlops.frozen_artifacts")
    out["scored"] = db.fetchone("select count(*) n, max(snapshot_month) last_month, max(scored_at) scored_at "
                                "from mlops.scored_customers")
    out["simulator"] = SIM.status()
    out["pending_approvals"] = db.fetchall("select id, model_version, requested_by, created_at from mlops.promotions "
                                           "where status='pending_approval' order by id desc")
    out["alerts_open"] = db.fetchone("select count(*) n from mlops.alerts where status <> 'resolved'")
    out["lifecycle"] = db.fetchone("select id, status, requested_by, created_at, finished_at from mlops.lifecycle_runs order by id desc limit 1")
    out["jobs"] = JOBS
    out["settings"] = SETTINGS
    return J(out)


# ── leaderboard ───────────────────────────────────────────────────────────
@app.get("/api/leaderboard")
def leaderboard(include_trials: bool = False, limit: int = 300):
    from ..pipeline import search_leaderboard

    try:
        df = search_leaderboard(max_results=limit)
    except Exception as e:
        raise HTTPException(502, f"MLflow unavailable: {e}")
    if df is None or df.empty:
        return J({"runs": [], "experiments": []})
    rows = []
    for r in df.to_dict("records"):
        stage = r.get("tags.stage") or ""
        if stage == "tuning-trial" and not include_trials:
            continue
        rows.append({
            "run_id": r["run_id"], "run_name": r.get("tags.mlflow.runName"), "experiment_id": r["experiment_id"],
            "stage": stage, "model_kind": r.get("params.model_kind") or r.get("tags.model_kind"),
            "start_time": r.get("start_time"), "status": r.get("status"),
            "valid_auc": r.get("metrics.valid_auc"), "test_auc": r.get("metrics.test_auc"),
            "test_pr_auc": r.get("metrics.test_pr_auc"), "test_logloss": r.get("metrics.test_logloss"),
            "test_lift_at_10": r.get("metrics.test_lift_at_10"), "test_brier": r.get("metrics.test_brier"),
            "fit_seconds": r.get("metrics.fit_seconds"), "n_features": r.get("params.n_features"),
            "train_window": r.get("params.train_window"), "test_window": r.get("params.test_window"),
            "params": {k[10:]: v for k, v in r.items() if k.startswith("params.hp_") and v is not None},
        })
    from ..pipeline import mlflow_setup

    m = mlflow_setup()
    exps = {e.experiment_id: e.name for e in m.search_experiments()}
    for r in rows:
        r["experiment"] = exps.get(r["experiment_id"], r["experiment_id"])
    return J({"runs": rows, "experiments": sorted(set(exps.values()))})


# ── registry ──────────────────────────────────────────────────────────────
class VersionBody(BaseModel):
    version: str
    reason: str = ""


class AliasBody(BaseModel):
    alias: str
    version: str


@app.get("/api/registry")
def registry_view():
    from .. import registry

    return J({"model": config.MODEL_NAME, "versions": registry.list_versions(), "aliases": registry.aliases(),
              "tags": registry.model_tags()})


@app.post("/api/registry/promote")
def promote(b: VersionBody, request: Request):
    """Kept for old clients: a promotion is now a release REQUEST (gates → staging → approval)."""
    return release_submit(ReleaseSubmit(version=b.version, note=b.reason), request)


@app.post("/api/registry/rollback")
def registry_rollback(request: Request):
    return release_rollback(RollbackBody(reason="rollback from the registry page"), request)


@app.post("/api/registry/alias")
def set_alias(b: AliasBody, request: Request):
    from .. import registry

    if b.alias != "challenger":
        raise HTTPException(400, "only `challenger` can be set directly — `champion` and `staging` move through the release pipeline")
    registry.set_alias(b.alias, b.version)
    _audit(request, "registry.set_alias", f"{config.MODEL_NAME}:v{b.version}", alias=b.alias)
    gw("POST", "/admin/reload")
    return J({"ok": True})


# ── release pipeline (Dev → Staging → Prod) ───────────────────────────────
class ReleaseSubmit(BaseModel):
    version: str
    note: str = ""
    baseline: str | None = None


class DecisionBody(BaseModel):
    note: str = ""


class RollbackBody(BaseModel):
    reason: str = ""


@app.get("/api/me")
def me(request: Request):
    from .. import release

    ident = who(request)
    return J({**ident, "approver_roles": sorted(release.APPROVER_ROLES), "can_approve": (ident.get("role") or "") in release.APPROVER_ROLES,
              "requester_min_role": release.REQUESTER_MIN_ROLE})


@app.get("/api/release")
def release_status():
    from .. import release

    st = release.status()
    try:
        st["serving"] = gw("GET", f"/admin/deployments/{config.DEPLOYMENT}")
    except HTTPException as e:
        st["serving_error"] = e.detail
    st["audit"] = db.fetchall("select * from mlops.audit_log order by id desc limit 60")
    return J(st)


@app.get("/api/release/{pid}")
def release_one(pid: int):
    from .. import release

    r = release.get(pid)
    if not r:
        raise HTTPException(404, "no such promotion")
    r["related"] = db.fetchall("select id, action, to_stage, status, created_at from mlops.promotions where model_version=%s order by id",
                               (r["model_version"],))
    return J(r)


_release_lock = threading.Lock()


@app.post("/api/release/submit")
def release_submit(b: ReleaseSubmit, request: Request):
    from .. import release

    ident = who(request, "person")
    if not _release_lock.acquire(blocking=False):
        raise HTTPException(409, "another release action is running")
    try:
        out = release.submit(b.version, ident["user"], ident["role"], ident["source"], b.note, b.baseline, verbose=False)
    except release.ReleaseError as e:
        raise HTTPException(403, str(e))
    finally:
        _release_lock.release()
    return J({"promotion": out["promotion"], "staged": out["staged"], "pending": out["pending"],
              "gates": out["gates"]["gates"], "holdout": out["gates"]["holdout"]})


@app.post("/api/release/{pid}/approve")
def release_approve(pid: int, b: DecisionBody, request: Request):
    from .. import release

    ident = who(request, "person")
    with _release_lock:
        try:
            out = release.approve(pid, ident["user"], ident["role"], ident["source"], b.note, verbose=False)
        except release.ReleaseError as e:
            raise HTTPException(403, str(e))
    return J(out)


@app.post("/api/release/{pid}/reject")
def release_reject(pid: int, b: DecisionBody, request: Request):
    from .. import release

    ident = who(request, "person")
    try:
        return J(release.reject(pid, ident["user"], ident["role"], ident["source"], b.note))
    except release.ReleaseError as e:
        raise HTTPException(403, str(e))


@app.post("/api/release/rollback")
def release_rollback(b: RollbackBody, request: Request):
    from .. import release

    ident = who(request, "person")
    with _release_lock:
        try:
            return J(release.rollback(ident["user"], ident["role"], ident["source"], b.reason, verbose=False))
        except release.ReleaseError as e:
            raise HTTPException(409 if "no " in str(e)[:5] else 403, str(e))


class ReconcileBody(BaseModel):
    policy: str = "shadow"
    canary_weight: float | None = None


@app.post("/api/release/reconcile")
def release_reconcile(b: ReconcileBody, request: Request):
    ident = who(request)
    out = gw("POST", f"/admin/deployments/{config.DEPLOYMENT}/reconcile",
             json={"policy": b.policy, "canary_weight": b.canary_weight, "actor": ident.get("user") or actor(request),
                   "reason": "reconcile from the control tower"})
    _audit(request, "serving.reconcile", config.DEPLOYMENT, policy=b.policy)
    return J(out)


# ── reproducibility ───────────────────────────────────────────────────────
class ReproBody(BaseModel):
    version: str


@app.get("/api/reproduce")
def reproductions():
    from .. import reproduce

    return J({"runs": reproduce.history()})


@app.post("/api/reproduce")
def start_reproduce(b: ReproBody, request: Request):
    from .. import reproduce

    if not b.version.isdigit():
        raise HTTPException(400, "version must be a number")
    if db.fetchone("select 1 from mlops.reproductions where status='running' and created_at > now() - interval '30 minutes'"):
        raise HTTPException(409, "a reproduction is already running")
    by = who(request).get("user") or actor(request)

    def run():
        try:
            reproduce.reproduce(b.version, by=by, verbose=False)
        except Exception as e:
            log.warning("reproduce v%s failed: %s", b.version, e)

    threading.Thread(target=run, daemon=True, name=f"reproduce-{b.version}").start()
    return J({"status": "started", "version": b.version})


@app.get("/api/reproduce/{rid}")
def reproduction(rid: int):
    r = db.fetchone("select * from mlops.reproductions where id=%s", (rid,))
    if not r:
        raise HTTPException(404, "no such reproduction")
    return J(r)


# ── outcomes: delayed labels, champion / challenger, business A/B ─────────
@app.get("/api/outcomes")
def outcomes_view():
    from .. import outcomes

    return J({"labels": outcomes.latest_label_eval(), "experiments": outcomes.experiments(),
              "history": db.fetchall("select id, created_at, n_scored, n_labelled, n_pending, breached, source from mlops.label_evaluations "
                                     "order by id desc limit 30")})


class ABBody(BaseModel):
    effect: float = 0.25
    target_share: float = 0.3
    month: str | None = None


@app.post("/api/outcomes/{what}")
def outcomes_run(what: str, request: Request, b: ABBody | None = None):
    from .. import outcomes

    by = who(request).get("user") or actor(request)
    if what == "labels":
        return J(outcomes.delayed_label_eval(source="manual"))
    if what == "shadow":
        return J(outcomes.champion_vs_challenger(by=by))
    if what == "canary":
        return J(outcomes.canary_compare(by=by))
    if what == "ab":
        b = b or ABBody()
        if not 0 <= b.effect <= 0.9 or not 0.05 <= b.target_share <= 0.9:
            raise HTTPException(400, "effect ∈ [0, 0.9], target_share ∈ [0.05, 0.9]")
        return J(outcomes.retention_offer_ab(b.month, b.target_share, b.effect, by=by))
    raise HTTPException(404, "labels | shadow | canary | ab")


# ── SLO, access (tokens), alerts, audit ───────────────────────────────────
@app.get("/api/slo")
def slo(minutes: int = 60):
    from .. import alerts
    from ..auth import list_tokens

    minutes = max(1, min(1440, minutes))
    try:
        http = gw("GET", "/admin/slo", params={"minutes": minutes})
    except HTTPException as e:
        http = {"error": e.detail}
    inst = db.fetchone("""select count(*) n, percentile_cont(0.5) within group (order by latency_ms) p50,
                          percentile_cont(0.95) within group (order by latency_ms) p95, percentile_cont(0.99) within group (order by latency_ms) p99
                          from mlops.inference_log where not shadow and ts > now() - %s * interval '1 minute'""", (minutes,))
    callers = db.fetchall("""select coalesce(caller, '(not recorded)') caller, count(*) n from mlops.inference_log
                             where not shadow and ts > now() - %s * interval '1 minute' group by 1 order by 2 desc limit 10""", (minutes,))
    return J({"http": http, "instance": inst, "callers": callers, "alerts": alerts.history(60), "tokens": list_tokens(),
              "retries": {"client": "bsp_mlops.deploy: 3 retries on connection errors / 502 / 503 / 504, back-off 0.5 s → 1 s → 2 s; "
                                    "request_id is kept across retries", "gateway": "registry outage → keeps serving loaded versions; "
                                    "new versions pre-loaded before an alias swap; failed inference-log writes re-queued"}})


class AckBody(BaseModel):
    resolution: str = ""


@app.post("/api/alerts/{aid}/{what}")
def alert_action(aid: int, what: str, b: AckBody, request: Request):
    from .. import alerts

    ident = who(request)
    by = ident.get("user") or actor(request)
    if what == "ack":
        return J(alerts.acknowledge(aid, by, ident.get("role"), ident.get("source")))
    if what == "resolve":
        return J(alerts.resolve(aid, by, b.resolution, ident.get("role"), ident.get("source")))
    raise HTTPException(404, "ack | resolve")


class TokenBody(BaseModel):
    name: str


@app.post("/api/gateway/tokens")
def issue_token(b: TokenBody, request: Request):
    from ..auth import has_role, issue_token as issue

    ident = who(request, "person")
    if not has_role(ident["role"], "admin"):
        raise HTTPException(403, "client tokens are issued by an admin")
    try:
        tok = issue(b.name, ident["user"])
    except ValueError as e:
        raise HTTPException(400, str(e))
    _audit(request, "gateway.token.issue", b.name)
    return J({"name": b.name, "token": tok, "note": "Shown once — only its sha256 is stored."})


@app.delete("/api/gateway/tokens/{name}")
def revoke_token(name: str, request: Request):
    from ..auth import has_role, revoke_token as revoke

    ident = who(request, "person")
    if not has_role(ident["role"], "admin"):
        raise HTTPException(403, "client tokens are revoked by an admin")
    if not revoke(name, ident["user"]):
        raise HTTPException(404, "no such client token")
    _audit(request, "gateway.token.revoke", name)
    return J({"revoked": name})


@app.get("/api/audit")
def audit_log(limit: int = 200):
    return J(db.fetchall("select * from mlops.audit_log order by id desc limit %s", (max(1, min(2000, limit)),)))


# ── teach: capability evidence, ownership map, code viewer ────────────────
@app.get("/api/evidence")
def evidence_view():
    from .. import evidence

    return J(evidence.checklist())


@app.get("/api/code")
def code(ref: str, context: int = 2):
    from .. import evidence

    if not all(ch.isalnum() or ch in "._:" for ch in ref):
        raise HTTPException(400, "bad reference")
    try:
        return J(evidence.source_of(ref, max(0, min(20, context))))
    except LookupError as e:
        raise HTTPException(404, str(e))


# ── run the full lifecycle (headless script as a subprocess) ──────────────
class LifecycleBody(BaseModel):
    trials: int = 6
    fresh_release_train: bool = False
    accounts: int = 5000                   # deterministic account sample (0 = all) — keeps a run around 10 minutes
    wait_approval_min: float = 15


@app.get("/api/lifecycle")
def lifecycle_runs():
    rows = db.fetchall("select * from mlops.lifecycle_runs order by id desc limit 20")
    return J({"runs": rows, "command": "python -m bsp_mlops.demo --requester <you> [--approver <someone else> --approver-role admin]"})


@app.post("/api/lifecycle")
def lifecycle_start(b: LifecycleBody, request: Request):
    import subprocess
    import sys as _sys

    ident = who(request, "person")
    if db.fetchone("select 1 from mlops.lifecycle_runs where status='running' and created_at > now() - interval '40 minutes'"):
        raise HTTPException(409, "a lifecycle run is already running")
    row = db.fetchone("insert into mlops.lifecycle_runs (status, requested_by, args) values ('running', %s, %s) returning id",
                      (ident["user"], db.jdump(b.model_dump())))
    lid = row["id"]
    config.ensure_dirs()
    logp = config.RUNS_DIR / f"lifecycle_{lid}.log"
    cmd = [_sys.executable, "-m", "bsp_mlops.demo", "--lifecycle-id", str(lid), "--requester", ident["user"],
           "--requester-role", ident["role"] or "analyst", "--requester-source", "sso-gate", "--trials", str(max(2, min(30, b.trials))), "--no-notebook",
           "--accounts", str(max(0, b.accounts)), "--wait-approval", str(max(0.0, min(60.0, b.wait_approval_min)))]
    if b.fresh_release_train:
        cmd.append("--fresh-release-train")
    fh = open(logp, "w")
    fh.write(f"$ {' '.join(cmd)}\n")
    fh.flush()
    proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=os.environ.copy())
    db.execute("update mlops.lifecycle_runs set log_path=%s, pid=%s where id=%s", (str(logp), proc.pid, lid))
    _audit(request, "lifecycle.start", f"lifecycle:{lid}", trials=b.trials)

    def reap():
        rc = proc.wait()
        fh.close()
        db.execute("update mlops.lifecycle_runs set status=%s, finished_at=now() where id=%s and status='running'",
                   ("succeeded" if rc == 0 else "failed", lid))

    threading.Thread(target=reap, daemon=True, name=f"lifecycle-{lid}").start()
    return J({"id": lid, "status": "running", "log": f"/mlops/api/lifecycle/{lid}/log"})


@app.get("/api/lifecycle/{lid}/log", response_class=PlainTextResponse)
def lifecycle_log(lid: int):
    p = config.RUNS_DIR / f"lifecycle_{lid}.log"
    return PlainTextResponse(p.read_text()[-40000:] if p.exists() else "(no log yet)")


# ── deployment (proxy to gateway admin) ───────────────────────────────────
class DeployBody(BaseModel):
    mode: str | None = None
    canary_weight: float | None = None
    shadow_enabled: bool | None = None
    blue_version: str | None = None
    green_version: str | None = None
    live_color: str | None = None


@app.get("/api/deployment")
def deployment():
    return J(gw("GET", f"/admin/deployments/{config.DEPLOYMENT}"))


@app.put("/api/deployment")
def change_deployment(b: DeployBody, request: Request):
    body = {**b.model_dump(exclude_none=True), "actor": actor(request)}
    out = gw("PUT", f"/admin/deployments/{config.DEPLOYMENT}", json=body)
    _audit(request, "serving.configure", config.DEPLOYMENT, **b.model_dump(exclude_none=True))
    return J(out)


@app.post("/api/deployment/switch")
def switch(request: Request):
    out = gw("POST", f"/admin/deployments/{config.DEPLOYMENT}/switch", params={"actor": actor(request)})
    _audit(request, "serving.switch", config.DEPLOYMENT, live_color=out.get("live_color"))
    return J(out)


@app.post("/api/deployment/rollback")
def deployment_rollback(request: Request):
    out = gw("POST", f"/admin/deployments/{config.DEPLOYMENT}/rollback", params={"actor": actor(request)})
    _audit(request, "serving.rollback", config.DEPLOYMENT)
    return J(out)


# ── telemetry ─────────────────────────────────────────────────────────────
@app.get("/api/telemetry")
def telemetry(minutes: int = 30):
    minutes = max(1, min(1440, minutes))
    p = (config.DEPLOYMENT, minutes)
    series = db.fetchall("""select date_trunc('minute', ts) as minute, model_version, colour, shadow, count(*) n,
            percentile_cont(0.5) within group (order by latency_ms) p50, percentile_cont(0.95) within group (order by latency_ms) p95,
            avg(score) mean_score
        from mlops.inference_log where deployment=%s and ts > now() - %s * interval '1 minute'
        group by 1,2,3,4 order by 1""", p)
    split = db.fetchall("""select model_version, colour, alias, shadow, count(*) n, avg(score) mean_score,
            percentile_cont(0.5) within group (order by latency_ms) p50, percentile_cont(0.95) within group (order by latency_ms) p95
        from mlops.inference_log where deployment=%s and ts > now() - %s * interval '1 minute' group by 1,2,3,4 order by n desc""", p)
    hist = db.fetchall("""select model_version, shadow, least(width_bucket(score, 0, 1, 20), 20) b, count(*) n
        from mlops.inference_log where deployment=%s and ts > now() - %s * interval '1 minute' group by 1,2,3 order by 1,2,3""", p)
    shadow = db.fetchone("""select count(*) pairs, avg(abs(s.score - l.score)) mean_abs_diff, corr(s.score, l.score) corr,
            avg(((s.score >= 0.5) = (l.score >= 0.5))::int) agree_at_05
        from mlops.inference_log l join mlops.inference_log s on s.request_id = l.request_id and s.shadow and not l.shadow
        where l.deployment=%s and l.ts > now() - %s * interval '1 minute'""", p)
    totals = db.fetchone("""select count(*) filter (where not shadow) served, count(*) filter (where shadow) shadow,
            count(distinct ban) accounts, min(ts) first_ts, max(ts) last_ts,
            percentile_cont(0.5) within group (order by latency_ms) p50, percentile_cont(0.95) within group (order by latency_ms) p95
        from mlops.inference_log where deployment=%s and ts > now() - %s * interval '1 minute'""", p)
    recent = db.fetchall("""select ts, request_id, ban, model_version, alias, colour, mode, shadow, round(score::numeric, 4) score,
            round(latency_ms::numeric, 2) latency_ms, source from mlops.inference_log where deployment=%s order by id desc limit 50""",
                         (config.DEPLOYMENT,))
    return J({"series": series, "split": split, "hist": hist, "shadow": shadow, "totals": totals, "recent": recent,
              "minutes": minutes})


# ── drift ─────────────────────────────────────────────────────────────────
class DriftCheck(BaseModel):
    minutes: float | None = None
    last_n: int | None = None
    retrain_on_breach: bool | None = None


@app.get("/api/drift")
def drift_reports(limit: int = 60):
    rows = db.fetchall("select * from mlops.drift_reports order by id desc limit %s", (limit,))
    return J({"reports": rows, "settings": SETTINGS, "job": JOBS["drift"],
              "next_run": str(SCHED.get_job("drift-monitor").next_run_time) if SCHED and SCHED.get_job("drift-monitor") else None})


@app.post("/api/drift/check")
def drift_check(b: DriftCheck):
    from .. import drift, retrain

    rep = drift.run_check(minutes=b.minutes if b.minutes is not None else SETTINGS["drift_window_min"],
                          last_n=b.last_n or int(SETTINGS["drift_last_n"]), source="manual")
    do_retrain = SETTINGS["auto_retrain"] if b.retrain_on_breach is None else b.retrain_on_breach
    if rep.get("breached"):
        from ..alerts import raise_alert

        raise_alert("drift", f"Drift breach on v{rep.get('model_version')}: " + "; ".join(rep["breach_reasons"])[:300], "warning",
                    {"drift_report_id": rep.get("id")}, dedupe_key=f"drift:v{rep.get('model_version')}")
    if rep.get("breached") and do_retrain:
        rep["retrain"] = retrain.trigger("drift", reason="; ".join(rep["breach_reasons"]), drift_report_id=rep.get("id"),
                                         auto_promote=SETTINGS["auto_promote"],
                                         start_shadow=SETTINGS["start_shadow_on_retrain"], margin=SETTINGS["promote_margin"])
    return J(rep)


@app.get("/api/settings")
def get_settings():
    return J(SETTINGS)


@app.put("/api/settings")
def put_settings(b: dict):
    for k, v in b.items():
        if k in DEFAULT_SETTINGS:
            SETTINGS[k] = type(DEFAULT_SETTINGS[k])(v) if not isinstance(DEFAULT_SETTINGS[k], bool) else bool(v)
    save_settings()
    reschedule()
    return J(SETTINGS)


# ── retrains ──────────────────────────────────────────────────────────────
class RetrainBody(BaseModel):
    reason: str = "manual retrain from the control tower"
    auto_promote: bool | None = None
    start_shadow: bool | None = None


@app.get("/api/retrains")
def retrains(limit: int = 50):
    from .. import retrain

    return J({"runs": retrain.history(limit), "settings": SETTINGS})


@app.post("/api/retrains")
def start_retrain(b: RetrainBody, request: Request):
    from .. import retrain

    return J(retrain.trigger("manual", reason=f"{b.reason} ({actor(request)})",
                             auto_promote=SETTINGS["auto_promote"] if b.auto_promote is None else b.auto_promote,
                             start_shadow=SETTINGS["start_shadow_on_retrain"] if b.start_shadow is None else b.start_shadow,
                             margin=SETTINGS["promote_margin"]))


@app.get("/api/retrains/{rid}")
def retrain_one(rid: int):
    from .. import retrain

    r = retrain.get(rid)
    if not r:
        raise HTTPException(404, "no such retrain")
    return J(r)


@app.get("/api/retrains/{rid}/log", response_class=PlainTextResponse)
def retrain_log(rid: int):
    p = config.RUNS_DIR / f"retrain_{rid}.log"
    return PlainTextResponse(p.read_text()[-20000:] if p.exists() else "(no log yet)")


# ── frozen artifacts ──────────────────────────────────────────────────────
class FreezeBody(BaseModel):
    version: str | None = None


@app.get("/api/frozen")
def frozen():
    from .. import freeze

    art = freeze.artifactory()
    try:
        listing = art.list(config.MODEL_NAME)
    except Exception as e:
        listing = [{"error": str(e)}]
    try:   # Studio cloud identity for Artifact Registry pushes (name only — never the key)
        from bsp_common import gcp
        gcp_identity = gcp.assigned_name("artifact_registry")
    except Exception:  # noqa: BLE001
        gcp_identity = ""
    return J({"bundles": freeze.list_frozen(), "repository": art.kind, "repo": freeze.REPO, "listing": listing,
              "gcp_identity": gcp_identity})


_freeze_lock = threading.Lock()


@app.post("/api/frozen")
def do_freeze(b: FreezeBody):
    from .. import freeze

    if not _freeze_lock.acquire(blocking=False):
        raise HTTPException(409, "a freeze is already running")
    try:
        return J(freeze.freeze_and_push(b.version, verbose=False))
    finally:
        _freeze_lock.release()


@app.get("/api/frozen/{fid}/manifest")
def frozen_manifest(fid: int):
    r = db.fetchone("select bundle_name, manifest from mlops.frozen_artifacts where id=%s", (fid,))
    if not r:
        raise HTTPException(404, "no such bundle")
    return Response(json.dumps(r["manifest"], indent=2), media_type="application/json",
                    headers={"content-disposition": f'attachment; filename="{r["bundle_name"]}.manifest.json"'})


# ── feature store ─────────────────────────────────────────────────────────
@app.get("/api/features")
def features():
    from .. import featurestore

    try:
        fr = featurestore.freshness()
    except Exception as e:
        fr, err = [], str(e)
    else:
        err = None
    online = db.fetchone("select count(*) n, max(event_ts) as_of, max(materialized_at) materialized_at from features.online_features") \
        if db.table_exists("features.online_features") else None
    feast_repo = config.FEATURE_REPO_DIR / "feast" / "feature_store.yaml"
    return J({"views": fr, "online": online, "error": err, "definitions": str(featurestore.definitions_path()),
              "feast": {"repo": str(feast_repo.parent), "present": feast_repo.exists()}})


_mat_lock = threading.Lock()


@app.post("/api/features/materialize")
def materialize():
    from .. import featurestore

    if not _mat_lock.acquire(blocking=False):
        raise HTTPException(409, "materialisation already running")
    try:
        off = featurestore.apply(verbose=False)
        on = featurestore.materialize_online(verbose=False)
        return J({"offline": off, "online": on})
    finally:
        _mat_lock.release()


@app.get("/api/features/online/{ban}")
def online(ban: str):
    from .. import featurestore

    r = featurestore.get_online_features([ban])
    if not r:
        raise HTTPException(404, "account not in the online store")
    return J(r[ban])


# ── simulator ─────────────────────────────────────────────────────────────
class SimBody(BaseModel):
    action: str = "start"            # start | stop | configure
    rate: float | None = None
    shift: bool | None = None
    id_only_share: float | None = None


@app.get("/api/simulator")
def sim_status():
    from ..simulator import SIM

    return J(SIM.status())


@app.post("/api/simulator")
def sim(b: SimBody):
    from ..simulator import SIM

    if b.action == "stop":
        return J(SIM.stop())
    if b.action == "configure":
        return J(SIM.configure(b.rate, b.shift, b.id_only_share))
    return J(SIM.start(rate=b.rate, shift=b.shift, id_only_share=b.id_only_share))


# ── scoring → dataset ─────────────────────────────────────────────────────
@app.post("/api/scoring/run")
def scoring_run(last_k: int = 6):
    from .. import scoring

    return J(scoring.batch_score(last_k=last_k, verbose=False))


@app.get("/api/scoring/summary")
def scoring_summary():
    by_month = db.fetchall("""select snapshot_month, model_version, count(*) n, avg(score) mean_score,
                              count(*) filter (where risk_band='high') high_risk from mlops.scored_customers
                              group by 1,2 order by 1 desc, 2 desc limit 24""")
    outcome = db.fetchall("""select model_version, decile, count(*) n, avg(score) mean_score, avg(churned_next_90d::int) realised
                             from mlops.v_score_outcomes where outcome_known group by 1,2 order by 1,2""") \
        if db.table_exists("mlops.v_score_outcomes") else []
    top = db.fetchall("""select ban, snapshot_month, round(score::numeric,4) score, decile, risk_band, model_version,
                         reason_1, reason_2, reason_3 from mlops.scored_customers
                         where snapshot_month = (select max(snapshot_month) from mlops.scored_customers)
                         order by score desc limit 25""")
    return J({"by_month": by_month, "outcome": outcome, "top": top})


# ── model journeys ────────────────────────────────────────────────────────
JOURNEY_ART_DIR = config.MLOPS_DIR / "cache" / "journey_artifacts"
_ART_TYPES = {".png": "image/png", ".json": "application/json", ".csv": "text/csv", ".ipynb": "application/x-ipynb+json",
              ".html": "text/html", ".txt": "text/plain"}


@app.get("/api/journeys")
def journeys():
    from .. import journey

    try:
        return J(journey.list_journeys())
    except Exception as e:
        raise HTTPException(502, f"MLflow unavailable: {e}")


@app.get("/api/journeys/id/{journey_id}")
def journey_by_id(journey_id: str):
    from .. import journey

    if not journey_id.replace("-", "").isalnum():
        raise HTTPException(400, "bad journey id")
    j = journey.read(journey_id)
    if not j:
        raise HTTPException(404, "no such journey")
    return J(j)


@app.get("/api/journeys/artifact/{run_id}/{path:path}")
def journey_artifact(run_id: str, path: str):
    """Proxy a journey artifact from MLflow's artifact store (the browser never
    needs MLflow credentials). Only runs of the journey experiment, only plain
    relative paths with a known file type."""
    import re

    from .. import journey

    if not re.fullmatch(r"[0-9a-f]{32}", run_id) or ".." in path or path.startswith("/") or not re.fullmatch(r"[\w./-]+", path):
        raise HTTPException(400, "bad artifact reference")
    ext = Path(path).suffix.lower()
    if ext not in _ART_TYPES:
        raise HTTPException(415, "artifact type not served")
    dst = JOURNEY_ART_DIR / run_id / path
    if not dst.exists():
        c = journey._client()
        try:
            run = c.get_run(run_id)
        except Exception:
            raise HTTPException(404, "no such run")
        if run.info.experiment_id != journey.experiment_id():
            raise HTTPException(403, "not a journey run")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            got = c.download_artifacts(run_id, path, str(dst.parent))
        except Exception as e:
            raise HTTPException(404, f"artifact not found: {e}")
        if Path(got) != dst and Path(got).exists():
            Path(got).replace(dst)
    headers = {"cache-control": "private, max-age=86400"}
    if ext in (".ipynb", ".csv"):
        headers["content-disposition"] = f'attachment; filename="{Path(path).name}"'
    if ext == ".html":
        headers["content-security-policy"] = "sandbox"
    return FileResponse(dst, media_type=_ART_TYPES[ext], headers=headers)


@app.get("/api/journeys/{model}/{version}")
def journey_for_version(model: str, version: str):
    from .. import journey

    if model != config.MODEL_NAME or not version.isdigit():
        raise HTTPException(404, "unknown model / version")
    try:
        j = journey.for_version(model, version)
    except Exception as e:
        raise HTTPException(404, f"no such version: {e}")
    if not j:
        raise HTTPException(404, f"v{version} has no journey yet — run: python -m bsp_mlops.journey backfill")
    return J(j)


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404)
    return RedirectResponse(url="/mlops/")
