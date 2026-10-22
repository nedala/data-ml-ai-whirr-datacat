"""Executive Story: live proof numbers for the guided narrative page.

GET /studio/api/story → {"generated_at", "cached", "chapters": {id: chapter}}

Every chapter is {"state": live|partial|absent, "tiles": [tile], "links": [link]}
and every tile is {"id", "label", "value", "detail", "state": ok|absent|error}.
Numbers are aggregates only (counts, versions, scores): no names, e-mail
addresses, account numbers or secrets ever leave this module. Each service is
asked over the internal network with a short timeout, all in parallel, and the
whole payload is cached for CACHE_SECONDS. A component that is not part of
this appliance (or is down) degrades its tiles to state "absent" instead of
failing the page. The narrative text itself lives in static/pages/story.js.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path
from typing import Any, Callable

import requests
from fastapi import APIRouter, Depends

import audit
import auth
import catalog
import composefile
import dockerops
import orchestrator
import settings
from vault import store

log = logging.getLogger("studio.story")
router = APIRouter(prefix="/studio/api")

CACHE_SECONDS = 60.0
TIMEOUT = float(os.environ.get("STORY_TIMEOUT", "3"))
BUDGET = 8.0   # seconds for the whole fan-out; stragglers degrade to "error"

# In-network base URLs (same upstreams nginx uses; overridable for tests).
URLS = {
    "canvas": "http://canvas:8093/canvas",
    "superset": "http://superset:8088/superset",
    "mlops": "http://mlops:8097/mlops",
    "disputes": "http://disputes:8099/disputes",
    "ccai": "http://ccai:8100/ccai",
    "harness": "http://harness:8096/ai",
    "rag": "http://rag:8101/rag",
}
for _k in list(URLS):
    URLS[_k] = os.environ.get(f"STORY_URL_{_k.upper()}", URLS[_k]).rstrip("/")

# Open-source building blocks shown in "Own it" (component → licence family).
OSS = [
    ("PostgreSQL", "PostgreSQL"), ("dbt Core", "Apache-2.0"), ("dlt", "Apache-2.0"), ("Trino", "Apache-2.0"),
    ("Apache Superset", "Apache-2.0"), ("MLflow", "Apache-2.0"), ("LightGBM", "MIT"), ("scikit-learn", "BSD-3"),
    ("JupyterLab", "BSD-3"), ("code-server", "MIT"), ("Ollama", "MIT"), ("Open WebUI", "BSD-3"),
    ("Qdrant", "Apache-2.0"), ("pgvector", "PostgreSQL"), ("MinIO", "AGPL-3.0"), ("Playwright", "Apache-2.0"),
    ("FastAPI", "MIT"), ("nginx", "BSD-2"), ("MCP", "MIT"), ("DataHub", "Apache-2.0"),
]

_cache: dict[str, Any] = {"at": 0.0, "data": None}
_lock = threading.Lock()


# ── tile helpers ─────────────────────────────────────────────────────────
def tile(tid: str, label: str, value: Any, detail: str = "", state: str = "ok") -> dict[str, Any]:
    return {"id": tid, "label": label, "value": value, "detail": detail, "state": state}


def absent(tid: str, label: str, why: str = "Not running in this appliance") -> dict[str, Any]:
    return tile(tid, label, None, why, "absent")


def _n(x: Any) -> str:
    """12345 → '12,345'; floats keep up to 3 significant decimals."""
    if isinstance(x, bool) or x is None:
        return str(x)
    if isinstance(x, int):
        return f"{x:,}"
    if isinstance(x, float):
        return f"{x:,.3f}".rstrip("0").rstrip(".") if x < 10 else f"{x:,.0f}"
    return str(x)


def _session() -> requests.Session:
    s = requests.Session()
    s.trust_env = False   # appliance-internal: never via px-proxy
    s.headers.update({"X-Auth-User": "studio-story", "X-Auth-Role": "viewer", "Accept": "application/json"})
    return s


def _get(svc: str, path: str, session: requests.Session | None = None, **kw) -> Any:
    s = session or _session()
    r = s.get(URLS[svc] + path, timeout=kw.pop("timeout", TIMEOUT), **kw)
    r.raise_for_status()
    return r.json()


def _status() -> dict[str, dict[str, Any]]:
    try:
        return dockerops.status()
    except Exception:  # noqa: BLE001 — no docker in tests / socket gone
        return {}


def _up(status: dict[str, dict[str, Any]], svc: str) -> bool:
    return status.get(svc, {}).get("state") in orchestrator.UP


def _links(status: dict[str, dict[str, Any]], *app_ids: str) -> list[dict[str, Any]]:
    """Deep links to catalog apps, in the given order, with availability."""
    apps = {a["id"]: a for a in catalog.load().get("apps", [])}
    out = []
    for aid in app_ids:
        app = apps.get(aid)
        if not app:
            continue
        out.append({"id": aid, "label": app.get("title", aid), "href": composefile._interpolate(str(app.get("path", ""))),
                    "up": _up(status, app.get("service", ""))})
    return out


def _chapter(tiles: list[dict[str, Any]], links: list[dict[str, Any]] | None = None, **extra) -> dict[str, Any]:
    ok = sum(1 for t in tiles if t["state"] == "ok")
    state = "live" if tiles and ok == len(tiles) else "partial" if ok else "absent"
    return {"state": state, "tiles": tiles, "links": links or [], **extra}


# ── per-chapter probes (each returns a chapter dict; exceptions → error tiles) ──
def ch_ask(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cat = catalog.load()
    services = cat["services"]
    running = [s for s in services if _up(st, s)]
    apps = [a for a in cat.get("apps", []) if _up(st, a.get("service", ""))]
    tiles = []
    if st:
        tiles.append(tile("running", "Services running right now", len(running),
                          f"of {len(services)} in the catalog · plus the always-on control plane"))
        tiles.append(tile("tools", "Self-service tools open right now", len(apps),
                          "behind one sign-in and one front door"))
    else:
        tiles.append(absent("running", "Services running right now", "Docker is not reachable from Studio"))
    tiles.append(tile("personas", "Personas served by one stack", len(cat.get("personas", {})),
                      " · ".join(v.get("title", k) for k, v in cat.get("personas", {}).items())))
    info = {}
    try:
        info = dockerops.info() if st else {}
    except Exception:  # noqa: BLE001
        info = {}
    if info.get("NCPU"):
        tiles.append(tile("host", "Current capacity", f"{info['NCPU']} vCPU · {round(int(info.get('MemTotal', 0)) / 2**30)} GiB",
                          "this deployment. The same images run anywhere containers run."))
    return _chapter(tiles, _links(st, "canvas", "superset", "mlops", "ai-harness"))


def ch_connect(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "canvas", "connectors", "ingress")
    if not _up(st, "canvas"):
        return _chapter([absent("pipelines", "No-code pipelines"), absent("rows", "Rows landed"),
                         absent("sources", "Source systems connected")], links)
    s = _session()
    pipes = _get("canvas", "/api/pipelines", s).get("pipelines", [])
    runs = _get("canvas", "/api/runs", s).get("runs", [])
    conns = _get("canvas", "/api/connections", s).get("connections", [])
    healthy = sum(1 for p in pipes if (p.get("last_run") or {}).get("status") == "success")
    rows = sum(int(x.get("rows") or 0) for r in runs for x in (r.get("loaded") or []))
    targets = {x.get("target") for r in runs for x in (r.get("loaded") or []) if x.get("target")}
    # connection types (only names/types are read — never host or user params)
    # plus API-style sources configured on the pipelines themselves
    kinds = {str(c["type"]) for c in conns if c.get("type") and not c.get("builtin")}
    kinds |= {k.split(".", 1)[-1] for p in pipes for k in (p.get("sources") or [])
              if k not in ("source.database", "source.storage")}
    nice = {"oracle": "Oracle", "mssql": "SQL Server", "gcs": "GCS", "s3": "S3 / MinIO", "bigquery": "BigQuery",
            "postgres": "Postgres", "outlook": "Outlook", "sharepoint": "SharePoint", "rest": "REST APIs"}
    ok_runs = sum(1 for r in runs if r.get("status") == "success")
    return _chapter([
        tile("pipelines", "No-code pipelines", len(pipes), f"{healthy} healthy on their last run"),
        tile("rows", "Rows landed (recent runs)", _n(rows), f"into {len(targets)} landing tables"),
        tile("runs", "Pipeline runs", len(runs), f"{ok_runs} succeeded"),
        tile("sources", "Source types connected", len(kinds),
             ", ".join(sorted(nice.get(k, k) for k in kinds))[:120]),
    ], links)


def _dbt_paths() -> tuple[Path, Path]:
    root = Path(os.environ.get("STORY_DBT_DIR") or (settings.cfg.project / "dbt_project"))
    return root / "target" / "manifest.json", root / "logs" / "dbt.log"


_DONE = re.compile(r"Done\. PASS=(\d+) WARN=(\d+) ERROR=(\d+) SKIP=(\d+)(?: NO-OP=(\d+))? TOTAL=(\d+)")


def _last_dbt_done(path: Path) -> dict[str, int] | None:
    """Last 'Done. PASS=…' summary in dbt.log (reads only the tail)."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4 * 2**20))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    best = None
    for m in _DONE.finditer(text):
        vals = [int(v or 0) for v in m.groups()]
        if vals[5] > 2:   # skip one-off seeds/snapshots; keep the last real build/test
            best = dict(zip(("pass", "warn", "error", "skip", "noop", "total"), vals))
    return best


def ch_model(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "dbt", "trino", "datahub")
    manifest_p, log_p = _dbt_paths()
    tiles = []
    try:
        m = json.loads(manifest_p.read_text(encoding="utf-8"))
        nodes = m.get("nodes", {}).values()
        models = [n for n in nodes if n.get("resource_type") == "model"]
        tests = sum(1 for n in nodes if n.get("resource_type") == "test")
        layers: dict[str, int] = {}
        for n in models:
            layer = (n.get("config") or {}).get("schema") or n.get("schema") or "other"
            layers[layer] = layers.get(layer, 0) + 1
        order = ["staging", "intermediate", "marts"]
        lay = " · ".join(f"{layers[k]} {k}" for k in sorted(layers, key=lambda k: (order.index(k) if k in order else 9, k)))
        tiles.append(tile("models", "Medallion models on BAN", len(models), lay))
        tiles.append(tile("tests", "Data tests defined", tests, f"{len(m.get('sources', {}))} governed sources"))
        edges = sum(len(v) for v in (m.get("parent_map") or {}).values())
        if edges:
            tiles.append(tile("lineage", "Lineage links (dbt graph)", _n(edges), "browse lineage and docs in dbt Studio"))
    except (OSError, ValueError):
        tiles.append(absent("models", "Medallion models on BAN", "dbt project has not been compiled yet"))
    done = _last_dbt_done(log_p)
    if done:
        tiles.insert(1, tile("passing", "Last dbt build", f"{_n(done['pass'])} passed",
                             f"{done['error']} errors · {done['warn']} warnings · {done['total']} nodes",
                             "ok" if not done["error"] else "error"))
    else:
        tiles.insert(1, absent("passing", "Last dbt build", "No dbt build recorded yet"))
    dh = False
    try:
        dh = bool(st) and any("datahub-gms" in c.name for c in dockerops.client().containers.list(
            filters={"name": "datahub-gms"}))
    except Exception:  # noqa: BLE001
        dh = False
    for link in links:
        if link["id"] == "datahub":
            link["up"] = dh
    return _chapter(tiles[:4], links, datahub=dh)


def _superset_token(s: requests.Session) -> str | None:
    try:
        pw = store.get("services.superset.SUPERSET_ADMIN_PASS", "") if store.is_unlocked() else ""
    except Exception:  # noqa: BLE001 — locked / missing
        pw = ""
    if not pw:
        return None
    r = s.post(URLS["superset"] + "/api/v1/security/login", timeout=TIMEOUT,
               json={"username": os.environ.get("SUPERSET_ADMIN_USER", "admin"), "password": pw, "provider": "db",
                     "refresh": False})
    if r.status_code != 200:
        return None
    return r.json().get("access_token")


def ch_see(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "superset")
    if not _up(st, "superset"):
        return _chapter([absent("dashboards", "Dashboards"), absent("charts", "Charts"),
                         absent("datasets", "Governed datasets")], links)
    s = _session()
    token = _superset_token(s)
    if not token:
        return _chapter([tile("dashboards", "Dashboards", None, "Superset is up; unlock the vault to count assets", "error")],
                        links)
    s.headers["Authorization"] = f"Bearer {token}"
    q = {"q": "(page_size:100,columns:!(dashboard_title,published))"}
    dash = _get("superset", "/api/v1/dashboard/", s, params=q)
    charts = _get("superset", "/api/v1/chart/", s, params={"q": "(page_size:1)"})
    dsets = _get("superset", "/api/v1/dataset/", s, params={"q": "(page_size:1)"})
    titles = [d.get("dashboard_title", "") for d in dash.get("result", []) if d.get("dashboard_title")]
    return _chapter([
        tile("dashboards", "Dashboards", int(dash.get("count", len(titles))), ", ".join(titles[:5])[:140]),
        tile("charts", "Charts", int(charts.get("count", 0)), "self-service in Explore and SQL Lab"),
        tile("datasets", "Governed datasets", int(dsets.get("count", 0)), "role-based access through single sign-on"),
    ], links)


def ch_predict(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "mlops", "mlflow", "model-gateway")
    if not _up(st, "mlops"):
        return _chapter([absent("champion", "Champion churn model"), absent("serving", "Live serving"),
                         absent("drift", "Drift watch")], links)
    s = _session()
    ov = _get("mlops", "/api/overview", s)
    tiles = []
    champ = (ov.get("aliases") or {}).get("champion")
    auc = None
    try:
        j = _get("mlops", "/api/journeys", s)
        for mdl in j.get("models", []):
            for v in mdl.get("versions", []):
                if str(v.get("version")) == str(champ):
                    auc = v.get("test_auc")
        n_journeys = len(j.get("journeys", []))
    except Exception:  # noqa: BLE001 — older tower without journeys
        n_journeys = 0
    chall = (ov.get("aliases") or {}).get("challenger")
    tiles.append(tile("champion", "Champion churn model",
                      f"v{champ}" if champ else "none yet",
                      (f"AUC {auc:.3f}" if isinstance(auc, (int, float)) else "AUC n/a")
                      + (f" · challenger v{chall}" if chall else ""), "ok" if champ else "error"))
    gw = ov.get("gateway") or {}
    traffic = ov.get("traffic") or {}
    mode = {"single": "single model", "blue_green": "blue / green", "canary": "canary", "shadow": "shadow"}.get(
        gw.get("mode"), gw.get("mode") or "n/a")
    tiles.append(tile("serving", "Live serving", mode, f"{_n(int(traffic.get('total') or 0))} scoring requests served"))
    drift = ov.get("drift") or {}
    rt = ov.get("retrain") or {}
    if drift:
        reasons = drift.get("breach_reasons") or []
        why = (reasons[0] if reasons else f"max PSI {drift.get('max_psi')}")[:80]
        if drift.get("breached") and rt.get("status") == "succeeded":
            what = "promoted" if rt.get("promoted") else "trained as challenger"
            value, detail = "auto-retrained", f"drift caught ({why}) · v{rt.get('model_version')} {what}"
        elif drift.get("breached"):
            value, detail = "drift detected", why + (f" · retrain {rt.get('status')}" if rt else "")
        else:
            value, detail = "stable", f"max PSI {drift.get('max_psi')} · checked every {(ov.get('settings') or {}).get('drift_interval_min', '?')} min"
        tiles.append(tile("drift", "Drift watch", value, detail))
    scored = ov.get("scored") or {}
    if scored.get("n"):
        tiles.append(tile("scored", "Customers scored", _n(int(scored["n"])),
                          f"batch for {scored.get('last_month', '')[:7]} · {n_journeys} audited journeys"))
    return _chapter(tiles[:4], links)


def ch_automate(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "disputes", "ccai", "connectors")
    tiles = []
    if _up(st, "disputes"):
        cases = _get("disputes", "/api/cases", params={"limit": 5000})
        by: dict[str, int] = {}
        for c in cases:
            by[c.get("status") or "?"] = by.get(c.get("status") or "?", 0) + 1
        sap = sum(1 for c in cases if c.get("sap_dispute_id"))
        auto = sum(1 for c in cases if c.get("auto_approved"))
        tiles.append(tile("disputes", "Dispute e-mails triaged", len(cases),
                          f"{auto} auto-approved · {by.get('needs_review', 0)} with a reviewer · {by.get('not_dispute', 0)} not disputes"))
        tiles.append(tile("sap", "Cases posted to SAP BRIM", sap,
                          f"{by.get('approved', 0)} more approved and ready to post (browser bridge)"))
    else:
        tiles += [absent("disputes", "Dispute e-mails triaged"), absent("sap", "Cases posted to SAP BRIM")]
    if _up(st, "ccai"):
        cst = _get("ccai", "/api/status")
        calls = (cst.get("counts") or {}).get("calls") or {}
        tags = (cst.get("counts") or {}).get("tags") or {}
        leaves = (cst.get("taxonomy") or {}).get("leaves")
        tiles.append(tile("calls", "Calls tagged", _n(int(calls.get("tagged") or 0)),
                          f"of {_n(int(calls.get('calls') or 0))} · {leaves or '?'}-leaf taxonomy"))
        acc = None
        try:
            acc = ((_get("ccai", "/api/quality").get("summary")) or {}).get("accuracy")
        except Exception:  # noqa: BLE001
            acc = None
        if isinstance(acc, (int, float)):
            tiles.append(tile("accuracy", "Tagging accuracy (manager-reviewed)", f"{acc * 100:.0f}%",
                              f"{tags.get('reviewed', 0)} calls reviewed in the portal"))
        else:
            conf = tags.get("avg_conf")
            tiles.append(tile("accuracy", "Average tag confidence",
                              f"{conf * 100:.0f}%" if isinstance(conf, (int, float)) else "n/a",
                              f"{tags.get('needs_review', 0)} routed to managers · {tags.get('churn', 0)} churn-risk calls"))
    else:
        tiles += [absent("calls", "Calls tagged"), absent("accuracy", "Tagging accuracy")]
    return _chapter(tiles, links)


def ch_ai(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    links = _links(st, "openwebui", "ai-harness", "rag", "mcp-tools")
    tiles = []
    if _up(st, "harness"):
        s = _session()
        tools = _get("harness", "/api/mcp/tools", s)
        groups = tools.get("groups") or {}
        tiles.append(tile("tools", "MCP tools available", sum(groups.values()) or len(tools.get("tools", [])),
                          f"across {len(groups)} systems: " + ", ".join(sorted(groups))[:110]))
        models = [m.get("id", "") for m in _get("harness", "/api/models", s).get("data", [])]
        prov: dict[str, int] = {}
        for mid in models:
            if "/" in mid:
                prov[mid.split("/", 1)[0]] = prov.get(mid.split("/", 1)[0], 0) + 1
        aliases = sum(1 for mid in models if "/" not in mid)
        pretty = {"vertex": "Vertex AI", "ollama": "local Ollama", "anthropic": "Anthropic", "openai": "OpenAI"}
        tiles.append(tile("models", "Model aliases", aliases,
                          " · ".join(f"{v} {pretty.get(k, k)}" for k, v in sorted(prov.items(), key=lambda kv: -kv[1]))
                          + " models behind them"))
        try:
            tel = (_get("harness", "/api/telemetry", s, params={"hours": 24}).get("total")) or {}
            calls, ok = int(tel.get("calls") or 0), int(tel.get("ok") or 0)
            tiles.append(tile("calls", "LLM calls (24 h)", _n(calls),
                              (f"{ok / calls * 100:.1f}% ok · p95 {tel.get('p95_ms', 0) / 1000:.1f}s" if calls else "no traffic yet")))
        except Exception:  # noqa: BLE001
            tiles.append(tile("calls", "LLM calls (24 h)", None, "telemetry unavailable", "error"))
    else:
        tiles += [absent("tools", "MCP tools available"), absent("models", "Model aliases")]
    if _up(st, "rag"):
        cols = _get("rag", "/api/collections").get("collections", [])
        main = max(cols, key=lambda c: int(c.get("n_docs") or 0), default={})
        tiles.append(tile("rag", "Runbooks indexed for RAG", int(main.get("n_docs") or 0),
                          f"{_n(int(main.get('n_chunks') or 0))} chunks · {len(cols)} collections (Qdrant + pgvector)"))
    else:
        tiles.append(absent("rag", "Runbooks indexed for RAG"))
    return _chapter(tiles[:4], links)


def _egress() -> dict[str, Any] | None:
    import pluginhost  # noqa: PLC0415
    for p in pluginhost.registry.plugins:
        if p.name == "egress":
            mod = p.module
            pol = mod.get_policy()
            rules = (pol.get("policy") or {}).get("rules") or []
            deny = sum(1 for r in rules if isinstance(r, dict) and str(r.get("action", "allow")).lower() == "deny")
            blocked = total = None
            try:
                stats = mod._px("GET", "/stats", params={"hours": 24}, timeout=TIMEOUT)
                if isinstance(stats, dict):
                    blocked, total = stats.get("blocked"), stats.get("total")
            except Exception:  # noqa: BLE001 — proxy down: policy file still counts
                pass
            return {"mode": pol.get("mode", "enforce"), "rules": len(rules), "deny": deny, "blocked": blocked,
                    "total": total}
    return None


def ch_govern(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cfg = settings.cfg
    vs = store.status()
    vault_txt = {"unlocked": "encrypted vault (unlocked)", "locked": "encrypted vault (locked)"}.get(vs, "vault not created")
    tiles = [tile("sso", "Single sign-on", "Entra ID" if cfg.oidc_enabled else "local accounts",
                  ("one gate for every tool" if cfg.oidc_enabled else "Entra ID OIDC ready to switch on")
                  + (f" · secrets in an {vault_txt}" if vs != "uninitialized" else f" · {vault_txt}"),
                  "ok")]
    try:
        eg = _egress()
    except Exception as exc:  # noqa: BLE001
        log.info("story: egress probe failed: %s", exc)
        eg = None
    if eg:
        tiles.append(tile("egress", "Egress allowlist rules", eg["rules"],
                          f"{eg['mode']} · {eg['deny']} deny rules"
                          + (f" · {_n(int(eg['blocked']))} of {_n(int(eg.get('total') or 0))} outbound calls blocked (24 h)"
                             if isinstance(eg.get("blocked"), (int, float)) else "")))
    else:
        tiles.append(absent("egress", "Egress allowlist rules", "Egress policy plugin not loaded"))
    events = audit.tail(limit=1_000_000)
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(timespec="seconds")
    recent = sum(1 for e in events if str(e.get("ts", "")) >= since)
    audit_tile = tile("audit", "Audit events", _n(len(events)), f"{recent} in the last 24 h · append-only")
    if vs == "unlocked":
        try:
            import gcp_identities  # noqa: PLC0415
            ids = gcp_identities.identities()
            asg = sum(1 for v in gcp_identities.assignments().values() if v)
            tiles.append(tile("identities", "Cloud identities", len(ids),
                              f"{asg} components with their own identity · least privilege per tool"))
        except Exception:  # noqa: BLE001
            pass
    tiles.append(audit_tile)
    return _chapter(tiles, [{"id": "audit", "label": "Audit log", "href": "/studio/#audit", "up": True},
                            {"id": "egress", "label": "Egress policy", "href": "/studio/#egress", "up": True},
                            {"id": "identities", "label": "Cloud identities", "href": "/studio/#identities", "up": True}])


def ch_own(st: dict[str, dict[str, Any]]) -> dict[str, Any]:
    cat = catalog.load()
    tiles = [tile("oss", "Open-source components", len(OSS), "no per-seat licences in the stack")]
    images = {v.get("image") for v in st.values() if v.get("state") in orchestrator.UP and v.get("image")}
    if st:
        tiles.append(tile("host", "Containers running", sum(1 for v in st.values() if v.get("state") in orchestrator.UP),
                          f"{len(images)} images · one platform · {len(cat.get('capabilities', {}))} capabilities"))
    exports, n = [], 0
    try:
        if _up(st, "mlops"):
            fz = int((_get("mlops", "/api/overview").get("frozen") or {}).get("n") or 0)
            if fz:
                exports.append(f"{fz} frozen model bundles")
                n += fz
        if _up(st, "canvas"):
            np_ = len(_get("canvas", "/api/pipelines").get("pipelines", []))
            exports.append(f"{np_} pipelines as Python")
            n += np_
    except Exception:  # noqa: BLE001
        pass
    manifest_p, _ = _dbt_paths()
    if manifest_p.is_file():
        exports.append("the dbt project")
        n += 1
    tiles.append(tile("exports", "Exportable artifacts", n or None, " · ".join(exports) or "none yet",
                      "ok" if exports else "absent"))
    if _up(st, "harness"):
        try:
            tel = (_get("harness", "/api/telemetry", params={"hours": 24}).get("total")) or {}
            tiles.append(tile("spend", "Cloud LLM spend (24 h)", f"${float(tel.get('cost_usd') or 0):,.2f}",
                              f"{_n(int(tel.get('prompt_tokens') or 0) + int(tel.get('completion_tokens') or 0))} tokens · local Ollama is free"))
        except Exception:  # noqa: BLE001
            pass
    return _chapter(tiles[:4], [{"id": "docs", "label": "Platform docs", "href": "/docs/", "up": True},
                                {"id": "services", "label": "Services", "href": "/studio/#services", "up": True}])


CHAPTERS: dict[str, Callable[[dict[str, dict[str, Any]]], dict[str, Any]]] = {
    "ask": ch_ask, "connect": ch_connect, "model": ch_model, "see": ch_see, "predict": ch_predict,
    "automate": ch_automate, "ai": ch_ai, "govern": ch_govern, "own": ch_own,
}


def _failed(cid: str, exc: BaseException | None) -> dict[str, Any]:
    why = "did not answer in time" if exc is None else f"{type(exc).__name__}"
    return _chapter([tile("error", "Live numbers", None, f"Service {why}. Try again in a minute.", "error")])


def collect() -> dict[str, Any]:
    st = _status()
    out: dict[str, Any] = {}
    pool = ThreadPoolExecutor(max_workers=len(CHAPTERS), thread_name_prefix="story")
    futs = {cid: pool.submit(fn, st) for cid, fn in CHAPTERS.items()}
    wait(futs.values(), timeout=BUDGET)
    for cid, f in futs.items():
        if not f.done():
            out[cid] = _failed(cid, None)
            continue
        exc = f.exception()
        if exc is not None:
            log.info("story: chapter %s failed: %s", cid, exc)
            out[cid] = _failed(cid, exc)
        else:
            out[cid] = f.result()
    pool.shutdown(wait=False, cancel_futures=True)
    return {"generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "appliance": settings.appliance_name(), "chapters": out}


@router.get("/story")
def story(refresh: bool = False, user: auth.User = Depends(auth.require_user)) -> dict[str, Any]:
    now = time.time()
    with _lock:
        hit = _cache["data"] if (not refresh and _cache["data"] and now - _cache["at"] < CACHE_SECONDS) else None
    if hit:
        return {**hit, "cached": True, "age_s": round(now - _cache["at"])}
    data = collect()
    with _lock:
        _cache.update(at=time.time(), data=data)
    return {**data, "cached": False, "age_s": 0, "ttl_s": CACHE_SECONDS}
