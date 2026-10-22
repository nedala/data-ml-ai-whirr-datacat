"""Routing config: config/ai/providers.yaml deep-merged with the UI override file,
env-var interpolation, validation and hot reload (mtime check on every access).

Base file:     $HARNESS_CONFIG   (default /opt/config/ai/providers.yaml, else the repo copy)
Override file: $HARNESS_DATA_DIR/providers.override.yaml (default /data/harness) — the only file the UI writes.
"""
from __future__ import annotations

import copy
import os
import re
import threading
from pathlib import Path
from typing import Any

import yaml

PROVIDER_TYPES = ("ollama", "vertex", "anthropic", "anthropic_vertex", "openai_compatible")
MODEL_KINDS = ("chat", "vlm", "embed")
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def data_dir() -> Path:
    p = Path(os.environ.get("HARNESS_DATA_DIR", "/data/harness"))
    try:
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        alt = Path(os.environ.get("TMPDIR", "/tmp")) / "bsp-harness"
        alt.mkdir(parents=True, exist_ok=True)
        return alt


def base_path() -> Path:
    env = os.environ.get("HARNESS_CONFIG")
    if env:
        return Path(env)
    for c in (Path("/opt/config/ai/providers.yaml"), Path(__file__).resolve().parents[3] / "config/ai/providers.yaml"):
        if c.exists():
            return c
    return Path("/opt/config/ai/providers.yaml")


def prefer_provider(cfg: dict, provider: str) -> dict:
    """LLM_PROVIDER=<name> moves that provider's routes to the front of every
    alias chain (the others stay as fallbacks). Empty = the file's order.
    Restricted-data policies still filter routes at request time."""
    provider = (provider or "").strip().lower()
    if not provider:
        return cfg
    for a in (cfg.get("aliases") or {}).values():
        if isinstance(a, dict) and isinstance(a.get("routes"), list):
            a["routes"] = sorted(a["routes"], key=lambda r: str(r).split("/", 1)[0].lower() != provider)
    return cfg


def override_path() -> Path:
    return data_dir() / "providers.override.yaml"


def interpolate(v: Any, env: dict[str, str] | None = None) -> Any:
    env = os.environ if env is None else env
    if isinstance(v, str):
        return _ENV.sub(lambda m: (env.get(m.group(1)) or "") or (m.group(2) or ""), v)
    if isinstance(v, list):
        return [interpolate(x, env) for x in v]
    if isinstance(v, dict):
        return {k: interpolate(x, env) for k, x in v.items()}
    return v


def deep_merge(a: Any, b: Any) -> Any:
    """b over a; dicts merge recursively, everything else (lists included) is replaced; None in b deletes."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            if v is None:
                out.pop(k, None)
            else:
                out[k] = deep_merge(a.get(k), v)
        return out
    return copy.deepcopy(b)


def validate(cfg: dict) -> list[str]:
    """Human-readable problems (empty = valid). Run on the interpolated config."""
    errs: list[str] = []
    if not isinstance(cfg, dict):
        return ["config must be a mapping"]
    provs = cfg.get("providers") or {}
    if not isinstance(provs, dict) or not provs:
        errs.append("providers: at least one provider is required")
        provs = {}
    for name, p in provs.items():
        if not isinstance(p, dict):
            errs.append(f"providers.{name}: must be a mapping")
            continue
        if p.get("type") not in PROVIDER_TYPES:
            errs.append(f"providers.{name}.type: must be one of {', '.join(PROVIDER_TYPES)}")
        if p.get("type") in ("ollama", "openai_compatible") and not p.get("base_url"):
            errs.append(f"providers.{name}.base_url: required for {p.get('type')}")
        for i, m in enumerate(p.get("models") or []):
            if not isinstance(m, dict) or not m.get("id"):
                errs.append(f"providers.{name}.models[{i}]: needs an id")
            elif m.get("kind", "chat") not in MODEL_KINDS:
                errs.append(f"providers.{name}.models[{i}].kind: one of {', '.join(MODEL_KINDS)}")
    aliases = cfg.get("aliases") or {}
    if not isinstance(aliases, dict):
        errs.append("aliases: must be a mapping")
        aliases = {}
    for name, a in aliases.items():
        if "/" in name:
            errs.append(f"aliases.{name}: alias names cannot contain '/'")
        if not isinstance(a, dict) or not a.get("routes"):
            errs.append(f"aliases.{name}.routes: at least one route is required")
            continue
        for r in a["routes"]:
            prov = str(r).split("/", 1)[0]
            if "/" not in str(r):
                errs.append(f"aliases.{name}.routes: '{r}' must be provider/model")
            elif prov not in provs:
                errs.append(f"aliases.{name}.routes: unknown provider '{prov}' in '{r}'")
    rules = ((cfg.get("policies") or {}).get("data_classification") or {}).get("rules") or {}
    if not isinstance(rules, dict):
        errs.append("policies.data_classification.rules: must be a mapping")
    return errs


class Config:
    """Thread-safe, hot-reloading view of the merged config."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sig: tuple = ()
        self._raw: dict = {}
        self._cfg: dict = {}
        self.error: str = ""

    def _signature(self) -> tuple:
        sig = []
        for p in (base_path(), override_path()):
            try:
                st = p.stat()
                sig.append((str(p), st.st_mtime_ns, st.st_size))
            except OSError:
                sig.append((str(p), 0, 0))
        return tuple(sig)

    def _load(self) -> None:
        base = yaml.safe_load(base_path().read_text()) if base_path().exists() else {}
        ov = {}
        if override_path().exists():
            ov = yaml.safe_load(override_path().read_text()) or {}
        raw = deep_merge(base or {}, ov)
        cfg = prefer_provider(interpolate(raw), os.environ.get("LLM_PROVIDER", ""))
        errs = validate(cfg)
        if errs and self._cfg:
            self.error = "; ".join(errs)  # keep serving the last good config
            return
        self._raw, self._cfg, self.error = raw, cfg, "; ".join(errs)

    def get(self) -> dict:
        sig = self._signature()
        if sig != self._sig:
            with self._lock:
                if sig != self._sig:
                    try:
                        self._load()
                    except Exception as e:  # noqa: BLE001 — a broken edit must not take the gateway down
                        self.error = f"{type(e).__name__}: {e}"
                    self._sig = sig
        return self._cfg

    def raw(self) -> dict:
        self.get()
        return self._raw

    def base_raw(self) -> dict:
        return yaml.safe_load(base_path().read_text()) if base_path().exists() else {}

    def override_raw(self) -> dict:
        return (yaml.safe_load(override_path().read_text()) or {}) if override_path().exists() else {}

    def write_override(self, override: dict) -> list[str]:
        """Validate (base ⊕ override, interpolated) and write the override file atomically."""
        merged = interpolate(deep_merge(self.base_raw() or {}, override or {}))
        errs = validate(merged)
        if errs:
            return errs
        p = override_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text("# Written by the AI harness UI — merged over config/ai/providers.yaml\n"
                       + yaml.safe_dump(override or {}, sort_keys=False))
        tmp.replace(p)
        self._sig = ()
        return []

    def reset_override(self) -> None:
        try:
            override_path().unlink()
        except FileNotFoundError:
            pass
        self._sig = ()

    # convenience accessors ------------------------------------------------
    def provider(self, name: str) -> dict | None:
        return (self.get().get("providers") or {}).get(name)

    def alias(self, name: str) -> dict | None:
        return (self.get().get("aliases") or {}).get(name)

    def defaults(self) -> dict:
        return self.get().get("defaults") or {}

    def policies(self) -> dict:
        return self.get().get("policies") or {}

    def model_info(self, provider: str, model: str) -> dict:
        for m in (self.provider(provider) or {}).get("models") or []:
            if m.get("id") == model:
                return m
        return {}


CONFIG = Config()
