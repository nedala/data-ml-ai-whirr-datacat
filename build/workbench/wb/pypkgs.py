"""Python package management for the user layer (PYTHONUSERBASE).

Layering model
--------------
The image's site-packages (the *platform*) is read-only. Analysts add
packages to ``/data/pyuser`` which sits first on PYTHONPATH for JupyterLab
and code-server.

``uv pip install --prefix`` alone would resolve as if the prefix were an empty
environment and re-install the whole dependency tree (pandas, numpy, …) into
the user layer, shadowing the platform. So with uv we:

1. resolve against the platform interpreter (``uv pip install --dry-run``),
   with the current user-layer versions as constraints so unrelated user
   packages do not move (relaxed automatically if that is unsatisfiable);
2. install exactly the planned ``name==version`` set into the prefix with
   ``--no-deps``.

Without uv we fall back to ``pip install --user`` which natively understands
the layering. Both paths finish with ``pip check`` diffed against the
platform's own baseline so only *new* conflicts are reported.
"""
from __future__ import annotations

import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sysconfig
import tempfile
import threading
import time
from pathlib import Path

import requests

from . import specs as S
from .config import (INDEX_ARGS, INSTALLER, PY_MANIFEST, PY_USER_SITE, PY_USERBASE, PYPI_JSON,
                     PYPI_SIMPLE, PYTHON, STATE_DIR)
from .jobs import Job, JobError
from .manifest import Manifest

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
    from packaging.version import InvalidVersion, Version
except Exception:  # pragma: no cover - packaging is in every image we ship
    Requirement = None  # type: ignore
    Version = None  # type: ignore

MANIFEST = Manifest(PY_MANIFEST, "Workbench Python manifest", S.canon,
                    lambda spec: S.py_spec(spec)["name"])

# Packages whose shadowing is likely to break the notebook servers themselves.
CRITICAL = {"jupyterlab", "jupyter-server", "notebook", "ipykernel", "ipython", "tornado",
            "pyzmq", "jupyter-client", "traitlets", "numpy", "pandas", "pyarrow",
            "fastapi", "starlette", "uvicorn", "pydantic", "pip", "setuptools", "uv",
            "psycopg2", "psycopg2-binary", "duckdb", "dbt-core", "dbt-postgres", "sqlalchemy"}


def _base_paths() -> list[str]:
    paths = []
    for key in ("purelib", "platlib"):
        p = sysconfig.get_paths().get(key)
        if p and p not in paths and Path(p).resolve() != PY_USER_SITE.resolve():
            paths.append(p)
    return paths


def installer() -> str:
    if INSTALLER == "pip":
        return "pip"
    if shutil.which("uv"):
        return "uv"
    if INSTALLER == "uv":
        return "pip"  # requested but missing → degrade
    return "pip"


# ── installed distributions ──────────────────────────────────────────────
_size_cache: dict[tuple[str, float], int] = {}


def _dist_size(dist: md.Distribution) -> int | None:
    try:
        base = getattr(dist, "_path", None)
        key = (str(base), os.stat(base).st_mtime if base else 0.0)
    except OSError:
        key = (str(getattr(dist, "_path", "")), 0.0)
    if key in _size_cache:
        return _size_cache[key]
    total = 0
    try:
        for f in dist.files or []:
            try:
                total += os.stat(dist.locate_file(f)).st_size
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return None
    _size_cache[key] = total
    return total


def _scan(paths: list[str], with_size: bool) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for d in md.distributions(path=paths):
        try:
            name = d.metadata["Name"]
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        c = S.canon(name)
        if c in out:
            continue
        rec = {"name": name, "canon": c, "version": d.version,
               "summary": (d.metadata.get("Summary") or "")[:300],
               "requires": d.requires or [], "_dist": d}
        if with_size:
            rec["size"] = _dist_size(d)
        out[c] = rec
    return out


def user_dists(with_size: bool = False) -> dict[str, dict]:
    if not PY_USER_SITE.is_dir():
        return {}
    return _scan([str(PY_USER_SITE)], with_size)


_base_cache: dict = {}


def base_dists() -> dict[str, dict]:
    paths = _base_paths()
    try:
        stamp = tuple(os.stat(p).st_mtime for p in paths)
    except OSError:
        stamp = ()
    if _base_cache.get("stamp") != stamp:
        _base_cache["data"] = _scan(paths, False)
        _base_cache["stamp"] = stamp
    return _base_cache["data"]


def _public(rec: dict) -> dict:
    return {k: v for k, v in rec.items() if not k.startswith("_") and k != "requires"}


def list_user() -> list[dict]:
    users = user_dists(with_size=True)
    base = base_dists()
    man = MANIFEST.entries()
    required_by = _reverse_deps(users, base)
    out = []
    for c, rec in users.items():
        m = man.get(c)
        r = _public(rec)
        r.update({
            "pinned": bool(m and m["pinned"]),
            "requested": bool(m),
            "spec": m["spec"] if m else None,
            "platform_version": base[c]["version"] if c in base else None,
            "required_by": sorted(required_by.get(c, []))[:20],
        })
        out.append(r)
    # manifest entries that are not installed (e.g. after a failed restore)
    for c, m in man.items():
        if c not in users:
            out.append({"name": m["name"], "canon": c, "version": None, "summary": "",
                        "size": None, "pinned": m["pinned"], "requested": True, "spec": m["spec"],
                        "platform_version": base[c]["version"] if c in base else None,
                        "missing": True, "required_by": []})
    out.sort(key=lambda r: r["name"].lower())
    return out


def list_platform() -> list[dict]:
    users = user_dists()
    return sorted(({**_public(r), "overridden_by": users[c]["version"] if c in users else None}
                   for c, r in base_dists().items()), key=lambda r: r["name"].lower())


def _reverse_deps(users: dict, base: dict) -> dict[str, set]:
    rev: dict[str, set] = {}
    if Requirement is None:
        return rev
    env = default_environment()
    env["extra"] = ""
    for c, rec in {**base, **users}.items():
        for req in rec.get("requires") or []:
            try:
                r = Requirement(req)
                if r.marker and not r.marker.evaluate(env):
                    continue
            except Exception:  # noqa: BLE001
                continue
            rev.setdefault(S.canon(r.name), set()).add(rec["name"])
    return rev


def orphans() -> list[dict]:
    """User-layer packages nothing requires and nobody asked for."""
    users = user_dists(with_size=True)
    man = MANIFEST.entries()
    keep = set(man)
    rev = _reverse_deps(users, base_dists())
    # iterate: removing an orphan can orphan its deps
    removed: set[str] = set()
    changed = True
    while changed:
        changed = False
        for c in users:
            if c in keep or c in removed:
                continue
            parents = {S.canon(p) for p in rev.get(c, set())} - removed - {c}
            if not parents:
                removed.add(c)
                changed = True
    return [_public(users[c]) for c in sorted(removed)]


# ── PyPI ─────────────────────────────────────────────────────────────────
_pypi_cache: dict[str, tuple[float, dict]] = {}
_session = requests.Session()


def _classify(exc: Exception) -> str:
    s = str(exc)
    if isinstance(exc, requests.exceptions.ProxyError) or "Tunnel connection failed" in s or "403" in s:
        return "PyPI is blocked by the egress proxy — ask an admin to allow pypi.org, or type an exact name to install from your configured index."
    if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
        return "PyPI is unreachable (offline?). You can still type an exact requirement and install it."
    return f"PyPI lookup failed: {s[:200]}"


def _vkey(v: str):
    try:
        return (0, Version(v))
    except Exception:  # noqa: BLE001
        return (-1, v)


def pypi_info(name: str) -> dict:
    name = S.py_name(name)
    c = S.canon(name)
    hit = _pypi_cache.get(c)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        r = _session.get(f"{PYPI_JSON}/{c}/json", timeout=(4, 12))
    except Exception as exc:  # noqa: BLE001
        return {"name": name, "found": False, "offline": True, "message": _classify(exc)}
    if r.status_code == 404:
        res = {"name": name, "found": False, "offline": False, "message": f"No project named “{name}” on PyPI."}
        _pypi_cache[c] = (time.time(), res)
        return res
    if r.status_code != 200:
        return {"name": name, "found": False, "offline": True,
                "message": f"PyPI returned HTTP {r.status_code}" + (" (blocked by egress proxy?)" if r.status_code in (403, 407) else "")}
    data = r.json()
    info = data.get("info") or {}
    rel = data.get("releases") or {}
    versions = []
    for v, files in rel.items():
        if files and all(f.get("yanked") for f in files):
            continue
        if not files:
            continue
        versions.append(v)
    versions.sort(key=_vkey, reverse=True)
    stable = [v for v in versions if Version and not _is_pre(v)]
    latest = info.get("version") or (stable[0] if stable else (versions[0] if versions else None))
    uploaded = None
    if latest and rel.get(latest):
        uploaded = rel[latest][0].get("upload_time_iso_8601")
    res = {
        "name": info.get("name") or name, "found": True, "offline": False,
        "latest": latest, "summary": (info.get("summary") or "")[:300],
        "versions": versions[:150], "requires_python": info.get("requires_python"),
        "home": info.get("home_page") or (info.get("project_urls") or {}).get("Homepage"),
        "url": info.get("package_url") or f"https://pypi.org/project/{c}/",
        "license": (info.get("license") or "")[:80] if len(info.get("license") or "") < 80 else None,
        "uploaded": uploaded,
    }
    _pypi_cache[c] = (time.time(), res)
    return res


def _is_pre(v: str) -> bool:
    try:
        return Version(v).is_prerelease
    except Exception:  # noqa: BLE001
        return True


# PyPI name index (for search-as-you-type). PyPI has no search API; the simple
# index lists every project name. Downloaded lazily, cached on disk 24h.
_INDEX_FILE = STATE_DIR / "pypi-names.txt"
_index = {"blob": "", "status": "idle", "loaded": 0.0, "message": ""}
_index_lock = threading.Lock()


def _load_index_bg() -> None:
    try:
        if _INDEX_FILE.exists() and time.time() - _INDEX_FILE.stat().st_mtime < 86400:
            _index["blob"] = "\n" + _INDEX_FILE.read_text(encoding="utf-8") + "\n"
            _index.update(status="ready", loaded=time.time())
            return
        r = _session.get(PYPI_SIMPLE, headers={"Accept": "application/vnd.pypi.simple.v1+json"},
                         timeout=(5, 120))
        r.raise_for_status()
        text = r.text
        names = re.findall(r'"name"\s*:\s*"([^"]+)"', text) if "json" in r.headers.get("content-type", "") \
            else re.findall(r">([^<>\s]+)</a>", text)
        del text
        names = sorted({S.canon(n) for n in names})
        blob = "\n".join(names)
        _INDEX_FILE.write_text(blob, encoding="utf-8")
        _index["blob"] = "\n" + blob + "\n"
        _index.update(status="ready", loaded=time.time())
    except Exception as exc:  # noqa: BLE001
        if _INDEX_FILE.exists():
            _index["blob"] = "\n" + _INDEX_FILE.read_text(encoding="utf-8") + "\n"
            _index.update(status="ready", loaded=time.time(), message="stale cache")
        else:
            _index.update(status="unavailable", message=_classify(exc), loaded=time.time())


def _ensure_index() -> str:
    if os.environ.get("WORKBENCH_PYPI_INDEX", "on") == "off":
        return "disabled"
    with _index_lock:
        st = _index["status"]
        if st == "idle" or (st == "unavailable" and time.time() - _index["loaded"] > 300):
            _index["status"] = "loading"
            threading.Thread(target=_load_index_bg, daemon=True).start()
        return _index["status"]


def _index_search(q: str, limit: int) -> list[str]:
    blob = _index["blob"]
    if not blob:
        return []
    q = S.canon(q)
    out: list[str] = []
    seen = set()
    i = blob.find("\n" + q)
    while i != -1 and len(out) < limit:
        j = blob.find("\n", i + 1)
        n = blob[i + 1:j]
        if n not in seen:
            out.append(n)
            seen.add(n)
        i = blob.find("\n" + q, j)
    if len(out) < limit and len(q) >= 3:
        i = blob.find(q)
        while i != -1 and len(out) < limit:
            s = blob.rfind("\n", 0, i)
            e = blob.find("\n", i)
            n = blob[s + 1:e]
            if n and n not in seen:
                out.append(n)
                seen.add(n)
            i = blob.find(q, e)
    out.sort(key=lambda n: (n != q, not n.startswith(q), len(n)))
    return out[:limit]


def search(q: str) -> dict:
    q = (q or "").strip()
    if not q or not S.PY_NAME.match(q):
        return {"query": q, "exact": None, "suggestions": [], "local": [], "index": _index["status"]}
    status = _ensure_index()
    exact = pypi_info(q)
    users = user_dists()
    base = base_dists()
    cq = S.canon(q)
    local, seen = [], set()
    for c, r in list(users.items()) + list(base.items()):
        if cq in c and c not in seen:
            seen.add(c)
            local.append({"name": r["name"], "canon": c, "version": r["version"],
                          "layer": "user" if c in users else "platform"})
    local.sort(key=lambda x: (x["canon"] != cq, not x["canon"].startswith(cq), x["layer"] != "user", len(x["canon"])))
    local = local[:6]
    sugg = _index_search(q, 12) if status == "ready" else []
    return {"query": q, "exact": exact, "suggestions": sugg, "local": local, "index": status,
            "index_message": _index.get("message", "")}


# ── resolve / install ────────────────────────────────────────────────────
def _env() -> dict:
    env = os.environ.copy()
    env["PYTHONUSERBASE"] = str(PY_USERBASE)
    pp = [str(PY_USER_SITE)] + [p for p in env.get("PYTHONPATH", "").split(":") if p and p != str(PY_USER_SITE)]
    env["PYTHONPATH"] = ":".join(pp)
    env.setdefault("UV_NO_PROGRESS", "1")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    return env


def _run(argv: list[str], job: Job | None, timeout: int = 900) -> tuple[int, str]:
    if job is not None:
        start = len(job.lines) + job.dropped
        rc = job.run(argv, env=_env(), check=False, timeout=timeout)
        lines, _ = job.lines_from(start + 1)  # skip the echoed "$ cmd" line
        return rc, "\n".join(lines)
    try:
        p = subprocess.run(argv, env=_env(), capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except FileNotFoundError:
        return 127, f"command not found: {argv[0]}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


_PLUS = re.compile(r"^\s*\+\s+([A-Za-z0-9._-]+)==(\S+)")


def _cmp(a: str | None, b: str | None) -> int:
    if not a or not b or Version is None:
        return 0
    try:
        va, vb = Version(a), Version(b)
    except InvalidVersion:
        return 0
    return (va > vb) - (va < vb)


def plan(spec_texts: list[str], upgrade: list[str] | None = None, job: Job | None = None) -> dict:
    """Dry-run resolution → list of user-layer changes + warnings."""
    specs = [S.py_spec(s) for s in spec_texts]
    if not specs:
        raise S.SpecError("nothing to install")
    users = user_dists()
    base = base_dists()
    asked = {s["canon"] for s in specs} | {S.canon(u) for u in (upgrade or [])}
    tool = installer()
    relaxed = False
    resolved: list[tuple[str, str]] = []
    log = ""
    if tool == "uv":
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            for c, r in users.items():
                if c not in asked:
                    fh.write(f"{r['name']}=={r['version']}\n")
            cfile = fh.name
        try:
            base_cmd = ["uv", "pip", "install", "--dry-run", "--python", PYTHON, *INDEX_ARGS]
            for u in upgrade or []:
                base_cmd += ["--upgrade-package", u]
            rc, out = _run(base_cmd + ["-c", cfile] + [s["text"] for s in specs], job, 600)
            log = out
            if rc != 0 and users and re.search(r"unsatisfiable|No solution found", out):
                relaxed = True
                if job:
                    job.log("Constrained resolve failed — retrying while allowing other user packages to change…")
                rc, out = _run(base_cmd + [s["text"] for s in specs], job, 600)
                log += "\n" + out
        finally:
            os.unlink(cfile)
        if rc != 0:
            return {"ok": False, "installer": tool, "error": _tail_error(out), "log": log, "changes": [], "warnings": []}
        for line in out.splitlines():
            m = _PLUS.match(line)
            if m:
                resolved.append((m.group(1), m.group(2)))
    else:
        with tempfile.TemporaryDirectory() as td:
            rep = os.path.join(td, "report.json")
            rc, out = _run([PYTHON, "-m", "pip", "install", "--user", "--dry-run", "--quiet",
                            "--report", rep, *INDEX_ARGS] + [s["text"] for s in specs], job, 600)
            log = out
            if rc != 0:
                return {"ok": False, "installer": tool, "error": _tail_error(out), "log": log, "changes": [], "warnings": []}
            try:
                data = json.load(open(rep, encoding="utf-8"))
            except (OSError, ValueError):
                data = {"install": []}
            for it in data.get("install", []):
                meta = it.get("metadata") or {}
                if meta.get("name"):
                    resolved.append((meta["name"], meta.get("version", "")))

    changes, warnings = [], []
    for name, ver in resolved:
        c = S.canon(name)
        u = users.get(c, {}).get("version")
        b = base.get(c, {}).get("version")
        if u == ver:
            continue
        ref = u or b
        if not ref:
            action = "add"
        else:
            d = _cmp(ver, ref)
            action = "upgrade" if d > 0 else "downgrade" if d < 0 else "reinstall"
        ch = {"name": name, "canon": c, "version": ver, "from": ref,
              "from_layer": "user" if u else ("platform" if b else None),
              "action": action, "requested": c in asked}
        changes.append(ch)
        if b and not u:
            sev = "high" if c in CRITICAL else "medium"
            if _cmp(ver, b) < 0:
                warnings.append({"severity": "high", "package": name,
                                 "message": f"Downgrades platform {name} {b} → {ver} (your layer will shadow the image version)."})
            else:
                warnings.append({"severity": sev, "package": name,
                                 "message": f"Shadows platform {name} {b} with {ver}" +
                                 (" — this package is used by the notebook servers; things may break." if c in CRITICAL else ".")})
        elif u and _cmp(ver, u) < 0:
            warnings.append({"severity": "medium", "package": name,
                             "message": f"Downgrades {name} in your layer {u} → {ver}."})
    if relaxed:
        moved = [c["name"] for c in changes if c["from_layer"] == "user" and not c["requested"]]
        if moved:
            warnings.append({"severity": "medium", "package": None,
                             "message": "To satisfy this request these packages in your layer also change: " + ", ".join(moved)})
    return {"ok": True, "installer": tool, "changes": changes, "warnings": warnings,
            "relaxed": relaxed, "log": log, "specs": [s["text"] for s in specs]}


def _tail_error(out: str) -> str:
    lines = [ln for ln in out.strip().splitlines() if ln.strip()]
    errs = [ln for ln in lines if re.search(r"error|cause|because|ERROR", ln)]
    return "\n".join((errs or lines)[-8:])[:2000] or "resolution failed"


# ── pip check (diffed against platform baseline) ──────────────────────────
_baseline: dict = {}


def _pip_check(user: bool) -> list[str]:
    env = _env()
    if not user:
        env["PYTHONNOUSERSITE"] = "1"
        env["PYTHONPATH"] = ":".join(p for p in env["PYTHONPATH"].split(":") if p != str(PY_USER_SITE))
    try:
        p = subprocess.run([PYTHON, "-m", "pip", "check"], env=env, capture_output=True, text=True,
                           timeout=180, stdin=subprocess.DEVNULL)
    except Exception as exc:  # noqa: BLE001
        return [f"pip check failed: {exc}"]
    out = (p.stdout or "") + (p.stderr or "")
    return [ln.strip() for ln in out.splitlines()
            if ln.strip() and "No broken requirements" not in ln]


def check() -> dict:
    stamp = tuple(os.stat(p).st_mtime for p in _base_paths() if os.path.exists(p))
    if _baseline.get("stamp") != stamp:
        _baseline["lines"] = set(_pip_check(False))
        _baseline["stamp"] = stamp
    now = _pip_check(True)
    new = [ln for ln in now if ln not in _baseline["lines"]]
    return {"conflicts": new, "baseline": len(_baseline["lines"]), "ok": not new}


# ── jobs ─────────────────────────────────────────────────────────────────
def _after(job: Job) -> None:
    job.log("")
    job.log("Checking environment consistency (pip check)…")
    res = check()
    job.result["conflicts"] = res["conflicts"]
    if res["conflicts"]:
        job.log(f"⚠ {len(res['conflicts'])} new dependency conflict(s):")
        for ln in res["conflicts"]:
            job.log("  " + ln)
    else:
        job.log("No new conflicts" + (f" ({res['baseline']} pre-existing in the platform image are ignored)" if res["baseline"] else "") + ".")


def install_job(spec_texts: list[str], pin: list[str] | None = None, upgrade: list[str] | None = None,
                record: bool = True, reason: str = "install"):
    specs = [S.py_spec(s) for s in spec_texts]
    pin_set = {S.canon(p) for p in (pin or [])}
    upgrade = [S.py_name(u) for u in (upgrade or [])]

    def fn(job: Job) -> dict:
        PY_USER_SITE.mkdir(parents=True, exist_ok=True)
        tool = installer()
        job.log(f"Installer: {tool} · target: {PY_USERBASE}")
        if tool == "uv":
            job.log("Resolving against the platform environment…")
            p = plan([s["text"] for s in specs], upgrade, job)
            if not p["ok"]:
                raise JobError("could not resolve: " + p["error"].splitlines()[-1] if p["error"] else "resolution failed")
            for w in p["warnings"]:
                job.log(f"⚠ {w['message']}")
            todo = [f"{c['name']}=={c['version']}" for c in p["changes"]]
            if not todo:
                job.log("Everything requested is already satisfied.")
            else:
                job.log("")
                job.log(f"Installing {len(todo)} package(s) into your layer…")
                job.run(["uv", "pip", "install", "--python", PYTHON, "--prefix", str(PY_USERBASE),
                         "--no-deps", *INDEX_ARGS, *todo], env=_env(), timeout=3600)
            job.result["changes"] = p["changes"]
        else:
            job.run([PYTHON, "-m", "pip", "install", "--user", *INDEX_ARGS,
                     *(["--upgrade"] if upgrade else []), *[s["text"] for s in specs]],
                    env=_env(), timeout=3600)
        if record:
            users = user_dists()

            def upd(e: dict) -> None:
                for s in specs:
                    inst = users.get(s["canon"], {}).get("version") or base_dists().get(s["canon"], {}).get("version")
                    old = e.get(s["canon"])
                    pinned = s["canon"] in pin_set or bool(old and old["pinned"] and reason in ("upgrade", "restore"))
                    if pinned and inst:
                        e[s["canon"]] = {"name": s["name"], "spec": f"{s['name']}{s['extras']}=={inst}", "pinned": True}
                    elif reason == "upgrade" and old:
                        e[s["canon"]] = {**old, "pinned": False} if not old["pinned"] else old
                    else:
                        e[s["canon"]] = {"name": s["name"], "spec": s["text"], "pinned": False}
            MANIFEST.update(upd)
            job.log(f"Manifest updated: {PY_MANIFEST}")
        _after(job)
        return {"restart_hint": True}
    return fn


def remove_job(names: list[str], forget: bool = True):
    names = [S.py_name(n) for n in names]

    def fn(job: Job) -> dict:
        users = user_dists()
        present = [n for n in names if S.canon(n) in users]
        missing = [n for n in names if S.canon(n) not in users]
        for n in missing:
            job.log(f"• {n} is not in your layer (platform packages are read-only) — skipped")
        if present:
            if installer() == "uv":
                job.run(["uv", "pip", "uninstall", "--python", PYTHON, "--prefix", str(PY_USERBASE), *present],
                        env=_env(), timeout=600)
            else:
                job.run([PYTHON, "-m", "pip", "uninstall", "-y", *present], env=_env(), timeout=600)
        if forget:
            gone = {S.canon(n) for n in names}
            MANIFEST.update(lambda e: [e.pop(c, None) for c in gone])
            job.log("Manifest updated.")
        _after(job)
        return {"removed": present, "restart_hint": True}
    return fn


def restore_job(text: str | None = None):
    def fn(job: Job) -> dict:
        if text is not None:
            MANIFEST.write_text(text if text.endswith("\n") else text + "\n")
            job.log(f"Wrote manifest {PY_MANIFEST}")
        entries = MANIFEST.entries()
        if not entries:
            job.log("Manifest is empty — nothing to restore.")
            return {}
        specs = [e["spec"] for e in entries.values()]
        job.log(f"Restoring {len(specs)} requirement(s)…")
        inner = install_job(specs, pin=[e["name"] for e in entries.values() if e["pinned"]],
                            record=False, reason="restore")
        return inner(job)
    return fn


def set_pin(names: list[str], pinned: bool) -> list[dict]:
    users = user_dists()
    base = base_dists()
    changed = []

    def upd(e: dict) -> None:
        for n in names:
            n = S.py_name(n)
            c = S.canon(n)
            ver = users.get(c, {}).get("version")
            if pinned:
                if not ver:
                    if c in base:
                        raise S.SpecError(f"{n} is a platform package — install it into your layer to pin it")
                    raise S.SpecError(f"{n} is not installed")
                nm = users[c]["name"]
                old = e.get(c)
                extras = S.py_spec(old["spec"])["extras"] if old else ""
                e[c] = {"name": nm, "spec": f"{nm}{extras}=={ver}", "pinned": True}
            else:
                old = e.get(c)
                if old:
                    sp = S.py_spec(old["spec"])
                    e[c] = {"name": sp["name"], "spec": f"{sp['name']}{sp['extras']}", "pinned": False}
            changed.append(e.get(c))
    MANIFEST.update(upd)
    return [c for c in changed if c]


def manifest_text() -> str:
    return MANIFEST.read_text()
