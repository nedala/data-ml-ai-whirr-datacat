"""Outcome measurement — what the model actually achieved once the truth arrived.

    delayed_label_eval()        scores logged at serve time (mlops.inference_log, served AND
                                shadow) joined to realised churn when the 90-day label window
                                closes → realised AUC / lift / calibration per scoring cohort
                                (snapshot month) and per serving week vs the AUC expected at
                                release → mlops.label_evaluations (+ alert on degradation)
    champion_vs_challenger()    paired comparison on the SAME requests (shadow: both models
                                scored every request) — paired bootstrap CI + DeLong of the AUC
                                difference once labels exist, agreement before
    canary_compare()            canary vs stable arms (independent samples)
    retention_offer_ab()        business A/B: a retention offer to the model's top-risk customers,
                                randomised treatment vs control, two-proportion z-test, CI,
                                power. The customers, scores and churn labels are the warehouse's;
                                the TREATMENT EFFECT is simulated (no real offer was sent) and is
                                reported as such.

CLI:  python -m bsp_mlops.outcomes labels | shadow | canary | ab [--effect 0.25]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import timedelta
from functools import lru_cache

import numpy as np
import pandas as pd

from . import config, db
from .config import LABEL

MIN_LABELLED = 200


@lru_cache(maxsize=64)
def expected_auc(version: str) -> float | None:
    from . import registry

    try:
        mv = registry.client().get_model_version(config.MODEL_NAME, str(version))
        r = registry.client().get_run(mv.run_id)
        return float(r.data.metrics.get("test_auc")) if "test_auc" in r.data.metrics else None
    except Exception:
        return None


def _metrics(g: pd.DataFrame) -> dict:
    from .stats import auc, ece, lift_at

    lab = g[g.y.notna()]
    y = lab.y.astype(int).to_numpy()
    p = lab.score.to_numpy()
    ok = len(lab) >= 30 and 0 < y.sum() < len(y)
    return {"n": int(len(g)), "n_labelled": int(len(lab)), "n_pending": int(len(g) - len(lab)), "positives": int(y.sum()) if len(y) else 0,
            "realised_auc": auc(y, p) if ok else None, "lift_at_10": lift_at(y, p) if ok else None,
            "ece": ece(y, p) if ok else None, "mean_score": float(g.score.mean()) if len(g) else None,
            "realised_rate": float(y.mean()) if len(y) else None}


def _logged(deployment: str, limit: int = 300_000) -> pd.DataFrame:
    rows = db.fetchall(f"""select l.id, l.ts, l.request_id, l.model_version, l.shadow, l.colour, l.snapshot_month, l.score, l.ban,
                              case when c.label_complete then c.{LABEL}::int end as y, c.technology
                           from mlops.inference_log l
                           left join features.churn_base c on c.ban = l.ban and c.snapshot_month = l.snapshot_month
                           where l.deployment = %s and l.snapshot_month is not null
                           order by l.id desc limit %s""", (deployment, limit))
    df = pd.DataFrame(rows)
    if len(df):
        df["y"] = pd.to_numeric(df["y"], errors="coerce")
        df["score"] = df["score"].astype(float)
    return df


def delayed_label_eval(deployment: str = config.DEPLOYMENT, write: bool = True, source: str = "manual") -> dict:
    db.ensure_schema()
    df = _logged(deployment)
    if df.empty:
        return {"status": "no_traffic"}
    df["role"] = np.where(df.shadow, "shadow", "served")
    df["week"] = pd.to_datetime(df.ts, utc=True).dt.tz_convert(None).dt.to_period("W").dt.start_time.dt.date.astype(str)
    df["cohort"] = df.snapshot_month.astype(str).str[:10]
    ends = {v: train_end(v) for v in df.model_version.unique()}
    df["in_sample"] = [ends.get(v) is not None and pd.Timestamp(c) <= ends[v] for v, c in zip(df.model_version, df.cohort)]
    cohorts, weeks, overall = [], [], []
    for (role, v, c), g in df.groupby(["role", "model_version", "cohort"]):
        m = _metrics(g)
        exp = expected_auc(v)
        due = (pd.Timestamp(c).to_pydatetime() + timedelta(days=90)).date().isoformat()
        cohorts.append({"role": role, "model_version": v, "cohort": c, "label_due": due, "in_sample": bool(g.in_sample.iloc[0]), **m, "expected_auc": exp,
                        "auc_gap": (m["realised_auc"] - exp) if m["realised_auc"] is not None and exp is not None else None})
    oos = df[~df.in_sample]           # performance is only judged on cohorts the version did not train on
    for (role, v, w), g in oos.groupby(["role", "model_version", "week"]):
        m = _metrics(g)
        weeks.append({"role": role, "model_version": v, "week": w, **m, "expected_auc": expected_auc(v)})
    for (role, v), g0 in df.groupby(["role", "model_version"]):
        g = g0[~g0.in_sample]
        m = _metrics(g)
        exp = expected_auc(v)
        overall.append({"role": role, "model_version": v, **m, "in_sample_excluded": int(g0.in_sample.sum()), "expected_auc": exp,
                        "auc_gap": (m["realised_auc"] - exp) if m["realised_auc"] is not None and exp is not None else None})
    breaches = [o for o in overall if o["role"] == "served" and o["n_labelled"] >= MIN_LABELLED and o["auc_gap"] is not None
                and o["auc_gap"] < -config.AUC_DROP_BREACH]
    res = {"status": "ok", "deployment": deployment, "n_scored": int(len(df)), "n_labelled": int(df.y.notna().sum()),
           "n_pending": int(df.y.isna().sum()), "overall": sorted(overall, key=lambda r: (r["role"], -int(r["model_version"]))),
           "cohorts": sorted(cohorts, key=lambda r: (r["role"], r["model_version"], r["cohort"])), "weeks": weeks,
           "breached": bool(breaches), "breach_threshold": config.AUC_DROP_BREACH,
           "note": "Realised AUC uses only rows whose 90-day churn label is complete (pending rows are counted, never guessed) and only "
                   "cohorts after the version's training window (in-sample cohorts are listed but not judged)."}
    if write:
        row = db.fetchone("""insert into mlops.label_evaluations (deployment, n_scored, n_labelled, n_pending, result, breached, source)
                             values (%s,%s,%s,%s,%s,%s,%s) returning id, created_at""",
                          (deployment, res["n_scored"], res["n_labelled"], res["n_pending"], db.jdump(res), res["breached"], source))
        res.update(id=row["id"], created_at=row["created_at"])
        if breaches:
            from .alerts import raise_alert

            b = breaches[0]
            raise_alert("performance", f"Realised AUC of v{b['model_version']} is {b['realised_auc']:.3f} vs {b['expected_auc']:.3f} expected",
                        "warning", {"label_evaluation_id": row["id"], **{k: b[k] for k in ("n_labelled", "realised_auc", "expected_auc")}},
                        dedupe_key=f"perf:v{b['model_version']}")
    return res


def latest_label_eval() -> dict | None:
    r = db.fetchone("select id, created_at, result from mlops.label_evaluations order by id desc limit 1")
    if not r:
        return None
    return {**r["result"], "id": r["id"], "created_at": r["created_at"]}


def _store(kind: str, name: str, champ, chall, status: str, result: dict, by: str | None) -> dict:
    row = db.fetchone("""insert into mlops.experiments (kind, name, champion_version, challenger_version, status, result, created_by)
                         values (%s,%s,%s,%s,%s,%s,%s) returning id, created_at""",
                      (kind, name, str(champ) if champ else None, str(chall) if chall else None, status, db.jdump(result), by or config.user()))
    return {**result, "id": row["id"], "created_at": row["created_at"]}


@lru_cache(maxsize=64)
def train_end(version: str):
    from . import registry

    try:
        r = registry.client().get_run(registry.client().get_model_version(config.MODEL_NAME, str(version)).run_id)
        tw = r.data.params.get("train_window", "")
        return pd.Timestamp(tw.split("..")[1] + "-01") if ".." in tw else None
    except Exception:
        return None


def champion_vs_challenger(deployment: str = config.DEPLOYMENT, since_id: int = 0, write: bool = True, by: str | None = None) -> dict:
    """Paired comparison on shadow traffic: every request scored by both."""
    from .stats import delong, paired_bootstrap_auc

    db.ensure_schema()
    rows = db.fetchall(f"""select l.request_id, l.model_version v_served, l.score s_served, s.model_version v_shadow, s.score s_shadow,
                              l.snapshot_month, case when c.label_complete then c.{LABEL}::int end as y
                           from mlops.inference_log l
                           join mlops.inference_log s on s.request_id = l.request_id and s.shadow and not l.shadow
                           left join features.churn_base c on c.ban = l.ban and c.snapshot_month = l.snapshot_month
                           where l.deployment = %s and l.id > %s order by l.id desc limit 200000""", (deployment, since_id))
    if not rows:
        return {"status": "no_pairs", "detail": "no request was scored by both champion and challenger (start shadow mode)"}
    df = pd.DataFrame(rows)
    counts = df.groupby(["v_served", "v_shadow"]).size().sort_values(ascending=False)
    try:      # prefer the CURRENT champion / challenger pair when it has traffic
        from . import registry

        al = registry.aliases()
        cur = (al.get("champion"), al.get("challenger"))
    except Exception:
        cur = (None, None)
    pair = cur if cur in counts.index else counts.index[0]
    d = df[(df.v_served == pair[0]) & (df.v_shadow == pair[1])].copy()
    d["y"] = pd.to_numeric(d["y"], errors="coerce")
    s1, s2 = d.s_served.astype(float).to_numpy(), d.s_shadow.astype(float).to_numpy()
    k = max(1, int(round(0.1 * len(d))))
    top1, top2 = set(np.argsort(-s1)[:k]), set(np.argsort(-s2)[:k])
    res = {"status": "ok", "mode": "shadow (paired: same requests)", "champion_version": pair[0], "challenger_version": pair[1],
           "n_pairs": int(len(d)), "corr": float(np.corrcoef(s1, s2)[0, 1]) if len(d) > 2 else None,
           "mean_abs_diff": float(np.mean(np.abs(s1 - s2))), "top_decile_overlap": len(top1 & top2) / k,
           "mean_score_champion": float(s1.mean()), "mean_score_challenger": float(s2.mean())}
    lab = d[d.y.notna()]
    # judge only out of sample: snapshot months after BOTH models' training windows
    cut = max(filter(None, (train_end(pair[0]), train_end(pair[1]))), default=None)
    if cut is not None:
        n0 = len(lab)
        lab = lab[pd.to_datetime(lab.snapshot_month) > cut]
        res["in_sample_excluded"] = int(n0 - len(lab))
        res["out_of_sample_after"] = cut.date().isoformat()
    res["n_labelled"] = int(len(lab))
    if len(lab) >= MIN_LABELLED and 0 < lab.y.sum() < len(lab):
        y = lab.y.astype(int).to_numpy()
        bs = paired_bootstrap_auc(y, lab.s_shadow.astype(float).to_numpy(), lab.s_served.astype(float).to_numpy())
        dl = delong(y, lab.s_shadow.astype(float).to_numpy(), lab.s_served.astype(float).to_numpy())
        res.update(auc_challenger=dl["auc_1"], auc_champion=dl["auc_2"], delta_auc=bs["diff"], ci_low=bs["ci_low"], ci_high=bs["ci_high"],
                   delong_p=dl["p_value"], positives=int(y.sum()),
                   verdict="challenger better" if bs["ci_low"] > 0 else "champion better" if bs["ci_high"] < 0 else "no significant difference")
    else:
        res["verdict"] = (f"agreement only — {len(lab)} out-of-sample labelled pairs (< {MIN_LABELLED}); the AUC comparison waits "
                          "for 90-day labels on months neither model trained on")
    return _store("shadow_paired", f"shadow v{pair[1]} vs champion v{pair[0]}", pair[0], pair[1], "ok", res, by) if write else res


def canary_compare(deployment: str = config.DEPLOYMENT, since_id: int = 0, write: bool = True, by: str | None = None) -> dict:
    from .stats import bootstrap_auc_diff_independent

    df = _logged(deployment)
    df = df[(df.id > since_id) & (~df.shadow) & (df.colour.isin(["canary", "stable"]))]
    if df.empty:
        return {"status": "no_canary_traffic"}
    arms = {c: g for c, g in df.groupby("colour")}
    res = {"status": "ok", "mode": "canary (independent arms, sticky per BAN)", "arms": {}}
    for c, g in arms.items():
        res["arms"][c] = {"model_version": g.model_version.iloc[0], **_metrics(g)}
    if {"canary", "stable"} <= set(arms):
        a, b = arms["canary"].dropna(subset=["y"]), arms["stable"].dropna(subset=["y"])
        if len(a) >= 100 and len(b) >= 100:
            bs = bootstrap_auc_diff_independent(a.y, a.score, b.y, b.score)
            res.update(delta_auc=bs["diff"], ci_low=bs["ci_low"], ci_high=bs["ci_high"])
    champ = res["arms"].get("stable", {}).get("model_version")
    chall = res["arms"].get("canary", {}).get("model_version")
    return _store("canary", f"canary v{chall} vs stable v{champ}", champ, chall, "ok", res, by) if write else res


def _arm(ban: str, salt: str) -> str:
    return "treatment" if int(hashlib.md5(f"{salt}:{ban}".encode()).hexdigest()[:8], 16) % 2 == 0 else "control"


def assign_arms(df: pd.DataFrame, salt: str, strata: int = 10) -> np.ndarray:
    """Stratified (blocked) randomisation: within each score decile, customers are
    ordered by hash(salt, BAN) and alternate treatment / control — reproducible,
    unpredictable per customer, and balanced on risk by construction."""
    d = df[[config.ENTITY, "score"]].copy()
    d["stratum"] = pd.qcut(d.score.rank(method="first"), min(strata, len(d)), labels=False)
    d["h"] = [hashlib.md5(f"{salt}:{b}".encode()).hexdigest() for b in d[config.ENTITY]]
    d = d.sort_values(["stratum", "h"])
    first = d.groupby("stratum").cumcount()
    flip = d.stratum.map(lambda s: int(hashlib.md5(f"{salt}:stratum{s}".encode()).hexdigest()[:2], 16) % 2)
    d["arm"] = np.where((first + flip) % 2 == 0, "treatment", "control")
    return d["arm"].reindex(df.index).to_numpy()


def retention_offer_ab(month: str | None = None, target_share: float = 0.3, effect: float = 0.25, seed: int = 11,
                       write: bool = True, by: str | None = None) -> dict:
    """Retention-offer experiment on the champion's top-risk customers.

    Population: the latest month whose churn labels are complete; eligible = top
    `target_share` by champion score. Randomisation: stratified by score decile,
    hash(BAN)-ordered alternation → treatment / control.
    Outcome: control = the real label; treatment = the real label with each churner
    retained with probability `effect` (SIMULATED offer effect). The analysis —
    two-proportion z-test, CI, power — is exactly what a real readout would run."""
    from . import registry
    from .data import windows
    from .pipeline import training_set
    from .stats import auc, power_two_proportion, two_proportion

    champ = registry.alias_version("champion")
    if champ is None:
        return {"status": "no_champion"}
    chall = registry.alias_version("challenger")
    m = pd.Timestamp(month) if month else windows()["labelled"][-1]
    feats = registry.model_features(champ.version)
    df = training_set((m, m), feats)
    df["score"] = registry.load_version(champ.version).predict_proba(df[feats])[:, 1]
    cut = df.score.quantile(1 - target_share)
    elig = df[df.score >= cut].copy()
    elig["arm"] = assign_arms(elig, f"offer-{m:%Y%m}")
    rng = np.random.default_rng(seed)
    saved = (elig.arm == "treatment") & (elig[LABEL] == 1) & (rng.random(len(elig)) < effect)
    elig["churned"] = np.where(saved, 0, elig[LABEL]).astype(int)
    t, c = elig[elig.arm == "treatment"], elig[elig.arm == "control"]
    tp = two_proportion(int(t.churned.sum()), len(t), int(c.churned.sum()), len(c))
    need = power_two_proportion(max(c.churned.mean(), 1e-3), -effect)
    # A/A balance check: the arms' churn BEFORE the simulated effect (pure randomisation noise)
    pre = two_proportion(int(t[LABEL].sum()), len(t), int(c[LABEL].sum()), len(c))
    res = {"status": "ok", "synthetic_effect": True, "true_effect_parameter": effect, "month": m.date().isoformat(),
           "champion_version": str(champ.version), "eligible": int(len(elig)), "target_share": target_share,
           "score_cutoff": float(cut), "treatment": {"n": int(len(t)), "churned": int(t.churned.sum()), "rate": float(t.churned.mean())},
           "control": {"n": int(len(c)), "churned": int(c.churned.sum()), "rate": float(c.churned.mean())},
           "abs_uplift": -tp["diff"], "rel_uplift": -tp["relative"], "ci_low": -tp["ci_high"], "ci_high": -tp["ci_low"],
           "p_value": tp["p_value"], "z": tp["z"], "significant": tp["p_value"] < 0.05, "n_per_arm_for_80pct_power": need,
           "saved_per_1000_offers": 1000 * (-tp["diff"]),
           "balance_check": {"treatment_rate_without_offer": pre["rate_1"], "control_rate": pre["rate_2"], "p_value": pre["p_value"],
                             "detail": "churn of both arms before the simulated offer — any gap is randomisation noise "
                                       "(assignment is stratified by score decile, so risk is balanced by construction)"},
           "expected_abs_uplift": effect * float(t[LABEL].mean()),
           "untargeted_rate": float(df[LABEL].mean()), "targeted_control_rate": float(c.churned.mean()),
           "note": "Customers, scores and churn labels are real warehouse rows; the offer's effect is SIMULATED "
                   f"(each treated churner retained with probability {effect}). Randomisation stratified by score decile, hash(BAN) order."}
    if chall is not None and str(chall.version) != str(champ.version):
        f2 = registry.model_features(chall.version)
        d2 = training_set((m, m), f2)
        d2["s2"] = registry.load_version(chall.version).predict_proba(d2[f2])[:, 1]
        j = df[[config.ENTITY, LABEL, "score"]].merge(d2[[config.ENTITY, "s2"]], on=config.ENTITY)
        k = int(round(target_share * len(j)))
        cap1 = j.nlargest(k, "score")[LABEL].sum() / max(1, j[LABEL].sum())
        cap2 = j.nlargest(k, "s2")[LABEL].sum() / max(1, j[LABEL].sum())
        res["targeting"] = {"challenger_version": str(chall.version), "capture_champion": float(cap1), "capture_challenger": float(cap2),
                            "auc_champion": auc(j[LABEL], j.score), "auc_challenger": auc(j[LABEL], j.s2),
                            "detail": f"share of the month's churners inside the top {target_share:.0%} each model would target"}
    return _store("ab_retention_offer", f"retention offer A/B {m:%Y-%m} (simulated effect {effect})", champ.version,
                  chall.version if chall else None, "ok", res, by) if write else res


def experiments(limit: int = 30) -> list[dict]:
    return db.fetchall("select * from mlops.experiments order by id desc limit %s", (limit,))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.outcomes")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("labels")
    sub.add_parser("shadow")
    sub.add_parser("canary")
    ab = sub.add_parser("ab")
    ab.add_argument("--effect", type=float, default=0.25)
    ab.add_argument("--month")
    a = ap.parse_args(argv)
    db.ensure_schema()
    fn = {"labels": lambda: delayed_label_eval(source="cli"), "shadow": champion_vs_challenger, "canary": canary_compare,
          "ab": lambda: retention_offer_ab(a.month, effect=a.effect)}[a.cmd]
    out = fn()
    out = {k: v for k, v in out.items() if k not in ("cohorts", "weeks")}
    print(json.dumps(out, indent=1, default=str)[:6000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
