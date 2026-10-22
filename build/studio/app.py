"""Studio — the appliance control plane, served natively under /studio/.

uvicorn app:app (working dir /opt/studio). See plan/v2-architecture.md §4."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import auth
import certs
import dockerops
import pluginhost
import routes_admin
import routes_auth
import routes_compose
import routes_gcp
import routes_secrets
import routes_services
import settings
import story
from backup import BackupError
from certs import CertError
from credentials import CredentialError
from gcp_identities import IdentityError
from dockerops import DockerUnavailable
from users import UserError
from vault import VaultLocked, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("studio")

STATIC = settings.HERE / "static"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
       "frame-ancestors 'self'; base-uri 'self'; form-action 'self'")


def _startup() -> None:
    if store.auto_unlock():
        log.info("vault unlocked from environment")
    pluginhost.load()
    for plugin in pluginhost.registry.plugins:
        router = plugin.hook("router")
        if router is None:
            continue
        try:
            app.include_router(router)
        except Exception as exc:  # noqa: BLE001 — a broken plugin must not take Studio down
            pluginhost.registry.fail(plugin.name, "router", exc)
    try:
        if certs.ensure_dev_cert(dockerops.host_name()):
            log.info("issued dev facade certificate from the appliance CA")
    except (OSError, ValueError) as exc:
        log.error("dev certificate issuance failed: %s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _startup()
    yield


app = FastAPI(title="DataPlatform Studio", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
for r in (routes_auth.router, routes_compose.router, routes_services.router, routes_secrets.router,
          routes_gcp.router, routes_admin.router, routes_admin.public, story.router):
    app.include_router(r)
app.mount("/studio/static", StaticFiles(directory=STATIC), name="static")
app.mount("/studio/ui-kit", StaticFiles(directory=settings.cfg.ui_kit), name="ui-kit")


# ── errors ───────────────────────────────────────────────────────────────
@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    body: dict[str, Any] = (dict(exc.detail, detail=exc.detail.get("message", ""))
                            if isinstance(exc.detail, dict) else {"detail": exc.detail})
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(VaultLocked)
async def locked(_: Request, exc: VaultLocked) -> JSONResponse:
    return JSONResponse({"detail": str(exc) or "The vault is locked", "locked": True}, status_code=423)


@app.exception_handler(DockerUnavailable)
async def no_docker(_: Request, exc: DockerUnavailable) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503)


for _exc in (UserError, CredentialError, BackupError, CertError, IdentityError):
    app.add_exception_handler(_exc, lambda _r, e: JSONResponse({"detail": str(e)}, status_code=400))


@app.exception_handler(RequestValidationError)
async def invalid(_: Request, exc: RequestValidationError) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(p) for p in first.get("loc", [])[1:])
    return JSONResponse({"detail": f"{where}: {first.get('msg', 'invalid request')}".strip(": ")}, status_code=422)


# ── headers ──────────────────────────────────────────────────────────────
@app.middleware("http")
async def headers(request: Request, call_next):
    response: Response = await call_next(request)
    path = request.url.path
    if path.startswith("/studio/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif path.startswith(("/studio/static/", "/studio/ui-kit/")):
        response.headers["Cache-Control"] = "no-cache"
    else:
        response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


# ── pages ────────────────────────────────────────────────────────────────
def _html(name: str) -> FileResponse:
    return FileResponse(STATIC / name, media_type="text/html", headers={"Cache-Control": "no-store"})


@app.get("/studio", include_in_schema=False)
def bare() -> RedirectResponse:
    return RedirectResponse("/studio/", status_code=308)


@app.get("/studio/", include_in_schema=False)
def shell(request: Request) -> Response:
    if not auth.read_session(request):
        target = "/studio/setup" if routes_auth.setup_needed() else "/studio/login?next=/studio/"
        return RedirectResponse(target, status_code=303)
    return _html("shell.html")


@app.get("/studio/login", include_in_schema=False)
def login_page(request: Request, next: str = "") -> Response:
    if routes_auth.setup_needed():
        return RedirectResponse("/studio/setup", status_code=303)
    if auth.read_session(request):
        return RedirectResponse(routes_auth.safe_next(next), status_code=303)
    return _html("auth.html")


@app.get("/studio/setup", include_in_schema=False)
def setup_page() -> Response:
    if not routes_auth.setup_needed():
        return RedirectResponse("/studio/login", status_code=303)
    return _html("auth.html")


@app.get("/studio/healthz", include_in_schema=False)
def healthz() -> dict[str, str]:
    return {"status": "ok", "vault": store.status()}
