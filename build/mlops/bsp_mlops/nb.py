"""Notebook helpers (work_templates/ml): one call to check the stack.

    from bsp_mlops import nb
    nb.setup()            # matplotlib defaults + service status table
"""

from __future__ import annotations

import os
import tempfile

import httpx

from . import config


def status() -> dict:
    out = {}
    for name, url in (("mlflow", config.MLFLOW_URI.rstrip("/") + "/health"),
                      ("model-gateway", config.GATEWAY_URL.rstrip("/") + "/healthz"),
                      ("mlops", config.MLOPS_URL.rstrip("/") + "/healthz")):
        try:
            out[name] = "up" if httpx.get(url, timeout=5).status_code == 200 else "down"
        except Exception:
            out[name] = "down"
    try:
        from . import db

        out["warehouse"] = "up" if db.fetchone("select 1 as ok") else "down"
        out["raw.customer_monthly"] = "seeded" if db.table_exists("raw.customer_monthly") else "missing — run: python -m bsp_seed --target postgres"
    except Exception as e:
        out["warehouse"] = f"down ({e.__class__.__name__})"
    return out


def setup(quiet: bool = False) -> dict:
    os.environ.setdefault("MPLCONFIGDIR", tempfile.gettempdir())
    try:
        import matplotlib.pyplot as plt

        plt.rcParams.update({"figure.figsize": (9, 4), "axes.spines.top": False, "axes.spines.right": False,
                             "axes.prop_cycle": plt.cycler(color=["#0b6e79", "#3380f0", "#ff8205", "#3ae1ce", "#828487"])})
    except Exception:
        pass
    import pandas as pd

    pd.set_option("display.width", 160)
    pd.set_option("display.max_columns", 30)
    s = status()
    if not quiet:
        for k, v in s.items():
            print(f"  {k:<22} {v}")
    return s


def tower(method: str, path: str, **kw):
    r = httpx.request(method, config.MLOPS_URL.rstrip("/") + path, headers={"X-Studio": "1"}, timeout=kw.pop("timeout", 900), **kw)
    r.raise_for_status()
    return r.json() if "json" in r.headers.get("content-type", "") else r.text
