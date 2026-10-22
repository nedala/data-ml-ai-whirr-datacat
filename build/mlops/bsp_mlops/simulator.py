"""Traffic simulator — replays real customer snapshots through the gateway.

  normal  samples the months just before the shift (what the champion was
          built for)
  shift   samples the last months (price-increase wave + outage cluster) —
          the drift monitor should light up within a few hundred requests

Requests carry the full feature payload (like an upstream CRM/billing caller
would) plus snapshot_month, so outcomes can be joined later; a configurable
share is sent id-only to exercise the gateway's online feature lookup.
"""

from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from . import config, db


def _pool(shift: bool, limit: int = 20000, seed: int = 11) -> pd.DataFrame:
    from .data import ensure_base, windows
    from .featurestore import all_features

    ensure_base()
    w = windows()
    a, b = w["shift_replay"] if shift else w["normal_replay"]
    feats = all_features()
    df = db.query_df(f"select ban, snapshot_month, {', '.join(feats)} from features.churn_base "
                     f"where snapshot_month between %s and %s order by md5(ban || snapshot_month::text || %s) limit %s",
                     (a, b, str(seed), limit))
    for c in ("autopay", "competitor_fiber_available", "recent_price_increase", "promo_expired", "line_quality_bad"):
        if c in df:
            df[c] = df[c].astype(float).fillna(0).astype(int)
    df["snapshot_month"] = df["snapshot_month"].astype(str)
    return df


def _instances(df: pd.DataFrame, id_only_share: float = 0.0, rng: random.Random | None = None) -> list[dict]:
    rng = rng or random.Random(5)
    out = []
    for rec in df.to_dict("records"):
        acct, snap = rec.pop("ban"), rec.pop("snapshot_month")
        if id_only_share and rng.random() < id_only_share:
            out.append({"ban": acct})
        else:
            f = {k: (None if isinstance(v, float) and np.isnan(v) else (v.item() if hasattr(v, "item") else v))
                 for k, v in rec.items()}
            out.append({"ban": acct, "features": f, "snapshot_month": snap})
    return out


def burst(n: int, shift: bool = False, batch: int = 250, id_only_share: float = 0.0, seed: int = 11,
          source: str = "simulator") -> dict:
    """Send `n` requests synchronously (used by the headless demo / notebooks)."""
    from . import deploy

    pool = _pool(shift, limit=max(n, 1000), seed=seed)
    pool = pool.sample(n, replace=n > len(pool), random_state=seed)
    inst = _instances(pool, id_only_share)
    sent, t0 = 0, time.time()
    counts: dict[str, int] = {}
    for i in range(0, len(inst), batch):
        preds = deploy.predict_batch(inst[i:i + batch], source=source + ("-shift" if shift else ""))
        for p in preds:
            key = f"v{p['model_version']}:{p['colour']}"
            counts[key] = counts.get(key, 0) + 1
        sent += len(preds)
    return {"sent": sent, "seconds": round(time.time() - t0, 1), "served_by": counts, "shift": shift}


class Simulator:
    """Background replay at a target rate (requests / second)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_evt = threading.Event()
        self.cfg = {"rate": 20.0, "shift": False, "id_only_share": 0.05}
        self.stats = {"sent": 0, "errors": 0, "started_at": None, "last_error": None, "last_batch_at": None}
        self._pools: dict[bool, pd.DataFrame] = {}

    def status(self) -> dict:
        return {"running": bool(self.thread and self.thread.is_alive()), **self.cfg, **self.stats}

    def configure(self, rate: float | None = None, shift: bool | None = None, id_only_share: float | None = None) -> dict:
        with self.lock:
            if rate is not None:
                self.cfg["rate"] = max(0.5, min(500.0, float(rate)))
            if shift is not None:
                self.cfg["shift"] = bool(shift)
            if id_only_share is not None:
                self.cfg["id_only_share"] = max(0.0, min(1.0, float(id_only_share)))
        return self.status()

    def start(self, **kw) -> dict:
        self.configure(**kw)
        if self.thread and self.thread.is_alive():
            return self.status()
        self.stop_evt.clear()
        self.stats.update(started_at=datetime.now(timezone.utc).isoformat())
        self.thread = threading.Thread(target=self._loop, daemon=True, name="traffic-sim")
        self.thread.start()
        return self.status()

    def stop(self) -> dict:
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=5)
        return self.status()

    def _loop(self) -> None:
        from . import deploy

        rng = random.Random(3)
        tick = 1.0
        while not self.stop_evt.is_set():
            t0 = time.time()
            try:
                shift = self.cfg["shift"]
                if shift not in self._pools:
                    self._pools[shift] = _pool(shift)
                pool = self._pools[shift]
                n = max(1, int(round(self.cfg["rate"] * tick)))
                sample = pool.sample(n, replace=n > len(pool), random_state=rng.randint(0, 10**6))
                preds = deploy.predict_batch(_instances(sample, self.cfg["id_only_share"], rng),
                                             source="simulator-shift" if shift else "simulator")
                self.stats["sent"] += len(preds)
                self.stats["last_batch_at"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                self.stats["errors"] += 1
                self.stats["last_error"] = str(e)[:300]
                time.sleep(2)
            self.stop_evt.wait(max(0.0, tick - (time.time() - t0)))


SIM = Simulator()
_ = config
