"""Reproducibility fingerprints recorded with every training run and every
promotion: what code, what data, what environment produced a model.

    code_fingerprint()      sha256 over the bsp_mlops source tree + feature definitions
                            (+ git commit when the tree is a git checkout)
    env_lock()              pip-freeze lock of the running interpreter + its sha256
    data_snapshot_hash(df)  order-independent sha256 of a training frame (rows x columns)
    env_diff(a, b)          added / removed / changed packages between two locks

The same functions run in the mlops container, the workspace (notebooks) and
CI, so the hashes compare across users and environments.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from functools import lru_cache
from pathlib import Path

import pandas as pd

PKG = Path(__file__).resolve().parent
TRACKED = ("*.py", "feature_repo/features.yaml", "tower/static/*.js")


@lru_cache(maxsize=1)
def code_fingerprint() -> dict:
    files = sorted({p for pat in TRACKED for p in PKG.glob(pat) if p.is_file()} |
                   {p for p in PKG.glob("*/*.py") if p.is_file()})
    h = hashlib.sha256()
    for p in files:
        h.update(str(p.relative_to(PKG)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return {"code_sha256": h.hexdigest(), "files": len(files), "git_sha": git_sha(), "package": "bsp_mlops"}


def git_sha() -> str | None:
    """Commit of the source tree when it is a git checkout (None otherwise —
    the appliance bind-mounts a plain copy, so the code hash is the identity)."""
    for d in (PKG, *PKG.parents):
        if (d / ".git").exists():
            try:
                out = subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5)
                return out.stdout.strip() or None
            except Exception:
                return None
    return None


@lru_cache(maxsize=1)
def env_lock() -> dict:
    from importlib import metadata

    pkgs = sorted({f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions() if d.metadata["Name"]}, key=str.lower)
    text = "\n".join(pkgs) + "\n"
    return {"lock": text, "env_lock_sha256": hashlib.sha256(text.encode()).hexdigest(), "packages": len(pkgs),
            "python": platform.python_version(), "platform": platform.platform()}


def data_snapshot_hash(df: pd.DataFrame, keys: tuple[str, ...] = ("ban", "snapshot_month")) -> str:
    """sha256 of the frame's content, independent of row order and of the
    in-memory dtype representation of dates (rows sorted by the entity keys,
    columns sorted by name)."""
    d = df.copy()
    for c in d.columns:
        first = d[c].dropna().iloc[0] if d[c].dtype == object and d[c].notna().any() else None
        if str(d[c].dtype).startswith("datetime") or hasattr(first, "isoformat"):
            d[c] = d[c].astype(str).str[:10]
    k = [c for c in keys if c in d.columns]
    d = d.sort_values(k, kind="mergesort").reset_index(drop=True) if k else d
    d = d[sorted(d.columns)]
    h = hashlib.sha256(",".join(d.columns).encode())
    h.update(pd.util.hash_pandas_object(d, index=False).to_numpy().tobytes())
    return h.hexdigest()


def parse_lock(text: str) -> dict[str, str]:
    out = {}
    for line in (text or "").splitlines():
        if "==" in line:
            n, v = line.split("==", 1)
            out[n.strip().lower()] = v.strip()
    return out


def env_diff(recorded: str, current: str) -> dict:
    a, b = parse_lock(recorded), parse_lock(current)
    changed = [{"package": k, "recorded": a[k], "current": b[k]} for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return {"identical": not changed and set(a) == set(b), "changed": changed,
            "added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "recorded_packages": len(a), "current_packages": len(b)}


def run_tags(data_hash: str | None = None) -> dict:
    """Tags every training run carries (reproducibility contract)."""
    c, e = code_fingerprint(), env_lock()
    t = {"bsp.code_sha256": c["code_sha256"], "bsp.env_lock_sha256": e["env_lock_sha256"], "bsp.python": e["python"],
         "bsp.git_sha": c["git_sha"] or "unavailable (source tree is not a git checkout; bsp.code_sha256 identifies it)"}
    if data_hash:
        t["bsp.data_snapshot_sha256"] = data_hash
    return t


def dumps(o) -> str:
    return json.dumps(o, indent=2, default=str, sort_keys=True)
