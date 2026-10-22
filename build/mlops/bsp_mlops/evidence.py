"""Capability evidence — the critique checklist (A–G) mapped to LIVE proof.

For every checklist item: how it is done here, its honest status, the code that
does it (resolved to file:line at request time, so the reference cannot go
stale), the notebook that teaches it, the GCP-native equivalent, and links to
the live evidence (MLflow runs, promotion records, drift / label reports,
export bundles) looked up in Postgres and MLflow when the page is opened.

Status vocabulary (never upgraded by wording):
    implemented        implemented & demonstrated in this appliance (evidence links prove it)
    delegated          deliberately owned by GCP / the platform (the boundary is named)
    needs_engineering  a known gap with a concrete fix
    roadmap            not started

    python -m bsp_mlops.evidence            # the checklist with live evidence, in the terminal
    python -m bsp_mlops.evidence --markdown # the same as a markdown table
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from pathlib import Path

from . import config

PKG = Path(__file__).resolve().parent
REPO_PREFIX = "build/mlops/bsp_mlops"
STATUS_LABEL = {"implemented": "Implemented & demonstrated here", "delegated": "Delegated to GCP / platform",
                "needs_engineering": "Needs engineering", "roadmap": "Roadmap"}


# ── code references (file:line resolved live) ─────────────────────────────
def code_ref(ref: str) -> dict:
    """'release:evaluate_gates' → {symbol, file, line, end, url}. Static
    'path/to/file.py' references (outside this package) pass through."""
    if ":" not in ref:
        return {"symbol": ref, "file": ref, "line": None, "url": None}
    mod, qual = ref.split(":", 1)
    try:
        m = importlib.import_module(f"bsp_mlops.{mod}")
        obj = m
        for part in qual.split("."):
            obj = getattr(obj, part)
        obj = inspect.unwrap(obj)
        lines, start = inspect.getsourcelines(obj)
        rel = Path(inspect.getsourcefile(obj)).resolve().relative_to(PKG)
        return {"symbol": f"{mod}.{qual}", "file": f"{REPO_PREFIX}/{rel}", "line": start, "end": start + len(lines) - 1,
                "url": f"/mlops/api/code?ref={ref}"}
    except Exception as e:
        return {"symbol": f"{mod}.{qual}", "file": f"{REPO_PREFIX}/{mod.replace('.', '/')}.py", "line": None, "url": None,
                "error": str(e)[:120]}


def source_of(ref: str, context: int = 0) -> dict:
    r = code_ref(ref)
    if not r.get("line"):
        raise LookupError(f"cannot resolve {ref}")
    path = PKG / Path(r["file"]).relative_to(REPO_PREFIX)
    lines = path.read_text().splitlines()
    a = max(1, r["line"] - context)
    b = min(len(lines), r["end"] + context)
    return {**r, "source": "\n".join(f"{i:>5}  {lines[i - 1]}" for i in range(a, b + 1))}


def nb(name: str) -> dict:
    return {"name": name, "path": f"work_templates/ml/{name}.ipynb", "workspace": f"~/work/ml/{name}.ipynb (ml-scientist: ~/work/{name}.ipynb)"}


# ── live evidence lookups ─────────────────────────────────────────────────
def _q(sql: str, params=None) -> list[dict]:
    from . import db

    try:
        return db.fetchall(sql, params)
    except Exception:
        return []


def _mlflow_run(run_id: str | None, label: str) -> dict | None:
    if not run_id:
        return None
    try:
        from .registry import client

        r = client().get_run(run_id)
        return {"label": label, "href": f"/mlflow/#/experiments/{r.info.experiment_id}/runs/{run_id}", "kind": "mlflow"}
    except Exception:
        return {"label": label, "href": f"/mlflow/#/runs/{run_id}", "kind": "mlflow"}


def live() -> dict:
    """One pass over the evidence sources; items pick what they need."""
    from . import registry

    ev: dict = {}
    try:
        al = registry.aliases()
    except Exception:
        al = {}
    ev["aliases"] = al
    champ = al.get("champion")
    if champ:
        try:
            mv = registry.client().get_model_version(config.MODEL_NAME, champ)
            ev["champion_run"] = mv.run_id
            ev["champion_tags"] = dict(mv.tags or {})
            ev["champion_run_tags"] = dict(registry.client().get_run(mv.run_id).data.tags)
        except Exception:
            pass
    ev["promotions"] = _q("select id, model_version, action, to_stage, status, requested_by, approved_by, approved_source, gates_passed, "
                          "evidence_run_id, created_at from mlops.promotions order by id desc limit 40")
    ev["drift"] = _q("select id, created_at, breached, max_psi, triggered_retrain_id from mlops.drift_reports order by id desc limit 10")
    ev["labels"] = _q("select id, created_at, n_labelled, n_pending, breached from mlops.label_evaluations order by id desc limit 3")
    ev["experiments"] = _q("select id, kind, name, created_at from mlops.experiments order by id desc limit 10")
    ev["repro"] = _q("select id, model_version, parity, data_hash_match, repro_run_id from mlops.reproductions where status='succeeded' order by id desc limit 3")
    ev["frozen"] = _q("select id, model_version, bundle_name, sha256, uri from mlops.frozen_artifacts order by id desc limit 3")
    ev["retrains"] = _q("select id, trigger, status, model_version, decision from mlops.retrain_runs order by id desc limit 3")
    ev["alerts"] = _q("select id, kind, status, title from mlops.alerts order by id desc limit 5")
    ev["audit"] = _q("select count(*) n, max(ts) last from mlops.audit_log")
    ev["lifecycle"] = _q("select id, status, requested_by, finished_at from mlops.lifecycle_runs order by id desc limit 1")
    ev["callers"] = _q("select caller, count(*) n from mlops.inference_log where caller is not null group by 1 order by 2 desc limit 5")
    return ev


def _links(item_id: str, ev: dict) -> list[dict]:
    """Evidence links for one checklist item (empty → nothing recorded yet)."""
    L: list[dict] = []
    champ = ev["aliases"].get("champion")
    rt = ev.get("champion_run_tags") or {}
    approved = [p for p in ev["promotions"] if p["to_stage"] == "production" and p["status"] == "approved"]
    gated = [p for p in ev["promotions"] if p.get("evidence_run_id")]
    blocked = [p for p in ev["promotions"] if p["status"] == "gates_failed"]
    rollbacks = [p for p in ev["promotions"] if p["action"] == "rollback"]

    def add(x):
        if x:
            L.append(x)

    if item_id in ("A1", "A2", "A5", "A6", "B6"):
        add(_mlflow_run(ev.get("champion_run"), f"champion v{champ} training run (data / code / env hashes as tags)") if champ else None)
        if rt.get("bsp.data_snapshot_sha256"):
            L.append({"label": f"data snapshot sha256 {rt['bsp.data_snapshot_sha256'][:12]}…", "href": f"#journey?v={champ}"})
    if item_id in ("A3", "A4"):
        L.append({"label": "Leaderboard (all experiments)", "href": "#leaderboard"})
    if item_id in ("A5", "C2", "C4"):
        for r in ev["repro"][:1]:
            add(_mlflow_run(r["repro_run_id"], f"reproduction #{r['id']} of v{r['model_version']}: {r['parity']}"))
    if item_id in ("A6",) and champ:
        L.append({"label": f"v{champ} model journey", "href": f"#journey?v={champ}"})
    if item_id in ("B1", "B2", "B3", "B4", "B5"):
        L.append({"label": "Feature store (views, versions, freshness)", "href": "#features"})
    if item_id in ("C1", "C2", "C4", "C6", "C8", "E7", "G2"):
        for p in (approved[:1] + gated[:1] + blocked[:1]):
            L.append({"label": f"promotion #{p['id']}: v{p['model_version']} → {p['to_stage']} ({p['status']})", "href": f"#release?id={p['id']}"})
        for p in gated[:1]:
            add(_mlflow_run(p["evidence_run_id"], f"gate evidence run (v{p['model_version']})"))
    if item_id in ("C5", "C6", "F4"):
        for p in rollbacks[:1]:
            L.append({"label": f"rollback #{p['id']} → v{p['model_version']}", "href": f"#release?id={p['id']}"})
        L.append({"label": "Routing & reconciliation", "href": "#registry"})
    if item_id in ("C3", "H2"):
        for f in ev["frozen"][:1]:
            L.append({"label": f"export bundle {f['bundle_name']} (sha256 {f['sha256'][:12]}…)", "href": "#frozen"})
    if item_id in ("D1",):
        L.append({"label": "Scores dataset (batch)", "href": "#scores"})
        L.append({"label": "Gateway API (real time)", "href": "/serve/docs"})
    if item_id in ("D2", "D3", "D4", "D5", "G2"):
        L.append({"label": "SLO, access & alerts", "href": "#slo"})
    if item_id in ("E1", "E2", "E3"):
        for d in ev["drift"][:1]:
            L.append({"label": f"drift report #{d['id']} (max PSI {d['max_psi']:.2f}{', breach' if d['breached'] else ''})", "href": "#drift"})
    if item_id in ("E4",):
        for d in ev["labels"][:1]:
            L.append({"label": f"delayed-label evaluation #{d['id']} ({d['n_labelled']:,} labelled, {d['n_pending']:,} pending)", "href": "#outcomes"})
    if item_id in ("E5",):
        for r in ev["retrains"][:1]:
            L.append({"label": f"retrain #{r['id']} ({r['trigger']}) → v{r['model_version'] or '…'}", "href": "#retrains"})
        for a in ev["alerts"][:2]:
            L.append({"label": f"alert #{a['id']} {a['kind']} ({a['status']})", "href": "#slo"})
    if item_id in ("E6", "E7", "G6"):
        if ev["audit"] and ev["audit"][0]["n"]:
            L.append({"label": f"audit log: {ev['audit'][0]['n']:,} entries", "href": "#release"})
    if item_id in ("F1", "F2", "F3", "F5"):
        for x in ev["experiments"][:3]:
            L.append({"label": f"experiment #{x['id']}: {x['name']}", "href": "#outcomes"})
    if item_id in ("H3",):
        for r in ev["lifecycle"][:1]:
            L.append({"label": f"lifecycle run #{r['id']} ({r['status']})", "href": "#teach"})
    return L


# ── the checklist ─────────────────────────────────────────────────────────
ITEMS: list[dict] = [
    # A · experimentation & reproducibility
    {"id": "A1", "area": "A", "title": "Versioned datasets and training configs", "status": "implemented",
     "how": "Every training run logs the sha256 of its exact training rows (order-independent), the train/valid/test windows, the feature-view "
            "versions + definition hashes and config/training_config.json (family, params, features, seed, recency weighting).",
     "code": ["lineage:data_snapshot_hash", "pipeline:train_and_log", "featurestore:apply"], "notebook": "05_pipeline_and_mlflow",
     "gcp": "Vertex AI Datasets / BigQuery table snapshots; Vertex ML Metadata artifacts",
     "limits": "The snapshot is a hash + reproducible query over features.churn_base, not a stored copy for every run (the export bundle stores the rows)."},
    {"id": "A2", "area": "A", "title": "Model artifact and environment versioning", "status": "implemented",
     "how": "MLflow registry versions (prep+model as one artifact, signature, input example); artifact sha256 pinned at registration and re-verified "
            "at every promotion; environment lock (pip freeze) + its hash recorded with each run.",
     "code": ["release:pin_artifact", "release:gate_artifact", "lineage:env_lock"], "notebook": "08_registry_champion_challenger",
     "gcp": "Vertex AI Model Registry + Artifact Registry (container digests)",
     "limits": "The serving container image is the shared appliance image; per-model images are generated (deploy/Dockerfile) but not built here."},
    {"id": "A3", "area": "A", "title": "Hyper-parameter optimisation + tracking", "status": "implemented",
     "how": "Optuna TPE study; every trial a nested MLflow run; the best params refit and logged as the parent's model.",
     "code": ["pipeline:tune"], "notebook": "07_hyperparameter_tuning", "gcp": "Vertex AI Vizier"},
    {"id": "A4", "area": "A", "title": "Meaningful leaderboard", "status": "implemented",
     "how": "Five model families on identical time-based windows; selected on validation AUC, reported on an untouched future test window "
            "(plus PR-AUC, logloss, Brier, lift@10). Hold-out strategies compared to show why time-based splitting matters.",
     "code": ["pipeline:leaderboard", "splits:compare"], "notebook": "06_leaderboard", "gcp": "Vertex AI Experiments comparison view"},
    {"id": "A5", "area": "A", "title": "Reproducibility across users / environments", "status": "implemented",
     "how": "`Reproduce vN` refits from the recorded recipe, rebuilds the rows through the same point-in-time join, checks the data hash, and "
            "compares every metric and every test prediction with the registered model; env-lock diff and code hash reported.",
     "code": ["reproduce:reproduce", "lineage:env_diff", "lineage:code_fingerprint"], "notebook": "15_reproduce_and_export",
     "gcp": "Vertex AI Pipelines caching + ML Metadata",
     "limits": "Parity is proven inside the appliance image; a second, different environment shows up as an env-lock diff (not yet run in CI)."},
    {"id": "A6", "area": "A", "title": "Experiment lineage", "status": "implemented",
     "how": "Model journey: one MLflow parent run per version, one child run per lifecycle stage; every training / tuning / selection / gate run "
            "carries bsp.journey_id; retrains link to the parent champion's journey.",
     "code": ["journey:Journey", "journey:read"], "notebook": "05_pipeline_and_mlflow", "gcp": "Vertex ML Metadata lineage graph"},
    # B · features
    {"id": "B1", "area": "B", "title": "Governed, reusable feature definitions", "status": "implemented",
     "how": "features.yaml is the system of record (owner, TTL, dtype, description); a change bumps the view version and its definition hash; "
            "old versions stay queryable. The leakage gate refuses models using features outside these definitions.",
     "code": ["featurestore:load_definitions", "featurestore:apply", "release:gate_leakage"], "notebook": "04_feature_store",
     "gcp": "Vertex AI Feature Store feature groups (BigQuery-backed)",
     "limits": "Definition changes are governed by code review of features.yaml — no separate approval workflow for features."},
    {"id": "B2", "area": "B", "title": "Feature store (offline + online)", "status": "implemented",
     "how": "Offline views in Postgres `features`, online store `features.online_features` read by the gateway for id-only requests; a Feast repo "
            "is generated from the same YAML with an online-value parity check.",
     "code": ["featurestore:materialize_online", "featurestore:get_online_features", "featurestore:feast_sync"], "notebook": "04_feature_store",
     "gcp": "Vertex AI Feature Store (Bigtable / optimized online serving)"},
    {"id": "B3", "area": "B", "title": "Training–serving consistency", "status": "implemented",
     "how": "One sklearn Pipeline (imputation, encoding, scaling + model) is the deployed artifact; the schema gate checks signature = feature list "
            "= feature-store dtypes and validates live payloads; drift compares served features with the release baseline.",
     "code": ["pipeline:build_pipeline", "release:gate_schema", "drift:run_check"], "notebook": "05_pipeline_and_mlflow",
     "gcp": "Vertex AI Feature Store online serving + Model Monitoring training-serving skew"},
    {"id": "B4", "area": "B", "title": "Point-in-time correct training sets", "status": "implemented",
     "how": "as-of join per feature view with TTL (merge_asof backward); exactness verified against the base table in the demo.",
     "code": ["featurestore:get_historical_features"], "notebook": "04_feature_store", "gcp": "Vertex AI Feature Store point-in-time lookup (BigQuery)"},
    {"id": "B5", "area": "B", "title": "Freshness and transformation lineage", "status": "implemented",
     "how": "Feature-store registry records per view: version, definition hash, offline table, rows, last event; base table built by "
            "data.integrate from named warehouse tables (dbt mart or raw).",
     "code": ["featurestore:freshness", "data:integrate"], "notebook": "01_integrate_and_eda", "gcp": "Dataplex lineage / BigQuery lineage",
     "limits": "Column-level lineage into the warehouse is in the catalog (Atlan / DataHub side stack), not in the MLOps tower."},
    {"id": "B6", "area": "B", "title": "Leakage controls", "status": "implemented",
     "how": "Base table is leakage-safe by construction (label strictly after the snapshot, as-of joins); group / time hold-outs expose shared-"
            "account leakage; release gate `leakage_guard` blocks label-derived or ungoverned columns.",
     "code": ["release:gate_leakage", "splits:compare"], "notebook": "03_holdout_strategies", "gcp": "— (practice + pipeline checks)"},
    # C · CI/CD & release
    {"id": "C1", "area": "C", "title": "Automated Dev → Staging → Prod promotion", "status": "implemented",
     "how": "dev (registered) → staging (all automated gates pass; becomes challenger in shadow) → production (four-eyes approval). "
            "Retrains enter the same pipeline automatically.",
     "code": ["release:submit", "release:approve", "retrain:_run"], "notebook": "14_release_gates_and_approval",
     "gcp": "Cloud Build / Cloud Deploy pipelines with approval gates",
     "limits": "Promotion is triggered in-app (CLI, tower, retrain), not by a git push — wiring `python -m bsp_mlops.release` into a CI "
               "trigger is engineering work."},
    {"id": "C2", "area": "C", "title": "Registry with approval gates", "status": "implemented",
     "how": "MLflow aliases emulate stages; mlops.promotions records requester, approver (role + identity source), gate results, hashes; "
            "approval requires an approver role and a different person (four-eyes).",
     "code": ["release:check_approver", "release:approve"], "notebook": "14_release_gates_and_approval", "gcp": "Vertex AI Model Registry + Cloud Deploy approvals"},
    {"id": "C3", "area": "C", "title": "Immutable artifacts / container versioning", "status": "implemented",
     "how": "Frozen bundle: deterministic tar.gz whose sha256 is the id, OCI image layout, read-only, content-addressed artifactory that refuses "
            "overwrites; artifact hash pinned in the registry.",
     "code": ["freeze:build_bundle", "freeze:LocalArtifactory.put"], "notebook": "13_freeze_and_artifactory", "gcp": "Artifact Registry (immutable tags)",
     "limits": "Container images per model are specified (deploy/Dockerfile) but built by the platform's CI, not by the appliance."},
    {"id": "C4", "area": "C", "title": "Automated model validation & regression tests", "status": "implemented",
     "how": "Gates: schema/data contract, AUC floor, non-regression (paired bootstrap + DeLong), calibration (ECE, Brier), segment parity "
            "(copper / fiber, state), latency smoke, artifact hash, lineage, leakage; a negative control (permuted labels) proves the gates block.",
     "code": ["release:evaluate_gates", "stats:delong", "stats:paired_bootstrap_auc"], "notebook": "14_release_gates_and_approval",
     "gcp": "Vertex AI Pipelines evaluation components / Model Evaluation"},
    {"id": "C5", "area": "C", "title": "Blue/green or canary", "status": "implemented",
     "how": "Gateway modes single / blue-green (instant switch, both colours warm) / canary (sticky by BAN) / shadow; every change recorded.",
     "code": ["gateway.app:route", "gateway.app:shadow_target"], "notebook": "09_deploy_blue_green_shadow_canary",
     "gcp": "Vertex AI Endpoints traffic split / Cloud Run revision traffic"},
    {"id": "C6", "area": "C", "title": "Rollback", "status": "implemented",
     "how": "Rollback re-points champion to the previous APPROVED production version (from the promotion records), keeps the demoted version "
            "in shadow, reconciles routing; routing-only rollback undoes the last gateway change.",
     "code": ["release:rollback", "release:previous_approved"], "notebook": "14_release_gates_and_approval", "gcp": "Cloud Run revision rollback / endpoint redeploy"},
    {"id": "C7", "area": "C", "title": "Version pinning", "status": "implemented",
     "how": "Serving resolves aliases to explicit versions (logged per request); blue/green pins exact versions; the export bundle pins model, "
            "lock and feature definitions by hash.",
     "code": ["gateway.app:Models.poll", "gateway.app:effective"], "notebook": "09_deploy_blue_green_shadow_canary", "gcp": "Vertex model version IDs / image digests"},
    {"id": "C8", "area": "C", "title": "Deployment traceability", "status": "implemented",
     "how": "Every inference row carries model_version, alias, colour, mode, caller; routing history with actor; divergence between registry "
            "and routing shown loudly and reconciled (serving derived from aliases).",
     "code": ["gateway.app:divergence", "gateway.app:reconcile"], "notebook": "09_deploy_blue_green_shadow_canary",
     "gcp": "Vertex request-response logging to BigQuery; Cloud Audit Logs"},
    # D · inference / runtime
    {"id": "D1", "area": "D", "title": "Batch + real-time scoring", "status": "implemented",
     "how": "Real time: FastAPI gateway (single + batch endpoints, online features for id-only calls). Batch: scoring.batch_score → "
            "mlops.scored_customers with deciles and reason codes.",
     "code": ["gateway.app:predict_many", "scoring:batch_score"], "notebook": "12_scores_to_dataset",
     "gcp": "Vertex AI Endpoints / Batch Prediction; BigQuery ML for in-warehouse scoring"},
    {"id": "D2", "area": "D", "title": "Endpoint authentication", "status": "implemented",
     "how": "Scoring and admin APIs require a Studio-gated identity or a bearer token (sha256-only token store, admin-issued client tokens, "
            "revocation); admin writes also need modeler/admin or the service token + CSRF header; caller logged per request.",
     "code": ["auth:identity", "gateway.app:guard"], "notebook": "09_deploy_blue_green_shadow_canary", "gcp": "IAM invoker + IAP / API Gateway",
     "limits": "Header identity is trusted on the internal network (uvicorn proxy headers); a signed identity assertion (IAP JWT on GCP) "
               "is the production fix."},
    {"id": "D3", "area": "D", "title": "Capacity / latency monitoring", "status": "implemented",
     "how": "Prometheus metrics (/serve/metrics), per-instance latency in inference_log, HTTP p50/p95/p99 and error rate per minute vs SLO.",
     "code": ["gateway.app:slo"], "notebook": "10_telemetry_and_drift", "gcp": "Cloud Monitoring (managed Prometheus) dashboards",
     "limits": "No load test / autoscaling here — single gateway process; capacity planning belongs to the Cloud Run / Vertex deployment."},
    {"id": "D4", "area": "D", "title": "Retries / failure handling", "status": "implemented",
     "how": "Client retries connection errors and 502/503/504 with exponential back-off (request_id makes retries traceable); the gateway "
            "keeps serving when the registry is down, pre-loads before alias swaps, re-queues failed log writes.",
     "code": ["deploy:_req", "gateway.app:LogWriter._run", "gateway.app:Models.poll"], "notebook": "09_deploy_blue_green_shadow_canary",
     "gcp": "Cloud Run / Endpoint retries, Pub/Sub dead-lettering for async"},
    {"id": "D5", "area": "D", "title": "SLO ownership", "status": "implemented",
     "how": "SLO targets (p95, availability, error rate) and the named owner are part of the gateway's /admin/slo; breaches feed the alert workflow.",
     "code": ["gateway.app:slo", "alerts:raise_alert"], "notebook": "10_telemetry_and_drift", "gcp": "Cloud Monitoring SLOs + error budgets",
     "limits": "Ownership is configuration (SLO_OWNER), not an on-call rota integration."},
    # E · monitoring & governance
    {"id": "E1", "area": "E", "title": "Data quality & schema drift", "status": "implemented",
     "how": "Schema / data-contract gate on live payloads (columns, technology-aware nulls, domains, ranges, unseen categories) before every "
            "promotion; drift monitor on served traffic.",
     "code": ["release:gate_schema"], "notebook": "10_telemetry_and_drift", "gcp": "Vertex Model Monitoring feature skew / Dataplex data quality",
     "limits": "The contract runs at release time and on demand, not on every request."},
    {"id": "E2", "area": "E", "title": "Data drift & performance drift", "status": "implemented",
     "how": "PSI + KS per feature vs the release baseline, prediction PSI, AUC on labelled traffic; breach → alert + retrain.",
     "code": ["drift:run_check", "drift:psi"], "notebook": "10_telemetry_and_drift", "gcp": "Vertex AI Model Monitoring"},
    {"id": "E3", "area": "E", "title": "Prediction distribution monitoring", "status": "implemented",
     "how": "Score histogram per version (Prometheus + telemetry page), prediction PSI in every drift check.",
     "code": ["drift:run_check"], "notebook": "10_telemetry_and_drift", "gcp": "Vertex Model Monitoring prediction drift"},
    {"id": "E4", "area": "E", "title": "Delayed-label performance evaluation", "status": "implemented",
     "how": "Scores logged at serve time are joined to realised churn when the 90-day label closes: realised AUC / lift / ECE per cohort and per "
            "serving week vs the AUC expected at release; pending labels counted, never guessed; degradation raises an alert.",
     "code": ["outcomes:delayed_label_eval"], "notebook": "10_telemetry_and_drift", "gcp": "Vertex Model Monitoring with ground truth (BigQuery join)"},
    {"id": "E5", "area": "E", "title": "Alerting, retrain triggers, incident workflow", "status": "implemented",
     "how": "Drift breach → alert + automatic retrain (cool-down) → release gates → staging; alerts acknowledged / resolved in the tower, "
            "every transition audited.",
     "code": ["alerts:raise_alert", "retrain:trigger"], "notebook": "11_retrain_trigger", "gcp": "Cloud Monitoring alerting → PagerDuty / Teams",
     "limits": "No outbound notification channel (Teams / e-mail / paging) — needs a webhook or Cloud Monitoring policy."},
    {"id": "E6", "area": "E", "title": "Provenance", "status": "implemented",
     "how": "Export manifest: sha256 of every file, run id, registry version, data snapshot hash, feature-view hashes, code + env hashes, lineage.json.",
     "code": ["freeze:export_extras", "freeze:build_bundle"], "notebook": "15_reproduce_and_export", "gcp": "Vertex ML Metadata; Artifact Analysis provenance"},
    {"id": "E7", "area": "E", "title": "Audit logs and approval trails", "status": "implemented",
     "how": "mlops.audit_log (actor, role, identity source, action, target), promotion records with requester / approver, routing history; "
            "promotion records mirrored to MLflow.",
     "code": ["db:audit", "release:_attach"], "notebook": "14_release_gates_and_approval", "gcp": "Cloud Audit Logs",
     "limits": "Postgres-only and writable by the shared warehouse user — tamper-evidence (append-only role, log export) needs engineering."},
    # F · business experimentation
    {"id": "F1", "area": "F", "title": "Champion / challenger", "status": "implemented",
     "how": "Aliases + release gates; the staged candidate is the challenger; paired comparison of both on the same requests.",
     "code": ["outcomes:champion_vs_challenger"], "notebook": "08_registry_champion_challenger", "gcp": "Vertex Endpoints traffic split + Experiments"},
    {"id": "F2", "area": "F", "title": "Shadow deployment", "status": "implemented",
     "how": "Challenger scored off the request path on every call and logged (shadow = true); zero customer impact; paired stats with CI.",
     "code": ["gateway.app:_shadow", "outcomes:champion_vs_challenger"], "notebook": "09_deploy_blue_green_shadow_canary", "gcp": "Endpoint shadow via request logging + batch replay"},
    {"id": "F3", "area": "F", "title": "A/B where possible", "status": "implemented",
     "how": "Retention-offer A/B on top-risk customers: arms stratified by score decile (hash order), A/A balance check, two-proportion z-test, CI, power.",
     "code": ["outcomes:retention_offer_ab", "stats:two_proportion"], "notebook": "14_release_gates_and_approval", "gcp": "— (BigQuery readout / Vertex Experiments)",
     "limits": "The offer's EFFECT is simulated (customers, scores and labels are real) — a real offer needs the CRM / campaign integration."},
    {"id": "F4", "area": "F", "title": "Controlled rollout / rollback", "status": "implemented",
     "how": "Shadow → canary (sticky weight) → approval → champion; rollback to the previous approved version, recorded.",
     "code": ["release:rollback", "gateway.app:reconcile"], "notebook": "09_deploy_blue_green_shadow_canary", "gcp": "Cloud Deploy canary / Vertex traffic split"},
    {"id": "F5", "area": "F", "title": "Outcome measurement with statistical validation", "status": "implemented",
     "how": "Paired bootstrap CI + DeLong for model comparisons; independent bootstrap for canary arms; z-test + CI + power for the business A/B.",
     "code": ["stats:delong", "stats:bootstrap_auc_diff_independent", "stats:power_two_proportion"], "notebook": "14_release_gates_and_approval",
     "gcp": "—"},
    # G · security
    {"id": "G1", "area": "G", "title": "Entra SSO + roles", "status": "delegated",
     "how": "Studio signs people in with Entra ID (OIDC authorization code, id_token validated) and maps roles viewer / analyst / modeler / admin; "
            "nginx enforces the gate on /mlops, /serve and /mlflow and forwards identity + role.",
     "code": ["build/studio/oidc.py", "build/nginx/snippets/gate.conf"], "notebook": None, "gcp": "Workforce Identity Federation (Entra) → Google IAM",
     "limits": "Owned by the platform control plane (Studio), not by the MLOps stack."},
    {"id": "G2", "area": "G", "title": "App RBAC", "status": "implemented",
     "how": "Release requests need ≥ analyst; production approval and rollback need an approver role (MLOPS_APPROVER_ROLES, default admin) and "
            "four-eyes; gateway admin writes need modeler / admin or the service token; client tokens issued by admins.",
     "code": ["release:check_approver", "gateway.app:guard"], "notebook": "14_release_gates_and_approval", "gcp": "IAM roles on Vertex / Cloud Run resources",
     "limits": "MLflow UI itself has no per-model permissions in this deployment (any signed-in user can edit tags in MLflow)."},
    {"id": "G3", "area": "G", "title": "IAM / workload identity", "status": "delegated",
     "how": "Generated Cloud Run spec runs under a dedicated service account (no key files); appliance GCP access uses Studio-assigned identities.",
     "code": ["freeze:export_extras"], "notebook": None, "gcp": "Workload Identity / service accounts per workload"},
    {"id": "G4", "area": "G", "title": "Secrets management", "status": "delegated",
     "how": "Credentials live in Studio's encrypted vault and reach containers only via process env; the gateway stores only token digests "
            "(0600) — a local stand-in for Secret Manager.",
     "code": ["auth:issue_token", "auth:service_token"], "notebook": None, "gcp": "Secret Manager"},
    {"id": "G5", "area": "G", "title": "Least privilege", "status": "needs_engineering",
     "how": "Containers run non-root on an internal network behind an egress allowlist; BUT the MLOps services share one warehouse user "
            "(analyst) for reads and writes.",
     "code": ["db:dsn"], "notebook": None, "gcp": "Per-service accounts + BigQuery dataset IAM",
     "limits": "Fix: separate DB roles (gateway: insert inference_log only; tower: mlops schema; notebooks: read-only)."},
    {"id": "G6", "area": "G", "title": "Audit", "status": "implemented",
     "how": "See E7 — who requested, approved, rolled back, changed routing, issued tokens; every scoring call's caller logged.",
     "code": ["db:audit"], "notebook": "14_release_gates_and_approval", "gcp": "Cloud Audit Logs"},
    {"id": "G7", "area": "G", "title": "GCP-native logging", "status": "needs_engineering",
     "how": "Services log to stdout and Postgres; nothing is forwarded to Cloud Logging from the appliance.",
     "code": [], "notebook": None, "gcp": "Cloud Logging (log router, sinks to BigQuery)",
     "limits": "Fix: structured JSON logs + the Ops Agent / Cloud Run native logging once deployed on GCP."},
    # H · cross-cutting asks
    {"id": "H1", "area": "H", "title": "OSS vs GCP ownership and integration boundaries", "status": "implemented",
     "how": "The ownership map (below / `Teach` page) lists, per lifecycle stage, the OSS component used here, the GCP-native equivalent, the "
            "boundary where they meet, and an honest status.",
     "code": ["evidence:checklist", "evidence:markdown"], "notebook": None, "gcp": "—"},
    {"id": "H2", "area": "H", "title": "Exportability of artifacts, models, pipelines, metadata", "status": "implemented",
     "how": "Export bundle per version: MLflow model, training + serving locks, feature definitions, training config, lineage.json, promotion "
            "records, Dockerfile + Cloud Run + Vertex specs, pipeline source + reproduce.sh, manifest with every hash.",
     "code": ["freeze:export_extras", "freeze:freeze_and_push"], "notebook": "15_reproduce_and_export", "gcp": "Vertex Model Registry import, Artifact Registry"},
    {"id": "H3", "area": "H", "title": "Reproducible, source-controlled workflows operable without a UI", "status": "implemented",
     "how": "One command runs the whole lifecycle with gates (`python -m bsp_mlops.demo`, `make mlops-lifecycle`); every action has a CLI "
            "(release, reproduce, outcomes, evidence, journey); the tower only calls the same functions.",
     "code": ["demo:run", "release:main", "reproduce:main"], "notebook": None, "gcp": "Vertex AI Pipelines / Cloud Composer",
     "limits": "The orchestration is a Python script, not a compiled pipeline DAG (KFP) — portable, but without per-step caching / retries."},
]

AREAS = {"A": "Experimentation & reproducibility", "B": "Features", "C": "CI/CD & release", "D": "Inference / runtime",
         "E": "Monitoring & governance", "F": "Controlled business experimentation", "G": "Security", "H": "Cross-cutting asks"}

OWNERSHIP: list[dict] = [
    {"stage": "Data integration / warehouse", "oss": "Postgres bsp_edw + dbt (marts) → features.churn_base", "gcp": "BigQuery + dbt / Dataform",
     "boundary": "features.churn_base contract (dbt target `bigquery` exists)", "status": "implemented", "note": "BigQuery path delegated"},
    {"stage": "Feature definitions & store", "oss": "features.yaml + Postgres offline / online + Feast mirror", "gcp": "Vertex AI Feature Store",
     "boundary": "Feast-compatible YAML; switch the Feast provider to GCP", "status": "implemented", "note": "GCP store delegated"},
    {"stage": "Experiment tracking & lineage", "oss": "MLflow 3 tracking + model journeys", "gcp": "Vertex AI Experiments + ML Metadata",
     "boundary": "MLflow REST; artifact store can point at GCS", "status": "implemented", "note": ""},
    {"stage": "Hyper-parameter tuning", "oss": "Optuna (TPE), nested MLflow runs", "gcp": "Vertex AI Vizier", "boundary": "objective in pipeline.tune",
     "status": "implemented", "note": ""},
    {"stage": "Pipeline orchestration", "oss": "Python modules + CLI + APScheduler + retrain subprocess", "gcp": "Vertex AI Pipelines (KFP) / Cloud Composer",
     "boundary": "each stage is a function → a KFP component", "status": "needs_engineering", "note": "compile to KFP for caching / per-step retries"},
    {"stage": "Model registry & stages", "oss": "MLflow registry aliases (staging / champion / challenger) + mlops.promotions",
     "gcp": "Vertex AI Model Registry (version aliases)", "boundary": "export bundle → deploy/vertex_model_upload.sh", "status": "implemented",
     "note": "Vertex registration generated, not executed"},
    {"stage": "Release gates & approvals", "oss": "bsp_mlops.release (gates, four-eyes, rollback)", "gcp": "Cloud Build + Cloud Deploy approvals",
     "boundary": "`python -m bsp_mlops.release gates|submit` as a CI step", "status": "implemented", "note": "git-triggered CI needs engineering"},
    {"stage": "Artifacts & containers", "oss": "Frozen bundle (deterministic tar, OCI layout) → local artifactory / JFrog", "gcp": "Artifact Registry",
     "boundary": "OCI layout + deploy/Dockerfile", "status": "implemented", "note": "image build + push needs engineering"},
    {"stage": "Real-time serving", "oss": "FastAPI model gateway (single / blue-green / canary / shadow)", "gcp": "Vertex AI Endpoints / Cloud Run",
     "boundary": "MLflow scoring container + generated Cloud Run spec", "status": "implemented", "note": "GCP hosting delegated"},
    {"stage": "Batch scoring", "oss": "scoring.batch_score → mlops.scored_customers", "gcp": "Vertex Batch Prediction / BigQuery ML",
     "boundary": "scores table contract", "status": "implemented", "note": ""},
    {"stage": "Endpoint auth", "oss": "Studio gate identity + bearer tokens", "gcp": "IAM invoker + IAP", "boundary": "verify IAP JWT instead of headers",
     "status": "implemented", "note": "signed assertion needs engineering"},
    {"stage": "Monitoring (drift, delayed labels, SLO)", "oss": "drift.py, outcomes.py, gateway /admin/slo, Prometheus", "gcp": "Vertex AI Model Monitoring + Cloud Monitoring",
     "boundary": "inference_log ≈ Vertex request-response logging (BigQuery)", "status": "implemented", "note": ""},
    {"stage": "Alerting & incidents", "oss": "mlops.alerts + tower workflow", "gcp": "Cloud Monitoring alerting policies", "boundary": "alert rows → notification channel",
     "status": "needs_engineering", "note": "outbound notifications missing"},
    {"stage": "Audit & logging", "oss": "mlops.audit_log, promotions, routing history", "gcp": "Cloud Logging + Cloud Audit Logs",
     "boundary": "structured stdout → log router", "status": "needs_engineering", "note": "no export to Cloud Logging yet"},
    {"stage": "Identity & RBAC", "oss": "Studio (Entra OIDC, roles) + nginx gate + app role checks", "gcp": "Workforce Identity Federation + IAM",
     "boundary": "X-Auth-User / X-Auth-Role headers", "status": "delegated", "note": "platform-owned"},
    {"stage": "Secrets", "oss": "Studio vault → process env; token digests on disk", "gcp": "Secret Manager", "boundary": "env injection",
     "status": "delegated", "note": ""},
    {"stage": "Reproducibility", "oss": "lineage hashes + bsp_mlops.reproduce", "gcp": "Vertex ML Metadata + pipeline caching",
     "boundary": "training_config.json + data snapshot hash", "status": "implemented", "note": ""},
    {"stage": "Business experiments", "oss": "outcomes.py (paired shadow, canary, retention A/B)", "gcp": "— (BigQuery readouts)",
     "boundary": "mlops.experiments", "status": "implemented", "note": "offer effect simulated"},
    {"stage": "Notebooks / IDE", "oss": "JupyterLab + VS Code workspace (work_templates/ml 01–15)", "gcp": "Vertex AI Workbench / Colab Enterprise",
     "boundary": "same bsp_mlops package on PYTHONPATH", "status": "implemented", "note": ""},
]

STAGE_TEACH: dict[str, dict] = {
    "integration": {"why": "A model is only as reproducible as its input. One leakage-safe base table, built by code from named warehouse tables, is the contract every later stage reads.",
                    "code": ["data:integrate"], "notebook": "01_integrate_and_eda", "gcp": "BigQuery + Dataform / dbt"},
    "eda": {"why": "Know the base rate, the segments and the shift before modelling — the drift story later is visible here first.",
            "code": ["eda:churn_by", "eda:shift_preview"], "notebook": "01_integrate_and_eda", "gcp": "BigQuery / Vertex Workbench"},
    "notebook": {"why": "The notebook is the human-readable twin of the pipeline code; its hash (and an executed copy) ties the explanation to the run.",
                 "code": ["journey:notebook_evidence"], "notebook": "01_integrate_and_eda", "gcp": "Vertex AI Workbench (scheduled notebooks)"},
    "selection": {"why": "Every feature costs serving effort and risk. Dropping noise with documented reasons (and a random probe) is governance, not taste.",
                  "code": ["selection:sift"], "notebook": "02_feature_sifting_selection", "gcp": "Vertex Explainable AI (attributions)"},
    "holdout": {"why": "Random splits flatter churn models (same account in train and test). Time / group hold-outs measure what production will see.",
                "code": ["splits:compare"], "notebook": "03_holdout_strategies", "gcp": "Vertex AI Pipelines (custom split component)"},
    "feature_store": {"why": "Point-in-time joins stop future data leaking into training; the online store gives serving the SAME definitions.",
                      "code": ["featurestore:get_historical_features", "featurestore:materialize_online"], "notebook": "04_feature_store", "gcp": "Vertex AI Feature Store"},
    "algo_selection": {"why": "A leaderboard on identical windows makes the model choice an auditable comparison instead of a preference.",
                       "code": ["pipeline:leaderboard"], "notebook": "06_leaderboard", "gcp": "Vertex AI Experiments"},
    "roc_auc": {"why": "Discrimination on an untouched future window is the first number a reviewer asks for.",
                "code": ["journey:roc_evidence"], "notebook": "05_pipeline_and_mlflow", "gcp": "Vertex Model Evaluation"},
    "tuning": {"why": "Tracked trials make tuning reproducible and show how much of the gain is search versus signal.",
               "code": ["pipeline:tune"], "notebook": "07_hyperparameter_tuning", "gcp": "Vertex AI Vizier"},
    "evaluation": {"why": "Calibration and lift decide whether scores can drive retention budgets, not just ranking.",
                   "code": ["metrics:evaluate", "journey:evaluation_evidence"], "notebook": "05_pipeline_and_mlflow", "gcp": "Vertex Model Evaluation"},
    "registration": {"why": "A registry version is the unit of release: immutable artifact, signature, lineage hashes, artifact hash pinned.",
                     "code": ["registry:register_run", "release:register_dev"], "notebook": "08_registry_champion_challenger", "gcp": "Vertex AI Model Registry"},
    "release": {"why": "Nothing reaches customers on a data scientist's say-so: automated gates prove it is valid and not worse, a second person approves.",
                "code": ["release:evaluate_gates", "release:submit", "release:approve", "release:rollback"], "notebook": "14_release_gates_and_approval",
                "gcp": "Cloud Build + Cloud Deploy approvals; Vertex Model Registry aliases"},
    "deployment": {"why": "Serving must follow the registry. Shadow and canary collect live evidence before customers depend on a new model.",
                   "code": ["gateway.app:route", "gateway.app:reconcile", "gateway.app:divergence"], "notebook": "09_deploy_blue_green_shadow_canary",
                   "gcp": "Vertex AI Endpoints traffic split / Cloud Run revisions"},
    "monitoring": {"why": "Drift is the early warning; realised performance after the 90-day label is the verdict.",
                   "code": ["drift:run_check", "outcomes:delayed_label_eval"], "notebook": "10_telemetry_and_drift", "gcp": "Vertex AI Model Monitoring"},
    "retrain": {"why": "Retraining is a governed event: triggered by evidence, run as code, and fed back into the same release gates.",
                "code": ["retrain:trigger", "pipeline:retrain"], "notebook": "11_retrain_trigger", "gcp": "Vertex AI Pipelines triggered by Model Monitoring"},
    "freeze": {"why": "An exportable, hashed bundle proves exactly what ran and lets it run elsewhere — no lock-in to this appliance.",
               "code": ["freeze:build_bundle", "freeze:export_extras", "reproduce:reproduce"], "notebook": "15_reproduce_and_export",
               "gcp": "Artifact Registry + Vertex Model Registry import"},
}


def teach(stage_key: str) -> dict | None:
    t = STAGE_TEACH.get(stage_key)
    if not t:
        return None
    return {"why": t["why"], "gcp": t["gcp"], "notebook": nb(t["notebook"]) if t.get("notebook") else None,
            "code": [code_ref(c) for c in t["code"]]}


def checklist(with_live: bool = True) -> dict:
    ev = live() if with_live else {"aliases": {}, "promotions": [], "drift": [], "labels": [], "experiments": [], "repro": [], "frozen": [],
                                   "retrains": [], "alerts": [], "audit": [], "lifecycle": [], "callers": []}
    items = []
    for it in ITEMS:
        links = _links(it["id"], ev) if with_live else []
        items.append({**it, "status_label": STATUS_LABEL[it["status"]], "code": [code_ref(c) for c in it["code"]],
                      "notebook": nb(it["notebook"]) if it.get("notebook") else None, "evidence": links,
                      "evidence_recorded": bool(links)})
    counts: dict[str, int] = {}
    for it in items:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"areas": AREAS, "items": items, "counts": counts, "status_labels": STATUS_LABEL,
            "ownership": [{**o, "status_label": STATUS_LABEL[o["status"]]} for o in OWNERSHIP], "aliases": ev.get("aliases", {})}


def markdown() -> str:
    c = checklist(with_live=False)
    out = ["| # | Item | Status | How (here) | Code | GCP-native equivalent | Limits |", "|---|---|---|---|---|---|---|"]
    for it in c["items"]:
        code = "<br>".join(f"`{r['file']}:{r['line']}`" if r.get("line") else f"`{r['file']}`" for r in it["code"])
        out.append(f"| {it['id']} | {it['title']} | {it['status_label']} | {it['how']} | {code} | {it['gcp']} | {it.get('limits', '')} |")
    out += ["", "| Stage | OSS here | GCP-native | Integration boundary | Status | Note |", "|---|---|---|---|---|---|"]
    for o in c["ownership"]:
        out.append(f"| {o['stage']} | {o['oss']} | {o['gcp']} | {o['boundary']} | {o['status_label']} | {o['note']} |")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.evidence")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.markdown:
        print(markdown())
        return 0
    c = checklist()
    if a.json:
        print(json.dumps(c, indent=1, default=str))
        return 0
    for area, title in AREAS.items():
        print(f"\n{area} · {title}")
        for it in (i for i in c["items"] if i["area"] == area):
            print(f"  {it['id']:<3} {it['status_label']:<32} {it['title']}")
            for e in it["evidence"][:3]:
                print(f"        ↳ {e['label']}")
    print("\n" + ", ".join(f"{STATUS_LABEL[k]}: {v}" for k, v in c["counts"].items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
