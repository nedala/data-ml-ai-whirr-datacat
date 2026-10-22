"""Model gateway — serves the churn model from the MLflow registry (/serve/).

    uvicorn bsp_mlops.gateway.app:app --host 0.0.0.0 --port 8098 --proxy-headers

Routing modes per deployment (state in mlops.deployments, changed through the
admin API by the MLOps control tower):

  single      everything → alias `champion`
  blue_green  two pinned versions (blue, green); `live_color` takes 100 % —
              switching is instant (both are pre-loaded) and reversible
  canary      `canary_weight` share → `challenger`, the rest → `champion`;
              sticky per customer (hash of ban)
  shadow      `champion` answers; `challenger` is scored asynchronously and
              only logged (shadow = true) — zero customer impact
  (`shadow_enabled` adds shadow scoring on top of single / canary /
   blue_green: the non-serving candidate is shadow-scored)

Every scored instance (served and shadow) is queued and batch-written to
mlops.inference_log. Prometheus metrics at /serve/metrics. Aliases are polled
from the registry; when `champion` / `challenger` move, the new version is
loaded in the background and swapped in (hot reload).

Serving is DERIVED from the registry: `champion` answers and `challenger` is
shadow-scored or canaried. Every deployment view carries `effective` (who
actually answers / is shadowed) and `divergence` (any disagreement between
the registry aliases and the routing config, e.g. stale blue/green pins);
POST /admin/deployments/{name}/reconcile re-derives routing from the aliases.

Authentication (GATEWAY_AUTH = enforce | monitor | off, default enforce):
scoring and admin calls need a Studio-gated identity (nginx X-Auth-User) or a
bearer token (bsp_mlops.auth). /healthz, /metrics and the docs stay open.
Admin writes additionally need X-Studio: 1 (CSRF) and the service token or a
modeler / admin role. Per-request HTTP status + latency feed /admin/slo.
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from .. import auth, config, db

os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "2")   # never block the alias watcher for minutes
os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "30")
os.environ.setdefault("MLFLOW_ALLOW_PICKLE_DESERIALIZATION", "true")  # models come only from our own gated registry
log = logging.getLogger("bsp.gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

RELOAD_SECONDS = float(os.environ.get("GATEWAY_RELOAD_SECONDS", "5"))
FLUSH_SECONDS = float(os.environ.get("GATEWAY_FLUSH_SECONDS", "1"))
MODES = ("single", "blue_green", "canary", "shadow")
UI_KIT = Path(os.environ.get("UI_KIT_DIR", "/opt/ui-kit"))

REQS = Counter("bsp_gateway_predictions_total", "Scored instances", ["deployment", "mode", "version", "colour", "shadow"])
LAT = Histogram("bsp_gateway_latency_seconds", "Per-request latency", ["deployment", "mode"],
                buckets=(.002, .005, .01, .02, .05, .1, .2, .5, 1, 2))
SCORE = Histogram("bsp_gateway_score", "Churn score distribution", ["version"],
                  buckets=tuple(np.round(np.linspace(0.05, 1.0, 20), 2)))
LOADED = Gauge("bsp_gateway_model_loaded", "Loaded model versions", ["version"])
ALIAS = Gauge("bsp_gateway_alias_version", "Registry alias → version", ["alias"])
LOGQ = Gauge("bsp_gateway_log_queue", "Inference-log rows waiting to be written")
ERRS = Counter("bsp_gateway_errors_total", "Errors", ["kind"])
HTTP = Counter("bsp_gateway_http_responses_total", "HTTP responses", ["route", "code"])
AUTH_MODE = os.environ.get("GATEWAY_AUTH", "enforce").strip().lower()
OPEN_PATHS = {"", "/", "/healthz", "/metrics", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
# SLO targets (owner: see /admin/slo) — per-instance model latency and HTTP-level reliability
SLO = {"owner": os.environ.get("SLO_OWNER", "Churn model owner (Data Science) · platform on-call (ML Platform)"),
       "p95_request_ms": float(os.environ.get("SLO_P95_MS", "250")), "availability": float(os.environ.get("SLO_AVAILABILITY", "0.995")),
       "error_rate": float(os.environ.get("SLO_ERROR_RATE", "0.005"))}
REQLOG: collections.deque = collections.deque(maxlen=200_000)    # (ts, route, status, ms, caller)


# ── model cache + alias watcher ───────────────────────────────────────────
class Models:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.cache: dict[str, dict] = {}
        self.alias: dict[str, str] = {}
        self.last_poll = 0.0
        self.events: list[dict] = []

    def load(self, version: str) -> dict:
        version = str(version)
        with self.lock:
            if version in self.cache:
                return self.cache[version]
        import mlflow.sklearn
        from mlflow.models import get_model_info

        t0 = time.time()
        uri = f"models:/{config.MODEL_NAME}/{version}"
        pipe = mlflow.sklearn.load_model(uri)
        info = get_model_info(uri)
        cols = info.signature.inputs.to_dict() if info.signature else []
        feats = [c["name"] for c in cols]
        types = {c["name"]: c.get("type", "double") for c in cols}
        m = {"version": version, "pipeline": pipe, "features": feats, "types": types,
             "loaded_at": datetime.now(timezone.utc).isoformat(), "load_seconds": round(time.time() - t0, 2),
             "run_id": info.run_id}
        with self.lock:
            self.cache[version] = m
        LOADED.labels(version).set(1)
        log.info("loaded %s v%s (%d features) in %.1fs", config.MODEL_NAME, version, len(feats), m["load_seconds"])
        return m

    def poll(self, force: bool = False) -> dict[str, str]:
        if not force and time.time() - self.last_poll < RELOAD_SECONDS:
            return self.alias
        self.last_poll = time.time()
        try:
            from mlflow import MlflowClient

            c = MlflowClient(tracking_uri=config.MLFLOW_URI, registry_uri=config.MLFLOW_URI)
            m = c.get_registered_model(config.MODEL_NAME)
            new = {k: str(v) for k, v in (m.aliases or {}).items()}
        except Exception as e:  # registry down or model not yet registered → keep serving what we have
            ERRS.labels("registry_poll").inc()
            log.debug("alias poll failed: %s", e)
            return self.alias
        for alias, ver in new.items():
            if self.alias.get(alias) != ver:
                try:
                    self.load(ver)  # pre-load BEFORE the swap → no cold request
                    self.events.append({"ts": datetime.now(timezone.utc).isoformat(), "alias": alias,
                                        "from": self.alias.get(alias), "to": ver})
                    self.events = self.events[-50:]
                    log.info("hot reload: alias %s → v%s", alias, ver)
                except Exception as e:
                    ERRS.labels("model_load").inc()
                    log.warning("could not load v%s for alias %s: %s", ver, alias, e)
                    new[alias] = self.alias.get(alias)
            ALIAS.labels(alias).set(float(new[alias]) if new[alias] else 0)
        with self.lock:
            self.alias = {k: v for k, v in new.items() if v}
        return self.alias

    def by_alias(self, alias: str) -> dict | None:
        v = self.alias.get(alias)
        return self.load(v) if v else None


MODELS = Models()


# ── deployment state (mlops.deployments) ──────────────────────────────────
class Deployments:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.state: dict[str, dict] = {}
        self.last = 0.0

    def refresh(self, force: bool = False) -> None:
        if not force and time.time() - self.last < RELOAD_SECONDS:
            return
        self.last = time.time()
        try:
            rows = db.fetchall("select * from mlops.deployments")
            with self.lock:
                self.state = {r["deployment"]: r for r in rows}
            if config.DEPLOYMENT not in self.state:
                db.execute("insert into mlops.deployments (deployment, model_name, mode, updated_by) values (%s,%s,'single','gateway') "
                           "on conflict do nothing", (config.DEPLOYMENT, config.MODEL_NAME))
                self.last = 0
        except Exception as e:
            ERRS.labels("db_refresh").inc()
            log.warning("deployment refresh failed: %s", e)

    def get(self, name: str) -> dict:
        self.refresh()
        with self.lock:
            d = self.state.get(name)
        if not d:
            if name == config.DEPLOYMENT:
                return {"deployment": name, "model_name": config.MODEL_NAME, "mode": "single", "live_color": "blue",
                        "canary_weight": 0.1, "shadow_enabled": False, "blue_version": None, "green_version": None}
            raise HTTPException(404, f"unknown deployment {name}")
        return d

    def update(self, name: str, changes: dict, actor: str, action: str) -> dict:
        cur = self.get(name)
        snapshot = {k: cur.get(k) for k in ("mode", "blue_version", "green_version", "live_color", "canary_weight",
                                            "shadow_enabled")}
        new = {**snapshot, **changes}
        if new["mode"] not in MODES:
            raise HTTPException(400, f"mode must be one of {MODES}")
        w = float(new.get("canary_weight") or 0)
        if not 0 <= w <= 1:
            raise HTTPException(400, "canary_weight must be within [0, 1]")
        if new["mode"] == "blue_green":
            al = MODELS.poll(force=True)
            new["blue_version"] = str(new.get("blue_version") or al.get("champion") or "") or None
            new["green_version"] = str(new.get("green_version") or al.get("challenger") or "") or None
            for v in (new["blue_version"], new["green_version"]):
                if v:
                    MODELS.load(v)  # both colours warm → switch is instant
        if new.get("live_color") not in ("blue", "green"):
            raise HTTPException(400, "live_color must be blue or green")
        event = {"ts": datetime.now(timezone.utc).isoformat(), "actor": actor, "action": action,
                 "before": snapshot, "after": {k: new.get(k) for k in snapshot}}
        db.execute("""update mlops.deployments set mode=%s, blue_version=%s, green_version=%s, live_color=%s,
                      canary_weight=%s, shadow_enabled=%s, updated_at=now(), updated_by=%s,
                      history = (case when jsonb_array_length(history) >= 200 then history - 0 else history end) || %s::jsonb
                      where deployment=%s""",
                   (new["mode"], new.get("blue_version"), new.get("green_version"), new["live_color"],
                    float(new.get("canary_weight") or 0), bool(new.get("shadow_enabled")), actor, json.dumps([event]), name))
        self.refresh(force=True)
        log.info("deployment %s: %s by %s → %s", name, action, actor, event["after"])
        return self.get(name)


DEPLOYMENTS = Deployments()


# ── inference log writer (batched) ────────────────────────────────────────
class LogWriter:
    COLS = ("request_id", "ts", "deployment", "mode", "model_name", "model_version", "alias", "colour", "ban",
            "snapshot_month", "features", "score", "latency_ms", "shadow", "source", "caller")

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue(maxsize=200_000)
        self.written = 0
        self.enqueued = 0
        self.dropped = 0
        threading.Thread(target=self._run, daemon=True, name="inference-log").start()

    def put(self, row: dict) -> None:
        try:
            self.q.put_nowait(row)
            self.enqueued += 1
        except queue.Full:
            self.dropped += 1
            ERRS.labels("log_dropped").inc()

    def _run(self) -> None:
        import psycopg2
        import psycopg2.extras

        conn = None
        while True:
            time.sleep(FLUSH_SECONDS)
            batch = []
            while len(batch) < 5000:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            LOGQ.set(self.q.qsize())
            if not batch:
                continue
            try:
                if conn is None or conn.closed:
                    conn = psycopg2.connect(db.dsn())
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur, f"insert into mlops.inference_log ({', '.join(self.COLS)}) values %s",
                        [tuple(r.get(c) for c in self.COLS) for r in batch], page_size=1000)
                conn.commit()
                self.written += len(batch)
            except Exception as e:
                ERRS.labels("log_write").inc()
                log.warning("inference-log write failed (%d rows re-queued): %s", len(batch), e)
                try:
                    conn and conn.close()
                except Exception:
                    pass
                conn = None
                for r in batch:
                    self.put(r)

    def flush_wait(self, timeout: float = 30) -> int:
        """Block until every row enqueued before this call is committed."""
        target = self.enqueued - self.dropped
        t0 = time.time()
        while self.written < target and time.time() - t0 < timeout:
            time.sleep(0.1)
        return max(0, target - self.written)


WRITER: LogWriter | None = None
SHADOW_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="shadow")


# ── scoring ───────────────────────────────────────────────────────────────
def _bucket(ban: str) -> float:
    return int(hashlib.md5(str(ban).encode()).hexdigest()[:8], 16) % 10000 / 10000.0


def _frame(model: dict, rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame([{f: r.get(f) for f in model["features"]} for r in rows])
    for f in model["features"]:
        t = model["types"].get(f, "double")
        if t == "string":
            df[f] = df[f].map(lambda x: None if x is None or (isinstance(x, float) and np.isnan(x)) else str(x)).astype(object)
        else:
            df[f] = pd.to_numeric(df[f].map(lambda x: float(x) if isinstance(x, bool) else x), errors="coerce").astype(float)
    return df


def _score(model: dict, rows: list[dict]) -> np.ndarray:
    return model["pipeline"].predict_proba(_frame(model, rows))[:, 1]


def route(dep: dict, ban: str) -> tuple[str, str, str]:
    """→ (version, alias-label, colour) for the instance that ANSWERS."""
    mode = dep["mode"]
    al = MODELS.alias
    champ = al.get("champion")
    chall = al.get("challenger")
    if not champ:
        raise HTTPException(503, f"no champion registered for {config.MODEL_NAME} yet")
    if mode == "blue_green":
        colour = dep.get("live_color") or "blue"
        v = dep.get(f"{colour}_version") or (champ if colour == "blue" else chall or champ)
        return str(v), ("champion" if str(v) == champ else "challenger" if str(v) == chall else f"v{v}"), colour
    if mode == "canary" and chall and chall != champ:
        if _bucket(ban) < float(dep.get("canary_weight") or 0):
            return chall, "challenger", "canary"
        return champ, "champion", "stable"
    return champ, "champion", "champion"


def shadow_target(dep: dict, served_version: str) -> str | None:
    if not (dep["mode"] == "shadow" or dep.get("shadow_enabled")):
        return None
    if dep["mode"] == "blue_green":
        other = "green" if (dep.get("live_color") or "blue") == "blue" else "blue"
        v = dep.get(f"{other}_version")
    else:
        v = MODELS.alias.get("challenger")
    return str(v) if v and str(v) != str(served_version) else None


def _jsonable(d: dict) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, (np.floating, np.integer)):
            v = v.item()
        elif isinstance(v, float) and np.isnan(v):
            v = None
        elif isinstance(v, (date, datetime)):
            v = v.isoformat()
        out[k] = v
    return out


def predict_many(instances: list[dict], deployment: str, source: str, caller: str | None = None) -> list[dict]:
    t0 = time.perf_counter()
    if not MODELS.alias:
        MODELS.poll(force=True)
    dep = DEPLOYMENTS.get(deployment)
    # online features for id-only requests
    need = [i for i in instances if not i.get("features")]
    online: dict[str, dict] = {}
    if need:
        from ..featurestore import get_online_features

        online = get_online_features([i["ban"] for i in need])
    rows, meta = [], []
    for inst in instances:
        acct = str(inst.get("ban") or "")
        feats = dict(inst.get("features") or online.get(acct) or {})
        if not feats:
            raise HTTPException(422, f"no features supplied and none in the online store for {acct!r}")
        snap = inst.get("snapshot_month") or feats.pop("_event_ts", None)
        feats.pop("_event_ts", None)
        v, alias, colour = route(dep, acct)
        rows.append(feats)
        meta.append({"ban": acct, "version": v, "alias": alias, "colour": colour, "snapshot_month": snap,
                     "request_id": inst.get("request_id") or uuid.uuid4().hex, "feature_source": "request" if inst.get("features") else "online_store"})
    scores = np.zeros(len(rows))
    for v in sorted({m["version"] for m in meta}):
        idx = [i for i, m in enumerate(meta) if m["version"] == v]
        scores[idx] = _score(MODELS.load(v), [rows[i] for i in idx])
    elapsed = (time.perf_counter() - t0) * 1000
    per = elapsed / max(1, len(rows))
    now = datetime.now(timezone.utc)
    out = []
    for i, m in enumerate(meta):
        s = float(scores[i])
        out.append({"request_id": m["request_id"], "ban": m["ban"], "score": round(s, 6),
                    "model_name": config.MODEL_NAME, "model_version": m["version"], "alias": m["alias"],
                    "colour": m["colour"], "mode": dep["mode"], "feature_source": m["feature_source"],
                    "latency_ms": round(per, 3)})
        REQS.labels(deployment, dep["mode"], m["version"], m["colour"], "false").inc()
        SCORE.labels(m["version"]).observe(s)
        if WRITER:
            WRITER.put({"request_id": m["request_id"], "ts": now, "deployment": deployment, "mode": dep["mode"],
                        "model_name": config.MODEL_NAME, "model_version": m["version"], "alias": m["alias"],
                        "colour": m["colour"], "ban": m["ban"], "snapshot_month": m["snapshot_month"],
                        "features": json.dumps(_jsonable(rows[i])), "score": s, "latency_ms": per, "shadow": False,
                        "source": source, "caller": caller})
    LAT.labels(deployment, dep["mode"]).observe(elapsed / 1000)
    # shadow scoring off the request path
    by_shadow: dict[str, list[int]] = {}
    for i, m in enumerate(meta):
        sv = shadow_target(dep, m["version"])
        if sv:
            by_shadow.setdefault(sv, []).append(i)
    for sv, idx in by_shadow.items():
        SHADOW_POOL.submit(_shadow, deployment, dep["mode"], sv, [rows[i] for i in idx], [meta[i] for i in idx], source, caller)
    return out


def _shadow(deployment: str, mode: str, version: str, rows: list[dict], meta: list[dict], source: str,
            caller: str | None = None) -> None:
    try:
        t0 = time.perf_counter()
        sc = _score(MODELS.load(version), rows)
        per = (time.perf_counter() - t0) * 1000 / max(1, len(rows))
        alias = "challenger" if MODELS.alias.get("challenger") == version else f"v{version}"
        now = datetime.now(timezone.utc)
        for r, m, s in zip(rows, meta, sc):
            REQS.labels(deployment, mode, version, "shadow", "true").inc()
            if WRITER:
                WRITER.put({"request_id": m["request_id"], "ts": now, "deployment": deployment, "mode": mode,
                            "model_name": config.MODEL_NAME, "model_version": version, "alias": alias, "colour": "shadow",
                            "ban": m["ban"], "snapshot_month": m["snapshot_month"],
                            "features": json.dumps(_jsonable(r)), "score": float(s), "latency_ms": per, "shadow": True,
                            "source": source, "caller": caller})
    except Exception as e:
        ERRS.labels("shadow").inc()
        log.warning("shadow scoring v%s failed: %s", version, e)


# ── API ───────────────────────────────────────────────────────────────────
app = FastAPI(title="DataPlatform Model Gateway", root_path="/serve", version="1.0",
              description="Churn model serving with single / blue-green / canary / shadow routing. "
                          "Every inference is logged to mlops.inference_log.")
if UI_KIT.is_dir():
    app.mount("/ui-kit", StaticFiles(directory=str(UI_KIT)), name="ui-kit")


def _rel(path: str) -> str:
    return path.rstrip("/").split("/serve", 1)[-1] if path.startswith("/serve") else path.rstrip("/")


@app.middleware("http")
async def guard(request: Request, call_next):
    """Authentication + CSRF + per-request SLO accounting."""
    rel = _rel(request.url.path)
    ident = auth.identity(request) if AUTH_MODE != "off" else {"user": "auth-off", "role": "service", "source": "off"}
    request.state.ident = ident
    open_path = rel in OPEN_PATHS or rel.startswith("/ui-kit")
    if not open_path and ident["source"] == "anonymous":
        ERRS.labels("unauthenticated").inc()
        if AUTH_MODE == "enforce":
            HTTP.labels(rel.split("/")[1] if "/" in rel else rel, "401").inc()
            return JSONResponse({"detail": "Authentication required: sign in through Studio (/serve/ via the front door) "
                                 "or send 'Authorization: Bearer <token>' (tokens: MLOps control tower → SLO & access)."},
                                status_code=401, headers={"WWW-Authenticate": "Bearer"})
    if rel.startswith("/admin") and request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-studio") != "1":
            return JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
        if AUTH_MODE == "enforce" and not (ident["source"] == "token" and ident["role"] == "service") \
                and not (ident["source"] == "sso-gate" and auth.has_role(ident["role"], "modeler")):
            return JSONResponse({"detail": "Admin changes need the service token or the modeler / admin role."}, status_code=403)
    t0 = time.perf_counter()
    code = 500
    try:
        resp = await call_next(request)
        code = resp.status_code
        return resp
    finally:
        if rel.startswith("/v1/") or rel.startswith("/admin/smoke"):
            route = rel if not rel.startswith("/admin/smoke") else "/admin/smoke"
            HTTP.labels(route, str(code)).inc()
            REQLOG.append((time.time(), route, code, (time.perf_counter() - t0) * 1000, ident.get("user")))


@app.on_event("startup")
def _startup() -> None:
    global WRITER
    for i in range(30):
        try:
            db.ensure_schema()
            break
        except Exception as e:
            log.warning("waiting for postgres (%s)", e)
            time.sleep(2)
    try:
        auth.ensure_service_token()
    except Exception as e:   # no writable data dir (tests) → tokens unavailable, gate identities still work
        log.warning("service token unavailable: %s", e)
    log.info("gateway auth mode: %s", AUTH_MODE)
    WRITER = LogWriter()
    DEPLOYMENTS.refresh(force=True)

    def watcher():
        while True:
            try:
                MODELS.poll(force=True)
                DEPLOYMENTS.refresh(force=True)
                d = DEPLOYMENTS.state.get(config.DEPLOYMENT) or {}
                for v in (d.get("blue_version"), d.get("green_version")):
                    if v:
                        MODELS.load(v)
            except Exception as e:
                log.debug("watcher: %s", e)
            time.sleep(RELOAD_SECONDS)

    threading.Thread(target=watcher, daemon=True, name="alias-watcher").start()


class Instance(BaseModel):
    ban: str = Field(..., examples=["3100000008"])
    features: dict[str, Any] | None = Field(None, description="Feature values; omit to read the online feature store")
    snapshot_month: str | None = Field(None, description="As-of month of the features (joins outcomes later)")
    request_id: str | None = None


class PredictRequest(Instance):
    deployment: str = config.DEPLOYMENT


class BatchRequest(BaseModel):
    instances: list[Instance]
    deployment: str = config.DEPLOYMENT
    source: str = "api"


@app.get("/healthz")
def healthz():
    return {"ok": True, "aliases": MODELS.alias, "loaded": sorted(MODELS.cache),
            "log_written": WRITER.written if WRITER else 0}


@app.get("/", response_class=HTMLResponse)
def home():
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Model Gateway</title>
<link rel="stylesheet" href="/serve/ui-kit/bsp.css"></head><body><main class="bsp-main" style="max-width:860px;margin:auto">
<div class="bsp-page-head"><h1>Model Gateway</h1><p class="lede">Serving <b>{config.MODEL_NAME}</b> from the MLflow registry —
single, blue/green, canary and shadow routing; every inference logged.</p></div>
<div class="card"><p><a class="btn primary" href="/serve/docs">API docs</a> <a class="btn secondary" href="/serve/metrics">Prometheus metrics</a>
<a class="btn secondary" href="/mlops/#deployments">Control tower</a></p>
<pre class="codeblock">curl -X POST $FACADE/serve/v1/predict -H 'content-type: application/json' \\
     -d '{{"ban": "3100000008"}}'</pre></div></main></body></html>"""


def _caller(request: Request) -> str | None:
    return (getattr(request.state, "ident", None) or {}).get("user")


@app.post("/v1/predict")
def predict(req: PredictRequest, request: Request):
    return predict_many([req.model_dump()], req.deployment, "api", _caller(request))[0]


@app.post("/v1/predict/batch")
def predict_batch(req: BatchRequest, request: Request):
    if len(req.instances) > 5000:
        raise HTTPException(413, "max 5000 instances per batch")
    preds = predict_many([i.model_dump() for i in req.instances], req.deployment, req.source, _caller(request))
    return {"predictions": preds, "n": len(preds)}


@app.get("/v1/models")
def models():
    MODELS.poll()
    return {"model": config.MODEL_NAME, "aliases": MODELS.alias,
            "loaded": [{k: v for k, v in m.items() if k != "pipeline"} for m in MODELS.cache.values()],
            "hot_reload_events": MODELS.events[-20:]}


@app.get("/metrics")
def metrics():
    if WRITER:
        LOGQ.set(WRITER.q.qsize())
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


class DeploymentChange(BaseModel):
    mode: str | None = None
    canary_weight: float | None = None
    shadow_enabled: bool | None = None
    blue_version: str | None = None
    green_version: str | None = None
    live_color: str | None = None
    actor: str = "api"


def _view(d: dict) -> dict:
    out = {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in d.items() if k != "history"}
    out["aliases"] = MODELS.alias
    out["history"] = (d.get("history") or [])[-20:]
    if d["mode"] == "blue_green":
        out["serving"] = {d.get("live_color"): d.get(f"{d.get('live_color')}_version")}
    elif d["mode"] == "canary":
        out["serving"] = {"champion": 1 - float(d.get("canary_weight") or 0), "challenger": float(d.get("canary_weight") or 0)}
    else:
        out["serving"] = {"champion": 1.0}
    out["shadow_version"] = shadow_target(d, (out.get("aliases") or {}).get("champion") or "") if d["mode"] != "blue_green" \
        else shadow_target(d, d.get(f"{d.get('live_color')}_version") or "")
    out["effective"] = effective(d, MODELS.alias)
    out["divergence"] = divergence(d, MODELS.alias, registry_aliases())
    out["in_sync"] = not any(x["level"] in ("error", "warning") for x in out["divergence"])
    return out


_REG = {"at": 0.0, "aliases": {}}


def registry_aliases() -> dict:
    """Aliases straight from the registry (not the gateway's loaded copy) — lag or a failed hot reload shows up as divergence."""
    if time.time() - _REG["at"] > 3:
        try:
            from mlflow import MlflowClient

            c = MlflowClient(tracking_uri=config.MLFLOW_URI, registry_uri=config.MLFLOW_URI)
            _REG["aliases"] = {k: str(v) for k, v in (c.get_registered_model(config.MODEL_NAME).aliases or {}).items()}
        except Exception:
            pass
        _REG["at"] = time.time()
    return dict(_REG["aliases"])


def effective(d: dict, al: dict) -> dict:
    """Who actually answers and who is shadow-scored, given the mode and the aliases."""
    champ, chall = al.get("champion"), al.get("challenger")
    mode = d.get("mode")
    serving: list[dict] = []
    if mode == "blue_green":
        live = d.get("live_color") or "blue"
        v = d.get(f"{live}_version") or (champ if live == "blue" else chall or champ)
        serving.append({"version": str(v) if v else None, "share": 1.0, "why": f"blue/green pin ({live} live)"})
    elif mode == "canary" and chall and chall != champ:
        w = float(d.get("canary_weight") or 0)
        serving += [{"version": champ, "share": 1 - w, "why": "alias champion"}, {"version": chall, "share": w, "why": "alias challenger (canary)"}]
    else:
        serving.append({"version": champ, "share": 1.0, "why": "alias champion"})
    sv = None
    if mode == "shadow" or d.get("shadow_enabled"):
        if mode == "blue_green":
            other = "green" if (d.get("live_color") or "blue") == "blue" else "blue"
            sv = d.get(f"{other}_version")
        else:
            sv = chall
        sv = str(sv) if sv and str(sv) != str(serving[0]["version"]) else None
    return {"serving": serving, "shadow": sv, "derived_from_registry": mode != "blue_green",
            "registry": {k: al.get(k) for k in ("champion", "challenger", "staging") if al.get(k)}}


def divergence(d: dict, loaded: dict, reg: dict) -> list[dict]:
    """Every way the routing config disagrees with the registry. level: error | warning | info."""
    out: list[dict] = []
    champ, chall = reg.get("champion"), reg.get("challenger")
    mode = d.get("mode")
    if not champ:
        out.append({"level": "error", "code": "no_champion", "message": "The registry has no `champion` alias — nothing can serve."})
    for a in ("champion", "challenger"):
        if reg.get(a) and loaded.get(a) and reg[a] != loaded[a]:
            out.append({"level": "error", "code": "hot_reload_lag", "message": f"Registry `{a}` is v{reg[a]} but the gateway still holds v{loaded[a]} "
                        "(hot reload pending or failed)."})
    pins = {c: d.get(f"{c}_version") for c in ("blue", "green") if d.get(f"{c}_version")}
    if mode == "blue_green":
        live = d.get("live_color") or "blue"
        lv = pins.get(live)
        if lv and champ and str(lv) != str(champ):
            out.append({"level": "error", "code": "serving_not_champion",
                        "message": f"Blue/green serves pinned v{lv} ({live}) but the registry champion is v{champ}. Customers are NOT on the approved model."})
        other = "green" if live == "blue" else "blue"
        if pins.get(other) and chall and str(pins[other]) != str(chall):
            out.append({"level": "warning", "code": "standby_not_challenger",
                        "message": f"Stand-by colour {other} is pinned to v{pins[other]}, the registry challenger is v{chall}."})
    elif pins:
        out.append({"level": "info", "code": "stale_pins",
                    "message": f"Blue/green pins ({', '.join(f'{k} v{v}' for k, v in pins.items())}) are ignored in mode {mode}; "
                               "reconcile clears them."})
    if chall and chall != champ:
        shadowed = mode == "shadow" or d.get("shadow_enabled")
        if mode not in ("canary",) and not shadowed and mode != "blue_green":
            out.append({"level": "warning", "code": "challenger_idle",
                        "message": f"Challenger v{chall} receives no traffic (no shadow / canary) — no live evidence is being collected."})
        if mode == "canary" and float(d.get("canary_weight") or 0) == 0:
            out.append({"level": "warning", "code": "canary_zero", "message": "Canary mode with weight 0 — the challenger receives nothing."})
    if reg.get("staging") and reg.get("staging") not in (champ, chall):
        out.append({"level": "info", "code": "staging_idle",
                    "message": f"Staging v{reg['staging']} is neither champion nor challenger, so it receives no traffic."})
    if mode == "shadow" and not chall:
        out.append({"level": "info", "code": "shadow_without_challenger", "message": "Shadow mode but no challenger alias — nothing is shadow-scored."})
    return out


@app.get("/admin/deployments")
def list_deployments():
    DEPLOYMENTS.refresh(force=True)
    MODELS.poll()
    return [_view(d) for d in DEPLOYMENTS.state.values()]


@app.get("/admin/deployments/{name}")
def get_deployment(name: str):
    MODELS.poll()
    return _view(DEPLOYMENTS.get(name))


def _actor(request: Request, given: str) -> str:
    ident = getattr(request.state, "ident", None) or {}
    return given if given and given != "api" else (ident.get("user") or "api")


@app.put("/admin/deployments/{name}")
def change_deployment(name: str, ch: DeploymentChange, request: Request):
    changes = ch.model_dump(exclude={"actor"}, exclude_none=True)
    return _view(DEPLOYMENTS.update(name, changes, _actor(request, ch.actor), "configure:" + ",".join(sorted(changes))))


@app.post("/admin/deployments/{name}/switch")
def switch(name: str, actor: str = "api"):
    d = DEPLOYMENTS.get(name)
    if d["mode"] != "blue_green":
        raise HTTPException(409, "switch is only valid in blue_green mode")
    target = "green" if d["live_color"] == "blue" else "blue"
    if not d.get(f"{target}_version"):
        raise HTTPException(409, f"{target} has no version pinned")
    return _view(DEPLOYMENTS.update(name, {"live_color": target}, actor, f"switch:{d['live_color']}→{target}"))


@app.post("/admin/deployments/{name}/rollback")
def rollback(name: str, actor: str = "api"):
    d = DEPLOYMENTS.get(name)
    hist = d.get("history") or []
    if not hist:
        raise HTTPException(409, "nothing to roll back")
    before = hist[-1]["before"]
    return _view(DEPLOYMENTS.update(name, {k: before.get(k) for k in ("mode", "blue_version", "green_version", "live_color",
                                                                       "canary_weight", "shadow_enabled")},
                                    actor, "rollback"))


class ReconcileBody(BaseModel):
    policy: str = Field("shadow", description="shadow (challenger shadow-scored) | canary | single")
    canary_weight: float | None = None
    actor: str = "api"
    reason: str = ""


@app.post("/admin/deployments/{name}/reconcile")
def reconcile(name: str, b: ReconcileBody, request: Request):
    """Re-derive routing from the registry: champion answers; the challenger is
    shadow-scored (policy shadow) or gets a canary share; stale pins cleared."""
    if b.policy not in ("shadow", "canary", "single"):
        raise HTTPException(400, "policy must be shadow, canary or single")
    MODELS.poll(force=True)
    al = MODELS.alias
    if not al.get("champion"):
        raise HTTPException(409, "no champion alias in the registry")
    has_chall = bool(al.get("challenger")) and al.get("challenger") != al.get("champion")
    mode = b.policy if has_chall else "single"
    ch = {"mode": mode, "blue_version": None, "green_version": None, "live_color": "blue",
          "shadow_enabled": mode == "shadow"}
    if mode == "canary":
        ch["canary_weight"] = b.canary_weight if b.canary_weight is not None else 0.1
    d = DEPLOYMENTS.update(name, ch, _actor(request, b.actor), f"reconcile:{b.policy}" + (f" ({b.reason[:120]})" if b.reason else ""))
    return _view(d)


@app.post("/admin/smoke/{version}")
def smoke(version: str, n: int = 50):
    """Latency smoke test of ONE registered version inside the serving process
    (load + n single-row requests + one 200-row batch, using real payloads from
    the online store). Used by the release gates; touches no routing."""
    if not version.isdigit():
        raise HTTPException(400, "version must be a number")
    n = max(5, min(500, n))
    t0 = time.perf_counter()
    try:
        m = MODELS.load(version)
    except Exception as e:
        raise HTTPException(404, f"cannot load v{version}: {e}")
    load_s = time.perf_counter() - t0
    rows = []
    try:
        ids = [r["ban"] for r in db.fetchall("select ban from features.online_features order by md5(ban) limit %s", (max(n, 200),))]
        from ..featurestore import get_online_features

        onl = get_online_features(ids)
        rows = [{k: v for k, v in f.items() if k != "_event_ts"} for f in onl.values()]
    except Exception as e:
        log.warning("smoke: online store unavailable (%s) — synthetic empty rows", e)
    if not rows:
        rows = [{f: None for f in m["features"]} for _ in range(max(n, 200))]
    lat, errors = [], 0
    for i in range(n):
        t = time.perf_counter()
        try:
            _score(m, [rows[i % len(rows)]])
        except Exception:
            errors += 1
        lat.append((time.perf_counter() - t) * 1000)
    t = time.perf_counter()
    try:
        _score(m, rows[:200])
        batch_ms = (time.perf_counter() - t) * 1000
    except Exception:
        errors += 1
        batch_ms = None
    a = np.array(lat)
    return {"version": version, "n": n, "errors": errors, "load_seconds": round(load_s, 3),
            "p50_ms": round(float(np.percentile(a, 50)), 3), "p95_ms": round(float(np.percentile(a, 95)), 3),
            "p99_ms": round(float(np.percentile(a, 99)), 3), "batch_200_ms": None if batch_ms is None else round(batch_ms, 2),
            "features": len(m["features"])}


@app.get("/admin/slo")
def slo(minutes: int = 60):
    """HTTP-level reliability of the scoring API over a window (in-process ring
    buffer since the last restart) against the SLO targets."""
    minutes = max(1, min(1440, minutes))
    since = time.time() - minutes * 60
    rows = [r for r in list(REQLOG) if r[0] >= since and r[1].startswith("/v1/")]
    n = len(rows)
    codes = collections.Counter(r[2] for r in rows)
    e5 = sum(v for k, v in codes.items() if k >= 500)
    e4 = sum(v for k, v in codes.items() if 400 <= k < 500)
    lat = np.array([r[3] for r in rows]) if rows else np.array([])
    pct = (lambda q: round(float(np.percentile(lat, q)), 2)) if n else (lambda q: None)
    avail = 1 - e5 / n if n else None
    budget = 1 - SLO["availability"]
    per_min: dict[int, list] = {}
    for r in rows:
        per_min.setdefault(int(r[0] // 60 * 60), []).append(r)
    series = [{"minute": datetime.fromtimestamp(k, timezone.utc).isoformat(), "n": len(v),
               "errors_5xx": sum(1 for x in v if x[2] >= 500), "p95_ms": round(float(np.percentile([x[3] for x in v], 95)), 2)}
              for k, v in sorted(per_min.items())]
    callers = collections.Counter(r[4] or "anonymous" for r in rows)
    return {"minutes": minutes, "requests": n, "codes": {str(k): v for k, v in sorted(codes.items())}, "errors_5xx": e5, "errors_4xx": e4,
            "error_rate": e5 / n if n else None, "availability": avail, "p50_ms": pct(50), "p95_ms": pct(95), "p99_ms": pct(99),
            "slo": SLO, "auth_mode": AUTH_MODE,
            "error_budget_consumed": (e5 / n) / budget if n and budget > 0 else None,
            "p95_ok": (pct(95) or 0) <= SLO["p95_request_ms"] if n else None,
            "availability_ok": avail >= SLO["availability"] if n else None,
            "since_restart": datetime.fromtimestamp(REQLOG[0][0], timezone.utc).isoformat() if REQLOG else None,
            "series": series[-120:], "callers": dict(callers.most_common(10)),
            "unauthenticated_rejected": ERRS.labels("unauthenticated")._value.get()}


class TokenBody(BaseModel):
    name: str
    scope: str = "score"


def _admin_person(request: Request) -> str:
    ident = getattr(request.state, "ident", None) or {}
    if not (ident.get("source") == "sso-gate" and auth.has_role(ident.get("role"), "admin")) and AUTH_MODE == "enforce":
        raise HTTPException(403, "Client tokens are issued / revoked by a signed-in admin.")
    return ident.get("user") or "admin"


@app.get("/admin/tokens")
def tokens():
    return {"tokens": auth.list_tokens(), "auth_mode": AUTH_MODE}


@app.post("/admin/tokens")
def issue(b: TokenBody, request: Request):
    who = _admin_person(request)
    try:
        tok = auth.issue_token(b.name, who, b.scope)
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.audit("gateway.token.issue", b.name, who, "admin", "sso-gate", scope=b.scope)
    return {"name": b.name, "token": tok, "note": "Shown once — only its sha256 is stored."}


@app.delete("/admin/tokens/{name}")
def revoke(name: str, request: Request):
    who = _admin_person(request)
    if not auth.revoke_token(name, who):
        raise HTTPException(404, "no such client token")
    db.audit("gateway.token.revoke", name, who, "admin", "sso-gate")
    return {"revoked": name}


@app.post("/admin/reload")
def reload_models():
    return {"aliases": MODELS.poll(force=True), "loaded": sorted(MODELS.cache)}


@app.post("/admin/flush")
def flush():
    """Block until queued inference-log rows are written (tests / demo)."""
    return {"remaining": WRITER.flush_wait() if WRITER else 0, "written": WRITER.written if WRITER else 0}
