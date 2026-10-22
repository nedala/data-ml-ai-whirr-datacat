"""Batch scoring → dataset shaping.

Scores every customer snapshot in the chosen months with the champion and
writes `mlops.scored_customers` (ban, snapshot_month, score, decile,
risk band, model version, top-3 reason codes). The view
`mlops.v_score_outcomes` joins those scores with realised churn once the
90-day window closes — the table BI (Superset / Power BI) and the
performance monitor read.

Reason codes are model-agnostic: for each feature, replace the customer's
value with the training-population typical value (median / mode) and
measure how much the score falls; the three largest positive contributions
become human-readable reasons.
"""

from __future__ import annotations

import json
import time
import uuid

import numpy as np
import pandas as pd

from . import config, db

REASON_TEXT = {
    "recent_price_increase": "recent price increase",
    "promo_expired": "promotion expired",
    "monthly_charge": "high monthly charge",
    "charge_x_increase": "price increase on a high bill",
    "outage_minutes": "outage minutes",
    "tickets_month": "service tickets this month",
    "tickets_decayed": "repeat service tickets",
    "truck_rolls_90d": "repair truck rolls (90 days)",
    "line_quality_bad": "poor line / optical quality",
    "calls_30d": "care calls (30 days)",
    "calls_decayed": "repeat care calls",
    "dispatches_90d": "field dispatches (90 days)",
    "disputes_90d": "billing disputes (90 days)",
    "ont_rx_power_dbm": "weak ONT optical signal",
    "ont_los_events": "ONT loss-of-signal events",
    "dsl_snr_margin_db": "low DSL SNR margin",
    "dsl_retrains": "DSL line retrains",
    "late_payments_decayed": "late payments",
    "nps_last": "low NPS",
    "last_survey_score": "low survey score",
    "competitor_fiber_available": "competitor fiber available",
    "technology": "access technology",
    "tenure_months": "tenure",
    "autopay": "no autopay",
    "credit_band": "credit band",
    "avg_latency_ms": "high latency",
    "gb_down": "usage level",
    "plan_id": "plan",
    "segment": "segment",
    "state": "state",
}


def reason_codes(pipe, X: pd.DataFrame, reference: pd.DataFrame, features: list[str], top: int = 3) -> list[list[dict]]:
    base = pipe.predict_proba(X[features])[:, 1]
    typical = {}
    for f in features:
        col = reference[f]
        typical[f] = col.mode().iat[0] if col.dtype == object else float(pd.to_numeric(col, errors="coerce").median())
    contrib = np.zeros((len(X), len(features)))
    for j, f in enumerate(features):
        Xp = X[features].copy()
        Xp[f] = typical[f]
        contrib[:, j] = base - pipe.predict_proba(Xp)[:, 1]
    vals = X[features].to_numpy(dtype=object)
    tops = np.argsort(-contrib, axis=1)[:, :top]
    out = []
    for i in range(len(X)):
        out.append([{"feature": features[j], "value": _v(vals[i, j]), "impact": round(float(contrib[i, j]), 4),
                     "text": f"{REASON_TEXT.get(features[j], features[j])} ({_v(vals[i, j])})"}
                    for j in tops[i] if contrib[i, j] > 0])
    return out


def _v(x):
    if isinstance(x, (np.floating, float)):
        return None if np.isnan(x) else round(float(x), 2)
    if isinstance(x, np.integer):
        return int(x)
    return x


def batch_score(months: list | None = None, version: str | None = None, last_k: int = 6, verbose: bool = True,
                reasons: bool = True) -> dict:
    from .data import months as all_months
    from .featurestore import get_historical_features
    from .registry import alias_version, load_version, model_features, reference_sample

    db.ensure_schema()
    t0 = time.time()
    if version is None:
        ch = alias_version("champion")
        if ch is None:
            raise RuntimeError("no champion registered")
        version = str(ch.version)
    pipe = load_version(version)
    feats = model_features(version)
    ref = reference_sample(version)
    ms = months or all_months()[-last_k:]
    ms = [pd.Timestamp(m).date().isoformat() for m in ms]
    ent = db.query_df(f"select ban, snapshot_month from features.churn_base where snapshot_month = any(%s::date[])",
                      (ms,))
    X = get_historical_features(ent, feats)
    X["score"] = pipe.predict_proba(X[feats])[:, 1]
    X["decile"] = X.groupby("snapshot_month").score.transform(
        lambda s: pd.qcut(s.rank(method="first", ascending=False), 10, labels=range(1, 11)).astype(int))
    X["risk_band"] = np.select([X.decile <= 1, X.decile <= 3], ["high", "elevated"], "standard")
    rc = reason_codes(pipe, X, ref, feats) if reasons else [[] for _ in range(len(X))]
    batch = uuid.uuid4().hex[:12]
    run_id = getattr(alias_version("champion"), "run_id", None)
    rows = []
    for i, r in enumerate(X[["ban", "snapshot_month", "score", "decile", "risk_band"]].itertuples(index=False)):
        codes = rc[i]
        texts = [c["text"] for c in codes] + [None, None, None]
        rows.append((r.ban, pd.Timestamp(r.snapshot_month).date(), float(r.score), int(r.decile), r.risk_band,
                     config.MODEL_NAME, version, run_id, texts[0], texts[1], texts[2], json.dumps(codes), batch))
    import psycopg2.extras

    with db.connect() as c, c.cursor() as cur:
        psycopg2.extras.execute_values(cur, """insert into mlops.scored_customers (ban, snapshot_month, score, decile,
            risk_band, model_name, model_version, run_id, reason_1, reason_2, reason_3, reason_codes, batch_id) values %s
            on conflict (ban, snapshot_month, model_version) do update set score=excluded.score, decile=excluded.decile,
            risk_band=excluded.risk_band, reason_1=excluded.reason_1, reason_2=excluded.reason_2, reason_3=excluded.reason_3,
            reason_codes=excluded.reason_codes, batch_id=excluded.batch_id, scored_at=now()""", rows, page_size=2000)
    db.ensure_monitoring_view()
    res = {"rows": len(rows), "months": ms, "model_version": version, "batch_id": batch, "seconds": round(time.time() - t0, 1)}
    if verbose:
        print(f"  · scored {len(rows):,} customer-months ({ms[0]} → {ms[-1]}) with v{version} in {res['seconds']}s")
    return res


def outcome_monitor(model_version: str | None = None) -> pd.DataFrame:
    """Realised churn by score decile (where the outcome is known) — the
    business check that the ranking holds up."""
    where = "where outcome_known"
    params = None
    if model_version:
        where += " and model_version = %s"
        params = (model_version,)
    return db.query_df(f"""select model_version, decile, count(*) customers, avg(score) mean_score,
                               avg(churned_next_90d::int) realised_churn
                           from mlops.v_score_outcomes {where} group by 1, 2 order by 1, 2""", params)
