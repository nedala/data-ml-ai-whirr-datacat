"""Drift detection on live traffic (mlops.inference_log) vs the champion's
training reference sample (logged with its MLflow run).

  feature drift      PSI per feature (quantile bins from the reference;
                     categories for strings) + two-sample KS for numerics
  prediction drift   PSI of the served score distribution
  performance drift  AUC of logged scores against realised churn, once the
                     90-day outcome of the scored snapshot is known

A report is breached when ≥ MIN_BREACHED_FEATURES features exceed the PSI
breach threshold, or prediction PSI exceeds it, or AUC fell by more than
AUC_DROP_BREACH. Every check is stored in mlops.drift_reports.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from . import config, db

EPS = 1e-4


def psi(ref, cur, bins: int = 10, categorical: bool = False) -> float:
    ref = pd.Series(ref).dropna()
    cur = pd.Series(cur).dropna()
    if len(ref) == 0 or len(cur) == 0:
        return float("nan")
    if categorical or ref.dtype == object:
        cats = sorted(set(ref.astype(str)) | set(cur.astype(str)))
        r = ref.astype(str).value_counts(normalize=True).reindex(cats, fill_value=0).to_numpy()
        c = cur.astype(str).value_counts(normalize=True).reindex(cats, fill_value=0).to_numpy()
    else:
        ref = ref.astype(float)
        cur = cur.astype(float)
        edges = np.unique(np.quantile(ref, np.linspace(0, 1, bins + 1)))
        if len(edges) < 2:
            edges = np.array([ref.min() - 1, ref.max() + 1])
        edges[0], edges[-1] = -np.inf, np.inf
        if len(edges) == 2:  # constant reference: split at the value
            v = ref.iloc[0]
            edges = np.array([-np.inf, v - 1e-9, v + 1e-9, np.inf])
        r = np.histogram(ref, edges)[0] / len(ref)
        c = np.histogram(cur, edges)[0] / len(cur)
    r = np.clip(r, EPS, None)
    c = np.clip(c, EPS, None)
    return float(np.sum((c - r) * np.log(c / r)))


def _open(edges: np.ndarray) -> np.ndarray:
    e = np.array(edges, dtype=float)
    if len(e) < 2:
        e = np.array([e[0] - 1, e[0] + 1]) if len(e) else np.array([-1.0, 1.0])
    e[0], e[-1] = -np.inf, np.inf
    return e


def ks(ref, cur) -> tuple[float, float]:
    from scipy.stats import ks_2samp

    a = pd.Series(ref).dropna().astype(float)
    b = pd.Series(cur).dropna().astype(float)
    if len(a) < 5 or len(b) < 5:
        return float("nan"), float("nan")
    r = ks_2samp(a, b)
    return float(r.statistic), float(r.pvalue)


def status_of(p: float) -> str:
    if not np.isfinite(p):
        return "n/a"
    return "breach" if p > config.PSI_BREACH else "warn" if p > config.PSI_WARN else "ok"


def compare(reference: pd.DataFrame, current: pd.DataFrame, features: list[str]) -> list[dict]:
    out = []
    for f in features:
        if f not in reference or f not in current:
            continue
        cat = reference[f].dtype == object
        p = psi(reference[f], current[f], categorical=cat)
        k, pv = (float("nan"), float("nan")) if cat else ks(reference[f], current[f])
        out.append({"feature": f, "psi": round(p, 4), "ks": None if not np.isfinite(k) else round(k, 4),
                    "ks_pvalue": None if not np.isfinite(pv) else float(f"{pv:.3g}"), "status": status_of(p),
                    "ref_mean": None if cat else round(float(pd.to_numeric(reference[f], errors='coerce').mean()), 4),
                    "cur_mean": None if cat else round(float(pd.to_numeric(current[f], errors='coerce').mean()), 4)})
    return sorted(out, key=lambda r: -(r["psi"] if np.isfinite(r["psi"]) else -1))


def current_window(deployment: str = config.DEPLOYMENT, minutes: float | None = None, last_n: int = 3000,
                   since=None) -> pd.DataFrame:
    """Served (non-shadow) inferences: the last `last_n`, optionally limited
    to the last `minutes` or to rows after `since`."""
    where, params = ["deployment = %s", "not shadow"], [deployment]
    if minutes:
        where.append("ts >= now() - %s * interval '1 minute'")
        params.append(minutes)
    if since is not None:
        where.append("ts > %s")
        params.append(since)
    rows = db.fetchall(f"""select ts, ban, snapshot_month, model_version, score, features from mlops.inference_log
                           where {' and '.join(where)} order by id desc limit %s""", params + [last_n])
    if not rows:
        return pd.DataFrame()
    feats = pd.DataFrame([r["features"] or {} for r in rows])
    meta = pd.DataFrame([{k: r[k] for k in ("ts", "ban", "snapshot_month", "model_version", "score")} for r in rows])
    return pd.concat([meta, feats], axis=1)


def _reference(version: str) -> pd.DataFrame:
    from .registry import reference_sample

    return reference_sample(version)


def _labels(df: pd.DataFrame) -> pd.DataFrame:
    """Realised churn for logged (ban, snapshot_month), when complete."""
    d = df.dropna(subset=["snapshot_month"])
    if d.empty:
        return pd.DataFrame()
    keys = list({(a, str(m)[:10]) for a, m in zip(d.ban, d.snapshot_month)})
    rows = db.fetchall(f"""select ban, snapshot_month::text m, ({config.LABEL})::int y from features.churn_base
                           where label_complete and (ban, snapshot_month) in
                           (select a, m::date from unnest(%s::text[], %s::text[]) as t(a, m))""",
                       ([k[0] for k in keys], [k[1] for k in keys]))
    if not rows:
        return pd.DataFrame()
    lab = pd.DataFrame(rows)
    d = d.assign(m=d.snapshot_month.astype(str).str[:10])
    return d.merge(lab, on=["ban", "m"], how="inner")


def run_check(deployment: str = config.DEPLOYMENT, minutes: float | None = None, last_n: int = 3000,
              source: str = "manual", since=None, min_rows: int = 200, write: bool = True) -> dict:
    from sklearn.metrics import roc_auc_score

    from .registry import alias_version, client, model_features

    db.ensure_schema()
    champ = alias_version("champion")
    if champ is None:
        return {"status": "no_champion"}
    version = str(champ.version)
    cur = current_window(deployment, minutes, last_n, since)
    if len(cur):
        cur_v = cur[cur.model_version == version]
        cur = cur_v if len(cur_v) >= min_rows else cur
    if len(cur) < min_rows:
        return {"status": "insufficient_data", "n_current": int(len(cur)), "min_rows": min_rows}
    ref = _reference(version)
    feats = model_features(version)
    for f in feats:
        if f in cur and ref[f].dtype != object:
            cur[f] = pd.to_numeric(cur[f], errors="coerce")
    fd = compare(ref, cur, feats)
    pred_psi = psi(ref["score"], cur["score"])
    n_breach = sum(1 for r in fd if r["status"] == "breach")
    max_psi = max((r["psi"] for r in fd if np.isfinite(r["psi"])), default=0.0)
    # performance drift (only where outcomes are known)
    auc_ref, train_end = None, None
    try:
        run = client().get_run(champ.run_id)
        auc_ref = float(run.data.metrics.get("test_auc"))
        tw = run.data.params.get("train_window", "")
        train_end = tw.split("..")[1] + "-01" if ".." in tw else None
    except Exception:
        pass
    lab = _labels(cur)
    if len(lab) and train_end:
        # performance is only judged out of sample: snapshots after the champion's training window
        lab = lab[lab.m > train_end]
    auc_cur = None
    if len(lab) >= min_rows and lab.y.nunique() == 2:
        auc_cur = float(roc_auc_score(lab.y, lab.score))
    reasons = []
    if n_breach >= config.MIN_BREACHED_FEATURES:
        top = ", ".join(f"{r['feature']} {r['psi']:.2f}" for r in fd if r["status"] == "breach")
        reasons.append(f"{n_breach} features PSI > {config.PSI_BREACH} ({top})")
    if np.isfinite(pred_psi) and pred_psi > config.PSI_BREACH:
        reasons.append(f"prediction PSI {pred_psi:.2f} > {config.PSI_BREACH}")
    if auc_ref is not None and auc_cur is not None and auc_ref - auc_cur > config.AUC_DROP_BREACH:
        reasons.append(f"AUC fell {auc_ref:.3f} → {auc_cur:.3f}")
    rep = {"deployment": deployment, "model_version": version,
           "window_start": pd.Timestamp(cur.ts.min()).to_pydatetime(), "window_end": pd.Timestamp(cur.ts.max()).to_pydatetime(),
           "n_reference": int(len(ref)), "n_current": int(len(cur)), "feature_drift": fd,
           "prediction_psi": round(float(pred_psi), 4), "max_psi": round(float(max_psi), 4), "n_features_breached": n_breach,
           "auc_reference": auc_ref, "auc_current": auc_cur, "n_labeled": int(len(lab)), "breached": bool(reasons),
           "breach_reasons": reasons, "source": source}
    if write:
        row = db.fetchone("""insert into mlops.drift_reports (deployment, model_version, window_start, window_end, n_reference,
                 n_current, feature_drift, prediction_psi, max_psi, n_features_breached, auc_reference, auc_current, n_labeled,
                 breached, breach_reasons, source) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id, created_at""",
                          (deployment, version, rep["window_start"], rep["window_end"], rep["n_reference"], rep["n_current"],
                           json.dumps(fd), rep["prediction_psi"], rep["max_psi"], n_breach, auc_ref, auc_cur, rep["n_labeled"],
                           rep["breached"], json.dumps(reasons), source))
        rep.update(id=row["id"], created_at=row["created_at"])
    rep["status"] = "breach" if rep["breached"] else "ok"
    return rep


def recent_reports(limit: int = 50) -> list[dict]:
    return db.fetchall("select * from mlops.drift_reports order by id desc limit %s", (limit,))


def since_minutes(m: float):
    return datetime.now(timezone.utc) - timedelta(minutes=m)
