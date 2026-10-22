"""DataPlatform Workbench — packages (/pkg/), files (/files/), import & export (/ingress/) and dbt Studio (/dbt/).

Runs inside the analyst workspace container as the analyst user:

    python -m uvicorn app:app --app-dir /opt/workbench --host 0.0.0.0 --port 8002 --proxy-headers

nginx passes /pkg/, /files/ and /ingress/ through unchanged (behind the facade session
gate), so every route here is served natively under those prefixes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402

from wb import dataio_api, files_api, jobs_api, pkg_api  # noqa: E402
from wb import dbt_api  # noqa: E402
from wb.config import STATIC_DIR, UI_KIT_DIR  # noqa: E402

app = FastAPI(title="Workbench", docs_url=None, redoc_url=None, openapi_url="/pkg/api/openapi.json")

SAFE = {"GET", "HEAD", "OPTIONS"}


class Guard:
    """Pure-ASGI middleware (keeps request/response streaming intact):
    CSRF header check on state-changing API calls + security headers."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        is_api = path.startswith(("/pkg/api/", "/files/api/", "/ingress/api/", "/dbt/api/"))
        if is_api and scope["method"] not in SAFE:
            hdrs = dict(scope.get("headers") or [])
            if hdrs.get(b"x-studio") != b"1":
                resp = JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
                return await resp(scope, receive, send)
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]
        if is_api and not path.endswith("/stream"):
            extra.append((b"cache-control", b"no-store"))
        elif "/static/" in path or "/ui-kit/" in path:
            extra.append((b"cache-control", b"no-cache"))  # revalidate (ETag) so UI updates land immediately

        async def wrapped(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k, v) for k, v in extra if k not in have]
            await send(message)
        return await self.app(scope, receive, wrapped)


app.add_middleware(Guard)
app.include_router(pkg_api.router)
app.include_router(files_api.router)
app.include_router(jobs_api.make_router("/pkg/api/jobs", {"python", "data"}))
app.include_router(dataio_api.router)
app.include_router(jobs_api.make_router("/ingress/api/jobs", {"ingress", "data"}))
app.include_router(jobs_api.make_router("/dbt/api/jobs", {dbt_api.QUEUE}))
app.include_router(dbt_api.router)

for prefix in ("/pkg", "/files", "/ingress"):
    if UI_KIT_DIR.is_dir():
        app.mount(f"{prefix}/ui-kit", StaticFiles(directory=str(UI_KIT_DIR)), name=f"{prefix[1:]}-kit")
    app.mount(f"{prefix}/static", StaticFiles(directory=str(STATIC_DIR)), name=f"{prefix[1:]}-static")

if UI_KIT_DIR.is_dir():
    app.mount("/dbt/ui-kit", StaticFiles(directory=str(UI_KIT_DIR)), name="dbt-kit")
app.mount("/dbt/static", StaticFiles(directory=str(STATIC_DIR)), name="dbt-static")


def _page(name: str) -> FileResponse:
    return FileResponse(STATIC_DIR / name, headers={"Cache-Control": "no-cache"})


@app.get("/pkg/", include_in_schema=False)
def pkg_page():
    return _page("pkg.html")


@app.get("/files/", include_in_schema=False)
def files_page():
    return _page("files.html")


@app.get("/ingress/", include_in_schema=False)
def ingress_page():
    return _page("ingress.html")


@app.get("/ingress", include_in_schema=False)
def ingress_redirect():
    return RedirectResponse("/ingress/")


@app.get("/pkg", include_in_schema=False)
def pkg_redirect():
    return RedirectResponse("/pkg/")


@app.get("/files", include_in_schema=False)
def files_redirect():
    return RedirectResponse("/files/")


@app.get("/dbt/", include_in_schema=False)
def dbt_page():
    return _page("dbt.html")


@app.get("/dbt", include_in_schema=False)
def dbt_redirect():
    return RedirectResponse("/dbt/")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/pkg/")


def _healthz():
    return {"ok": True, "app": "workbench"}


for _prefix in ("/pkg", "/files", "/ingress", "/dbt"):
    app.add_api_route(f"{_prefix}/healthz", _healthz, methods=["GET"], include_in_schema=False)
