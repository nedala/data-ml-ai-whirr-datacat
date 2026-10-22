"""Enterprise connectors service — served natively under /connectors/.

    uvicorn bsp_connectors.app:app --host 0.0.0.0 --port 8092 --proxy-headers

/connectors/            status page: each system, mode (mock/live), what is configured, test connection
/connectors/docs        OpenAPI docs
/connectors/healthz     liveness
/connectors/<system>/…  plugin routers: every bsp_connectors/<system>/routes.py exposing `router`
                        (an APIRouter) is mounted here — plan/architecture.md §4 plugin rule.
                        A routes module may also set ALIASES = ["sap"] for extra mount points.
"""

from __future__ import annotations

import importlib
import logging
import os
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import _common
from ._common import config_report, env, mode_of

log = logging.getLogger("bsp_connectors.app")
PKG = Path(__file__).resolve().parent
STATIC = PKG.parent / "static"
UI_KIT = Path(env("UI_KIT_DIR", "/opt/ui-kit"))
if not UI_KIT.is_dir():
    UI_KIT = PKG.parents[1] / "ui-kit"
PREFIX = "/connectors"

SYSTEMS: dict[str, dict[str, Any]] = {
    "m365": {"title": "Microsoft 365", "blurb": "Outlook mailboxes, SharePoint & OneDrive files via Microsoft Graph",
             "icon": "cloud", "explore": "m365/mailboxes"},
    "atlan": {"title": "Atlan", "blurb": "Data catalog: assets, glossary, owners, PII classifications and lineage",
              "icon": "search", "explore": "atlan/info"},
    "bigquery": {"title": "BigQuery", "blurb": "Enterprise EDW (project GCP_PROJECT_ID) — datasets, schemas, SQL",
                 "icon": "database", "explore": "bigquery/datasets"},
    "powerbi": {"title": "Power BI", "blurb": "Workspaces, reports, datasets, refreshes and DAX queries",
                "icon": "dashboard", "explore": "powerbi/v1.0/myorg/groups"},
    "sap_brim": {"title": "SAP BRIM", "blurb": "Billing disputes in FI-CA via OData or the web UI (Playwright)",
                 "icon": "contract", "explore": None},
}


def _test_m365() -> str:
    from . import m365
    files = m365.list_files(None, "", recursive=False, include_folders=True)
    msg = f"{len(files)} item(s) in the default document library"
    mb = env("DISPUTES_MAILBOX")
    if mb:
        try:
            n = len(m365.list_messages(mb, "inbox", top=5))
            msg += f"; {mb}: {n} recent message(s) readable"
        except Exception as e:  # mailbox access is scoped separately
            msg += f"; mailbox {mb}: {type(e).__name__}: {str(e)[:120]}"
    return msg


def _test_atlan() -> str:
    from . import atlan
    hits = atlan.search_assets("", type_names=["Table"], limit=5)
    return f"search ok — {len(hits)} table asset(s) returned"


def _test_bigquery() -> str:
    from . import bigquery
    ds = bigquery.list_datasets()
    return f"{len(ds)} dataset(s): " + ", ".join(d["dataset_id"] for d in ds[:6])


def _test_powerbi() -> str:
    from . import powerbi
    ws = powerbi.list_workspaces()
    return f"{len(ws)} workspace(s): " + ", ".join(w["name"] for w in ws[:5])


def _test_sap_brim() -> str:
    import bsp_connectors
    mod = getattr(bsp_connectors, "sap_brim")
    for fn in ("test_connection", "health", "ping", "describe"):
        f = getattr(mod, fn, None) if mod else None
        if callable(f):
            return str(f())
    mod.create_dispute  # noqa: B018 — raises a clear error when the package is missing
    return "module loaded (no health check exposed)"


TESTS: dict[str, Callable[[], str]] = {"m365": _test_m365, "atlan": _test_atlan, "bigquery": _test_bigquery,
                                       "powerbi": _test_powerbi, "sap_brim": _test_sap_brim}

# ── app ──────────────────────────────────────────────────────────────────
api = FastAPI(title="DataPlatform Enterprise Connectors",
              description="Mock-or-live connectors for Microsoft 365 (Graph), Atlan, BigQuery, Power BI and SAP BRIM. "
                          "Python SDK: `from bsp_connectors import m365, atlan, bigquery, powerbi, sap_brim, mode_of`.",
              version="1.0", docs_url="/docs", redoc_url=None, openapi_url="/openapi.json")
PLUGINS: dict[str, dict[str, Any]] = {}


def mount_plugins(target: FastAPI) -> dict[str, dict[str, Any]]:
    """Auto-mount bsp_connectors/<system>/routes.py:router under /<system>/ (+ ALIASES)."""
    found: dict[str, dict[str, Any]] = {}
    for d in sorted(PKG.iterdir()):
        if not (d.is_dir() and (d / "routes.py").is_file() and not d.name.startswith(("_", "."))):
            continue
        name = d.name
        try:
            mod = importlib.import_module(f"bsp_connectors.{name}.routes")
            router = getattr(mod, "router", None)
            if not isinstance(router, APIRouter):
                found[name] = {"mounted": False, "error": "routes.py has no APIRouter `router`"}
                continue
            prefixes = [f"/{name}"] + [f"/{a.strip('/')}" for a in getattr(mod, "ALIASES", [])]
            for p in prefixes:
                target.include_router(router, prefix=p)
            found[name] = {"mounted": True, "prefixes": [PREFIX + p for p in prefixes], "routes": len(router.routes)}
        except Exception as e:  # one broken plugin must not take the service down
            log.exception("plugin %s failed to mount", name)
            found[name] = {"mounted": False, "error": f"{type(e).__name__}: {e}",
                           "trace": traceback.format_exc(limit=3)}
    return found


@api.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "service": "bsp-connectors"}


@api.get("/api/systems", tags=["status"], summary="Each system with its mode and configuration (names only)")
def systems() -> dict:
    out = []
    for sid, meta in SYSTEMS.items():
        try:
            mode = mode_of(sid)
        except Exception as e:
            mode = f"error: {e}"
        plug = PLUGINS.get(sid, {"mounted": False})
        row = {"id": sid, **meta, "mode": mode, "configured": config_report(sid),
               "required": _common.REQUIRED_ENV.get(sid, ["SAP_BRIM_BASE_URL", "SAP_BRIM_USER", "SAP_BRIM_PASSWORD"]
                                                     if sid == "sap_brim" else []),
               "plugin": {k: v for k, v in plug.items() if k != "trace"}}
        if sid == "atlan":
            row["url"] = env("ATLAN_BASE_URL") or "https://atlan.example.com"
        out.append(row)
    return {"systems": out, "fixtures": str(_common.fixtures_dir()), "state": str(_common.state_dir("_service").parent)}


@api.post("/api/systems/{sid}/test", tags=["status"], summary="Test the connection (cheap read call)")
def test_system(sid: str) -> dict:
    if sid not in TESTS:
        raise HTTPException(404, f"unknown system {sid}")
    t0 = time.time()
    try:
        detail, ok = TESTS[sid](), True
    except Exception as e:
        detail, ok = f"{type(e).__name__}: {str(e)[:400]}", False
    try:
        mode = mode_of(sid)
    except Exception:
        mode = "unknown"
    return {"system": sid, "ok": ok, "mode": mode, "detail": detail, "ms": int((time.time() - t0) * 1000)}


@api.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


api.mount("/static", StaticFiles(directory=STATIC), name="static")
if UI_KIT.is_dir():
    api.mount("/ui-kit", StaticFiles(directory=UI_KIT), name="ui-kit")

PLUGINS.update(mount_plugins(api))


@api.middleware("http")
async def guard(request, call_next):
    """CSRF guard for the status API (browser posts carry X-Studio: 1 via BSP.api)."""
    if request.url.path.startswith((PREFIX + "/api/", "/api/")) and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-studio") != "1":
        return JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/", include_in_schema=False)
@app.get(PREFIX, include_in_schema=False)
def _root() -> RedirectResponse:
    return RedirectResponse(PREFIX + "/")


@app.get("/healthz", include_in_schema=False)
def _hz() -> dict:
    return healthz()


app.mount(PREFIX, api)

if os.environ.get("CONNECTORS_LOG_LEVEL"):
    logging.basicConfig(level=os.environ["CONNECTORS_LOG_LEVEL"].upper())
