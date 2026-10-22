"""Feature sifting and selection.

A staged funnel over the candidate set (data.CANDIDATES) on the training
window only (never the hold-out):

  1. missingness filter          drop > max_missing share missing
  2. variance filter             drop near-constant columns
  3. correlation / VIF pruning   drop one of each |r| > max_corr pair (keep the
                                 one with higher MI), then iterate VIF > max_vif
  4. mutual information          univariate relevance (numeric + encoded cats)
  5. permutation importance      model-based, on a validation slice
  6. SHAP global importance      mean |SHAP| from a gradient-boosted model
  7. stability selection         L1-logistic over bootstrap halves; selection
                                 frequency per feature
  + random probe                 any feature ranking below `probe_random` in
                                 permutation importance AND MI is noise

Output: a `SelectionReport` (tables + the kept list) that can be written as
an HTML/JSON artifact and logged to MLflow."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from .config import LABEL
from .data import CATEGORICAL


@dataclass
class SelectionReport:
    candidates: list[str]
    kept: list[str]
    dropped: dict[str, str]                       # feature -> reason
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)
    seconds: float = 0.0

    def summary(self) -> pd.DataFrame:
        rows = []
        for f in self.candidates:
            rows.append({"feature": f, "decision": "keep" if f in self.kept else "drop",
                         "reason": self.dropped.get(f, "")})
        out = pd.DataFrame(rows).set_index("feature")
        for name in ("mutual_info", "permutation", "shap", "stability"):
            t = self.tables.get(name)
            if t is not None and len(t):
                out = out.join(t.iloc[:, :1].rename(columns={t.columns[0]: name}), how="left")
        return out

    def to_dict(self) -> dict:
        return {"candidates": self.candidates, "kept": self.kept, "dropped": self.dropped,
                "seconds": self.seconds,
                "tables": {k: json.loads(v.reset_index().to_json(orient="records")) for k, v in self.tables.items()}}

    def write(self, out_dir: str | Path) -> dict[str, Path]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        js = out / "feature_selection.json"
        js.write_text(json.dumps(self.to_dict(), indent=2, default=str))
        html = out / "feature_selection.html"
        parts = ["<html><head><meta charset='utf-8'><title>Feature selection report</title>"
                 "<style>body{font:13px system-ui;margin:24px}table{border-collapse:collapse;margin:8px 0 24px}"
                 "td,th{border:1px solid #ddd;padding:3px 8px;text-align:right}th{background:#f4f4f4}"
                 "td:first-child,th:first-child{text-align:left}</style></head><body>",
                 "<h1>Churn feature selection</h1>",
                 f"<p>{len(self.kept)} kept of {len(self.candidates)} candidates in {self.seconds:.0f}s.</p>",
                 "<h2>Decision</h2>", self.summary().to_html(float_format=lambda x: f"{x:.4f}")]
        for k, v in self.tables.items():
            parts += [f"<h2>{k}</h2>", v.to_html(float_format=lambda x: f"{x:.4f}")]
        parts.append("</body></html>")
        html.write_text("".join(parts))
        return {"json": js, "html": html}


def _encode(X: pd.DataFrame) -> tuple[pd.DataFrame, list[bool]]:
    """Ordinal-encode categoricals for MI / VIF; returns (X_num, discrete mask)."""
    Xn = X.copy()
    discrete = []
    for c in Xn.columns:
        if c in CATEGORICAL or Xn[c].dtype == object:
            Xn[c] = Xn[c].astype("category").cat.codes.astype(float)
            discrete.append(True)
        else:
            Xn[c] = Xn[c].astype(float).fillna(Xn[c].median())
            discrete.append(bool(Xn[c].nunique() <= 2))
    return Xn, discrete


def _vif(Xn: pd.DataFrame) -> pd.Series:
    from statsmodels.stats.outliers_influence import variance_inflation_factor

    Z = (Xn - Xn.mean()) / Xn.std().replace(0, 1)
    Z = Z.assign(_const=1.0)
    vals = {c: float(variance_inflation_factor(Z.values, i)) for i, c in enumerate(Z.columns) if c != "_const"}
    return pd.Series(vals).sort_values(ascending=False)


def sift(df: pd.DataFrame, candidates: list[str], *, max_missing: float = 0.5, min_variance_share: float = 0.995,
         max_corr: float = 0.95, max_vif: float = 10.0, sample: int = 30000, seed: int = 7,
         stability_rounds: int = 20, verbose: bool = True) -> SelectionReport:
    t0 = time.time()
    rng = np.random.default_rng(seed)
    if len(df) > sample:
        df = df.sample(sample, random_state=seed)
    y = df[LABEL].to_numpy().astype(int)
    dropped: dict[str, str] = {}
    tables: dict[str, pd.DataFrame] = {}
    say = print if verbose else (lambda *a, **k: None)

    # 1. missingness
    miss = df[candidates].isna().mean().rename("missing_share").to_frame()
    tables["missingness"] = miss.sort_values("missing_share", ascending=False)
    for f, m in miss.missing_share.items():
        if m > max_missing:
            dropped[f] = f"missingness {m:.0%} > {max_missing:.0%}"
    # 2. variance (dominant value share)
    dom = {f: float(df[f].value_counts(normalize=True, dropna=False).iloc[0]) for f in candidates}
    tables["dominant_value_share"] = pd.Series(dom, name="dominant_share").to_frame().sort_values("dominant_share", ascending=False)
    for f, d in dom.items():
        if f not in dropped and d >= min_variance_share:
            dropped[f] = f"near-constant ({d:.2%} one value)"
    alive = [f for f in candidates if f not in dropped]
    say(f"  · missingness/variance: {len(candidates)} → {len(alive)}")

    # 4 (computed early — used to break correlation ties). mutual information
    from sklearn.feature_selection import mutual_info_classif

    Xn, discrete = _encode(df[alive])
    mi = mutual_info_classif(Xn.values, y, discrete_features=discrete, random_state=seed)
    mi_s = pd.Series(mi, index=alive, name="mutual_info").sort_values(ascending=False)
    tables["mutual_info"] = mi_s.to_frame()

    # 3. correlation pruning (numeric) then VIF
    num = [f for f in alive if f not in CATEGORICAL]
    corr = Xn[num].corr().abs()
    pairs = []
    for i, a in enumerate(num):
        for b in num[i + 1:]:
            if corr.loc[a, b] > max_corr:
                pairs.append((a, b, float(corr.loc[a, b])))
    tables["high_correlation_pairs"] = pd.DataFrame(pairs, columns=["a", "b", "abs_corr"]).set_index("a") if pairs else pd.DataFrame(columns=["abs_corr"])
    for a, b, r in sorted(pairs, key=lambda p: -p[2]):
        if a in dropped or b in dropped:
            continue
        # keep the higher-MI member; near-ties go to the earlier (core) candidate
        loser = b if mi_s.get(a, 0) >= mi_s.get(b, 0) - 2e-3 else a
        dropped[loser] = f"|r|={r:.3f} with {a if loser == b else b}"
    num = [f for f in num if f not in dropped]
    vif_hist = []
    while len(num) > 2:
        v = _vif(Xn[num])
        vif_hist.append(v.rename(f"round_{len(vif_hist) + 1}"))
        if v.iloc[0] <= max_vif:
            break
        dropped[v.index[0]] = f"VIF {v.iloc[0]:.1f} > {max_vif}"
        num.remove(v.index[0])
    if vif_hist:
        tables["vif"] = vif_hist[-1].to_frame("vif")
    alive = [f for f in alive if f not in dropped]
    say(f"  · correlation/VIF: → {len(alive)}")

    # 5. permutation importance + 6. SHAP from one gradient-boosted model
    from sklearn.inspection import permutation_importance
    from sklearn.metrics import roc_auc_score

    from .pipeline import build_pipeline

    cut = int(len(df) * 0.75)
    idx = rng.permutation(len(df))
    tr, va = df.iloc[idx[:cut]], df.iloc[idx[cut:]]
    pipe = build_pipeline("lightgbm", alive, params={"n_estimators": 200, "learning_rate": 0.08, "num_leaves": 31})
    pipe.fit(tr[alive], tr[LABEL].astype(int))
    auc = roc_auc_score(va[LABEL], pipe.predict_proba(va[alive])[:, 1])
    pi = permutation_importance(pipe, va[alive], va[LABEL].astype(int), scoring="roc_auc", n_repeats=4,
                                random_state=seed, n_jobs=1)
    perm = pd.DataFrame({"perm_auc_drop": pi.importances_mean, "perm_std": pi.importances_std},
                        index=alive).sort_values("perm_auc_drop", ascending=False)
    tables["permutation"] = perm
    try:
        tables["shap"] = shap_importance(pipe, va[alive].head(1500))
    except Exception as e:  # SHAP is best-effort (version drift); never blocks selection
        say(f"  · SHAP skipped: {e}")
    say(f"  · model-based (LightGBM, val AUC {auc:.3f}): permutation + SHAP done")

    # 7. stability selection (L1 logistic on bootstrap halves)
    from sklearn.linear_model import LogisticRegression

    from .pipeline import build_preprocessor

    pre = build_preprocessor(alive)
    Xs = pre.fit_transform(df[alive])
    names = list(pre.get_feature_names_out())
    owner = [_owner(n, alive) for n in names]
    freq = pd.Series(0.0, index=alive)
    for r in range(stability_rounds):
        sub = rng.choice(len(df), size=len(df) // 2, replace=False)
        lr = LogisticRegression(penalty="l1", C=0.02, solver="liblinear", max_iter=300)
        lr.fit(Xs[sub], y[sub])
        hit = {owner[i] for i, w in enumerate(lr.coef_[0]) if abs(w) > 1e-6}
        for f in hit & set(freq.index):
            freq[f] += 1
    freq = (freq / stability_rounds).sort_values(ascending=False)
    tables["stability"] = freq.rename("selection_frequency").to_frame()

    # random-probe rule: below the probe on permutation AND MI AND unstable → noise
    if "probe_random" in alive:
        p_perm = perm.loc["probe_random", "perm_auc_drop"]
        p_mi = mi_s.get("probe_random", 0)
        for f in alive:
            if f == "probe_random":
                continue
            if perm.loc[f, "perm_auc_drop"] <= p_perm and mi_s.get(f, 0) <= p_mi * 1.5 and freq.get(f, 0) < 0.5:
                dropped[f] = "indistinguishable from the random probe"
        dropped["probe_random"] = "random probe (reference only)"
    kept = [f for f in candidates if f not in dropped]
    say(f"  · stability + probe rule: kept {len(kept)} of {len(candidates)}")
    return SelectionReport(candidates=list(candidates), kept=kept, dropped=dropped, tables=tables,
                           seconds=time.time() - t0)


def _owner(encoded_name: str, features: list[str]) -> str:
    base = encoded_name.split("__", 1)[-1]
    if base.startswith("missingindicator_"):          # SimpleImputer(add_indicator=True) → owned by its feature
        base = base[len("missingindicator_"):]
    for f in sorted(features, key=len, reverse=True):
        if base == f or base.startswith(f + "_"):
            return f
    return base


def shap_importance(pipe, X: pd.DataFrame) -> pd.DataFrame:
    """Mean |SHAP| aggregated back to original features (one-hots summed)."""
    import shap

    pre, model = pipe.named_steps["prep"], pipe.named_steps["model"]
    Xt = pre.transform(X)
    if hasattr(Xt, "toarray"):
        Xt = Xt.toarray()
    names = list(pre.get_feature_names_out())
    try:
        explainer = shap.TreeExplainer(model)
        sv = explainer.shap_values(Xt)
    except Exception:
        bg = shap.sample(Xt, 100, random_state=0)
        explainer = shap.Explainer(lambda a: model.predict_proba(a)[:, 1], bg)
        sv = explainer(Xt[:300]).values
    if isinstance(sv, list):
        sv = sv[1]
    sv = np.asarray(sv)
    if sv.ndim == 3:
        sv = sv[..., 1]
    feats = list(X.columns)
    agg: dict[str, float] = {}
    for i, n in enumerate(names):
        o = _owner(n, feats)
        agg[o] = agg.get(o, 0.0) + float(np.abs(sv[:, i]).mean())
    return pd.Series(agg, name="mean_abs_shap").sort_values(ascending=False).to_frame()
