"""Run the FULL model lifecycle headless — every stage, in order, against the
running services, with release gates and approvals, plain-English narration and
a PASS / FAIL proof table. This is the source-controlled workflow; the control
tower's "Run the full lifecycle" button starts exactly this script.

    python -m bsp_mlops.demo --requester alice --approver bob --fresh-release-train
    python -m bsp_mlops.demo --requester alice --wait-approval 15     # approve in the tower (#release)
    python -m bsp_mlops.demo --trials 6 --no-notebook                 # faster
    make mlops-lifecycle REQUESTER=alice APPROVER=bob                 # same, from the host

Stages: integrate → EDA → feature selection → hold-outs → feature store →
leaderboard → Optuna → register (dev) → release gates → staging (+ a negative
control the gates must block) → four-eyes production approval → blue/green,
canary, shadow drills → serving reconciled with the registry → paired champion /
challenger statistics → telemetry + SLO → drift → alert → retrain → gates →
(approval + rollback drill) → delayed-label evaluation → retention-offer A/B →
batch scoring → reproduce the champion → export bundle → journeys + capability
evidence.

--approver records an operator-declared identity (source "cli"); in the tower
the approval comes from the signed-in person (source "sso-gate"). Needs:
postgres (bsp_edw, raw.* seeded), mlflow, model-gateway and mlops services.
Exit code 0 only if every proof point passed; the summary is written to
<data root>/mlops/runs/demo_summary.json and mlops.lifecycle_runs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx

from . import config, db

T0 = time.time()
PROOF: list[tuple[str, bool, str]] = []
SUMMARY: dict = {}
LIFECYCLE_ID: int | None = None
TOWER_HEADERS = {"X-Studio": "1"}


def banner(n: int, title: str, what: str) -> None:
    el = time.time() - T0
    print(f"\n{'═' * 92}\n STAGE {n:>2} │ {title}   [{el / 60:4.1f} min]\n{'─' * 92}\n {what}\n", flush=True)


def proof(name: str, ok: bool, detail: str = "") -> bool:
    PROOF.append((name, bool(ok), detail))
    print(f"   {'✔' if ok else '✘'} {name}: {detail}", flush=True)
    return ok


def tower(method: str, path: str, **kw):
    r = httpx.request(method, config.MLOPS_URL.rstrip("/") + path, headers=TOWER_HEADERS, timeout=kw.pop("timeout", 600), **kw)
    r.raise_for_status()
    return r.json()


def wait_http(url: str, name: str, timeout: float = 180) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if httpx.get(url, timeout=5).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError(f"{name} not reachable at {url}")


def served_counts(since_id: int) -> dict:
    rows = db.fetchall("""select model_version, colour, shadow, count(*) n from mlops.inference_log where id > %s
                          group by 1,2,3 order by 1,2,3""", (since_id,))
    return {f"v{r['model_version']}:{r['colour']}{':shadow' if r['shadow'] else ''}": r["n"] for r in rows}


def max_log_id() -> int:
    return int(db.fetchone("select coalesce(max(id), 0) m from mlops.inference_log")["m"])


def run(args) -> int:
    from . import data, deploy, drift, eda, featurestore, outcomes, pipeline, registry, release, reproduce, scoring, selection, simulator, splits
    from . import journey as JY

    # ── 0. preflight ──────────────────────────────────────────────────────
    banner(0, "Preflight", "Checking the warehouse and the three MLOps services are up.")
    if args.seed:
        from bsp_seed.generate import build
        from bsp_seed.load import to_postgres

        print(f"   seeding {args.seed} synthetic customers × 24 months into raw.* …", flush=True)
        to_postgres(build(customers=args.seed, months=24))
    wait_http(config.MLFLOW_URI.rstrip("/") + "/health", "mlflow")
    deploy.wait_ready()
    wait_http(config.MLOPS_URL.rstrip("/") + "/healthz", "mlops")
    db.ensure_schema()
    proof("services reachable", True, f"mlflow {config.MLFLOW_URI} · gateway {config.GATEWAY_URL} · tower {config.MLOPS_URL}")
    if args.fresh:
        _reset()
    if args.accounts:
        os.environ["CHURN_MAX_ACCOUNTS"] = str(args.accounts)    # fast variant: deterministic account sample
    # the model journey: one MLflow parent run; every stage below logs its evidence as a child run
    global JOURNEY
    J = JOURNEY = JY.Journey.start("training", title=f"demo · {config.MODEL_NAME} champion search")
    print(f"   journey {J.id} → control tower: /mlops/#journey?id={J.id}")

    # ── 1. data integration ───────────────────────────────────────────────
    banner(1, "Data integration", "Join the monthly customer snapshot (dbt mart if present, else raw) with NPS surveys and\n"
           " service tickets into the leakage-safe base table features.churn_base.")
    with J.stage("integration") as st:
        s = data.integrate()
        JY.safe(st, JY.integration_evidence, s)
    proof("integrated base table", s["n"] > 0, f"{s['n']:,} rows, {s['accounts']:,} accounts from {s['source']}")
    w = data.windows()
    print(f"   windows: train {w['train'][0]:%Y-%m}..{w['train'][1]:%Y-%m} · valid {w['valid'][0]:%Y-%m}..{w['valid'][1]:%Y-%m} · "
          f"test {w['test'][0]:%Y-%m}..{w['test'][1]:%Y-%m} · shift {w['shift'][0]:%Y-%m}..{w['shift'][-1]:%Y-%m}")

    # ── 2. EDA ────────────────────────────────────────────────────────────
    banner(2, "Exploratory data analysis", "Profile, churn by technology, and a preview of the late-period shift.")
    with J.stage("eda") as st:
        full = data.load()
        lab = full[full.label_complete == 1]
        print(eda.churn_by(lab, "technology").round(3).to_string())
        sp = eda.shift_preview(full, w["shift"][0]).head(5)
        print("\n   biggest moves in the shift window (mean, % change):")
        print(sp.round(3).to_string())
        JY.safe(st, JY.eda_evidence, full)
        if st:
            st.table("shift_preview", "Biggest moves in the shift window", eda.shift_preview(full, w["shift"][0]).head(8).reset_index())
    with J.stage("notebook") as st:
        JY.safe(st, JY.notebook_evidence, NOTEBOOKS, execute=None if args.no_notebook else "01_integrate_and_eda")
    proof("EDA", len(lab) > 0, f"churn rate {lab[config.LABEL].mean():.3%} over {len(lab):,} labelled snapshots; "
          f"top shift: {sp.index[0]} {sp.change_pct.iloc[0]:+.0f}%")

    # ── 3. feature sifting / selection ────────────────────────────────────
    banner(3, "Feature sifting & selection", "Missingness, variance, correlation/VIF, mutual information, permutation\n"
           " importance, SHAP, stability selection and a random probe — on the training window only.")
    train_df = full[(full.snapshot_month >= w["train"][0]) & (full.snapshot_month <= w["train"][1]) & (full.label_complete == 1)]
    with J.stage("selection") as st:
        rep = selection.sift(train_df, data.CANDIDATES, sample=args.sift_sample)
        for f, why in rep.dropped.items():
            print(f"     drop {f:<26} {why}")
        mlflow = pipeline.mlflow_setup(config.SELECTION_EXPERIMENT)
        with mlflow.start_run(run_name="feature-sifting", tags={"stage": "selection", **JY.link_tags()}) as sel_run:
            with tempfile.TemporaryDirectory() as td:
                rep.write(td)
                mlflow.log_artifacts(td, artifact_path="selection")
            mlflow.log_params({"n_candidates": len(rep.candidates), "n_kept": len(rep.kept)})
            mlflow.log_dict({"kept": rep.kept, "dropped": rep.dropped}, "selection/decision.json")
        fs_feats = set(featurestore.all_features())
        features = [f for f in rep.kept if f in fs_feats]
        JY.safe(st, JY.selection_evidence, rep.to_dict(), features)
        st.source_run(sel_run.info.run_id, "feature-sifting run (HTML report)", sel_run.info.experiment_id)
    proof("feature selection report", "probe_random" in rep.dropped and "outage_hours" in rep.dropped
          and len(features) >= 8, f"kept {len(rep.kept)}/{len(rep.candidates)}; model uses {len(features)} feature-store features")

    # ── 4. hold-out strategies ────────────────────────────────────────────
    banner(4, "Train / validation / test hold-outs", "Same LightGBM under random, stratified, time-based and group\n"
           " splits, each scored on its own test split AND on a truly future, untouched window.")
    pre = full[(full.snapshot_month >= w["train"][0]) & (full.snapshot_month <= w["valid"][1]) & (full.label_complete == 1)]
    fut = full[(full.snapshot_month >= w["test"][0]) & (full.snapshot_month <= w["test"][1]) & (full.label_complete == 1)]
    with J.stage("holdout") as jst:
        cmp = splits.compare(pre, features, fut)
        with mlflow.start_run(run_name="holdout-strategies", tags={"stage": "splits", **JY.link_tags()}) as ho_run:
            mlflow.log_table(cmp.reset_index(), "splits/holdout_comparison.json")
            for st, r in cmp.iterrows():
                mlflow.log_metrics({f"{st}_test_auc": r.test_auc, f"{st}_future_auc": r.future_auc, f"{st}_optimism": r.optimism})
        sizes = {"n_train": int(((full.snapshot_month >= w["train"][0]) & (full.snapshot_month <= w["train"][1]) & (full.label_complete == 1)).sum()),
                 "n_valid": int(((full.snapshot_month >= w["valid"][0]) & (full.snapshot_month <= w["valid"][1]) & (full.label_complete == 1)).sum()),
                 "n_test": len(fut)}
        JY.safe(jst, JY.holdout_evidence, cmp, w, sizes)
        jst.source_run(ho_run.info.run_id, "holdout-strategies run", ho_run.info.experiment_id)
    proof("hold-out comparison", cmp.loc["random", "accounts_in_train_and_test"] > 0 and cmp.loc["group", "accounts_in_train_and_test"] == 0,
          f"random shares {cmp.loc['random', 'accounts_in_train_and_test']:,} accounts with test; group 0; "
          f"time-based optimism {cmp.loc['time', 'optimism']:+.4f} vs random {cmp.loc['random', 'optimism']:+.4f}")

    # ── 5. feature store ──────────────────────────────────────────────────
    banner(5, "Feature store", "Register + materialise feature views (offline, Postgres `features`), build a\n"
           " point-in-time training set, materialise the online store, mirror to Feast.")
    fs_stage = J.stage("feature_store")
    fst = fs_stage.__enter__()
    off = featurestore.apply()
    on = featurestore.materialize_online()
    ent = full[[config.ENTITY, config.TS]].sample(2000, random_state=1)
    pit = featurestore.get_historical_features(ent, ["monthly_charge", "recent_price_increase", "outage_minutes"])
    chk = full.set_index([config.ENTITY, config.TS]).loc[list(zip(pit[config.ENTITY], pit[config.TS])), "monthly_charge"].to_numpy()
    pit_ok = bool(((pit.monthly_charge.to_numpy() - chk) ** 2).sum() < 1e-6)
    fe = featurestore.feast_sync() if not args.no_feast else {"status": "skipped"}
    if fe.get("status") == "applied":
        try:
            acct = full[config.ENTITY].iloc[0]
            got = featurestore.feast_online([acct], ["monthly_charge"])
            mine = featurestore.get_online_features([acct])[acct]["monthly_charge"]
            fe["parity"] = abs(float(got["monthly_charge"][0]) - float(mine)) < 1e-6
        except Exception as e:
            fe["parity"] = f"error: {e}"
    JY.safe(fst, JY.feature_store_evidence, off, on, pit_ok, fe)
    fs_stage.__exit__(None, None, None)
    proof("feature store", len(off) == 4 and on["accounts"] > 0 and pit_ok,
          f"{len(off)} views, {on['accounts']:,} online rows as of {on['as_of']}, PIT join exact = {pit_ok}, "
          f"Feast mirror: {fe.get('status')}{' parity=' + str(fe.get('parity')) if 'parity' in fe else ''}")

    # ── 6/7. pipelines in MLflow + leaderboard ────────────────────────────
    banner(6, "Transformation pipelines → MLflow · leaderboard", "Each run logs the ColumnTransformer on its own AND\n"
           " prep+model as one sklearn/pyfunc model (signature + input example), plus metrics, plots, SHAP, lineage.")
    with J.stage("algo_selection") as st:
        lb = pipeline.leaderboard(features, w, kinds=args.models.split(","))
        print("\n" + lb.drop(columns=["run_id"]).round(4).to_string())
        tune_kind = "lightgbm" if "lightgbm" in set(lb.model) else lb.model.iloc[0]
        lb_rows = JY.leaderboard_rows(list(lb.run_id))
        JY.safe(st, JY.algo_evidence, lb_rows, next((r["run_id"] for r in lb_rows if r["model"] == tune_kind), None),
                chosen_label=f"{tune_kind} → Optuna tuning")
    proof("leaderboard", len(lb) >= 4 and lb.valid_auc.max() > 0.6,
          f"{len(lb)} models; best {lb.model.iloc[0]} valid AUC {lb.valid_auc.iloc[0]:.4f} / test AUC {lb.test_auc.iloc[0]:.4f}")

    # ── 8. tuning ─────────────────────────────────────────────────────────
    banner(8, "Hyper-parameter tuning", f"Optuna TPE, {args.trials} trials on validation AUC; every trial a nested MLflow run.")
    with J.stage("tuning") as st:
        tuned = pipeline.tune(features, tune_kind, n_trials=args.trials, windows=w)
        JY.safe(st, JY.tuning_evidence, tuned["parent_run_id"])
    proof("tuning", tuned["n_trials"] >= min(3, args.trials), f"best trial AUC {tuned['best_trial_auc']:.4f}; tuned model valid "
          f"{tuned['metrics']['valid_auc']:.4f} / test {tuned['metrics']['test_auc']:.4f}")

    # ── 9. registration (dev) ─────────────────────────────────────────────
    banner(9, "Model registry — register candidates (stage dev)", "Best candidate by validation AUC and the best model of a different\n"
           " family are registered as new versions: stage dev, artifact sha256 pinned, data / code / environment hashes copied\n"
           " onto the version. Nothing is promoted by registering.")
    cands = [{"run_id": r.run_id, "model": r.model, "valid_auc": r.valid_auc, "test_auc": r.test_auc} for r in lb.itertuples()]
    cands.append({"run_id": tuned["run_id"], "model": "lightgbm-tuned", "valid_auc": tuned["metrics"]["valid_auc"],
                  "test_auc": tuned["metrics"]["test_auc"]})
    cands.sort(key=lambda c: -c["valid_auc"])
    champ = cands[0]
    fam = champ["model"].split("-")[0]
    chall = next(c for c in cands[1:] if c["model"].split("-")[0] != fam)
    if args.fresh_release_train:
        ret = release.retire_all(args.requester, f"lifecycle run{(' #' + str(LIFECYCLE_ID)) if LIFECYCLE_ID else ''}: fresh release train")
        print(f"   fresh release train: retired {', '.join('v' + r['model_version'] + ' (' + r['from_stage'] + ')' for r in ret) or 'nothing'} "
              f"— recorded as promotion rows, nothing deleted")
    with J.stage("registration") as st:
        v_champ = registry.register_run(champ["run_id"], description=f"{champ['model']} · valid AUC {champ['valid_auc']:.4f}")
        v_chall = registry.register_run(chall["run_id"], description=f"{chall['model']} · valid AUC {chall['valid_auc']:.4f}")
        d1, d2 = release.register_dev(v_champ, "lifecycle candidate"), release.register_dev(v_chall, "lifecycle runner-up")
        JY.safe(st, JY.registration_evidence, v_champ, champ["run_id"], None,
                f"{champ['model']} · best validation AUC {champ['valid_auc']:.4f} of {len(cands)} candidates — stage dev")
        if st:
            st.table("candidates", "Registration candidates (by validation AUC)", cands, columns=["model", "valid_auc", "test_auc", "run_id"])
    J.attach_version(v_champ)
    y_te, p_te, win_te, _ = JY.run_test_predictions(champ["run_id"])
    with J.stage("roc_auc") as st:
        JY.safe(st, JY.roc_evidence, y_te, p_te, f"v{v_champ} {champ['model']}", win_te)
    with J.stage("evaluation") as st:
        JY.safe(st, JY.evaluation_evidence, y_te, p_te, champ["run_id"], f"v{v_champ} {champ['model']}")
    rtags = registry.client().get_run(champ["run_id"]).data.tags
    proof("registered as dev with lineage", all(rtags.get(k) for k in ("bsp.data_snapshot_sha256", "bsp.code_sha256", "bsp.env_lock_sha256")),
          f"v{v_champ} ({champ['model']}) + v{v_chall} ({chall['model']}); artifact sha256 {d1['artifact_sha256'][:12]}…; data snapshot "
          f"{rtags.get('bsp.data_snapshot_sha256', '')[:12]}…; code {rtags.get('bsp.code_sha256', '')[:12]}…")

    # ── 10. release gates → staging ───────────────────────────────────────
    banner(10, "Release gates → staging", "Automated, recorded gates: artifact hash, lineage, leakage guard, schema & data contract,\n"
           " AUC floor, non-regression (paired bootstrap CI + DeLong on the SAME hold-out), calibration (ECE, Brier), segment\n"
           " parity (copper / fiber, state) and a latency smoke test inside the gateway. Pass → alias `staging`.")
    incumbent = registry.alias_version("champion")
    base = None if incumbent is not None else v_chall           # first release: compare with the strongest alternative
    sub = release.submit(v_champ, args.requester, args.requester_role, args.requester_source, note="lifecycle release candidate", baseline=base)
    gl = sub["gates"]["gates"]
    nr = next((g for g in gl if g["key"] == "non_regression"), {})
    proof("release gates evaluated + recorded", bool(sub["promotion"]["id"]) and bool(sub["gates"].get("evidence_run_id")),
          f"promotion #{sub['promotion']['id']}: {sum(g['status'] in ('pass', 'warn', 'skip') for g in gl)}/{len(gl)} gates passed; "
          f"hold-out {sub['gates']['holdout']['window']} ({sub['gates']['holdout']['rows']:,} rows); {nr.get('detail', '')[:110]}")
    pending_id = sub["pending"]["id"] if sub["pending"] else None
    # negative control: same pipeline, permuted labels → the gates must block it
    neg = None
    if not args.skip_negative_control:
        banner(10, "Release gates — negative control", "A logistic regression trained on PERMUTED labels (no real signal) goes through the\n"
               " same gates. A gate that never fails proves nothing; this one must be blocked.")
        nc = pipeline.train_and_log("logreg", features, w, experiment=config.EXPERIMENT, run_name="negative-control-permuted",
                                    tags={"stage": "negative-control"}, log_shap=False, permute_labels=13, verbose=True)
        v_neg = registry.register_run(nc["run_id"], description="NEGATIVE CONTROL — trained on permuted labels; must never be promoted")
        registry.client().set_model_version_tag(config.MODEL_NAME, v_neg, "bsp.negative_control", "true")
        release.register_dev(v_neg, "negative control")
        neg = release.submit(v_neg, args.requester, args.requester_role, args.requester_source, note="negative control (permuted labels)",
                             baseline=v_champ, request_production=False, make_challenger=False)
        failed = [g["name"] for g in neg["gates"]["gates"] if g["blocking"] and g["status"] == "fail"]
        proof("gates block a model with no signal", not neg["staged"] and bool(failed),
              f"v{v_neg} (permuted labels) blocked by: {', '.join(failed)} (promotion #{neg['promotion']['id']})")

    # ── 11. production approval (four-eyes) ───────────────────────────────
    banner(11, "Production approval — four-eyes", f"Production needs a person with an approver role {sorted(release.APPROVER_ROLES)} who is NOT\n"
           f" the requester ({args.requester}). Approval re-verifies the artifact hash, moves `champion`, records who / when / why,\n"
           " and re-derives serving from the registry.")
    approved = False
    if pending_id:
        try:      # the rule must refuse self-approval
            release.check_approver(release.get(pending_id), args.requester, "admin", "cli")
            self_refused = False
        except release.ReleaseError as e:
            self_refused = True
            print(f"   ✔ self-approval refused: {e}")
        proof("four-eyes refuses self-approval", self_refused, f"requester {args.requester} cannot approve promotion #{pending_id}")
        if args.approver:
            ap = release.approve(pending_id, args.approver, args.approver_role, "cli",
                                 note=f"lifecycle approval after reviewing promotion #{sub['promotion']['id']}")
            approved = ap["promotion"]["status"] == "approved"
        elif args.wait_approval > 0:
            approved = wait_for_approval(pending_id, args.wait_approval)
        else:
            print(f"   promotion #{pending_id} awaits approval in the control tower (#release) — no --approver given")
    if approved:
        rec = release.get(pending_id)
        proof("approved to production (four-eyes)", rec["approved_by"].lower() != rec["requested_by"].lower(),
              f"#{pending_id}: v{v_champ} → champion; requested by {rec['requested_by']} ({rec['requested_role']}), approved by "
              f"{rec['approved_by']} ({rec['approved_role']}, identity source {rec['approved_source']}); previous champion v{rec['champion_before'] or '—'}")
    al = registry.aliases()
    champ_v = al.get("champion")
    if not champ_v:
        proof("a champion is serving", False, "no approved champion — post-release stages need one (approve the pending request, or run with "
                                              "--approver / without --fresh-release-train)")
        return 1
    # the runner-up: gated against the new champion; staged → challenger (shadow); blocked → shadow-only, never canary
    blocked_challenger = False
    if v_chall != champ_v:
        sc = release.submit(v_chall, args.requester, args.requester_role, args.requester_source, note="lifecycle runner-up → challenger",
                            request_production=False)
        if not sc["staged"]:
            blocked_challenger = True
            registry.set_alias("challenger", v_chall)        # shadow is zero-risk; canary / blue-green would expose customers
            deploy.reconcile("shadow", actor="lifecycle", reason=f"runner-up v{v_chall} blocked by gates — shadow only")
    al = registry.aliases()
    chall_v = al.get("challenger")
    print(f"   registry now: champion v{champ_v} · challenger v{chall_v or '—'}{' (blocked by gates: shadow only)' if blocked_challenger else ''}")

    # ── 12. deployment patterns ───────────────────────────────────────────
    banner(12, "Deployment — single · blue/green · canary · shadow", "Routing changes go through the gateway admin API (authenticated);\n"
           " counts below come straight from mlops.inference_log. Finally serving is RECONCILED with the registry.")
    deploy.wait_alias("champion", champ_v)
    if chall_v:
        deploy.wait_alias("challenger", chall_v)
    deploy.set_mode("single", actor="lifecycle", shadow_enabled=False)
    p = deploy.predict(full[config.ENTITY].iloc[0])  # id-only → online feature store
    deploy.flush()
    proof("online-feature scoring (authenticated)", p["model_version"] == champ_v and p["feature_source"] == "online_store",
          f"{p['ban']} → score {p['score']:.4f} by v{p['model_version']} using {p['feature_source']} (service token)")
    unauth = httpx.post(config.GATEWAY_URL.rstrip("/") + "/v1/predict", json={"ban": p["ban"]}, timeout=10).status_code
    proof("endpoint rejects unauthenticated calls", unauth == 401, f"POST /serve/v1/predict without identity or token → HTTP {unauth}")
    canary_since = None
    if chall_v and not blocked_challenger:
        deploy.set_mode("blue_green", blue_version=champ_v, green_version=chall_v, live_color="blue", actor="lifecycle")
        m0 = max_log_id(); simulator.burst(300, seed=21); deploy.flush(); c_blue = served_counts(m0)
        deploy.switch(actor="lifecycle")
        m1 = max_log_id(); simulator.burst(300, seed=22); deploy.flush(); c_green = served_counts(m1)
        dv = deploy.status()["divergence"]
        deploy.rollback(actor="lifecycle")
        m2 = max_log_id(); simulator.burst(200, seed=23); deploy.flush(); c_back = served_counts(m2)
        proof("blue/green switch + rollback", c_blue == {f"v{champ_v}:blue": 300} and c_green == {f"v{chall_v}:green": 300}
              and c_back == {f"v{champ_v}:blue": 200}, f"before {c_blue} → after switch {c_green} → after rollback {c_back}")
        proof("divergence shown loudly while green ≠ champion", any(d["code"] == "serving_not_champion" for d in dv),
              next((d["message"] for d in dv if d["code"] == "serving_not_champion"), "not flagged")[:140])
        deploy.set_mode("canary", canary_weight=0.2, actor="lifecycle")
        m3 = canary_since = max_log_id(); simulator.burst(2000, seed=24); deploy.flush(); c_can = served_counts(m3)
        n_can = c_can.get(f"v{chall_v}:canary", 0)
        proof("canary 20 % split", 0.15 <= n_can / 2000 <= 0.25, f"{c_can} → {n_can / 2000:.1%} to challenger (sticky by account)")
    elif blocked_challenger:
        print(f"   blue/green and canary drills skipped: challenger v{chall_v} did not pass the gates (shadow only)")
    rec_ = deploy.reconcile("shadow", actor="lifecycle", reason="lifecycle: serving derived from the registry")
    bad = [d for d in rec_["divergence"] if d["level"] in ("error", "warning")]
    proof("serving reconciled with the registry", not bad and rec_["effective"]["serving"][0]["version"] == champ_v,
          f"mode {rec_['mode']}: v{champ_v} answers 100 %, shadow v{rec_['effective']['shadow'] or '—'}; divergence: "
          f"{'none' if not rec_['divergence'] else '; '.join(d['code'] for d in rec_['divergence'])}")

    # ── 13. shadow + canary evidence (paired statistics) ──────────────────
    banner(13, "Champion vs challenger — statistical validation", "Shadow: both models scored the SAME requests → paired bootstrap CI\n"
           " and DeLong on realised labels. Canary: independent arms → bootstrap CI of the AUC difference.")
    sh = {}
    if chall_v:
        m4 = max_log_id(); simulator.burst(1500, seed=25); time.sleep(2); deploy.flush(); c_sh = served_counts(m4)
        proof("shadow deployment", c_sh.get(f"v{champ_v}:champion", 0) == 1500 and c_sh.get(f"v{chall_v}:shadow:shadow", 0) == 1500,
              f"{c_sh} — customers only ever saw v{champ_v}")
        sh = outcomes.champion_vs_challenger(since_id=m4, by=args.requester)
        proof("paired champion / challenger comparison", sh.get("ci_low") is not None,
              f"experiment #{sh.get('id')}: n={sh.get('n_pairs')} pairs ({sh.get('n_labelled')} labelled); ΔAUC challenger−champion "
              f"{sh.get('delta_auc', float('nan')):+.4f} [{sh.get('ci_low', float('nan')):+.4f}, {sh.get('ci_high', float('nan')):+.4f}], "
              f"DeLong p={sh.get('delong_p', float('nan')):.3g} → {sh.get('verdict')}")
        if canary_since is not None:
            cc = outcomes.canary_compare(since_id=canary_since, by=args.requester)
            proof("canary arms compared", cc.get("status") == "ok",
                  f"experiment #{cc.get('id')}: " + ", ".join(f"{k} v{v['model_version']} AUC {v['realised_auc'] or float('nan'):.3f} (n={v['n_labelled']})"
                                                              for k, v in (cc.get("arms") or {}).items())
                  + (f"; ΔAUC {cc['delta_auc']:+.4f} [{cc['ci_low']:+.4f}, {cc['ci_high']:+.4f}]" if cc.get("delta_auc") is not None else ""))

    # ── 14. telemetry + SLO ───────────────────────────────────────────────
    banner(14, "Telemetry & SLO", "Every request and response in mlops.inference_log (with the caller); Prometheus on /serve/metrics;\n"
           " HTTP latency / error rate vs the SLO from the gateway.")
    t = tower("GET", "/api/telemetry?minutes=60")
    tot = t["totals"]
    print(f"   served {tot['served']:,} · shadow {tot['shadow']:,} · p50 {tot['p50']:.2f} ms · p95 {tot['p95']:.2f} ms per instance")
    met = httpx.get(config.GATEWAY_URL.rstrip("/") + "/metrics", timeout=10).text
    n_metric = sum(1 for line in met.splitlines() if line.startswith("bsp_gateway_predictions_total{"))
    slo = deploy.slo(60)
    proof("telemetry + SLO", tot["served"] > 0 and n_metric > 0 and slo["requests"] > 0,
          f"{tot['served']:,} served rows, {n_metric} Prometheus series; HTTP p95 {slo['p95_ms']} ms (SLO {slo['slo']['p95_request_ms']} ms), "
          f"availability {slo['availability']:.4f} (SLO {slo['slo']['availability']}), owner: {slo['slo']['owner']}")

    # ── 15. drift → alert → retrain → gates ───────────────────────────────
    banner(15, "Drift → alert → retrain → release gates", "Back to shadow routing. 1) normal months → expect stable. 2) shifted months\n"
           " (price increases + outage cluster) → breach → alert + automatic retrain, which goes through the SAME release gates\n"
           " (staging + challenger in shadow) and waits for a human approval.")
    tower("PUT", "/api/settings", json={"auto_retrain": True, "auto_promote": True, "promote_margin": args.margin})
    m5 = max_log_id()
    simulator.burst(1500, shift=False, seed=31); deploy.flush()
    d_ok = drift.run_check(since=_ts_of(m5), last_n=1500, source="lifecycle", write=True)
    proof("no false alarm on normal traffic", not d_ok["breached"],
          f"report #{d_ok.get('id')}: max feature PSI {d_ok['max_psi']:.3f} ({d_ok['feature_drift'][0]['feature']}), prediction PSI "
          f"{d_ok['prediction_psi']:.3f} — below the {config.PSI_BREACH} breach level")
    simulator.burst(1500, shift=True, seed=32); deploy.flush()
    d_bad = tower("POST", "/api/drift/check", json={"minutes": 30, "last_n": 1500, "retrain_on_breach": True})
    proof("drift detected on shifted traffic", d_bad["breached"], f"report #{d_bad.get('id')}: " + "; ".join(d_bad["breach_reasons"])[:200])
    al_open = db.fetchone("select id, title from mlops.alerts where kind='drift' and status <> 'resolved' order by id desc limit 1")
    proof("alert raised", bool(al_open), f"alert #{al_open['id']}: {al_open['title'][:120]}" if al_open else "none")
    rt = d_bad.get("retrain") or {}
    rid = rt.get("retrain_id")
    if not rid:
        print(f"   (auto trigger returned {rt}; starting a manual retrain instead)")
        rid = tower("POST", "/api/retrains", json={"reason": "lifecycle: drift breach"})["retrain_id"]
    t0 = time.time()
    while True:
        r = tower("GET", f"/api/retrains/{rid}")
        if r["status"] in ("succeeded", "failed") or time.time() - t0 > 1200:
            break
        time.sleep(4)
    rel = (r.get("details") or {}).get("release") or {}
    v_new = r.get("model_version")
    print(f"   retrain #{rid}: {r['status']} — {r.get('decision')}")
    proof("retrain → release gates → staging (no auto-champion)", r["status"] == "succeeded" and rel.get("gates_passed") is not None
          and registry.aliases().get("champion") == champ_v,
          f"#{rid} → v{v_new} (AUC {r.get('challenger_auc') or 0:.4f} vs champion {r.get('champion_auc') or 0:.4f} on the newest month); gates "
          f"{'passed → staging, request #' + str(rel.get('pending_promotion_id')) if rel.get('gates_passed') else 'FAILED'}; champion still v{champ_v}")
    pend_new = rel.get("pending_promotion_id")

    # ── 16. approval of the retrain + rollback drill ──────────────────────
    if pend_new and args.approver and not args.no_rollback_drill:
        banner(16, "Approval + rollback drill", f"{args.approver} approves the retrained v{v_new}; then an operator rolls back to the previous\n"
               " APPROVED version (recorded), and the retrained model goes back through the gates to wait as challenger.")
        release.approve(pend_new, args.approver, args.approver_role, "cli", note="lifecycle: retrain beats champion on the newest month")
        rb = release.rollback(args.approver, args.approver_role, "cli", reason="lifecycle rollback drill")
        ok_rb = registry.aliases().get("champion") == champ_v and rb["promotion"]["details"]["target_was_approved"]
        proof("rollback to the previous approved version", ok_rb,
              f"rollback #{rb['promotion']['id']}: champion v{v_new} → v{champ_v} (target approved before: "
              f"{rb['promotion']['details']['target_was_approved']}); routing {rb['serving']['mode'] if rb['serving'] else '?'}")
        again = release.submit(v_new, "retrain-orchestrator", "service", "automation", note="re-request after the rollback drill")
        pend_new = again["pending"]["id"] if again["pending"] else None
    elif pend_new and args.wait_approval > 0:
        wait_for_approval(pend_new, args.wait_approval)
    al = registry.aliases()
    print(f"   registry now: champion v{al.get('champion')} · challenger v{al.get('challenger') or '—'} · staging v{al.get('staging') or '—'}"
          + (f" · production request #{pend_new} pending" if pend_new and release.get(pend_new)["status"] == "pending_approval" else ""))

    # ── 17. delayed-label evaluation + new shadow evidence ────────────────
    banner(17, "Delayed-label evaluation", "Scores logged at serve time joined to realised churn once the 90-day label closes: realised\n"
           " AUC / lift / calibration per scoring cohort and per serving week vs the AUC expected at release.")
    if al.get("challenger"):
        deploy.wait_alias("challenger", al["challenger"])
        m7 = max_log_id(); simulator.burst(2000, shift=True, seed=41); time.sleep(2); deploy.flush()   # newest months: out of sample for a retrain
        sh2 = outcomes.champion_vs_challenger(since_id=m7, by=args.requester)
        print(f"   shadow v{sh2.get('challenger_version')} vs champion v{sh2.get('champion_version')}: ΔAUC {sh2.get('delta_auc', float('nan')):+.4f} "
              f"[{sh2.get('ci_low', float('nan')):+.4f}, {sh2.get('ci_high', float('nan')):+.4f}] p={sh2.get('delong_p', float('nan')):.3g} → {sh2.get('verdict')}")
    le = outcomes.delayed_label_eval(source="lifecycle")
    served = [o for o in le.get("overall", []) if o["role"] == "served" and o["model_version"] == al.get("champion")]
    o = served[0] if served else {}
    proof("delayed-label evaluation", le.get("n_labelled", 0) > 0 and o.get("realised_auc") is not None,
          f"evaluation #{le.get('id')}: {le.get('n_labelled', 0):,} labelled / {le.get('n_pending', 0):,} pending; champion v{al.get('champion')} "
          f"realised AUC {o.get('realised_auc') or float('nan'):.4f} vs expected {o.get('expected_auc') or float('nan'):.4f} on "
          f"{o.get('n_labelled', 0):,} labelled requests")

    # ── 18. business A/B ──────────────────────────────────────────────────
    banner(18, "Business outcome — retention-offer A/B", "Top-risk customers of the latest labelled month, hash-randomised into offer vs\n"
           " control; two-proportion z-test, CI, power. The customers, scores and labels are real; the OFFER EFFECT is simulated.")
    ab = outcomes.retention_offer_ab(effect=args.ab_effect, by=args.requester)
    proof("A/B readout with significance", ab.get("status") == "ok" and ab.get("p_value") is not None,
          f"experiment #{ab.get('id')}: churn {ab['control']['rate']:.1%} control vs {ab['treatment']['rate']:.1%} offer (n={ab['control']['n']}/"
          f"{ab['treatment']['n']}), uplift {ab['abs_uplift']:+.1%} [{ab['ci_low']:+.1%}, {ab['ci_high']:+.1%}], p={ab['p_value']:.2g} "
          f"(simulated effect {ab['true_effect_parameter']})" if ab.get("status") == "ok" else str(ab))

    # ── 19. scores → dataset ──────────────────────────────────────────────
    banner(19, "Batch scoring → dataset shaping", "Champion batch-scores the last 6 months into mlops.scored_customers (decile, risk band,\n"
           " top-3 reason codes); mlops.v_score_outcomes joins realised churn as it arrives.")
    sc = scoring.batch_score(last_k=6)
    mon = scoring.outcome_monitor(sc["model_version"])
    top_dec = mon[mon.decile == 1].realised_churn.mean() if len(mon) else float("nan")
    base_rate = (mon.realised_churn * mon.customers).sum() / mon.customers.sum() if len(mon) else float("nan")
    proof("batch scores in the warehouse", sc["rows"] > 0 and len(mon) == 10,
          f"{sc['rows']:,} rows by v{sc['model_version']}; decile-1 realised churn {top_dec:.1%} vs {base_rate:.1%} overall")

    # ── 20. reproduce ─────────────────────────────────────────────────────
    banner(20, f"Reproduce v{al.get('champion')}", "Refit the champion from its RECORDED recipe (config, windows, seed) on rows rebuilt through\n"
           " the same point-in-time join; compare the data hash, every metric and every test prediction; diff the environment lock.")
    rp = reproduce.reproduce(al.get("champion"), by=args.requester)
    proof("reproduction parity", rp["parity"] in ("identical", "within_tolerance") and rp["data_hash_match"] is not False,
          f"reproduction #{rp['id']}: {rp['parity']}; data hash match {rp['data_hash_match']}, code match {rp['code_match']}, "
          f"environment identical {rp['env_match']}; max |Δprediction| {rp['details']['max_prediction_diff']:.1e}")

    # ── 21. export bundle ─────────────────────────────────────────────────
    banner(21, "Export bundle → artifactory", "Immutable, hashed bundle: MLflow model + locks + feature definitions + training config +\n"
           " lineage + promotion records + Dockerfile / Cloud Run / Vertex specs + pipeline source; registry tagged frozen=true.")
    fz = tower("POST", "/api/frozen", json={})
    tags = registry.client().get_model_version(config.MODEL_NAME, fz["model_version"]).tags
    man = db.fetchone("select manifest from mlops.frozen_artifacts where id=%s", (fz["id"],))["manifest"]
    need = ["model/MLmodel", "config/training_config.json", "lineage/lineage.json", "release/promotions.json", "deploy/Dockerfile",
            "deploy/cloudrun-service.yaml", "features/features.yaml", "env/requirements.lock", "pipelines/reproduce.sh"]
    missing = [f for f in need if f not in man["files"]]
    proof("export bundle", fz["verify"]["ok"] and tags.get("frozen_sha256") == fz["sha256"] and not missing,
          f"{fz['bundle']} · {fz['files']} files · sha256 {fz['sha256'][:16]}… · {fz['uri']}" + (f" · missing {missing}" if missing else ""))
    SUMMARY.update(export_bundle=fz["uri"], export_dir=f"{config.FROZEN_DIR}/{fz['bundle']}", export_sha256=fz["sha256"])

    # ── 22. journeys + capability evidence ────────────────────────────────
    banner(22, "Model journeys + capability evidence", "Snapshot the operational evidence into each version's journey, backfill journeys,\n"
           " and resolve the A–G checklist against the live records.")
    JY.snapshot_ops(J.id, v_champ)
    J.finish(True)
    JY.backfill(verbose=False)
    jj = JY.for_version(config.MODEL_NAME, v_champ)
    missing = [s_["key"] for s_ in jj["stages"] if s_["status"] not in ("done", "skipped")]
    proof("champion journey", jj["status"] == "done" and not [m for m in missing if m not in ("freeze", "retrain")],
          f"v{v_champ}: {jj['progress']['done']}/{jj['progress']['total']} stages done" + (f"; pending {', '.join(missing)}" if missing else ""))
    from . import evidence

    cl = evidence.checklist()
    with_ev = sum(1 for i in cl["items"] if i["status"] == "implemented" and i["evidence_recorded"])
    n_impl = cl["counts"].get("implemented", 0)
    proof("capability checklist resolved", with_ev >= n_impl * 0.6,
          f"{n_impl} implemented items, {with_ev} with live evidence links; delegated {cl['counts'].get('delegated', 0)}, needs engineering "
          f"{cl['counts'].get('needs_engineering', 0)}")
    al = registry.aliases()
    SUMMARY.update(champion=al.get("champion"), challenger=al.get("challenger"), staging=al.get("staging"), candidate=v_champ,
                   runner_up=v_chall, negative_control=neg and neg["promotion"]["model_version"], promotion=sub["promotion"]["id"],
                   production_request=pending_id, retrain_id=rid, retrain_version=v_new, retrain_request=pend_new,
                   shadow=sh and {k: sh.get(k) for k in ("id", "delta_auc", "ci_low", "ci_high", "delong_p", "verdict")},
                   delayed_labels={"id": le.get("id"), "realised_auc": o.get("realised_auc"), "expected_auc": o.get("expected_auc")},
                   ab={k: ab.get(k) for k in ("id", "abs_uplift", "ci_low", "ci_high", "p_value")}, reproduction=rp["id"], parity=rp["parity"])
    return 0


def wait_for_approval(pid: int, minutes: float) -> bool:
    from . import release

    print(f"   waiting up to {minutes:.0f} min for an approver (not {release.get(pid)['requested_by']}) to approve promotion #{pid} "
          f"in the control tower: /mlops/#release?id={pid}", flush=True)
    t0 = time.time()
    while time.time() - t0 < minutes * 60:
        st = release.get(pid)["status"]
        if st != "pending_approval":
            print(f"   promotion #{pid}: {st}", flush=True)
            return st == "approved"
        time.sleep(5)
    print(f"   no decision on #{pid} within {minutes:.0f} min — continuing without it", flush=True)
    return False


JOURNEY = None
NOTEBOOKS = ["01_integrate_and_eda", "02_feature_sifting_selection", "03_holdout_strategies", "04_feature_store",
             "05_pipeline_and_mlflow", "06_leaderboard", "07_hyperparameter_tuning", "08_registry_champion_challenger"]


def _ts_of(log_id: int):
    r = db.fetchone("select ts from mlops.inference_log where id=%s", (log_id,))
    return r["ts"] if r else None


def _reset() -> None:
    """--fresh: archive previous demo state (inference log, reports, registry aliases)."""
    print("   --fresh: clearing mlops tables and registry aliases", flush=True)
    db.execute("truncate mlops.inference_log, mlops.drift_reports, mlops.retrain_runs restart identity")
    db.execute("update mlops.deployments set mode='single', shadow_enabled=false, blue_version=null, green_version=null, "
               "live_color='blue', history='[]'::jsonb")
    try:
        from . import registry

        c = registry.client()
        for a in ("champion", "challenger"):
            try:
                c.delete_registered_model_alias(config.MODEL_NAME, a)
            except Exception:
                pass
    except Exception:
        pass


def main(argv=None) -> int:
    global LIFECYCLE_ID
    ap = argparse.ArgumentParser(prog="bsp_mlops.demo", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=0, help="seed N synthetic customers into raw.* first (0 = use what is there)")
    ap.add_argument("--trials", type=int, default=int(os.environ.get("DEMO_TRIALS", "12")))
    ap.add_argument("--models", default="logreg,random_forest,lightgbm,xgboost,hist_gb")
    ap.add_argument("--margin", type=float, default=config.PROMOTE_MARGIN, help="AUC margin reported by the retrain (informational)")
    ap.add_argument("--sift-sample", type=int, default=25000)
    ap.add_argument("--no-feast", action="store_true")
    ap.add_argument("--fresh", action="store_true", help="DESTRUCTIVE: truncate demo telemetry / drift / retrain tables and clear aliases first")
    ap.add_argument("--fresh-release-train", action="store_true",
                    help="retire the current champion / challenger / staging (recorded, nothing deleted) so this run's candidate is the first release")
    ap.add_argument("--requester", default=config.user(), help="who requests the release (recorded)")
    ap.add_argument("--requester-role", default="modeler")
    ap.add_argument("--requester-source", default="cli", choices=["cli", "sso-gate"],
                    help="identity source of the requester (the control tower passes sso-gate for the signed-in person)")
    ap.add_argument("--approver", default="", help="operator-declared approver (must differ from --requester); omit to approve in the tower")
    ap.add_argument("--approver-role", default="admin")
    ap.add_argument("--wait-approval", type=float, default=0, help="minutes to wait for an approval in the tower when no --approver is given")
    ap.add_argument("--no-rollback-drill", action="store_true")
    ap.add_argument("--skip-negative-control", action="store_true")
    ap.add_argument("--ab-effect", type=float, default=0.25, help="SIMULATED relative churn reduction of the retention offer")
    ap.add_argument("--lifecycle-id", type=int, default=0, help="(set by the control tower) mlops.lifecycle_runs row to update")
    ap.add_argument("--accounts", type=int, default=int(os.environ.get("CHURN_MAX_ACCOUNTS", "0") or 0),
                    help="fast variant: model a deterministic sample of N accounts (warehouse untouched)")
    ap.add_argument("--no-notebook", action="store_true", help="hash the notebooks but do not execute 01 with papermill")
    a = ap.parse_args(argv)
    if a.approver and a.approver.strip().lower() == a.requester.strip().lower():
        ap.error("--approver must be a different person from --requester (four-eyes)")
    os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
    os.environ.setdefault("MLFLOW_SUPPRESS_PRINTING_URL_TO_STDOUT", "true")
    try:
        sys.stdout.reconfigure(line_buffering=True)   # narration appears live even when piped to a file
    except Exception:
        pass
    print(f"DataPlatform ML Scientist appliance — full model lifecycle (churn)\n"
          f"model {config.MODEL_NAME} · deployment {config.DEPLOYMENT} · tracking {config.MLFLOW_URI}\n"
          f"requester {a.requester} ({a.requester_role}) · approver {a.approver or '(in the control tower)'}", flush=True)
    try:
        db.ensure_schema()
        if a.lifecycle_id:
            LIFECYCLE_ID = a.lifecycle_id
        else:
            LIFECYCLE_ID = db.fetchone("insert into mlops.lifecycle_runs (status, requested_by, args, pid) values ('running', %s, %s, %s) returning id",
                                       (a.requester, db.jdump(vars(a)), os.getpid()))["id"]
        print(f"lifecycle run #{LIFECYCLE_ID}")
    except Exception as e:
        print(f"(lifecycle record unavailable: {e})")
    rc = 1
    try:
        rc = run(a)
    except Exception:
        traceback.print_exc()
        proof("lifecycle completed", False, "exception — see traceback above")
        if JOURNEY is not None:
            JOURNEY.finish(False)
    ok = all(p[1] for p in PROOF)
    minutes = round((time.time() - T0) / 60, 1)
    print(f"\n{'═' * 92}\n PROOF SUMMARY  ({minutes} min)\n{'─' * 92}")
    for name, good, detail in PROOF:
        print(f" {'PASS' if good else 'FAIL'}  {name:<46} {detail[:150]}")
    print(f"{'═' * 92}\n {'ALL STAGES PASSED' if ok and rc == 0 else 'SOME STAGES FAILED'}\n")
    summary = {"ok": ok and rc == 0, "minutes": minutes, "lifecycle_id": LIFECYCLE_ID, **SUMMARY,
               "proof": [{"stage": n, "ok": g, "detail": d} for n, g, d in PROOF]}
    try:
        Path(config.RUNS_DIR).mkdir(parents=True, exist_ok=True)
        (Path(config.RUNS_DIR) / "demo_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    except OSError:
        pass
    if LIFECYCLE_ID:
        try:
            db.execute("update mlops.lifecycle_runs set status=%s, finished_at=now(), summary=%s where id=%s",
                       ("succeeded" if summary["ok"] else "failed", db.jdump(summary), LIFECYCLE_ID))
        except Exception:
            pass
    return 0 if ok and rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
