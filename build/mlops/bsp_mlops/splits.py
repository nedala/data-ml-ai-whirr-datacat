"""Train / validation / test hold-out strategies and an honest comparison.

  random      rows shuffled — the same account lands in train and test (leaky
              for panel data: one customer appears in many monthly snapshots)
  stratified  random, but churn prevalence identical in every split
  time        train on the past, validate / test on later months (how the
              model is actually used — the production default here)
  group       GroupShuffleSplit by ban — no customer in two splits

`compare()` trains the same model under each strategy and scores it on its
own test split AND on a truly future, untouched out-of-time window. The gap
between the two ("optimism") shows which hold-outs flatter the model."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import ENTITY, LABEL, TS

STRATEGIES = ("random", "stratified", "time", "group")


def split(df: pd.DataFrame, strategy: str = "time", valid_size: float = 0.15, test_size: float = 0.15,
          seed: int = 7) -> dict[str, pd.DataFrame]:
    n = len(df)
    if strategy == "random":
        idx = np.random.default_rng(seed).permutation(n)
        nt, nv = int(n * test_size), int(n * valid_size)
        parts = {"test": idx[:nt], "valid": idx[nt:nt + nv], "train": idx[nt + nv:]}
    elif strategy == "stratified":
        from sklearn.model_selection import train_test_split

        all_idx = np.arange(n)
        y = df[LABEL].to_numpy()
        rest, test = train_test_split(all_idx, test_size=test_size, stratify=y, random_state=seed)
        train, valid = train_test_split(rest, test_size=valid_size / (1 - test_size), stratify=y[rest], random_state=seed)
        parts = {"train": train, "valid": valid, "test": test}
    elif strategy == "time":
        ms = np.sort(df[TS].unique())
        k_test = max(1, int(round(len(ms) * test_size)))
        k_val = max(1, int(round(len(ms) * valid_size)))
        test_m, val_m = ms[-k_test:], ms[-(k_test + k_val):-k_test]
        t = df[TS].to_numpy()
        parts = {"train": np.flatnonzero(t < val_m[0]), "valid": np.flatnonzero(np.isin(t, val_m)),
                 "test": np.flatnonzero(np.isin(t, test_m))}
    elif strategy == "group":
        from sklearn.model_selection import GroupShuffleSplit

        g = df[ENTITY].to_numpy()
        all_idx = np.arange(n)
        rest, test = next(GroupShuffleSplit(1, test_size=test_size, random_state=seed).split(all_idx, groups=g))
        tr, va = next(GroupShuffleSplit(1, test_size=valid_size / (1 - test_size), random_state=seed)
                      .split(rest, groups=g[rest]))
        parts = {"train": rest[tr], "valid": rest[va], "test": test}
    else:
        raise ValueError(f"unknown strategy {strategy!r}; choose from {STRATEGIES}")
    return {k: df.iloc[v] for k, v in parts.items()}


def describe(parts: dict[str, pd.DataFrame]) -> dict:
    tr, te = parts["train"], parts["test"]
    return {
        "n_train": len(tr), "n_valid": len(parts["valid"]), "n_test": len(te),
        "churn_train": float(tr[LABEL].mean()), "churn_valid": float(parts["valid"][LABEL].mean()),
        "churn_test": float(te[LABEL].mean()),
        "accounts_in_train_and_test": int(len(set(tr[ENTITY]) & set(te[ENTITY]))),
        "test_months_seen_in_train": int(len(set(tr[TS]) & set(te[TS]))),
        "train_months": f"{tr[TS].min():%Y-%m} → {tr[TS].max():%Y-%m}",
        "test_months": f"{te[TS].min():%Y-%m} → {te[TS].max():%Y-%m}",
    }


def compare(df: pd.DataFrame, features: list[str], future: pd.DataFrame, model: str = "lightgbm",
            seed: int = 7, verbose: bool = True) -> pd.DataFrame:
    """Same model, four hold-outs; score each on its test split and on `future`."""
    from sklearn.metrics import roc_auc_score

    from .pipeline import build_pipeline

    rows = []
    for s in STRATEGIES:
        parts = split(df, s, seed=seed)
        pipe = build_pipeline(model, features, params={"n_estimators": 250, "learning_rate": 0.06})
        pipe.fit(parts["train"][features], parts["train"][LABEL].astype(int))
        r = {"strategy": s, **describe(parts)}
        r["valid_auc"] = roc_auc_score(parts["valid"][LABEL], pipe.predict_proba(parts["valid"][features])[:, 1])
        r["test_auc"] = roc_auc_score(parts["test"][LABEL], pipe.predict_proba(parts["test"][features])[:, 1])
        r["future_auc"] = roc_auc_score(future[LABEL], pipe.predict_proba(future[features])[:, 1])
        r["optimism"] = r["test_auc"] - r["future_auc"]
        rows.append(r)
        if verbose:
            print(f"  · {s:<10} test AUC {r['test_auc']:.4f}  future AUC {r['future_auc']:.4f}  "
                  f"optimism {r['optimism']:+.4f}  shared accounts {r['accounts_in_train_and_test']:,}")
    return pd.DataFrame(rows).set_index("strategy")
