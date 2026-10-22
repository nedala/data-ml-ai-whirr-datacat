"""Statistical tests used by the release gates, the champion / challenger
comparison and the business experiments. Pure numpy / scipy — no services.

    auc(y, p)                         rank-based ROC AUC (ties averaged)
    delong(y, p1, p2)                 paired DeLong test of AUC(p1) - AUC(p2) on the SAME rows
    paired_bootstrap_auc(y, p1, p2)   stratified paired bootstrap CI of the AUC difference
    bootstrap_auc_diff_independent    two independent samples (canary arms)
    ece(y, p)                         expected calibration error (equal-frequency bins)
    two_proportion(x1, n1, x2, n2)    z-test + Wald / Newcombe-free CI for a difference in rates
    power_two_proportion(...)         sample size per arm for a target power

References: DeLong, DeLong & Clarke-Pearson (1988); Sun & Xu (2014), "Fast
implementation of DeLong's algorithm"; Efron & Tibshirani (1993) bootstrap.
"""

from __future__ import annotations

import math

import numpy as np


def _midrank(x: np.ndarray) -> np.ndarray:
    from scipy.stats import rankdata

    return rankdata(x, method="average")


def auc(y, p) -> float:
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    n1 = int(y.sum())
    n0 = len(y) - n1
    if n1 == 0 or n0 == 0:
        return float("nan")
    r = _midrank(p)
    return float((r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0))


def _structural(y: np.ndarray, preds: list[np.ndarray]):
    """Placement values (Sun & Xu 2014) for k score vectors on the same rows."""
    pos, neg = y == 1, y == 0
    m, n = int(pos.sum()), int(neg.sum())
    k = len(preds)
    aucs = np.zeros(k)
    v10 = np.zeros((k, m))
    v01 = np.zeros((k, n))
    for i, p in enumerate(preds):
        x, z = p[pos], p[neg]
        tx, tz, txz = _midrank(x), _midrank(z), _midrank(np.concatenate([x, z]))
        aucs[i] = (txz[:m].sum() - m * (m + 1) / 2) / (m * n)
        v10[i] = (txz[:m] - tx) / n
        v01[i] = 1.0 - (txz[m:] - tz) / m
    return aucs, v10, v01, m, n


def delong(y, p1, p2) -> dict:
    """Paired DeLong test, H0: AUC(p1) == AUC(p2). Two-sided p-value."""
    from scipy.stats import norm

    y = np.asarray(y).astype(int)
    p1, p2 = np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)
    if y.min() == y.max():
        return {"auc_1": float("nan"), "auc_2": float("nan"), "diff": float("nan"), "se": float("nan"), "z": float("nan"), "p_value": float("nan")}
    aucs, v10, v01, m, n = _structural(y, [p1, p2])
    s10, s01 = np.cov(v10), np.cov(v01)
    s = s10 / m + s01 / n
    var = float(s[0, 0] + s[1, 1] - 2 * s[0, 1])
    diff = float(aucs[0] - aucs[1])
    if var <= 1e-15:
        return {"auc_1": float(aucs[0]), "auc_2": float(aucs[1]), "diff": diff, "se": 0.0, "z": 0.0 if diff == 0 else float("inf"),
                "p_value": 1.0 if diff == 0 else 0.0}
    se = math.sqrt(var)
    z = diff / se
    return {"auc_1": float(aucs[0]), "auc_2": float(aucs[1]), "diff": diff, "se": se, "z": float(z),
            "p_value": float(2 * norm.sf(abs(z))), "ci_low": diff - 1.959964 * se, "ci_high": diff + 1.959964 * se}


def _strat_indices(y: np.ndarray, rng: np.random.Generator, n_boot: int) -> np.ndarray:
    pos, neg = np.flatnonzero(y == 1), np.flatnonzero(y == 0)
    a = rng.choice(pos, size=(n_boot, len(pos)), replace=True)
    b = rng.choice(neg, size=(n_boot, len(neg)), replace=True)
    return np.concatenate([a, b], axis=1)


def _auc_rows(y_sorted_pos: int, p: np.ndarray) -> float:
    return auc(np.r_[np.ones(y_sorted_pos), np.zeros(len(p) - y_sorted_pos)], p)


def paired_bootstrap_auc(y, p1, p2, n_boot: int = 1000, seed: int = 7, alpha: float = 0.05) -> dict:
    """Stratified (positives / negatives resampled separately) paired bootstrap
    of AUC(p1) - AUC(p2): the same resampled rows score both models."""
    y = np.asarray(y).astype(int)
    p1, p2 = np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)
    if y.min() == y.max():
        return {"diff": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "n_boot": 0}
    rng = np.random.default_rng(seed)
    idx = _strat_indices(y, rng, n_boot)
    npos = int(y.sum())
    d = np.empty(n_boot)
    for b in range(n_boot):
        ii = idx[b]
        d[b] = _auc_rows(npos, p1[ii]) - _auc_rows(npos, p2[ii])
    lo, hi = np.quantile(d, [alpha / 2, 1 - alpha / 2])
    return {"diff": auc(y, p1) - auc(y, p2), "ci_low": float(lo), "ci_high": float(hi), "n_boot": n_boot,
            "p_le_0": float((d <= 0).mean()), "boot_se": float(d.std(ddof=1))}


def bootstrap_auc_diff_independent(y1, p1, y2, p2, n_boot: int = 1000, seed: int = 7, alpha: float = 0.05) -> dict:
    """AUC(arm 1) - AUC(arm 2) for two independent samples (e.g. canary vs stable)."""
    y1, y2 = np.asarray(y1).astype(int), np.asarray(y2).astype(int)
    p1, p2 = np.asarray(p1, dtype=float), np.asarray(p2, dtype=float)
    if min(y1.min(), y2.min()) == max(y1.max(), y2.max()) or y1.min() == y1.max() or y2.min() == y2.max():
        return {"diff": float("nan"), "ci_low": float("nan"), "ci_high": float("nan"), "n_boot": 0}
    rng = np.random.default_rng(seed)
    i1, i2 = _strat_indices(y1, rng, n_boot), _strat_indices(y2, rng, n_boot)
    n1, n2 = int(y1.sum()), int(y2.sum())
    d = np.array([_auc_rows(n1, p1[i1[b]]) - _auc_rows(n2, p2[i2[b]]) for b in range(n_boot)])
    lo, hi = np.quantile(d, [alpha / 2, 1 - alpha / 2])
    return {"diff": auc(y1, p1) - auc(y2, p2), "ci_low": float(lo), "ci_high": float(hi), "n_boot": n_boot}


def ece(y, p, bins: int = 10) -> float:
    """Expected calibration error with equal-frequency bins (stable at a ~7 %
    base rate, where equal-width bins leave the top bins nearly empty)."""
    y = np.asarray(y).astype(float)
    p = np.asarray(p, dtype=float)
    if len(y) == 0:
        return float("nan")
    order = np.argsort(p, kind="mergesort")
    parts = np.array_split(order, min(bins, len(p)))
    return float(sum(len(ix) / len(p) * abs(y[ix].mean() - p[ix].mean()) for ix in parts if len(ix)))


def brier(y, p) -> float:
    y = np.asarray(y).astype(float)
    p = np.asarray(p, dtype=float)
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def lift_at(y, p, frac: float = 0.10) -> float:
    y = np.asarray(y).astype(int)
    p = np.asarray(p, dtype=float)
    if len(y) == 0 or y.sum() == 0:
        return float("nan")
    k = max(1, int(round(len(p) * frac)))
    top = y[np.argsort(-p, kind="mergesort")[:k]]
    return float(top.mean() / y.mean())


def two_proportion(x1: int, n1: int, x2: int, n2: int, alpha: float = 0.05) -> dict:
    """Rate 1 - rate 2: pooled two-sided z-test and an unpooled (Wald) CI."""
    from scipy.stats import norm

    if n1 == 0 or n2 == 0:
        return {"diff": float("nan"), "p_value": float("nan")}
    r1, r2 = x1 / n1, x2 / n2
    pool = (x1 + x2) / (n1 + n2)
    se0 = math.sqrt(max(pool * (1 - pool) * (1 / n1 + 1 / n2), 1e-15))
    z = (r1 - r2) / se0
    se = math.sqrt(max(r1 * (1 - r1) / n1 + r2 * (1 - r2) / n2, 1e-15))
    q = norm.ppf(1 - alpha / 2)
    return {"rate_1": r1, "rate_2": r2, "diff": r1 - r2, "relative": (r1 - r2) / r2 if r2 else float("nan"),
            "z": z, "p_value": float(2 * norm.sf(abs(z))), "ci_low": r1 - r2 - q * se, "ci_high": r1 - r2 + q * se}


def power_two_proportion(p_control: float, rel_effect: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Customers per arm needed to detect a relative change `rel_effect` in a rate."""
    from scipy.stats import norm

    p1, p2 = p_control, p_control * (1 + rel_effect)
    za, zb = norm.ppf(1 - alpha / 2), norm.ppf(power)
    pbar = (p1 + p2) / 2
    num = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return int(math.ceil(num / max((p1 - p2) ** 2, 1e-12)))
