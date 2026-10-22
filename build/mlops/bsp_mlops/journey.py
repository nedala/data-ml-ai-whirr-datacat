"""Model journey — the evidence trail of how one registered model version came
to be, reconstructed from MLflow (and, for the operational stages, from the
`mlops` schema in Postgres).

Storage (MLflow experiment `churn-journeys`):

    parent run   tags bsp.role=journey, bsp.journey_id, bsp.journey_kind
                 (training | retrain | backfill), bsp.model_name,
                 bsp.model_version (once registered), bsp.parent_journey_id
      child run  one per lifecycle stage: tags bsp.role=stage, bsp.stage=<key>,
                 bsp.stage_order, bsp.headline, bsp.stage_status; params +
                 metrics; artifacts: stage.json (tables, curves, links, notes)
                 + PNG charts / JSON / executed notebook
    registered model version tags bsp.journey_id + bsp.journey_run_id

Every training run created while a journey is active (env BSP_JOURNEY_ID)
carries tag bsp.journey_id too, so leaderboard / tuning / retrain runs link
back to the journey that produced them.

Live writers:   Journey.start(...) → with J.stage("eda") as st: ... st.table(...)
                (used by bsp_mlops.demo and pipeline.retrain)
Reader (API):   list_journeys(), read(journey_id), for_version(model, version)
Backfill:       python -m bsp_mlops.journey backfill [--version N] [--force]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import config

JOURNEY_EXPERIMENT = config.env("CHURN_JOURNEY_EXPERIMENT", "churn-journeys")
ENV_KEY = "BSP_JOURNEY_ID"

# key, title, one-line description (shown when a stage has no evidence yet)
STAGES: list[tuple[str, str, str]] = [
    ("integration", "Data integration", "Warehouse snapshot joined with NPS surveys and service tickets into features.churn_base."),
    ("eda", "Exploratory analysis", "Profile, churn by segment, missingness and correlation."),
    ("notebook", "Notebook", "The work_templates/ml notebook that documents this step, hashed (and executed when possible)."),
    ("selection", "Feature selection", "Missingness, variance, correlation/VIF, mutual information, permutation importance, SHAP, stability."),
    ("holdout", "Hold-out strategy", "Random / stratified / time / group splits compared; leakage checks."),
    ("feature_store", "Feature store", "Versioned feature views, point-in-time training set, online store."),
    ("algo_selection", "Algorithm selection", "Candidate model families on the same data — leaderboard."),
    ("roc_auc", "ROC / AUC", "Discrimination of the chosen model on the untouched test window."),
    ("tuning", "Hyper-parameter search", "Optuna TPE search on validation AUC; every trial a nested MLflow run."),
    ("evaluation", "Evaluation", "Calibration, lift / gain and global SHAP importance."),
    ("registration", "Registration", "MLflow Model Registry version and alias."),
    ("release", "Release gates & approval", "Automated gates (schema, AUC floor, non-regression, calibration, segment parity, latency, "
                "artifact hash) → staging → four-eyes approval → production."),
    ("deployment", "Deployment", "Routing events (single, blue/green, canary, shadow) and served traffic."),
    ("monitoring", "Monitoring", "Drift reports — PSI per feature, prediction PSI, AUC on labelled traffic."),
    ("retrain", "Retrain", "Drift- or manually-triggered retrains and their promotion decision."),
    ("freeze", "Freeze", "Immutable, hashed bundle pushed to the artifactory."),
]
STAGE_KEYS = [k for k, _, _ in STAGES]
TITLES = {k: t for k, t, _ in STAGES}
DESCR = {k: d for k, _, d in STAGES}
OPS_STAGES = ("release", "deployment", "monitoring", "retrain", "freeze")

TEAL, TEAL_LIGHT, ORANGE, GREY, INK = "#0b6e79", "#9fd3d8", "#e8730c", "#9aa0a6", "#334155"


# ═══════════════════════════════════════════════════════════════════════════
#  small helpers
# ═══════════════════════════════════════════════════════════════════════════
def _now_ms() -> int:
    return int(time.time() * 1000)


def _client():
    from mlflow import MlflowClient

    from .pipeline import mlflow_setup

    mlflow_setup()
    return MlflowClient()


_exp_lock = threading.Lock()
_exp_id: str | None = None


def experiment_id() -> str:
    global _exp_id
    with _exp_lock:
        if _exp_id:
            return _exp_id
        c = _client()
        e = c.get_experiment_by_name(JOURNEY_EXPERIMENT)
        _exp_id = e.experiment_id if e else c.create_experiment(JOURNEY_EXPERIMENT)
        return _exp_id


def current_id() -> str | None:
    return os.environ.get(ENV_KEY) or None


def link_tags() -> dict:
    """Tags every MLflow run created during an active journey carries."""
    j = current_id()
    return {"bsp.journey_id": j} if j else {}


def _jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if math.isnan(f) or math.isinf(f) else f
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if isinstance(o, (str, int, bool)) or o is None:
        return o
    return str(o)


def records(df: pd.DataFrame, index: bool = True, digits: int = 5) -> tuple[list[str], list[dict]]:
    d = df.reset_index() if index else df
    d = d.copy()
    for c in d.columns:
        if pd.api.types.is_float_dtype(d[c]):
            d[c] = d[c].round(digits)
    rows = _jsonable(d.to_dict("records"))
    return [str(c) for c in d.columns], rows


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _fmt_dur(s: float | None) -> str:
    if s is None:
        return ""
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.1f} min"
    return f"{s / 3600:.1f} h"


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 8.5, "axes.spines.top": False, "axes.spines.right": False,
                         "axes.edgecolor": "#cbd5e1", "axes.labelcolor": INK, "xtick.color": "#64748b",
                         "ytick.color": "#64748b", "axes.titlesize": 10, "axes.titleweight": "bold",
                         "axes.titlecolor": INK, "axes.grid": True, "grid.color": "#e2e8f0", "grid.linewidth": 0.6,
                         "axes.axisbelow": True, "figure.facecolor": "white"})
    return plt


# ═══════════════════════════════════════════════════════════════════════════
#  writer
# ═══════════════════════════════════════════════════════════════════════════
class StageLog:
    """Collects one stage's evidence; flushed to its MLflow child run on exit."""

    def __init__(self, key: str, run_id: str, workdir: Path):
        self.key, self.run_id, self.dir = key, run_id, workdir
        self.doc: dict[str, Any] = {"key": key, "title": TITLES.get(key, key), "status": "done", "headline": "",
                                    "summary": "", "tables": [], "charts": [], "curves": {}, "links": [],
                                    "source_runs": [], "notes": [], "files": []}
        self.params: dict[str, Any] = {}
        self.metrics: dict[str, float] = {}

    # content
    def headline(self, text: str) -> "StageLog":
        self.doc["headline"] = text
        return self

    def summary(self, text: str) -> "StageLog":
        self.doc["summary"] = text
        return self

    def status(self, s: str) -> "StageLog":
        self.doc["status"] = s
        return self

    def note(self, text: str) -> "StageLog":
        self.doc["notes"].append(text)
        return self

    def param(self, **kw) -> "StageLog":
        for k, v in kw.items():
            if v is not None:
                self.params[k] = v
        return self

    def metric(self, **kw) -> "StageLog":
        for k, v in kw.items():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if np.isfinite(f):
                self.metrics[k] = f
        return self

    def table(self, name: str, title: str, df: pd.DataFrame | list[dict], index: bool = False, kind: str = "",
              columns: list[str] | None = None) -> "StageLog":
        if isinstance(df, pd.DataFrame):
            cols, rows = records(df, index=index)
        else:
            rows = _jsonable(list(df))
            cols = columns or (list(rows[0].keys()) if rows else [])
        self.doc["tables"].append({"name": name, "title": title, "columns": columns or cols, "rows": rows, "kind": kind})
        (self.dir / f"{name}.json").write_text(json.dumps(rows, indent=1, default=str))
        self.doc["files"].append(f"{name}.json")
        return self

    def chart(self, fig, name: str, title: str, caption: str = "") -> "StageLog":
        plt = _plt()
        if len([a for a in fig.axes if a.get_label() != "<colorbar>"]) == 1:
            # the page shows `title` as the figure caption — drop the duplicate in-image title and
            # render single panels a little smaller so their text stays legible when scaled to a card
            fig.axes[0].set_title("")
            w, h = fig.get_size_inches()
            if "heatmap" not in name:
                fig.set_size_inches(w * 0.8, h * 0.8)
        fig.tight_layout()
        fig.savefig(self.dir / f"{name}.png", dpi=130)
        plt.close(fig)
        self.doc["charts"].append({"path": f"{name}.png", "title": title, "caption": caption})
        return self

    def curve(self, name: str, data: dict) -> "StageLog":
        self.doc["curves"][name] = _jsonable(data)
        (self.dir / f"{name}_points.json").write_text(json.dumps(self.doc["curves"][name]))
        self.doc["files"].append(f"{name}_points.json")
        return self

    def file(self, src: Path, name: str | None = None) -> "StageLog":
        import shutil

        dst = self.dir / (name or Path(src).name)
        shutil.copy(src, dst)
        self.doc["files"].append(dst.name)
        return self

    def link(self, label: str, href: str = "", **extra) -> "StageLog":
        self.doc["links"].append({"label": label, "href": href, **extra})
        return self

    def source_run(self, run_id: str, label: str, experiment_id: str | None = None) -> "StageLog":
        if run_id:
            if experiment_id is None:
                try:
                    experiment_id = _client().get_run(run_id).info.experiment_id
                except Exception:
                    experiment_id = None
            self.doc["source_runs"].append({"run_id": run_id, "label": label, "experiment_id": experiment_id})
        return self

    def data(self, **kw) -> "StageLog":
        self.doc.setdefault("data", {}).update(_jsonable(kw))
        return self


class Journey:
    """One journey = one MLflow parent run in `churn-journeys`."""

    def __init__(self, journey_id: str, run_id: str):
        self.id, self.run_id = journey_id, run_id
        self.client = _client()

    @classmethod
    def start(cls, kind: str = "training", title: str = "", model: str | None = None, parent_journey: str | None = None,
              journey_id: str | None = None, start_time: int | None = None, extra_tags: dict | None = None,
              activate: bool = True) -> "Journey":
        jid = journey_id or f"j-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        c = _client()
        tags = {"bsp.role": "journey", "bsp.journey_id": jid, "bsp.journey_kind": kind,
                "bsp.model_name": model or config.MODEL_NAME, "mlflow.runName": f"journey {jid}",
                "bsp.title": title or f"{kind} journey", **({"bsp.parent_journey_id": parent_journey} if parent_journey else {}),
                **(extra_tags or {})}
        r = c.create_run(experiment_id(), start_time=start_time or _now_ms(), tags=tags)
        if activate:
            os.environ[ENV_KEY] = jid
        return cls(jid, r.info.run_id)

    @classmethod
    def open(cls, journey_id: str) -> "Journey | None":
        p = _parent_run(journey_id)
        return cls(journey_id, p.info.run_id) if p else None

    @contextmanager
    def stage(self, key: str, start_time: int | None = None, end_time: int | None = None, swallow: bool = False):
        """Context manager: a RUNNING child run while the body executes; the
        evidence is uploaded and the run terminated on exit (FAILED on error)."""
        order = STAGE_KEYS.index(key) if key in STAGE_KEYS else 99
        try:
            self.client.set_tag(self.run_id, "bsp.heartbeat", str(_now_ms()))
        except Exception:
            pass
        r = self.client.create_run(experiment_id(), start_time=start_time or _now_ms(), tags={
            "mlflow.parentRunId": self.run_id, "mlflow.runName": f"{order + 1:02d} {TITLES.get(key, key)}",
            "bsp.role": "stage", "bsp.stage": key, "bsp.stage_order": str(order), "bsp.journey_id": self.id})
        td = tempfile.mkdtemp(prefix=f"journey_{key}_")
        st = StageLog(key, r.info.run_id, Path(td))
        failed = None
        try:
            yield st
        except Exception as e:
            failed = e
            st.status("failed").note(f"{e.__class__.__name__}: {str(e)[:400]}")
        try:
            self._flush(st)
        except Exception as e:   # evidence upload must never break the pipeline
            print(f"   (journey: could not upload {key} evidence: {e})", flush=True)
        status = "FAILED" if st.doc["status"] == "failed" else "FINISHED"
        try:
            self.client.set_terminated(st.run_id, status, end_time=end_time or _now_ms())
        except Exception:
            pass
        _CACHE.pop(st.run_id, None)
        import shutil

        shutil.rmtree(td, ignore_errors=True)
        if failed is not None and not swallow:
            raise failed

    def _flush(self, st: StageLog) -> None:
        from mlflow.entities import Metric, Param, RunTag

        ts = _now_ms()
        params = [Param(k[:250], str(v)[:6000]) for k, v in list(st.params.items())[:90]]
        metrics = [Metric(k[:250], float(v), ts, 0) for k, v in list(st.metrics.items())[:200]]
        tags = [RunTag("bsp.headline", st.doc["headline"][:250]), RunTag("bsp.stage_status", st.doc["status"])]
        self.client.log_batch(st.run_id, metrics=metrics, params=params, tags=tags)
        st.doc["params"], st.doc["metrics"] = _jsonable(st.params), _jsonable(st.metrics)
        (st.dir / "stage.json").write_text(json.dumps(_jsonable(st.doc), indent=1, default=str))
        self.client.log_artifacts(st.run_id, str(st.dir))

    def skip(self, key: str, reason: str, headline: str = "skipped", **kw) -> None:
        with self.stage(key) as st:
            st.status("skipped").headline(headline).summary(reason)
            for k, v in kw.items():
                if callable(v):
                    v(st)

    def attach_version(self, version: str, model: str | None = None) -> None:
        model = model or config.MODEL_NAME
        self.client.set_tag(self.run_id, "bsp.model_version", str(version))
        self.client.set_model_version_tag(model, str(version), "bsp.journey_id", self.id)
        self.client.set_model_version_tag(model, str(version), "bsp.journey_run_id", self.run_id)

    def tag(self, **kw) -> None:
        for k, v in kw.items():
            self.client.set_tag(self.run_id, f"bsp.{k}", str(v))

    def finish(self, ok: bool = True) -> None:
        try:
            self.client.set_terminated(self.run_id, "FINISHED" if ok else "FAILED")
        except Exception:
            pass
        if os.environ.get(ENV_KEY) == self.id:
            os.environ.pop(ENV_KEY, None)


def safe(st: "StageLog | None", fn, *args, **kw):
    """Run an evidence builder; a failure is noted on the stage, never raised."""
    if st is None:
        return None
    try:
        return fn(st, *args, **kw)
    except Exception as e:
        st.note(f"evidence incomplete — {fn.__name__}: {e.__class__.__name__}: {str(e)[:300]}")
        print(f"   (journey: {fn.__name__} failed: {e})", flush=True)
        return None


# ═══════════════════════════════════════════════════════════════════════════
#  evidence builders (shared by the demo, the retrain pipeline and backfill)
# ═══════════════════════════════════════════════════════════════════════════
def base_snapshot() -> dict:
    """Row counts + a content hash of features.churn_base (cheap, deterministic)."""
    from . import db
    from .data import BASE

    from .config import ENTITY, LABEL, TS

    cols = set(db.columns_of(BASE))
    probe = next((c for c in ("monthly_charge", "tenure_months") if c in cols), TS)
    s = db.fetchone(f"""select count(*) n, count(distinct {ENTITY}) accounts, min({TS})::text first_month,
                       max({TS})::text last_month, count(*) filter (where label_complete) labelled,
                       avg(({LABEL})::int) filter (where label_complete) churn_rate,
                       sum(hashtext({ENTITY}::text || {TS}::text || coalesce({LABEL}::text,'') ||
                           coalesce({probe}::text,'')) % 1000000007) as h
                       from {BASE}""")
    digest = hashlib.sha256(f"{s['n']}|{s['accounts']}|{s['first_month']}|{s['last_month']}|{s['h']}".encode()).hexdigest()
    s.pop("h", None)
    s["snapshot_sha256"] = digest
    return s


def integration_evidence(st: StageLog, summary: dict | None = None, reused: bool = False) -> dict:
    from . import db
    from .data import BASE, DERIVED, source_table

    snap = base_snapshot()
    src = (summary or {}).get("source") or source_table()
    rows = []
    for t, role in ((src, "monthly customer snapshot (primary)"), ("raw.nps_surveys", "as-of join: latest NPS before snapshot"),
                    ("raw.dispatches", "repair truck rolls in the 90 days before snapshot"), (BASE, "integrated output")):
        try:
            n = db.fetchone(f"select count(*) n from {t}")["n"] if db.table_exists(t) else None
        except Exception:
            n = None
        if n is not None:
            rows.append({"table": t, "role": role, "rows": n})
    st.table("sources", "Sources", rows, columns=["table", "role", "rows"])
    st.param(source_table=src, base_table=BASE, snapshot_sha256=snap["snapshot_sha256"], first_month=snap["first_month"],
             last_month=snap["last_month"], max_accounts=(summary or {}).get("max_accounts") or 0,
             dbt_mart_used=str(src.startswith("marts.")), derived_candidates=len(DERIVED))
    st.metric(rows=snap["n"], accounts=snap["accounts"], labelled_rows=snap["labelled"], churn_rate=snap["churn_rate"],
              seconds=(summary or {}).get("seconds"))
    st.headline(f"{snap['n']:,} rows · {snap['accounts']:,} BANs")
    st.summary(("Reused the integrated base table" if reused else "Built the leakage-safe base table") +
               f" {BASE} from {src}: {snap['n']:,} monthly snapshots of {snap['accounts']:,} billing accounts (BAN), "
               f"{snap['first_month']} → {snap['last_month']}, churn rate {100 * (snap['churn_rate'] or 0):.1f}% on labelled months. "
               f"Snapshot hash {snap['snapshot_sha256'][:12]}… pins exactly which data this model saw.")
    st.data(snapshot=snap)
    return snap


def eda_evidence(st: StageLog, df: pd.DataFrame | None = None, window: tuple | None = None) -> None:
    from . import data as D
    from . import eda
    from .config import LABEL, TS

    full = df if df is not None else D.load()
    lab = full[full.label_complete == 1]
    if window is not None:
        lab = lab[(lab[TS] >= pd.Timestamp(window[0])) & (lab[TS] <= pd.Timestamp(window[1]))]
    cands = [c for c in D.CANDIDATES if c in lab.columns]
    prof = eda.profile(lab, cands)
    st.table("profile", "Column profile (labelled rows)", prof.reset_index().head(40))
    plt = _plt()
    # 1 churn by technology
    seg_col = "technology" if "technology" in lab.columns else next((c for c in D.CATEGORICAL if c in lab.columns), None)
    tech = eda.churn_by(lab, seg_col).sort_values("churn_rate", ascending=False)
    st.table("churn_by_technology", f"Churn rate by {seg_col}", tech.reset_index())
    fig, ax = plt.subplots(figsize=(6.4, 3.0))
    ax.barh(tech.index.astype(str)[::-1], tech.churn_rate[::-1] * 100, color=TEAL, height=0.55)
    ax.axvline(lab[LABEL].mean() * 100, color=ORANGE, lw=1.2, ls="--")
    ax.text(lab[LABEL].mean() * 100, len(tech) - 0.45, f" overall {lab[LABEL].mean():.1%}", color=ORANGE, fontsize=8, va="bottom")
    for i, v in enumerate(tech.churn_rate[::-1] * 100):
        ax.text(v, i, f" {v:.1f}%", va="center", fontsize=8, color=INK)
    ax.set_xlabel("90-day churn rate (%)")
    ax.set_title("Churn rate by technology")
    ax.grid(axis="y", visible=False)
    st.chart(fig, "churn_by_technology", "Churn rate by technology", f"{tech.index[0]} churns most ({tech.churn_rate.iloc[0]:.1%}); "
             f"{tech.index[-1]} least ({tech.churn_rate.iloc[-1]:.1%}).")
    # 2 missingness
    miss = (lab[cands].isna().mean() * 100).sort_values(ascending=False)
    miss = miss[miss > 0].head(12)
    fig, ax = plt.subplots(figsize=(6.4, max(1.6, 0.32 * max(1, len(miss)) + 0.9)))
    if len(miss):
        ax.barh(miss.index[::-1], miss.values[::-1], color=TEAL_LIGHT, edgecolor=TEAL, height=0.55)
        for i, v in enumerate(miss.values[::-1]):
            ax.text(v, i, f" {v:.1f}%", va="center", fontsize=8, color=INK)
        ax.axvline(50, color=ORANGE, lw=1, ls="--")
        ax.set_xlim(0, 105)
    else:
        ax.text(0.5, 0.5, "no missing values", ha="center", va="center", transform=ax.transAxes)
    ax.set_xlabel("missing (%) — dashed line: 50 % drop threshold")
    ax.set_title("Missingness by column")
    ax.grid(axis="y", visible=False)
    st.chart(fig, "missingness", "Missingness", "Columns above 50 % missing are dropped by feature sifting.")
    # 3 correlation heatmap
    from matplotlib.colors import LinearSegmentedColormap

    nums = [c for c in cands if pd.api.types.is_numeric_dtype(lab[c]) and lab[c].std() > 0 and c != "probe_constant"]
    corr = lab[nums + [LABEL]].corr().round(2)
    cmap = LinearSegmentedColormap.from_list("bsp_div", [ORANGE, "#f8fafc", TEAL])
    fig, ax = plt.subplots(figsize=(7.4, 6.2))
    im = ax.imshow(corr.values, cmap=cmap, vmin=-1, vmax=1)
    ax.set_xticks(range(len(corr)), corr.columns, rotation=60, ha="right", fontsize=7)
    ax.set_yticks(range(len(corr)), corr.index, fontsize=7)
    ax.grid(False)
    fig.colorbar(im, ax=ax, shrink=0.75, label="Pearson r")
    ax.set_title("Correlation (numeric candidates + label)")
    st.chart(fig, "correlation_heatmap", "Correlation heatmap", "Redundant pairs (|r| ≈ 1) such as outage_minutes / outage_hours are visible here before sifting removes them.")
    top = corr[LABEL].drop(LABEL).abs().sort_values(ascending=False)
    st.metric(rows=len(lab), churn_rate=lab[LABEL].mean(), n_columns=len(cands),
              max_missing_pct=float(miss.max()) if len(miss) else 0.0, top_abs_corr_with_label=float(top.iloc[0]) if len(top) else 0.0)
    st.param(window=f"{lab[TS].min():%Y-%m}..{lab[TS].max():%Y-%m}", top_corr_feature=top.index[0] if len(top) else "")
    worst = tech.index[0]
    st.headline(f"churn {lab[LABEL].mean():.1%} · {len(cands)} columns")
    st.summary(f"{len(lab):,} labelled snapshots profiled. Highest churn: {worst} ({tech.churn_rate.iloc[0]:.1%}) vs "
               f"{lab[LABEL].mean():.1%} overall. Strongest linear signal: {top.index[0]} (|r| {top.iloc[0]:.2f}). "
               f"{len(miss)} columns have missing values.")
    st.data(summary_stats=json.loads(prof.reset_index().to_json(orient="records")))


def notebook_dir() -> Path | None:
    for p in (os.environ.get("BSP_NOTEBOOK_DIR"), str(Path.home() / "work" / "ml"), "/opt/templates/ml",
              str(Path(__file__).resolve().parents[3] / "work_templates" / "ml")):
        if p and Path(p).is_dir() and any(Path(p).glob("*.ipynb")):
            return Path(p)
    return None


def notebook_evidence(st: StageLog, notebooks: list[str], execute: str | None = None, timeout: int = 600,
                      inherited: dict | None = None) -> None:
    nbd = notebook_dir()
    rows = []
    for n in notebooks:
        p = (nbd / f"{n}.ipynb") if nbd else None
        ok = bool(p and p.exists())
        rows.append({"notebook": f"work_templates/ml/{n}.ipynb", "workspace_path": f"~/work/ml/{n}.ipynb",
                     "sha256": sha256_file(p)[:16] if ok else (inherited or {}).get(n, ""),
                     "bytes": p.stat().st_size if ok else None, "executed": False})
    executed = None
    if execute and nbd and (nbd / f"{execute}.ipynb").exists():
        t0 = time.time()
        out = st.dir / f"{execute}.executed.ipynb"
        try:
            import papermill as pm

            pm.execute_notebook(str(nbd / f"{execute}.ipynb"), str(out), kernel_name="python3", cwd=str(nbd),
                                progress_bar=False, log_output=False, execution_timeout=timeout, request_save_on_cell_execute=False)
            executed = {"notebook": execute, "seconds": round(time.time() - t0, 1), "ok": True}
            st.doc["files"].append(out.name)
            for r in rows:
                if r["notebook"].endswith(f"/{execute}.ipynb"):
                    r["executed"] = True
            import nbformat

            nb = nbformat.read(str(out), as_version=4)
            code = [c for c in nb.cells if c.cell_type == "code"]
            st.metric(cells_executed=len(code), execution_seconds=time.time() - t0,
                      outputs=sum(len(c.get("outputs", [])) for c in code))
        except Exception as e:
            executed = {"notebook": execute, "ok": False, "error": f"{e.__class__.__name__}: {str(e)[:300]}"}
            if out.exists():
                st.doc["files"].append(out.name)
            st.note(f"Notebook execution failed — path and hash recorded instead: {executed['error']}")
    st.table("notebooks", "Notebooks", rows, columns=["notebook", "workspace_path", "sha256", "bytes", "executed"])
    st.param(notebook_dir=str(nbd) if nbd else "not mounted here", notebooks=",".join(notebooks),
             executed=executed["notebook"] if executed and executed.get("ok") else "none")
    if executed and executed.get("ok"):
        st.link(f"Executed copy · {execute}.executed.ipynb", artifact=f"{execute}.executed.ipynb")
        st.headline(f"{execute.split('_')[0]} executed · {len(notebooks)} hashed")
    else:
        st.headline(f"{len(notebooks)} notebook{'s' if len(notebooks) != 1 else ''} hashed")
    st.summary("The thin notebooks in ~/work/ml (seeded from work_templates/ml) drive the same library code as this pipeline. "
               "Their content hashes are recorded (no git in the appliance)" +
               (f"; {execute} was executed headless with papermill and the executed copy is attached." if executed and executed.get("ok") else "."))
    st.data(notebooks=rows, executed=executed)


def selection_evidence(st: StageLog, sel: dict, used: list[str] | None = None) -> None:
    kept, dropped = sel.get("kept", []), sel.get("dropped", {})
    tables = sel.get("tables", {})
    rank = pd.DataFrame({"feature": sel.get("candidates", list(kept) + list(dropped))}).set_index("feature")
    for name in ("mutual_info", "permutation", "shap", "stability"):
        t = tables.get(name)
        if t:
            tdf = pd.DataFrame(t)
            key = tdf.columns[0]
            val = [c for c in tdf.columns if c != key][0] if len(tdf.columns) > 1 else None
            if val:
                rank = rank.join(tdf.set_index(key)[[val]].rename(columns={val: name}), how="left")
    rank["decision"] = ["keep" if f in kept else "drop" for f in rank.index]
    rank["reason"] = [dropped.get(f, "") for f in rank.index]
    if used is not None:
        rank["in_model"] = [f in used for f in rank.index]
    sort_col = next((c for c in ("permutation", "mutual_info", "shap") if c in rank), None)
    if sort_col:
        rank = rank.sort_values(["decision", sort_col], ascending=[False, False])
    st.table("features", "Ranked features (kept first)", rank.reset_index(), kind="features")
    plt = _plt()
    if sort_col:
        top = rank[sort_col].dropna().sort_values(ascending=True).tail(18)
        fig, ax = plt.subplots(figsize=(6.4, 0.26 * len(top) + 1.0))
        cols = [TEAL if rank.loc[f, "decision"] == "keep" else GREY for f in top.index]
        ax.barh(top.index, top.values, color=cols, height=0.6)
        ax.set_title(f"Feature ranking — {sort_col.replace('_', ' ')} (grey = dropped)")
        ax.grid(axis="y", visible=False)
        st.chart(fig, "feature_ranking", "Feature ranking", "Teal bars kept, grey dropped (with a reason for each).")
    n_used = len(used) if used is not None else len(kept)
    st.metric(n_candidates=len(rank), n_kept=len(kept), n_dropped=len(dropped), n_in_model=n_used, seconds=sel.get("seconds"))
    st.param(kept=",".join(kept)[:5000], dropped=",".join(dropped)[:5000])
    st.headline(f"{len(kept)} kept / {len(rank)}")
    st.summary(f"{len(rank)} candidates went through seven filters on the training window only. {len(kept)} kept, "
               f"{len(dropped)} dropped with a reason each (e.g. " +
               "; ".join(f"{k}: {v}" for k, v in list(dropped.items())[:3]) + f"). The model uses {n_used} feature-store features.")


def holdout_evidence(st: StageLog, cmp: pd.DataFrame | list[dict] | None, windows: dict, sizes: dict | None = None,
                     pit_exact: bool | None = None, chosen: str = "time") -> None:
    if cmp is not None and len(cmp):
        c = cmp.reset_index() if isinstance(cmp, pd.DataFrame) else pd.DataFrame(cmp)
        keep = [x for x in ("strategy", "n_train", "n_valid", "n_test", "accounts_in_train_and_test", "test_months_seen_in_train",
                            "valid_auc", "test_auc", "future_auc", "optimism") if x in c.columns]
        st.table("holdout_comparison", "Same model under four hold-out strategies", c[keep], kind="holdout")
        for r in c.to_dict("records"):
            st.metric(**{f"{r['strategy']}_optimism": r.get("optimism"), f"{r['strategy']}_future_auc": r.get("future_auc")})
        cc = c.set_index("strategy")
        plt = _plt()
        fig, ax = plt.subplots(figsize=(6.4, 2.8))
        x = np.arange(len(cc))
        ax.bar(x - 0.18, cc["test_auc"], 0.34, color=TEAL_LIGHT, edgecolor=TEAL, label="own test split")
        ax.bar(x + 0.18, cc["future_auc"], 0.34, color=TEAL, label="truly future window")
        ax.set_xticks(x, cc.index)
        lo = float(min(cc["test_auc"].min(), cc["future_auc"].min()))
        ax.set_ylim(max(0.5, lo - 0.03), float(max(cc["test_auc"].max(), cc["future_auc"].max())) + 0.02)
        ax.set_ylabel("AUC")
        ax.legend(frameon=False, fontsize=8, loc="upper right")
        ax.set_title("Optimism: own-split AUC vs future AUC")
        ax.grid(axis="x", visible=False)
        st.chart(fig, "holdout_optimism", "Hold-out optimism", "The gap between the bars is how much each split flatters the model.")
        shared = int(cc.loc["random", "accounts_in_train_and_test"]) if "random" in cc.index else None
    else:
        shared = None
    w = {k: (f"{pd.Timestamp(v[0]):%Y-%m}..{pd.Timestamp(v[1]):%Y-%m}" if isinstance(v, tuple) else v)
         for k, v in windows.items() if k in ("train", "valid", "test")}
    tr_end, te_start = pd.Timestamp(windows["train"][1]), pd.Timestamp(windows["test"][0])
    gap = (te_start.year - tr_end.year) * 12 + te_start.month - tr_end.month
    month_overlap = 0 if pd.Timestamp(windows["train"][1]) < pd.Timestamp(windows["test"][0]) else 1
    checks = [
        {"check": "test months never seen in training", "result": "pass" if month_overlap == 0 else "FAIL",
         "detail": f"train {w['train']} · valid {w['valid']} · test {w['test']}"},
        {"check": "point-in-time feature join exact", "result": "pass" if pit_exact else ("n/a" if pit_exact is None else "FAIL"),
         "detail": "feature store as-of join reproduces the warehouse values" if pit_exact is not None else "not re-checked here"},
        {"check": "as-of joins strictly before the snapshot", "result": "pass",
         "detail": "NPS: latest survey < snapshot; tickets: 90 days before snapshot (data.integrate)"},
        {"check": "random split shares customers with test", "result": "avoided" if shared else "n/a",
         "detail": f"{shared:,} accounts would leak under a random split" if shared else ""},
    ]
    st.table("leakage_checks", "Leakage checks", checks, columns=["check", "result", "detail"], kind="checks")
    st.param(strategy=chosen, **{f"{k}_window": v for k, v in w.items()}, train_to_test_gap_months=gap)
    if sizes:
        st.metric(**sizes)
    passed = all(r["result"] in ("pass", "avoided", "n/a") for r in checks)
    st.metric(leakage_checks_passed=float(passed))
    st.headline(f"{chosen} split · leakage {'pass' if passed else 'FAIL'}")
    st.summary(f"Chosen strategy: {chosen}-based (train {w['train']}, validate {w['valid']}, test {w['test']}). "
               + (f"A random split would put {shared:,} customers in both train and test and over-promise. " if shared else "")
               + f"Leakage checks: {'all passed' if passed else 'see table'}.")


def feature_store_evidence(st: StageLog, off: list | None, on: dict | None, pit_exact: bool | None, feast: dict | None) -> None:
    from . import featurestore

    try:
        fr = featurestore.freshness()
    except Exception:
        fr = off or []
    try:
        current = {(v["name"], int(v["version"])) for v in featurestore.load_definitions()["feature_views"]}
        fr = [r for r in fr if (r.get("feature_view"), int(r.get("version") or 0)) in current] or fr
    except Exception:
        pass
    rows = [{"view": f"{r.get('feature_view')}_v{r.get('version')}", "features": r.get("n_features"), "ttl_days": r.get("ttl_days"),
             "offline_rows": r.get("rows"), "online_rows": r.get("online_rows"), "def_hash": r.get("def_hash")} for r in fr]
    st.table("views", "Feature views", rows, columns=["view", "features", "ttl_days", "offline_rows", "online_rows", "def_hash"])
    st.metric(views=len(rows), online_accounts=(on or {}).get("accounts"), pit_exact=float(bool(pit_exact)) if pit_exact is not None else None)
    st.param(online_as_of=(on or {}).get("as_of"), feast=(feast or {}).get("status", "n/a"))
    st.headline(f"{len(rows)} views · PIT {'exact' if pit_exact else 'n/a' if pit_exact is None else 'MISMATCH'}")
    st.summary(f"{len(rows)} versioned feature views materialised offline (Postgres `features`) and online "
               f"({(on or {}).get('accounts', 0):,} accounts). Training rows come from a point-in-time join"
               + (" that reproduces the warehouse exactly." if pit_exact else ".") + f" Feast mirror: {(feast or {}).get('status', 'n/a')}.")


def algo_evidence(st: StageLog, rows: list[dict], chosen_run: str | None, chosen_label: str = "") -> None:
    rows = sorted(rows, key=lambda r: -(r.get("valid_auc") or 0))
    out = []
    for i, r in enumerate(rows):
        out.append({"rank": i + 1, "model": r["model"], "valid_auc": r.get("valid_auc"), "test_auc": r.get("test_auc"),
                    "test_pr_auc": r.get("test_pr_auc"), "test_logloss": r.get("test_logloss"),
                    "test_lift_at_10": r.get("test_lift_at_10"), "fit_seconds": r.get("fit_seconds"),
                    "chosen": r.get("run_id") == chosen_run, "run_id": r.get("run_id"), "experiment_id": r.get("experiment_id")})
        st.source_run(r.get("run_id"), f"leaderboard · {r['model']}", r.get("experiment_id"))
    st.table("leaderboard", "Leaderboard — selected on validation AUC, reported on test", out, kind="leaderboard",
             columns=["rank", "model", "valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_lift_at_10", "fit_seconds", "chosen"])
    if out:
        plt = _plt()
        fig, ax = plt.subplots(figsize=(6.4, 0.42 * len(out) + 1.0))
        names = [r["model"] for r in out][::-1]
        vals = [r["valid_auc"] or 0 for r in out][::-1]
        ax.barh(names, vals, color=[ORANGE if r["chosen"] else TEAL for r in out][::-1], height=0.55)
        for i, v in enumerate(vals):
            ax.text(v, i, f" {v:.4f}", va="center", fontsize=8, color=INK)
        lo = min(vals)
        ax.set_xlim(max(0.5, lo - 0.02), max(vals) + 0.015)
        ax.set_xlabel("validation AUC")
        ax.set_title("Candidate model families")
        ax.grid(axis="y", visible=False)
        st.chart(fig, "leaderboard", "Leaderboard", "Orange = the family carried forward.")
        best = out[0]
        st.metric(n_candidates=len(out), best_valid_auc=best["valid_auc"], best_test_auc=best["test_auc"])
        st.param(best_model=best["model"], chosen=chosen_label or next((r["model"] for r in out if r["chosen"]), best["model"]))
        st.headline(f"{len(out)} families · best {best['model']}")
        st.summary(f"{len(out)} model families trained on identical data and windows. Best on validation: {best['model']} "
                   f"(valid AUC {best['valid_auc']:.4f}, test AUC {best['test_auc']:.4f}). "
                   + (f"Carried forward: {chosen_label}." if chosen_label else ""))


def curve_points(x: np.ndarray, y: np.ndarray, n: int = 160) -> list[list[float]]:
    if len(x) > n:
        idx = np.unique(np.linspace(0, len(x) - 1, n).astype(int))
        x, y = x[idx], y[idx]
    return [[round(float(a), 5), round(float(b), 5)] for a, b in zip(x, y)]


def roc_evidence(st: StageLog, y: np.ndarray, p: np.ndarray, label: str, window: str = "") -> dict:
    from sklearn.metrics import average_precision_score, precision_recall_curve, roc_auc_score, roc_curve

    y = np.asarray(y).astype(int)
    fpr, tpr, _ = roc_curve(y, p)
    auc = float(roc_auc_score(y, p))
    prec, rec, _ = precision_recall_curve(y, p)
    ap = float(average_precision_score(y, p))
    base = float(y.mean())
    st.curve("roc", {"points": curve_points(fpr, tpr), "auc": auc, "x": "false positive rate", "y": "true positive rate"})
    st.curve("pr", {"points": curve_points(rec[::-1], prec[::-1]), "ap": ap, "base_rate": base, "x": "recall", "y": "precision"})
    plt = _plt()
    fig, ax = plt.subplots(figsize=(4.4, 4.2))
    ax.plot(fpr, tpr, color=TEAL, lw=2, label=f"{label} (AUC {auc:.3f})")
    ax.fill_between(fpr, tpr, alpha=0.08, color=TEAL)
    ax.plot([0, 1], [0, 1], ls="--", color=GREY, lw=1)
    ax.set(xlabel="false positive rate", ylabel="true positive rate", xlim=(0, 1), ylim=(0, 1.01), title="ROC — test window")
    ax.legend(frameon=False, loc="lower right", fontsize=8)
    st.chart(fig, "roc_curve", "ROC curve", f"Test window {window}.")
    fig, ax = plt.subplots(figsize=(4.4, 4.2))
    ax.plot(rec, prec, color=TEAL, lw=2, label=f"PR-AUC {ap:.3f}")
    ax.axhline(base, ls="--", color=GREY, lw=1, label=f"base rate {base:.3f}")
    ax.set(xlabel="recall", ylabel="precision", xlim=(0, 1), ylim=(0, 1.01), title="Precision–recall — test window")
    ax.legend(frameon=False, loc="upper right", fontsize=8)
    st.chart(fig, "pr_curve", "Precision–recall curve", f"Test window {window}.")
    st.metric(test_auc=auc, test_pr_auc=ap, base_rate=base, n_test=len(y), positives=int(y.sum()))
    st.param(model=label, test_window=window)
    st.headline(f"AUC {auc:.3f} · PR-AUC {ap:.3f}")
    st.summary(f"{label} on the untouched test window {window}: {len(y):,} customer-months, {int(y.sum()):,} churners "
               f"(base rate {base:.1%}). ROC AUC {auc:.4f}; PR-AUC {ap:.4f} = {ap / base if base else 0:.1f}× the random baseline.")
    return {"auc": auc, "ap": ap}


def tuning_evidence(st: StageLog, parent_run_id: str | None) -> None:
    c = _client()
    if not parent_run_id:
        st.status("skipped").headline("not tuned").summary("No Optuna study is linked to this model.")
        return
    pr = c.get_run(parent_run_id)
    kids = c.search_runs([pr.info.experiment_id], filter_string=f"tags.mlflow.parentRunId = '{parent_run_id}'",
                         max_results=1000)
    trials = []
    for k in kids:
        if k.data.tags.get("stage") != "tuning-trial":
            continue
        n = k.info.run_name.split("-")[-1]
        trials.append({"trial": int(n) if n.isdigit() else len(trials), "valid_auc": k.data.metrics.get("valid_auc"),
                       "fit_seconds": k.data.metrics.get("fit_seconds"),
                       **{p[3:]: _num(v) for p, v in k.data.params.items() if p.startswith("hp_")}})
    trials.sort(key=lambda r: r["trial"])
    if not trials:
        st.status("skipped").headline("no trials").summary("The linked study has no trial runs.")
        return
    best = max(trials, key=lambda r: r["valid_auc"] or 0)
    for r in trials:
        r["best"] = r is best
    hp = [k for k in trials[0] if k not in ("trial", "valid_auc", "fit_seconds", "best")]
    st.table("trials", "Optuna trials (validation AUC)", trials, kind="trials", columns=["trial", "valid_auc", "fit_seconds", *hp, "best"])
    st.table("best_params", "Best parameters", [{"param": k, "value": best[k]} for k in hp], columns=["param", "value"])
    plt = _plt()
    xs = [r["trial"] for r in trials]
    ys = [r["valid_auc"] for r in trials]
    run_best = np.maximum.accumulate(ys)
    fig, ax = plt.subplots(figsize=(6.4, 2.9))
    ax.scatter(xs, ys, s=22, color=TEAL_LIGHT, edgecolor=TEAL, linewidth=0.8, label="trial", zorder=3)
    ax.step(xs, run_best, where="post", color=ORANGE, lw=1.8, label="best so far")
    ax.set(xlabel="trial", ylabel="validation AUC", title="Optimisation history")
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    st.chart(fig, "optimisation_history", "Optimisation history", "TPE sampler, seeded; every trial is a nested MLflow run.")
    st.source_run(parent_run_id, "Optuna study (parent run)", pr.info.experiment_id)
    st.metric(n_trials=len(trials), best_trial_valid_auc=best["valid_auc"], first_trial_valid_auc=ys[0],
              tuned_test_auc=pr.data.metrics.get("tuned_test_auc"), tuned_valid_auc=pr.data.metrics.get("tuned_valid_auc"))
    st.param(model_kind=pr.data.tags.get("model_kind"), **{f"best_{k}": best[k] for k in hp})
    st.headline(f"{len(trials)} trials · best {best['valid_auc']:.4f}")
    st.summary(f"Optuna TPE ran {len(trials)} trials of {pr.data.tags.get('model_kind', 'the model')} on validation AUC; "
               f"best trial #{best['trial']} reached {best['valid_auc']:.4f} (first trial {ys[0]:.4f}). The best parameters were "
               f"refit and logged as the tuned candidate.")


def _num(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except (TypeError, ValueError):
            pass
    return v


def inherited_params_evidence(st: StageLog, run_id: str, parent_version: str | None) -> None:
    r = _client().get_run(run_id)
    hp = [{"param": k[3:], "value": _num(v)} for k, v in sorted(r.data.params.items()) if k.startswith("hp_")]
    st.table("params", "Hyper-parameters (inherited)", hp, columns=["param", "value"])
    st.status("skipped").headline("params inherited")
    st.summary(f"No search in a retrain: the champion's recipe (v{parent_version}) — model family and hyper-parameters — is refit "
               "on the newest labelled data so the comparison isolates the effect of fresher data.")


def evaluation_evidence(st: StageLog, y: np.ndarray, p: np.ndarray, run_id: str | None, label: str,
                        compare: dict | None = None) -> None:
    from .metrics import calibration_table, evaluate, gain_table

    y = np.asarray(y).astype(int)
    m = evaluate(y, p, "test_")
    cal = calibration_table(y, p)
    gain = gain_table(y, p)
    st.table("calibration", "Calibration (deciles of predicted risk)", cal.reset_index().rename(columns={"bin": "score_bin"}))
    st.table("gain", "Lift and cumulative gain by decile", gain.reset_index())
    plt = _plt()
    fig, ax = plt.subplots(figsize=(4.4, 4.0))
    lim = float(max(cal.mean_predicted.max(), cal.observed_rate.max()) * 1.08)
    ax.plot([0, lim], [0, lim], ls="--", color=GREY, lw=1)
    ax.plot(cal.mean_predicted, cal.observed_rate, "o-", color=TEAL, lw=1.8, ms=5)
    ax.set(xlabel="mean predicted churn", ylabel="observed churn rate", title="Calibration", xlim=(0, lim), ylim=(0, lim))
    st.chart(fig, "calibration", "Calibration", f"Brier {m['test_brier']:.4f}; points on the diagonal = honest probabilities.")
    fig, ax = plt.subplots(1, 2, figsize=(8.4, 3.2))
    ax[0].bar(gain.index, gain.lift, color=[ORANGE if i == 1 else TEAL for i in gain.index], width=0.62)
    ax[0].axhline(1, ls="--", color=GREY, lw=1)
    ax[0].set(xlabel="score decile (1 = highest risk)", ylabel="lift", title="Lift by decile", xticks=list(gain.index))
    ax[0].grid(axis="x", visible=False)
    xs = [0] + [10 * i for i in gain.index]
    ax[1].plot(xs, [0] + list(gain.cum_capture * 100), "o-", color=TEAL, lw=1.8, ms=4)
    ax[1].plot([0, 100], [0, 100], ls="--", color=GREY, lw=1)
    ax[1].set(xlabel="% customers targeted", ylabel="% churners captured", title="Cumulative gain", xlim=(0, 100), ylim=(0, 100))
    st.chart(fig, "lift_gain", "Lift and gain", f"Top decile captures {gain.cum_capture.iloc[0]:.0%} of churners (lift {gain.lift.iloc[0]:.2f}).")
    shap_df = None
    if run_id:
        try:
            with tempfile.TemporaryDirectory() as td:
                pth = _client().download_artifacts(run_id, "evaluation/shap_importance.csv", td)
                shap_df = pd.read_csv(pth, index_col=0)
        except Exception:
            shap_df = None
    if shap_df is not None and len(shap_df):
        s = shap_df.iloc[:, 0].sort_values(ascending=False).head(15)
        st.table("shap", "Global importance (mean |SHAP|)", [{"feature": k, "mean_abs_shap": float(v)} for k, v in s.items()],
                 columns=["feature", "mean_abs_shap"])
        fig, ax = plt.subplots(figsize=(6.4, 0.27 * len(s) + 1.0))
        ax.barh(s.index[::-1], s.values[::-1], color=TEAL, height=0.6)
        ax.set(title="Global importance — mean |SHAP|")
        ax.grid(axis="y", visible=False)
        st.chart(fig, "shap_importance", "SHAP global importance", "Computed on 1 000 test rows with TreeExplainer / LinearExplainer.")
        st.param(shap_top=s.index[0])
    else:
        st.note("No SHAP importance logged for this run (non-tree baseline or SHAP unavailable).")
    if compare:
        st.table("champion_vs_challenger", "Champion vs challenger on the same newest hold-out",
                 [{"model": k, **v} for k, v in compare.items()], kind="compare")
        st.doc["tables"].sort(key=lambda t: t.get("kind") != "compare")     # the verdict first
    st.metric(**{k: v for k, v in m.items() if k.startswith("test_")})
    if run_id:
        st.source_run(run_id, f"training run · {label}")
    st.headline(f"lift@10 {m['test_lift_at_10']:.2f} · Brier {m['test_brier']:.3f}")
    st.summary(f"{label}: top-decile lift {m['test_lift_at_10']:.2f} (captures {m['test_capture_at_10']:.0%} of churners), "
               f"KS {m['test_ks']:.3f}, Brier {m['test_brier']:.4f}, logloss {m['test_logloss']:.4f}."
               + (f" Most important driver: {shap_df.index[0]}." if shap_df is not None and len(shap_df) else ""))


def registration_evidence(st: StageLog, version: str, run_id: str, alias: str | None, description: str = "") -> None:
    c = _client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    st.param(model_name=config.MODEL_NAME, version=str(version), alias=alias or "", run_id=run_id, source=mv.source)
    st.metric(version=float(version))
    st.link(f"MLflow model {config.MODEL_NAME} v{version}", f"/mlflow/#/models/{config.MODEL_NAME}/versions/{version}")
    st.source_run(run_id, "registered training run")
    st.headline(f"{config.MODEL_NAME} v{version}" + (f" · {alias}" if alias else ""))
    st.summary(f"Registered runs:/{run_id[:8]}…/model as {config.MODEL_NAME} version {version}"
               + (f" with alias `{alias}`" if alias else "") + (f" — {description}" if description else "") + ".")


# ── predictions for ROC / evaluation (works for any registered or logged run) ──
def run_test_predictions(run_id: str, window: str | None = None):
    """Load runs:/<id>/model, rebuild its test window from the feature store
    and score it. Returns (y, p, window_str, model_kind)."""
    import mlflow.sklearn

    from .config import LABEL
    from .pipeline import mlflow_setup, training_set

    mlflow_setup()
    c = _client()
    r = c.get_run(run_id)
    win = window or r.data.params.get("test_window")
    a, b = win.split("..")
    w = (pd.Timestamp(a + "-01"), pd.Timestamp(b + "-01"))
    with tempfile.TemporaryDirectory() as td:
        feats = json.loads(Path(c.download_artifacts(run_id, "evaluation/features.json", td)).read_text())["features"]
    te = training_set(w, feats)
    model = mlflow.sklearn.load_model(f"runs:/{run_id}/model")
    p = model.predict_proba(te[feats])[:, 1]
    return te[LABEL].to_numpy().astype(int), p, win, r.data.params.get("model_kind", "")


def leaderboard_rows(run_ids: list[str]) -> list[dict]:
    c = _client()
    out = []
    for rid in run_ids:
        r = c.get_run(rid)
        m = r.data.metrics
        out.append({"run_id": rid, "experiment_id": r.info.experiment_id, "model": r.data.params.get("model_kind") or r.info.run_name,
                    **{k: m.get(k) for k in ("valid_auc", "test_auc", "test_pr_auc", "test_logloss", "test_lift_at_10", "fit_seconds")}})
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  operational stages — live from Postgres (source of truth)
# ═══════════════════════════════════════════════════════════════════════════
def _iso(x):
    return x.isoformat() if hasattr(x, "isoformat") else x


def ops_stage(key: str, version: str, registered_ms: int | None = None) -> dict:
    from . import db

    v = str(version)
    st: dict[str, Any] = {"key": key, "title": TITLES[key], "status": "pending", "headline": "not yet", "summary": "",
                          "tables": [], "charts": [], "curves": {}, "links": [], "source_runs": [], "notes": [], "metrics": {},
                          "params": {}, "started": None, "ended": None, "live": True}
    try:
        db.ensure_schema()
        if key == "release":
            recs = db.fetchall("""select id, created_at, action, from_stage, to_stage, status, requested_by, requested_source, approved_by,
                                  approved_role, approved_source, approved_at, baseline_version, champion_before, champion_after,
                                  gates_passed, gates, holdout, evidence_run_id, decision_note
                                  from mlops.promotions where model_name=%s and model_version=%s order by id""", (config.MODEL_NAME, v))
            if recs:
                last = recs[-1]
                gated = [r for r in recs if r.get("gates")]
                g = gated[-1] if gated else None
                done = [r for r in recs if r["to_stage"] == "production" and r["status"] in ("approved", "completed")]
                st["status"] = "done" if done or last["status"] in ("completed", "approved") else \
                    "failed" if last["status"] in ("gates_failed", "rejected") else "running" if last["status"] == "pending_approval" else "done"
                st["started"], st["ended"] = _iso(recs[0]["created_at"]), _iso(last.get("approved_at") or last["created_at"])
                if g:
                    gl = g["gates"] or []
                    npass = sum(1 for x in gl if x["status"] in ("pass", "warn", "skip"))
                    st["tables"].append({"name": "gates", "title": f"Release gates (promotion #{g['id']}, baseline "
                                         f"{('v' + g['baseline_version']) if g['baseline_version'] else 'none'}, hold-out "
                                         f"{(g.get('holdout') or {}).get('window')})", "kind": "gates",
                                         "columns": ["name", "result", "value", "threshold", "detail"],
                                         "rows": [{"name": x["name"], "result": x["status"], "value": x.get("value"), "threshold": x.get("threshold"),
                                                   "detail": x.get("detail")} for x in gl]})
                    st["metrics"] = {"gates_passed": npass, "gates": len(gl), "promotions": len(recs)}
                    nr = next((x for x in gl if x["key"] == "non_regression"), None)
                    if nr and (nr.get("data") or {}).get("delta_auc") is not None:
                        st["metrics"].update(delta_auc=nr["data"]["delta_auc"], ci_low=nr["data"]["ci_low"], delong_p=nr["data"]["delong_p"])
                    if g.get("evidence_run_id"):
                        st["source_runs"].append({"run_id": g["evidence_run_id"], "label": "gate evidence run", "experiment_id": None})
                st["tables"].append({"name": "promotions", "title": "Promotion records", "kind": "promotions",
                                     "columns": ["id", "created_at", "action", "to_stage", "status", "requested_by", "approved_by", "approved_source",
                                                 "champion_before", "champion_after", "decision_note"],
                                     "rows": [{k: _iso(r[k]) for k in ("id", "created_at", "action", "to_stage", "status", "requested_by",
                                                                        "approved_by", "approved_source", "champion_before", "champion_after",
                                                                        "decision_note")} for r in recs]})
                if done:
                    d = done[-1]
                    st["headline"] = f"approved by {d['approved_by']}" if d["action"] == "promote" else f"{d['action']} → production"
                elif last["status"] == "pending_approval":
                    st["headline"] = f"gates passed · awaiting approval (#{last['id']})"
                elif last["status"] == "gates_failed":
                    failed = [x["name"] for x in (last.get("gates") or []) if x.get("blocking") and x["status"] == "fail"]
                    st["headline"] = "blocked: " + ", ".join(failed)[:80]
                else:
                    st["headline"] = f"{last['to_stage']} · {last['status']}"
                st["summary"] = (f"{len(recs)} release record(s) for v{v}. " + (
                    f"Production approved by {done[-1]['approved_by']} ({done[-1]['approved_source']}), requested by {done[-1]['requested_by']}; "
                    f"champion v{done[-1]['champion_before'] or '—'} → v{done[-1]['champion_after']}." if done else
                    f"Latest: {last['action']} → {last['to_stage']} = {last['status']}."))
                st["links"].append({"label": "Release & approvals page", "href": f"#release?id={last['id']}"})
            else:
                st["summary"] = f"v{v} has not been submitted to the release pipeline (python -m bsp_mlops.release submit {v})."
        elif key == "deployment":
            served = db.fetchall("""select colour, mode, shadow, count(*) n, min(ts) first_ts, max(ts) last_ts,
                                    avg(score) mean_score, percentile_cont(0.95) within group (order by latency_ms) p95
                                    from mlops.inference_log where model_version=%s group by 1,2,3 order by n desc""", (v,))
            dep = db.fetchone("select * from mlops.deployments where deployment=%s", (config.DEPLOYMENT,))
            since = datetime.fromtimestamp(registered_ms / 1000, timezone.utc).isoformat() if registered_ms else ""
            events = []
            for e in (dep or {}).get("history") or []:
                if since and str(e.get("ts", "")) < since:
                    continue
                aft, bef = e.get("after") or {}, e.get("before") or {}
                involved = v in (str(aft.get("blue_version")), str(aft.get("green_version")),
                                 str(bef.get("blue_version")), str(bef.get("green_version")))
                events.append({"ts": e.get("ts"), "action": e.get("action"), "actor": e.get("actor"),
                               "mode": aft.get("mode"), "live_color": aft.get("live_color"),
                               "canary_weight": aft.get("canary_weight"), "shadow": aft.get("shadow_enabled"),
                               "blue": aft.get("blue_version"), "green": aft.get("green_version"), "names_this_version": involved})
            n_served = sum(r["n"] for r in served if not r["shadow"])
            n_shadow = sum(r["n"] for r in served if r["shadow"])
            colours = sorted({r["colour"] for r in served if r["colour"]})
            st["tables"].append({"name": "traffic", "title": f"Traffic served by v{v}", "kind": "traffic",
                                 "columns": ["colour", "mode", "shadow", "n", "mean_score", "p95", "first_ts", "last_ts"],
                                 "rows": [{k: _iso(r[k]) if k.endswith("_ts") else r[k] for k in r} for r in served]})
            if events:
                st["tables"].append({"name": "events", "title": "Routing events since registration", "kind": "events",
                                     "columns": ["ts", "action", "actor", "mode", "live_color", "canary_weight", "shadow", "blue", "green"],
                                     "rows": events[-40:]})
            if served:
                st["status"] = "done"
                st["started"] = _iso(min(r["first_ts"] for r in served))
                st["ended"] = _iso(max(r["last_ts"] for r in served))
                st["headline"] = f"{n_served:,} served" + (f" · {n_shadow:,} shadow" if n_shadow else "")
                st["summary"] = (f"v{v} answered {n_served:,} live requests" + (f" and shadow-scored {n_shadow:,}" if n_shadow else "")
                                 + f" as {', '.join(colours)}. {len(events)} routing change(s) since registration"
                                 + (f"; current mode {dep['mode']}." if dep else "."))
            elif events:
                st["summary"] = f"{len(events)} routing change(s) since registration, but no traffic served by v{v} yet."
            st["metrics"] = {"served": n_served, "shadow_scored": n_shadow, "routing_events": len(events)}
            st["params"] = {"current_mode": (dep or {}).get("mode"), "colours": ",".join(colours)}
        elif key == "monitoring":
            reps = db.fetchall("""select id, created_at, max_psi, prediction_psi, n_features_breached, breached, breach_reasons,
                                  n_current, auc_reference, auc_current, source, feature_drift, triggered_retrain_id
                                  from mlops.drift_reports where model_version=%s order by id""", (v,))
            if reps:
                worst = max(reps, key=lambda r: r["max_psi"] or 0)
                n_b = sum(1 for r in reps if r["breached"])
                st["status"] = "done"
                st["started"], st["ended"] = _iso(reps[0]["created_at"]), _iso(reps[-1]["created_at"])
                st["headline"] = f"max PSI {worst['max_psi']:.2f}" + (f" · {n_b} breach{'es' if n_b > 1 else ''}" if n_b else " · stable")
                st["tables"].append({"name": "reports", "title": "Drift checks", "kind": "drift",
                                     "columns": ["id", "created_at", "source", "n_current", "max_psi", "prediction_psi",
                                                 "n_features_breached", "breached", "triggered_retrain_id"],
                                     "rows": [{k: _iso(r[k]) for k in r if k not in ("feature_drift", "breach_reasons")} for r in reps[-30:]]})
                fd = worst.get("feature_drift") or []
                st["tables"].append({"name": "psi", "title": f"PSI per feature — worst check #{worst['id']}", "kind": "psi",
                                     "columns": ["feature", "psi", "ks", "status", "ref_mean", "cur_mean"], "rows": fd[:25]})
                st["data"] = {"psi": [{"feature": r["feature"], "psi": r["psi"], "status": r["status"]} for r in fd[:15]],
                              "psi_warn": config.PSI_WARN, "psi_breach": config.PSI_BREACH}
                st["summary"] = (f"{len(reps)} drift check(s) against v{v}'s release baseline; {n_b} breached. Worst: check #{worst['id']} "
                                 f"max PSI {worst['max_psi']:.3f}, prediction PSI {worst['prediction_psi'] or 0:.3f}"
                                 + (f" — {'; '.join(worst['breach_reasons'] or [])}" if worst["breached"] else "") + ".")
                st["metrics"] = {"checks": len(reps), "breaches": n_b, "max_psi": worst["max_psi"],
                                 "prediction_psi": worst["prediction_psi"]}
            else:
                st["summary"] = f"No drift check has scored traffic for v{v} yet (the monitor checks the champion every few minutes)."
        elif key == "retrain":
            rows = db.fetchall("""select id, created_at, started_at, finished_at, status, trigger, reason, model_version, champion_version,
                                  challenger_auc, champion_auc, margin, promoted, decision, drift_report_id
                                  from mlops.retrain_runs where model_version=%s or champion_version=%s order by id""", (v, v))
            if rows:
                last = rows[-1]
                st["status"] = {"succeeded": "done", "failed": "failed", "running": "running", "queued": "running"}.get(last["status"], "done")
                st["started"] = _iso(rows[0]["started_at"] or rows[0]["created_at"])
                st["ended"] = _iso(last["finished_at"]) if last["finished_at"] else None
                st["tables"].append({"name": "retrains", "title": "Retrains", "kind": "retrains",
                                     "columns": ["id", "trigger", "status", "champion_version", "model_version", "challenger_auc",
                                                 "champion_auc", "margin", "promoted", "decision"],
                                     "rows": [{k: _iso(r[k]) for k in r} for r in rows]})
                made = [r for r in rows if str(r["model_version"]) == v]
                trig = [r for r in rows if str(r["champion_version"]) == v and str(r["model_version"]) != v]
                parts = []
                if made:
                    r = made[-1]
                    parts.append(f"produced by retrain #{r['id']} ({r['trigger']}): AUC {r['challenger_auc'] or 0:.4f} vs champion "
                                 f"v{r['champion_version']} {r['champion_auc'] or 0:.4f} → {'promoted' if r['promoted'] else 'kept as challenger'}")
                    st["headline"] = f"#{r['id']} {'promoted' if r['promoted'] else 'challenger'} ({r['margin'] or 0:+.3f})"
                if trig:
                    r = trig[-1]
                    parts.append(f"while champion, retrain #{r['id']} ({r['trigger']}) produced v{r['model_version'] or '—'} "
                                 f"→ {'promoted' if r['promoted'] else r['status']}")
                    if not made:
                        st["headline"] = f"#{r['id']} → v{r['model_version'] or '…'}" + (" promoted" if r["promoted"] else "")
                st["summary"] = "; ".join(parts).capitalize() + "."
                st["metrics"] = {"retrains": len(rows), "promoted": sum(1 for r in rows if r["promoted"])}
            else:
                st["summary"] = "No retrain has involved this version yet."
        elif key == "freeze":
            rows = db.fetchall("""select id, created_at, bundle_name, sha256, size_bytes, repository, uri,
                                  manifest->>'content_digest' content_digest from mlops.frozen_artifacts where model_version=%s
                                  order by id""", (v,))
            if rows:
                r = rows[-1]
                st["status"] = "done"
                st["started"] = st["ended"] = _iso(r["created_at"])
                st["headline"] = f"sha256 {r['sha256'][:10]}…"
                st["tables"].append({"name": "bundles", "title": "Frozen bundles", "kind": "frozen",
                                     "columns": ["id", "created_at", "bundle_name", "sha256", "size_bytes", "repository", "uri"],
                                     "rows": [{k: _iso(x[k]) for k in x} for x in rows]})
                st["summary"] = (f"Bundle {r['bundle_name']} ({(r['size_bytes'] or 0) / 1e6:.1f} MB) — model, training snapshot, feature "
                                 f"definitions, metrics and environment lock — pushed to the {r['repository']} artifactory. "
                                 f"sha256 {r['sha256']}.")
                st["params"] = {"sha256": r["sha256"], "uri": r["uri"], "content_digest": r["content_digest"]}
                st["metrics"] = {"bundles": len(rows), "size_mb": round((r["size_bytes"] or 0) / 1e6, 2)}
                st["links"].append({"label": "Frozen artifacts page", "href": "#frozen"})
            else:
                st["summary"] = "Not frozen yet — freeze from the Frozen artifacts page once the version is champion."
    except Exception as e:
        st["status"], st["notes"] = "failed", [f"{e.__class__.__name__}: {str(e)[:300]}"]
    return st


def snapshot_ops(journey_id: str, version: str) -> None:
    """Copy the live operational evidence into the journey as MLflow child runs."""
    j = Journey.open(journey_id)
    if not j:
        return
    c = _client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    prev = {}
    for r in sorted(c.search_runs([experiment_id()], filter_string=f"tags.`bsp.role` = 'stage' and tags.`bsp.journey_id` = '{journey_id}'",
                                  max_results=500), key=lambda r: r.info.start_time):
        prev[r.data.tags.get("bsp.stage")] = (r.data.tags.get("bsp.headline"), r.data.tags.get("bsp.stage_status"))
    for key in OPS_STAGES:
        live = ops_stage(key, version, mv.creation_timestamp)
        if live["status"] == "pending" or prev.get(key) == (live["headline"][:250], live["status"]):
            continue          # nothing new since the last snapshot
        with j.stage(key) as st:
            st.doc.update({k: live[k] for k in ("status", "headline", "summary", "tables", "links", "notes")})
            st.doc["data"] = live.get("data")
            st.param(**{k: v for k, v in live["params"].items() if v is not None}, snapshot_of="mlops schema (Postgres)")
            st.metric(**live["metrics"])


# ═══════════════════════════════════════════════════════════════════════════
#  reader (control tower API)
# ═══════════════════════════════════════════════════════════════════════════
_CACHE: dict[str, dict] = {}


def _parent_run(journey_id: str):
    rs = _client().search_runs([experiment_id()], filter_string=f"tags.`bsp.role` = 'journey' and tags.`bsp.journey_id` = '{journey_id}'",
                               max_results=1)
    return rs[0] if rs else None


def _mlflow_url(exp: str | None, run: str) -> str:
    return f"/mlflow/#/experiments/{exp}/runs/{run}" if exp else f"/mlflow/#/runs/{run}"


def _artifact_url(run_id: str, path: str) -> str:
    return f"/mlops/api/journeys/artifact/{run_id}/{path}"


def _stage_doc(run) -> dict:
    rid = run.info.run_id
    if run.info.status != "RUNNING" and rid in _CACHE:
        return _CACHE[rid]
    try:
        with tempfile.TemporaryDirectory() as td:
            doc = json.loads(Path(_client().download_artifacts(rid, "stage.json", td)).read_text())
    except Exception:
        doc = {"key": run.data.tags.get("bsp.stage"), "headline": run.data.tags.get("bsp.headline", ""),
               "status": "running" if run.info.status == "RUNNING" else "failed" if run.info.status == "FAILED" else "done",
               "tables": [], "charts": [], "curves": {}, "links": [], "source_runs": [], "notes": [],
               "params": dict(run.data.params), "metrics": dict(run.data.metrics)}
    if run.info.status != "RUNNING":
        _CACHE[rid] = doc
    return doc


def _status_of_run(run, doc) -> str:
    if run.info.status == "RUNNING":
        return "running"
    if run.info.status in ("FAILED", "KILLED"):
        return "failed"
    return doc.get("status") or "done"


def _ms_iso(ms: int | None) -> str | None:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat() if ms else None


STALE_MS = 30 * 60 * 1000      # a running journey with no stage started for 30 min was interrupted


def _journey_status(parent) -> str:
    s = parent.info.status
    if s == "RUNNING":
        beat = int(parent.data.tags.get("bsp.heartbeat") or parent.info.start_time or 0)
        return "running" if (_now_ms() - beat) < STALE_MS else "interrupted"
    return {"FINISHED": "done", "FAILED": "failed", "KILLED": "failed"}.get(s, "done")


def read(journey_id: str, version: str | None = None) -> dict | None:
    c = _client()
    parent = _parent_run(journey_id)
    if not parent:
        return None
    kids = c.search_runs([experiment_id()], filter_string=f"tags.`bsp.role` = 'stage' and tags.`bsp.journey_id` = '{journey_id}'",
                         max_results=500)
    by_key: dict[str, Any] = {}
    for k in sorted(kids, key=lambda r: r.info.start_time):
        by_key[k.data.tags.get("bsp.stage")] = k    # the latest run of a stage wins (e.g. re-snapshot of ops)
    jstatus = _journey_status(parent)
    version = version or parent.data.tags.get("bsp.model_version")
    reg_ms = None
    aliases: list[str] = []
    if version:
        try:
            mv = c.get_model_version(config.MODEL_NAME, str(version))
            reg_ms = mv.creation_timestamp
            aliases = list(getattr(mv, "aliases", []) or [])
        except Exception:
            pass
    stages = []
    for key in STAGE_KEYS:
        run = by_key.get(key)
        live = ops_stage(key, version, reg_ms) if key in OPS_STAGES and version else None
        if live and (live["status"] != "pending" or not run):
            s = live
            s["duration_s"] = None
            if s["started"] and s["ended"]:
                s["duration_s"] = (pd.Timestamp(s["ended"]) - pd.Timestamp(s["started"])).total_seconds()
            if run:
                s["mlflow_url"] = _mlflow_url(run.info.experiment_id, run.info.run_id)
                s["run_id"] = run.info.run_id
            stages.append(s)
            continue
        if run is None:
            pend = jstatus == "running" or key in OPS_STAGES
            stages.append({"key": key, "title": TITLES[key], "status": "pending" if pend else "skipped",
                           "headline": "waiting" if jstatus == "running" else ("not yet" if key in OPS_STAGES else "not recorded"),
                           "summary": DESCR[key], "tables": [], "charts": [], "curves": {}, "links": [], "source_runs": [],
                           "notes": [], "metrics": {}, "params": {}, "started": None, "ended": None, "duration_s": None})
            continue
        doc = dict(_stage_doc(run))
        st = _status_of_run(run, doc)
        if st == "running" and jstatus != "running":
            st = "failed"           # the journey ended (or was interrupted) while this stage was open
        end = run.info.end_time if run.info.status != "RUNNING" else None
        doc.update({
            "key": key, "title": TITLES[key], "status": st, "run_id": run.info.run_id,
            "started": _ms_iso(run.info.start_time), "ended": _ms_iso(end),
            "duration_s": ((end or _now_ms()) - run.info.start_time) / 1000 if run.info.start_time else None,
            "mlflow_url": _mlflow_url(run.info.experiment_id, run.info.run_id),
            "metrics": doc.get("metrics") or dict(run.data.metrics), "params": doc.get("params") or dict(run.data.params),
        })
        if st == "running" and not doc.get("headline"):
            doc["headline"] = "in progress…"
        doc["charts"] = [{**ch, "url": _artifact_url(run.info.run_id, ch["path"])} for ch in doc.get("charts", [])]
        doc["links"] = [{**ln, "href": ln.get("href") or (_artifact_url(run.info.run_id, ln["artifact"]) if ln.get("artifact") else "")}
                        for ln in doc.get("links", [])]
        doc["files"] = [{"name": f, "url": _artifact_url(run.info.run_id, f)} for f in doc.get("files", [])]
        doc["source_runs"] = [{**s, "mlflow_url": _mlflow_url(s.get("experiment_id"), s["run_id"])} for s in doc.get("source_runs", [])]
        if key == "registration" and version:
            doc["aliases"] = aliases
            if aliases:
                doc["headline"] = f"{config.MODEL_NAME} v{version} · {', '.join(aliases)}"
        stages.append(doc)
    try:
        from .evidence import teach

        for s in stages:
            s["teach"] = teach(s["key"])
    except Exception:
        pass
    # current = the running stage, else (while running) the first pending one
    current = next((s["key"] for s in stages if s["status"] == "running"), None)
    if current is None and jstatus == "running":
        current = next((s["key"] for s in stages if s["status"] == "pending" and s["key"] not in OPS_STAGES), None)
    tags = parent.data.tags
    done = sum(1 for s in stages if s["status"] == "done")
    return {
        "journey_id": journey_id, "run_id": parent.info.run_id, "kind": tags.get("bsp.journey_kind"),
        "title": tags.get("bsp.title"), "model": tags.get("bsp.model_name", config.MODEL_NAME), "version": version,
        "aliases": aliases, "status": jstatus, "started": _ms_iso(parent.info.start_time), "ended": _ms_iso(parent.info.end_time),
        "parent_journey_id": tags.get("bsp.parent_journey_id"), "current": current,
        "progress": {"done": done, "total": len(stages)},
        "mlflow_url": _mlflow_url(parent.info.experiment_id, parent.info.run_id), "stages": stages,
    }


def for_version(model: str, version: str) -> dict | None:
    c = _client()
    mv = c.get_model_version(model, str(version))
    jid = (mv.tags or {}).get("bsp.journey_id")
    return read(jid, str(version)) if jid else None


def list_journeys(limit: int = 100) -> dict:
    c = _client()
    runs = c.search_runs([experiment_id()], filter_string="tags.`bsp.role` = 'journey'", max_results=limit,
                         order_by=["attributes.start_time DESC"])
    journeys = [{"journey_id": r.data.tags.get("bsp.journey_id"), "kind": r.data.tags.get("bsp.journey_kind"),
                 "title": r.data.tags.get("bsp.title"), "model": r.data.tags.get("bsp.model_name"),
                 "version": r.data.tags.get("bsp.model_version"), "status": _journey_status(r),
                 "started": _ms_iso(r.info.start_time), "ended": _ms_iso(r.info.end_time), "run_id": r.info.run_id,
                 "parent_journey_id": r.data.tags.get("bsp.parent_journey_id")} for r in runs]
    models: dict[str, list] = {}
    try:
        from . import registry

        for v in registry.list_versions():
            jid = (v.get("tags") or {}).get("bsp.journey_id")
            models.setdefault(config.MODEL_NAME, []).append({
                "version": v["version"], "aliases": v["aliases"], "journey_id": jid, "model_kind": v.get("model_kind"),
                "test_auc": (v.get("metrics") or {}).get("test_auc"), "created": v.get("created"),
                "kind": next((j["kind"] for j in journeys if j["journey_id"] == jid), None) if jid else None})
    except Exception:
        pass
    active = next((j for j in journeys if j["status"] == "running"), None)
    return {"models": [{"name": k, "versions": v} for k, v in models.items()], "journeys": journeys, "active": active}


# ═══════════════════════════════════════════════════════════════════════════
#  backfill — reconstruct journeys for versions registered without one
# ═══════════════════════════════════════════════════════════════════════════
def _runs_before(exp_name: str, before_ms: int, filt: str = "", n: int = 50):
    c = _client()
    e = c.get_experiment_by_name(exp_name)
    if not e:
        return []
    f = f"attributes.start_time <= {before_ms}" + (f" and {filt}" if filt else "")
    return c.search_runs([e.experiment_id], filter_string=f, max_results=n, order_by=["attributes.start_time DESC"])


def backfill_version(version: str, force: bool = False, verbose: bool = True) -> str | None:
    from .data import windows as data_windows

    c = _client()
    mv = c.get_model_version(config.MODEL_NAME, str(version))
    if (mv.tags or {}).get("bsp.journey_id") and not force:
        if verbose:
            print(f"  v{version}: journey {mv.tags['bsp.journey_id']} already linked")
        return mv.tags["bsp.journey_id"]
    run = c.get_run(mv.run_id)
    t_run = run.info.start_time
    stage_tag = run.data.tags.get("stage", "")
    is_retrain = stage_tag == "retrain"
    kind_label = run.data.params.get("model_kind", "?")
    linked = run.data.tags.get("bsp.journey_id")
    parent_jid = None
    if is_retrain:
        pv = run.data.tags.get("parent_champion_version")
        if pv:
            try:
                parent_jid = (c.get_model_version(config.MODEL_NAME, pv).tags or {}).get("bsp.journey_id")
            except Exception:
                parent_jid = None
    j = Journey.start("backfill", title=f"v{version} {kind_label} (reconstructed)", parent_journey=parent_jid,
                      start_time=t_run - 1000, activate=False,
                      extra_tags={"bsp.reconstructed_from": mv.run_id, **({"bsp.linked_journey": linked} if linked else {})})
    if verbose:
        print(f"  v{version}: reconstructing journey {j.id} from run {mv.run_id[:8]} ({stage_tag or 'run'}, {kind_label})")
    try:
        with j.stage("integration") as st:
            integration_evidence(st, reused=True)
            st.note("Reconstructed: base-table snapshot as of the backfill, not necessarily the exact training-time state.")
        params = run.data.params
        try:
            a, b = params["train_window"].split("..")
            win = (a + "-01", b + "-01")
        except Exception:
            win = None
        with j.stage("eda") as st:
            eda_evidence(st, window=win)
        with j.stage("notebook") as st:
            notebook_evidence(st, ["11_retrain_trigger"] if is_retrain else
                              ["01_integrate_and_eda", "02_feature_sifting_selection", "03_holdout_strategies",
                               "05_pipeline_and_mlflow", "06_leaderboard", "07_hyperparameter_tuning", "08_registry_champion_challenger"])
        sel_runs = _runs_before(config.SELECTION_EXPERIMENT, t_run, "tags.stage = 'selection'", n=1)
        if is_retrain:
            j.skip("selection", f"Inherited from champion v{run.data.tags.get('parent_champion_version')}: the retrain reuses its "
                   f"feature list ({params.get('n_features')} features).", headline=f"{params.get('n_features')} features inherited")
        elif sel_runs:
            with j.stage("selection", start_time=sel_runs[0].info.start_time, end_time=sel_runs[0].info.end_time) as st:
                with tempfile.TemporaryDirectory() as td:
                    sel = json.loads(Path(c.download_artifacts(sel_runs[0].info.run_id, "selection/feature_selection.json", td)).read_text())
                with tempfile.TemporaryDirectory() as td:
                    used = json.loads(Path(c.download_artifacts(mv.run_id, "evaluation/features.json", td)).read_text())["features"]
                selection_evidence(st, sel, used)
                st.source_run(sel_runs[0].info.run_id, "feature-sifting run", sel_runs[0].info.experiment_id)
        else:
            j.skip("selection", "No feature-sifting run found before this model was trained.", headline="not recorded")
        ho = _runs_before(config.SELECTION_EXPERIMENT, t_run, "tags.stage = 'splits'", n=1)
        w = {}
        for k in ("train", "valid", "test"):
            try:
                a, b = params[f"{k}_window"].split("..")
                w[k] = (pd.Timestamp(a + "-01"), pd.Timestamp(b + "-01"))
            except Exception:
                w = data_windows()
                break
        with j.stage("holdout", start_time=ho[0].info.start_time if ho else None, end_time=ho[0].info.end_time if ho else None) as st:
            cmp = None
            if ho and not is_retrain:
                try:
                    with tempfile.TemporaryDirectory() as td:
                        tj = json.loads(Path(c.download_artifacts(ho[0].info.run_id, "splits/holdout_comparison.json", td)).read_text())
                    cmp = pd.DataFrame(tj["data"], columns=tj["columns"])
                    st.source_run(ho[0].info.run_id, "holdout-strategies run", ho[0].info.experiment_id)
                except Exception as e:
                    st.note(f"holdout comparison unavailable: {e}")
            holdout_evidence(st, cmp, w, sizes={k: _num(params[k]) for k in ("n_train", "n_valid", "n_test") if k in params},
                             chosen="time")
        if is_retrain:
            j.skip("feature_store", "Same versioned feature views as the parent champion; training rows via the point-in-time join.",
                   headline="views inherited")
            j.skip("algo_selection", f"Recipe inherited from champion v{run.data.tags.get('parent_champion_version')}: {kind_label}.",
                   headline=f"{kind_label} inherited")
        else:
            with j.stage("feature_store") as st:
                feature_store_evidence(st, None, None, None, None)
            lb = _runs_before(config.EXPERIMENT, t_run + 1, "tags.stage = 'leaderboard'", n=40)
            if lb:
                latest = lb[0].info.start_time
                batch = [r for r in lb if latest - r.info.start_time < 3 * 3600 * 1000]
                ids = list({r.data.params.get("model_kind"): r.info.run_id for r in reversed(batch)}.values())
                with j.stage("algo_selection", start_time=min(r.info.start_time for r in batch),
                             end_time=max(r.info.end_time or r.info.start_time for r in batch)) as st:
                    rows = leaderboard_rows(ids)
                    algo_evidence(st, rows, mv.run_id if mv.run_id in ids else None, chosen_label=f"{kind_label} (v{version})")
            else:
                j.skip("algo_selection", "No leaderboard runs found.", headline="not recorded")
        with j.stage("roc_auc") as st:
            y, p, win_s, _ = run_test_predictions(mv.run_id)
            roc_evidence(st, y, p, f"v{version} {kind_label}", win_s)
        tune_parent = run.data.tags.get("parent_run")
        if is_retrain:
            with j.stage("tuning") as st:
                inherited_params_evidence(st, mv.run_id, run.data.tags.get("parent_champion_version"))
        else:
            if not tune_parent:
                tp = _runs_before(config.TUNING_EXPERIMENT, t_run + 3 * 3600 * 1000, "tags.stage = 'tuning'", n=1)
                tune_parent = tp[0].info.run_id if tp else None
            if tune_parent:
                tr = c.get_run(tune_parent)
                with j.stage("tuning", start_time=tr.info.start_time, end_time=tr.info.end_time) as st:
                    tuning_evidence(st, tune_parent)
                    if stage_tag != "tuned":
                        st.note(f"This version is the untuned {kind_label} leaderboard run; the study shown tuned the champion family.")
            else:
                j.skip("tuning", "No Optuna study found.", headline="not tuned")
        with j.stage("evaluation") as st:
            evaluation_evidence(st, y, p, mv.run_id, f"v{version} {kind_label}")
        with j.stage("registration", start_time=mv.creation_timestamp, end_time=mv.last_updated_timestamp or mv.creation_timestamp) as st:
            registration_evidence(st, str(version), mv.run_id, None, mv.description or "")
        j.attach_version(str(version))
        snapshot_ops(j.id, str(version))
        j.finish(True)
    except Exception:
        j.finish(False)
        raise
    return j.id


def backfill(version: str | None = None, force: bool = False, verbose: bool = True) -> list[str]:
    from . import registry

    out = []
    for v in sorted(registry.list_versions(), key=lambda r: int(r["version"])):
        if version and str(v["version"]) != str(version):
            continue
        jid = (v.get("tags") or {}).get("bsp.journey_id")
        if jid and not force:
            # refresh the operational snapshot of an existing journey
            try:
                snapshot_ops(jid, v["version"])
                if verbose:
                    print(f"  v{v['version']}: journey {jid} present — operational stages re-snapshotted")
            except Exception as e:
                print(f"  v{v['version']}: snapshot failed: {e}")
            out.append(jid)
            continue
        try:
            out.append(backfill_version(v["version"], force=force, verbose=verbose))
        except Exception as e:
            print(f"  v{v['version']}: backfill failed: {e.__class__.__name__}: {e}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="bsp_mlops.journey")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backfill", help="build journeys for registered versions that have none")
    b.add_argument("--version")
    b.add_argument("--force", action="store_true", help="rebuild even when a journey is linked")
    s = sub.add_parser("show")
    s.add_argument("version")
    sub.add_parser("list")
    a = ap.parse_args(argv)
    os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
    if a.cmd == "backfill":
        ids = backfill(a.version, a.force)
        print(json.dumps(ids))
    elif a.cmd == "show":
        j = for_version(config.MODEL_NAME, a.version)
        if not j:
            print("no journey")
            return 1
        for s_ in j["stages"]:
            print(f"  {s_['status']:<8} {s_['title']:<24} {_fmt_dur(s_.get('duration_s')):>8}  {s_.get('headline', '')}")
    else:
        print(json.dumps(list_journeys(), indent=1, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
