"""Classification metrics for a churn model: discrimination, calibration and
the business view (lift / gain by decile)."""

from __future__ import annotations

import numpy as np
import pandas as pd


def evaluate(y: np.ndarray, p: np.ndarray, prefix: str = "") -> dict[str, float]:
    from sklearn.metrics import average_precision_score, brier_score_loss, log_loss, roc_auc_score

    y = np.asarray(y).astype(int)
    p = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    base = y.mean() if len(y) else 0.0
    order = np.argsort(-p)
    k = max(1, int(round(len(p) * 0.10)))
    top = y[order[:k]]
    out = {
        "auc": roc_auc_score(y, p) if 0 < base < 1 else float("nan"),
        "pr_auc": average_precision_score(y, p) if 0 < base < 1 else float("nan"),
        "logloss": log_loss(y, p, labels=[0, 1]),
        "brier": brier_score_loss(y, p),
        "lift_at_10": (top.mean() / base) if base > 0 else float("nan"),
        "capture_at_10": (top.sum() / y.sum()) if y.sum() > 0 else float("nan"),
        "ks": _ks(y, p),
        "base_rate": float(base),
        "mean_score": float(p.mean()),
        "n": float(len(y)),
    }
    return {f"{prefix}{k}": float(v) for k, v in out.items()}


def _ks(y: np.ndarray, p: np.ndarray) -> float:
    from scipy.stats import ks_2samp

    if y.min() == y.max():
        return float("nan")
    return float(ks_2samp(p[y == 1], p[y == 0]).statistic)


def gain_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({"y": np.asarray(y).astype(int), "p": np.asarray(p, dtype=float)})
    df["decile"] = pd.qcut(df.p.rank(method="first", ascending=False), bins, labels=range(1, bins + 1)).astype(int)
    g = df.groupby("decile").agg(customers=("y", "size"), churners=("y", "sum"), mean_score=("p", "mean"))
    g["churn_rate"] = g.churners / g.customers
    g["lift"] = g.churn_rate / df.y.mean()
    g["cum_capture"] = g.churners.cumsum() / max(1, df.y.sum())
    return g


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int = 10) -> pd.DataFrame:
    df = pd.DataFrame({"y": np.asarray(y).astype(int), "p": np.asarray(p, dtype=float)})
    df["bin"] = pd.qcut(df.p, bins, duplicates="drop")
    g = df.groupby("bin", observed=True).agg(mean_predicted=("p", "mean"), observed_rate=("y", "mean"), n=("y", "size"))
    g.index = g.index.astype(str)
    return g


def plots(y: np.ndarray, p: np.ndarray, out_dir, title: str = "") -> list:
    """ROC, PR, calibration and cumulative gain PNGs; returns file paths."""
    import matplotlib

    matplotlib.use("Agg")
    from pathlib import Path

    import matplotlib.pyplot as plt
    from sklearn.metrics import precision_recall_curve, roc_curve

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = []
    fig, ax = plt.subplots(1, 4, figsize=(18, 4.2))
    fpr, tpr, _ = roc_curve(y, p)
    ax[0].plot(fpr, tpr, color="#0b6e4f"); ax[0].plot([0, 1], [0, 1], "--", color="#999")
    ax[0].set(title="ROC", xlabel="false positive rate", ylabel="true positive rate")
    pr, rc, _ = precision_recall_curve(y, p)
    ax[1].plot(rc, pr, color="#0b6e4f"); ax[1].axhline(np.mean(y), ls="--", color="#999")
    ax[1].set(title="Precision–recall", xlabel="recall", ylabel="precision")
    cal = calibration_table(y, p)
    ax[2].plot(cal.mean_predicted, cal.observed_rate, "o-", color="#0b6e4f")
    lim = max(cal.mean_predicted.max(), cal.observed_rate.max()) * 1.05
    ax[2].plot([0, lim], [0, lim], "--", color="#999")
    ax[2].set(title="Calibration", xlabel="mean predicted", ylabel="observed churn rate")
    g = gain_table(y, p)
    ax[3].plot([0] + list(g.index * 10), [0] + list(g.cum_capture * 100), "o-", color="#0b6e4f")
    ax[3].plot([0, 100], [0, 100], "--", color="#999")
    ax[3].set(title="Cumulative gain", xlabel="% customers targeted", ylabel="% churners captured")
    fig.suptitle(title)
    fig.tight_layout()
    f = out / "evaluation.png"
    fig.savefig(f, dpi=90)
    plt.close(fig)
    files.append(f)
    return files
