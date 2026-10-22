"""Static analysis of the compose files: which services consume which
`${VARIABLE}` — so rotating a credential recreates exactly the containers
that read it — and which image each service runs.

The root docker-compose.yml `include:`s one fragment per component (paths
relative to the repo root) and fragments `extends` the abstract services in
compose.base.yml; both are followed here (no `docker compose config` call:
that would need every secret in the environment)."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

import settings

MAIN_FILE = "docker-compose.yml"
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")
_lock = threading.Lock()
_cache: dict[str, Any] = {}


def _strings(node: Any):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)


def _read(path: Path) -> dict[str, Any]:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}


def _files() -> list[Path]:
    root = settings.cfg.project
    main = root / MAIN_FILE
    out = [main]
    for inc in (_read(main).get("include") or []):
        rel = inc.get("path") if isinstance(inc, dict) else inc
        if isinstance(rel, list):
            rel = rel[0] if rel else None
        if isinstance(rel, str):
            out.append(root / rel)
    return out


def _resolve_extends(svc: dict[str, Any], seen: int = 0) -> dict[str, Any]:
    """Merge `extends: {file, service}` (environment + image) into `svc`."""
    ext = svc.get("extends")
    if not ext or seen > 5:
        return svc
    if isinstance(ext, str):
        ext = {"service": ext}
    base_file = settings.cfg.project / ext.get("file", MAIN_FILE)
    base = ((_read(base_file).get("services") or {}).get(ext.get("service", "")) or {})
    base = _resolve_extends(base, seen + 1)
    merged = {**base, **svc}
    env: dict[str, Any] = {}
    for part in (base.get("environment"), svc.get("environment")):
        if isinstance(part, dict):
            env.update(part)
        elif isinstance(part, list):
            for item in part:
                k, _, v = str(item).partition("=")
                env[k] = v
    merged["environment"] = env
    return merged


def services_model() -> dict[str, dict[str, Any]]:
    """{compose service: definition with `extends` resolved}, across includes."""
    files = _files()
    stamp = tuple((str(f), f.stat().st_mtime if f.exists() else 0) for f in files)
    base = settings.cfg.project / "compose.base.yml"
    stamp += ((str(base), base.stat().st_mtime if base.exists() else 0),)
    with _lock:
        hit = _cache.get("model")
        if hit and hit[0] == stamp:
            return hit[1]
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        for name, svc in (_read(f).get("services") or {}).items():
            out[name] = _resolve_extends(svc or {})
    with _lock:
        _cache["model"] = (stamp, out)
    return out


def env_refs(file: str = MAIN_FILE) -> dict[str, set[str]]:
    """{compose service: {VARIABLE referenced in its environment}}."""
    out: dict[str, set[str]] = {}
    for name, svc in services_model().items():
        refs: set[str] = set()
        for text in _strings(svc.get("environment") or {}):
            refs.update(_VAR.findall(text))
        out[name] = refs
    return out


def services() -> set[str]:
    return set(services_model())


def consumers(variable: str) -> list[str]:
    """Compose services whose containers read `variable`."""
    return [svc for svc, refs in env_refs(MAIN_FILE).items() if variable in refs]


_INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::?-([^}]*))?\}")


def _interpolate(text: str) -> str:
    """Resolve ${VAR} / ${VAR:-default} the way compose does for image names
    (process env, then the project .env, then the default)."""
    env = settings.dotenv()

    def sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2) or ""
        return settings._env(name) or env.get(name) or default
    return _INTERP.sub(sub, text)


def images(file: str = MAIN_FILE) -> dict[str, str]:
    """{compose service: fully resolved image reference}."""
    out: dict[str, str] = {}
    for name, svc in services_model().items():
        image = svc.get("image")
        if isinstance(image, str) and not name.startswith("x-"):
            ref = _interpolate(image)
            out[name] = ref if ":" in ref.rsplit("/", 1)[-1] or "@" in ref else f"{ref}:latest"
    return out
