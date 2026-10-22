"""Exploratory data analysis helpers (tables first; plots are optional)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import LABEL, TS
from .data import BOOLEAN, CATEGORICAL, NUMERIC


def profile(df: pd.DataFrame, columns: list[str] | None = None) -> pd.DataFrame:
    """One row per column: type, missing %, distinct, basic stats, churn correlation."""
    cols = columns or [c for c in df.columns if c not in (LABEL, "label_complete")]
    y = df[LABEL] if LABEL in df else None
    rows = []
    for c in cols:
        s = df[c]
        num = pd.api.types.is_numeric_dtype(s)
        r = {"feature": c, "dtype": "numeric" if num else "categorical", "missing_pct": round(100 * s.isna().mean(), 2),
             "distinct": int(s.nunique(dropna=True))}
        if num:
            r.update(mean=float(s.mean()), std=float(s.std()), min=float(s.min()), p50=float(s.median()), max=float(s.max()))
            if y is not None and s.std() > 0:
                r["corr_with_churn"] = float(np.corrcoef(s.fillna(s.median()), y)[0, 1])
        else:
            r["top"] = str(s.mode().iat[0]) if s.notna().any() else None
        rows.append(r)
    return pd.DataFrame(rows).set_index("feature")


def churn_by(df: pd.DataFrame, col: str, bins: int | None = None) -> pd.DataFrame:
    s = pd.qcut(df[col], bins, duplicates="drop") if bins else df[col]
    g = df.groupby(s, observed=True)[LABEL].agg(["count", "mean"]).rename(columns={"mean": "churn_rate"})
    g["lift"] = g.churn_rate / df[LABEL].mean()
    return g


def shift_preview(df: pd.DataFrame, shift_start) -> pd.DataFrame:
    """Mean of each feature before vs during the shift window (the story the
    drift monitor will later find on live traffic)."""
    pre = df[df[TS] < pd.Timestamp(shift_start)]
    post = df[df[TS] >= pd.Timestamp(shift_start)]
    rows = []
    for c in NUMERIC + BOOLEAN:
        a, b = pre[c].mean(), post[c].mean()
        rows.append({"feature": c, "before": a, "shift_window": b, "change_pct": 100 * (b - a) / a if a else np.nan})
    return pd.DataFrame(rows).set_index("feature").sort_values("change_pct", key=lambda s: -s.abs())


def categorical_mix(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {c: df[c].value_counts(normalize=True).round(4) for c in CATEGORICAL if c in df}
