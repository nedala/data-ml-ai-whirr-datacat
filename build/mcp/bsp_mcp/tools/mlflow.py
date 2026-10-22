"""MLflow tracking server (REST API, read-only): experiments, run leaderboard, registered models, aliases."""
from __future__ import annotations

import os
from typing import Annotated, Any

import httpx
from pydantic import Field

from ..common import UserError, clamp, md_table, result, tool

G = "mlflow"


def _api(method: str, path: str, **kw: Any) -> dict:
    base = os.environ.get("MLFLOW_TRACKING_URI", "http://mlflow:5000/mlflow").rstrip("/")
    try:
        with httpx.Client(timeout=20, trust_env=False) as c:
            r = c.request(method, f"{base}/api/2.0/mlflow/{path}", **kw)
    except httpx.HTTPError as e:
        raise UserError(f"MLflow not reachable at {base} ({type(e).__name__}) — start the MLOps capability") from None
    if r.status_code == 404:
        raise UserError(f"MLflow: not found ({r.json().get('message', '') if r.headers.get('content-type', '').startswith('application/json') else ''})")
    if r.status_code >= 400:
        raise UserError(f"MLflow HTTP {r.status_code}: {r.text[:300]}")
    return r.json()


@tool(G, "List MLflow experiments")
def mlflow_list_experiments(name_contains: Annotated[str | None, Field(description="Filter by name substring")] = None) -> dict:
    """Experiments on the tracking server (id, name, lifecycle, last update)."""
    body: dict[str, Any] = {"max_results": 200}
    if name_contains:
        body["filter"] = f"name LIKE '%{name_contains.replace(chr(39), '')}%'"
    exps = _api("POST", "experiments/search", json=body).get("experiments") or []
    rows = [{"experiment_id": e["experiment_id"], "name": e["name"], "lifecycle": e.get("lifecycle_stage"),
             "last_update_time": e.get("last_update_time")} for e in exps]
    return result(f"{len(rows)} experiment(s)\n\n" + md_table(rows, 30), experiments=rows)


@tool(G, "Leaderboard of runs in an experiment")
def mlflow_leaderboard(
    experiment: Annotated[str, Field(description="Experiment name or id")],
    metric: Annotated[str, Field(description="Metric to rank by, e.g. 'auc', 'val_auc', 'rmse'")] = "auc",
    ascending: Annotated[bool, Field(description="true for loss-like metrics (lower is better)")] = False,
    top: Annotated[int, Field(description="Runs to return (1–50)")] = 10,
) -> dict:
    """Top runs of an experiment ranked by a metric, with key params and tags."""
    eid = experiment
    if not experiment.isdigit():
        eid = (_api("GET", "experiments/get-by-name", params={"experiment_name": experiment}).get("experiment") or {}).get("experiment_id")
    runs = _api("POST", "runs/search", json={"experiment_ids": [eid], "max_results": clamp(top, 1, 50),
                                              "order_by": [f"metrics.`{metric}` {'ASC' if ascending else 'DESC'}"],
                                              "filter": f"metrics.`{metric}` > -1e30"}).get("runs") or []
    rows = []
    for r in runs:
        info, data = r.get("info") or {}, r.get("data") or {}
        metrics = {m["key"]: m["value"] for m in data.get("metrics") or []}
        params = {p["key"]: p["value"] for p in data.get("params") or []}
        rows.append({"run_id": info.get("run_id"), "run_name": info.get("run_name"), metric: metrics.get(metric),
                     "status": info.get("status"), "params": dict(list(params.items())[:8]),
                     "other_metrics": {k: v for k, v in metrics.items() if k != metric}})
    return result(f"top {len(rows)} run(s) by {metric}\n\n" + md_table([{k: v for k, v in r.items() if k not in ('params', 'other_metrics')}
                                                                       for r in rows]), runs=rows)


@tool(G, "List registered models and aliases")
def mlflow_registered_models(name_contains: Annotated[str | None, Field(description="Filter by name substring")] = None) -> dict:
    """Registered models with latest versions and aliases (e.g. champion / challenger)."""
    params: dict[str, Any] = {"max_results": 200}
    if name_contains:
        params["filter"] = f"name LIKE '%{name_contains.replace(chr(39), '')}%'"
    models = _api("GET", "registered-models/search", params=params).get("registered_models") or []
    rows = [{"name": m["name"], "aliases": {a["alias"]: a["version"] for a in m.get("aliases") or []},
             "latest_versions": [v.get("version") for v in m.get("latest_versions") or []],
             "description": (m.get("description") or "")[:120]} for m in models]
    return result(f"{len(rows)} registered model(s)\n\n" + md_table(rows), models=rows)


@tool(G, "Resolve a model alias to a version")
def mlflow_model_alias(name: Annotated[str, Field(description="Registered model name")],
                       alias: Annotated[str, Field(description="Alias, e.g. champion")] = "champion") -> dict:
    """Which version an alias points to, with its run id, source and tags."""
    v = _api("GET", "registered-models/alias", params={"name": name, "alias": alias}).get("model_version") or {}
    return result(f"{name}@{alias} → version {v.get('version')} (run {v.get('run_id')})", version=v)
