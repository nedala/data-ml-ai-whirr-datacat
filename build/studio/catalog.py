"""build/studio/catalog.yaml + catalog.d/*.yaml: loading and dependency resolution.

Catalog = the core file merged with every fragment in catalog.d/ (one per
component, sorted by name): `services`, `capabilities` and
`shared_credentials` merge by key, `apps` / `app_groups` by `id`; `personas`
come from the core file only. A persona that names a capability no fragment
declares (component not installed) skips it with a hint instead of failing.

Selection model: a set of capabilities (personas are presets that expand into
capabilities). Resolution closes over capability `requires`, maps capabilities
to services, then closes over service `needs`. `soft_needs` are never
auto-included; they are surfaced as hints."""

from __future__ import annotations

import threading
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import logging

import yaml

import settings

# Always-on control plane (not part of the catalog): service → title.
CONTROL_PLANE = {"nginx": "Front door (nginx)", "studio": "Studio", "px-proxy": "Egress proxy", "docs": "Docs"}
# pseudo-service owning the shared external credentials (vault services.shared.*)
SHARED = "shared"
CONTROL_PLANE_MB = 512

log = logging.getLogger("studio.catalog")
_lock = threading.Lock()
_cache: dict[str, Any] = {"mtime": None, "data": None}


def _sources() -> list[Path]:
    core = settings.cfg.catalog
    frag_dir = core.parent / "catalog.d"
    frags = sorted(frag_dir.glob("*.yaml")) + sorted(frag_dir.glob("*.yml")) if frag_dir.is_dir() else []
    return [core, *frags]


def _merge_list(base: list[dict[str, Any]], extra: Any) -> list[dict[str, Any]]:
    out = list(base)
    index = {item.get("id"): i for i, item in enumerate(out) if isinstance(item, dict)}
    for item in extra or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        if item["id"] in index:
            out[index[item["id"]]] = {**out[index[item["id"]]], **item}
        else:
            index[item["id"]] = len(out)
            out.append(item)
    return out


def merge(core: dict[str, Any], fragments: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """Pure merge (unit-tested): core catalog + [(fragment name, doc)]."""
    data = dict(core or {})
    for key in ("services", "capabilities", "personas", "shared_credentials"):
        data[key] = dict(data.get(key) or {})
    data["apps"] = list(data.get("apps") or [])
    data["app_groups"] = list(data.get("app_groups") or [])
    data["fragments"] = []
    for name, doc in fragments:
        if not isinstance(doc, dict):
            continue
        data["fragments"].append(name)
        for key in ("services", "capabilities", "shared_credentials"):
            for k, v in (doc.get(key) or {}).items():
                data[key][k] = {**data[key].get(k, {}), **(v or {})}
        data["apps"] = _merge_list(data["apps"], doc.get("apps"))
        data["app_groups"] = _merge_list(data["app_groups"], doc.get("app_groups"))
    return data


def load() -> dict[str, Any]:
    paths = _sources()
    stamp = tuple((str(p), p.stat().st_mtime) for p in paths if p.exists())
    with _lock:
        if _cache["mtime"] != stamp:
            core = yaml.safe_load(paths[0].read_text(encoding="utf-8")) or {}
            frags: list[tuple[str, dict[str, Any]]] = []
            errors: list[str] = []
            for p in paths[1:]:
                try:
                    frags.append((p.stem, yaml.safe_load(p.read_text(encoding="utf-8")) or {}))
                except (OSError, yaml.YAMLError) as exc:   # a broken fragment must not take Studio down
                    errors.append(f"{p.name}: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}")
                    log.error("catalog fragment %s skipped: %s", p, exc)
            data = merge(core, frags)
            data["fragment_errors"] = errors
            _cache.update(mtime=stamp, data=data)
        return _cache["data"]


def shared_slots() -> list[dict[str, Any]]:
    """Shared external credentials as cred slots of the `shared` pseudo-service."""
    return [{"env": env, "label": spec.get("label", env), "external": True,
             "multiline": bool(spec.get("multiline")), "hint": spec.get("hint", ""), "policy": "any"}
            for env, spec in (load().get("shared_credentials") or {}).items()]


def service_spec(service: str) -> dict[str, Any]:
    if service == SHARED:
        return {"title": "Shared (all components)", "kind": "shared", "creds": shared_slots()}
    return load()["services"].get(service, {})


def cred_slots(service: str) -> list[dict[str, Any]]:
    if service == SHARED:
        return shared_slots()
    return list(load()["services"].get(service, {}).get("creds", []))


def endpoints(service: str) -> list[dict[str, str]]:
    """Connection strings for a service (`<facade>` is filled in by the browser)."""
    import composefile  # noqa: PLC0415 — ${VAR:-default} resolved like compose does
    return [{"label": e["label"], "value": composefile._interpolate(str(e["value"]))}
            for e in load()["services"].get(service, {}).get("endpoints", [])]


def capabilities_of_service(service: str) -> list[str]:
    return [c for c, spec in load()["capabilities"].items() if service in spec.get("services", [])]


def composable_services() -> set[str]:
    """Services reachable from some capability (the composer may stop these)."""
    caps = list(load()["capabilities"])
    return set(resolve(capabilities=caps).services)


@dataclass
class Resolution:
    capabilities: dict[str, dict[str, Any]] = field(default_factory=dict)
    services: dict[str, dict[str, Any]] = field(default_factory=dict)
    profiles: list[str] = field(default_factory=list)
    hints: list[dict[str, str]] = field(default_factory=list)

    @property
    def ram_mb(self) -> int:
        svcs = load()["services"]
        return sum(int(svcs.get(s, {}).get("ram_mb", 0)) for s in self.services)


def persona_capabilities(personas: list[str], missing: list[str] | None = None) -> list[str]:
    """Union of the personas' capabilities that are actually declared; names
    without a fragment go to `missing` (if given) instead of raising."""
    cat = load()
    out: list[str] = []
    for p in personas:
        if p not in cat["personas"]:
            raise ValueError(f"unknown persona {p!r}")
        for c in cat["personas"][p].get("capabilities", []):
            if c not in cat["capabilities"]:
                if missing is not None and c not in missing:
                    missing.append(c)
                continue
            if c not in out:
                out.append(c)
    return out


def default_persona() -> str:
    """APPLIANCE_PERSONA if it names a persona, else the first persona."""
    personas = list(load()["personas"])
    want = settings.appliance_persona()
    return want if want in personas else (personas[0] if personas else "")


def resolve(personas: list[str] | None = None, capabilities: list[str] | None = None) -> Resolution:
    """`capabilities` (explicit set) wins; otherwise the union of `personas`."""
    cat = load()
    caps_spec, svc_spec = cat["capabilities"], cat["services"]
    missing: list[str] = []
    if capabilities is not None:
        explicit = []
        for c in capabilities:
            if c in caps_spec:
                explicit.append(c)
            elif c not in missing:
                missing.append(c)
    else:
        explicit = persona_capabilities(personas or [], missing)

    res = Resolution()
    for c in missing:
        res.hints.append({"capability": c, "missing": "capability",
                          "detail": f"Capability {c!r} is not installed in this appliance (no catalog fragment) — skipped"})
    queue = list(explicit)
    for c in explicit:
        res.capabilities[c] = {"state": "selected", "required_by": []}
    while queue:
        cap = queue.pop(0)
        for req in caps_spec[cap].get("requires", []):
            if req not in caps_spec:
                res.hints.append({"capability": cap, "missing": "capability",
                                  "detail": f"{cap} requires {req!r}, which is not installed — skipped"})
                continue
            entry = res.capabilities.setdefault(req, {"state": "implied", "required_by": []})
            if cap not in entry["required_by"]:
                entry["required_by"].append(cap)
            if len(entry["required_by"]) == 1 and entry["state"] == "implied":
                queue.append(req)

    for cap in res.capabilities:
        for svc in caps_spec[cap].get("services", []):
            if svc not in svc_spec:
                res.hints.append({"capability": cap, "missing": "service",
                                  "detail": f"{cap}: service {svc!r} is not declared in the catalog — skipped"})
                continue
            res.services.setdefault(svc, {"capabilities": [], "needed_by": []})["capabilities"].append(cap)

    queue = list(res.services)
    while queue:
        svc = queue.pop(0)
        for dep in svc_spec.get(svc, {}).get("needs", []):
            if dep not in svc_spec:
                res.hints.append({"service": svc, "missing": "service",
                                  "detail": f"{svc} needs {dep!r}, which is not declared in the catalog — skipped"})
                continue
            entry = res.services.get(dep)
            if entry is None:
                entry = res.services[dep] = {"capabilities": [], "needed_by": []}
                queue.append(dep)
            if svc not in entry["needed_by"]:
                entry["needed_by"].append(svc)

    res.services = {s: res.services[s] for s in _topo_order(res.services)}
    for svc in res.services:
        for soft in svc_spec[svc].get("soft_needs", []):
            if soft not in res.services:
                res.hints.append({"service": svc, "wants": soft,
                                  "detail": f"{svc_spec[svc].get('title', svc)} works better with {svc_spec.get(soft, {}).get('title', soft)}"})
        prof = svc_spec[svc].get("profile")
        if prof and prof not in res.profiles:
            res.profiles.append(prof)
    return res


def _topo_order(services: dict[str, Any]) -> list[str]:
    spec = load()["services"]
    order: list[str] = []
    seen: set[str] = set()

    def visit(s: str) -> None:
        if s in seen:
            return
        seen.add(s)
        for dep in spec.get(s, {}).get("needs", []):
            if dep in services and dep in spec:
                visit(dep)
        order.append(s)

    for s in services:
        visit(s)
    return order


def with_needs(services: list[str]) -> list[str]:
    """`services` plus their transitive `needs`, dependency-first."""
    spec = load()["services"]
    found: dict[str, Any] = {}
    queue = list(services)
    while queue:
        s = queue.pop(0)
        if s in found:
            continue
        if s not in spec:
            if s in services:
                raise ValueError(f"unknown service {s!r}")
            continue            # a `needs` entry whose component is not installed
        found[s] = True
        queue.extend(spec[s].get("needs", []))
    return _topo_order(found)
