"""Integration canvas service — served natively under /canvas/.

    uvicorn bsp_canvas.app:app --host 0.0.0.0 --port 8093 --proxy-headers
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import config, connections, engine, scheduler, store
from .config import STATIC, sub, ui_kit_dir
from .nodes import palette

log = logging.getLogger("bsp_canvas")
PREFIX = "/canvas"

api = FastAPI(title="DataPlatform Integration Canvas", version="1.0", docs_url="/docs", redoc_url=None,
              openapi_url="/openapi.json",
              description="No-code pipelines: sources → transforms → destinations, loaded with dlt.")


def _bad(e: Exception, status: int = 400) -> HTTPException:
    return HTTPException(status, detail=str(e))


@api.get("/healthz", include_in_schema=False)
def healthz() -> dict:
    return {"status": "ok", "service": "bsp-canvas"}


@api.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


# ── palette & pipelines ──────────────────────────────────────────────────
@api.get("/api/palette")
def get_palette() -> dict:
    return {"nodes": palette(), "connection_types": connections.TYPES, "gcp": gcp_identities()}


@api.get("/api/gcp-identities")
def gcp_identities() -> dict:
    """Studio cloud identity NAMES and which one each consumer uses — never keys."""
    import json as _json
    try:
        assigned = _json.loads(os.environ.get("GCP_ASSIGNMENTS") or "{}")
    except ValueError:
        assigned = {}
    return {"identities": connections.identity_names(), "assigned": assigned if isinstance(assigned, dict) else {}}


def missing_secrets(p: dict) -> list[str]:
    """Vault secrets the pipeline's connections / nodes reference but that are
    not set — the pipeline shows "Needs setup" and refuses to run until then."""
    refs: set[str] = set()

    def collect(params: dict) -> None:
        for k, v in (params or {}).items():
            if k.endswith("_ref") and isinstance(v, str) and v.startswith("vault:"):
                refs.add(v[6:].strip())

    for n in p.get("nodes", []):
        cfg = n.get("config") or {}
        collect(cfg)
        if cfg.get("connection"):
            try:
                collect(connections.get(cfg["connection"]).get("params") or {})
            except KeyError:
                refs.add(f"connection:{cfg['connection']}")
    return sorted(r for r in refs if r.startswith("connection:") or not config.secret_available(r))


def _summary(p: dict) -> dict:
    runs = store.list_runs(p["id"], limit=1)
    return {"id": p["id"], "name": p.get("name"), "description": p.get("description", ""), "tags": p.get("tags", []),
            "example": p.get("example", False), "schedule": p.get("schedule"), "next_run": scheduler.upcoming(p),
            "nodes": len(p.get("nodes", [])), "updated_at": p.get("updated_at"),
            "sources": [n["type"] for n in p.get("nodes", []) if n.get("type", "").startswith("source.")],
            "destinations": [n["type"] for n in p.get("nodes", []) if n.get("type", "").startswith("dest.")],
            "last_run": runs[0] if runs else None, "running": engine.is_running(p["id"]),
            "needs_setup": missing_secrets(p)}


@api.get("/api/pipelines")
def list_pipelines() -> dict:
    return {"pipelines": [_summary(p) for p in store.list_pipelines()]}


@api.post("/api/pipelines")
def save_pipeline(doc: dict = Body(...)) -> dict:
    try:
        if not doc.get("id"):
            base = store.slug(doc.get("name", ""))
            pid, i = base, 2
            existing = {p["id"] for p in store.list_pipelines()}
            while pid in existing:
                pid, i = f"{base}-{i}", i + 1
            doc["id"] = pid
        sch = doc.get("schedule") or {}
        if sch.get("enabled") and not scheduler.valid(sch.get("cron", "")):
            raise ValueError(f"invalid cron expression {sch.get('cron')!r}")
        saved = store.save(doc)
    except ValueError as e:
        raise _bad(e)
    return {"pipeline": saved, "issues": engine.validate(saved)}


@api.get("/api/pipelines/{pid}")
def get_pipeline(pid: str) -> dict:
    try:
        p = store.get(pid)
    except (KeyError, ValueError):
        raise HTTPException(404, f"pipeline {pid} not found")
    return {"pipeline": p, "issues": engine.validate(p), "state": store.get_state(pid), "next_run": scheduler.upcoming(p)}


@api.delete("/api/pipelines/{pid}")
def delete_pipeline(pid: str) -> dict:
    store.delete(pid)
    return {"ok": True}


@api.post("/api/validate")
def validate(doc: dict = Body(...)) -> dict:
    return {"issues": engine.validate(doc)}


@api.post("/api/preview")
def preview(body: dict = Body(...)) -> dict:
    doc, node_id = body.get("pipeline") or {}, body.get("node_id")
    if not node_id or node_id not in {n["id"] for n in doc.get("nodes", [])}:
        raise HTTPException(400, "node_id must be a node of the pipeline")
    try:
        return engine.preview(doc, node_id, int(body.get("limit", 50)))
    except engine.NodeError as e:
        return JSONResponse({"detail": str(e), "node": e.node}, status_code=422)
    except Exception as e:
        return JSONResponse({"detail": f"{type(e).__name__}: {e}"}, status_code=422)


@api.post("/api/pipelines/{pid}/run")
def run(pid: str, wait: bool = False) -> dict:
    try:
        missing = missing_secrets(store.get(pid))
    except KeyError:
        raise HTTPException(404, f"pipeline {pid} not found")
    if missing:
        names = ", ".join(m.replace("connection:", "connection ") for m in missing)
        raise HTTPException(409, f"Needs setup: {names} — add the secret(s) in {config.SECRETS_HELP}, then run again")
    try:
        return {"run": engine.start_run(pid, trigger="api" if wait else "manual", wait=wait)}
    except KeyError:
        raise HTTPException(404, f"pipeline {pid} not found")
    except RuntimeError as e:
        raise _bad(e, 409)
    except ValueError as e:
        raise _bad(e)


@api.get("/api/pipelines/{pid}/runs")
def runs(pid: str, limit: int = 30) -> dict:
    return {"runs": store.list_runs(pid, limit)}


@api.get("/api/runs")
def all_runs(limit: int = 50) -> dict:
    return {"runs": store.list_runs(None, limit)}


@api.get("/api/runs/{pid}/{run_id}")
def get_run(pid: str, run_id: str) -> dict:
    try:
        return {"run": store.get_run(pid, run_id)}
    except KeyError:
        raise HTTPException(404, "run not found")


@api.get("/api/runs/{pid}/{run_id}/log")
def run_log(pid: str, run_id: str, offset: int = 0) -> dict:
    lines, nxt = store.read_log(pid, run_id, offset)
    try:
        status = store.get_run(pid, run_id)["status"]
    except KeyError:
        status = "unknown"
    return {"lines": lines, "offset": nxt, "status": status}


@api.get("/api/runs/{pid}/{run_id}/lineage")
def run_lineage(pid: str, run_id: str) -> dict:
    return {"events": store.get_lineage(run_id)}


@api.get("/api/pipelines/{pid}/state")
def get_state(pid: str) -> dict:
    return {"state": store.get_state(pid)}


@api.delete("/api/pipelines/{pid}/state")
def reset_state(pid: str) -> dict:
    """Clear the incremental cursor AND dlt's memory of the pipeline (next run = clean full load)."""
    store.reset_state(pid)
    return {"ok": True, **engine.reset_dlt(pid)}


@api.get("/api/pipelines/{pid}/export", response_class=PlainTextResponse)
def export(pid: str) -> PlainTextResponse:
    try:
        return PlainTextResponse(store.export_yaml(pid), media_type="application/x-yaml",
                                 headers={"Content-Disposition": f'attachment; filename="{pid}.yaml"'})
    except (OSError, ValueError):
        raise HTTPException(404, "pipeline not found")


@api.post("/api/import")
def import_yaml(body: dict = Body(...)) -> dict:
    try:
        doc = store.import_yaml(body.get("yaml", ""))
    except Exception as e:
        raise _bad(e)
    return save_pipeline(doc)


@api.post("/api/pipelines/{pid}/upload")
async def upload(pid: str, file: UploadFile = File(...)) -> dict:
    if not store.ID_RE.match(pid):
        raise HTTPException(400, "bad pipeline id")
    name = Path(file.filename or "upload.bin").name
    d = sub("uploads") / pid
    d.mkdir(parents=True, exist_ok=True)
    size = 0
    with open(d / name, "wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > 1 << 30:
                raise HTTPException(413, "file larger than 1 GB — put it in the lake instead")
            fh.write(chunk)
    return {"file": name, "size": size}


@api.post("/api/examples/seed")
def seed(force: bool = False) -> dict:
    return {"seeded": store.seed_examples(force=force)}


# ── connections ──────────────────────────────────────────────────────────
def _conn(name: str) -> dict:
    try:
        return connections.get(name)
    except KeyError as e:
        raise HTTPException(404, str(e))


@api.get("/api/connections")
def list_connections() -> dict:
    return {"connections": connections.list_connections(), "types": connections.TYPES}


@api.post("/api/connections")
def save_connection(body: dict = Body(...)) -> dict:
    try:
        return {"connection": connections.save(body)}
    except ValueError as e:
        raise _bad(e)
    except PermissionError as e:       # Studio: not on the identity's access list / no session
        raise _bad(e, getattr(e, "status", 403) or 403)


@api.delete("/api/connections/{name}")
def delete_connection(name: str) -> dict:
    connections.delete(name)
    return {"ok": True}


@api.post("/api/connections/test")
def test_connection(body: dict = Body(...)) -> dict:
    conn = _conn(body["name"]) if body.get("name") and not body.get("type") else body
    try:
        return connections.test(conn)
    except (ConnectionError, TimeoutError) as e:   # already a readable sentence (unreachable / timed out)
        return {"ok": False, "detail": str(e)[:500]}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {str(e)[:500]}"}


def _guard(fn, conn: dict, *a) -> Any:
    """Browse calls: fail fast on an unreachable host and never hang past BROWSE_TIMEOUT."""
    try:
        connections.preflight(conn)
        return connections.with_timeout(fn, conn, *a, timeout=connections.BROWSE_TIMEOUT,
                                        what=f"{conn.get('name', 'connection')} {fn.__name__}")
    except HTTPException:
        raise
    except (ConnectionError, TimeoutError) as e:
        raise HTTPException(422, str(e)[:500])
    except Exception as e:
        raise HTTPException(422, f"{type(e).__name__}: {str(e)[:500]}")


@api.get("/api/connections/{name}/schemas")
def schemas(name: str) -> dict:
    return {"schemas": _guard(connections.db_schemas, _conn(name))}


@api.get("/api/connections/{name}/tables")
def tables(name: str, schema: str = "", q: str = "") -> dict:
    return {"tables": _guard(connections.db_tables, _conn(name), schema or None, q)}


@api.get("/api/connections/{name}/columns")
def columns(name: str, table: str, schema: str = "") -> dict:
    return {"columns": _guard(connections.db_columns, _conn(name), schema or None, table)}


@api.get("/api/connections/{name}/estimate")
def estimate(name: str, table: str, schema: str = "") -> dict:
    return _guard(connections.row_estimate, _conn(name), schema or None, table)


@api.get("/api/connections/{name}/browse")
def browse(name: str, path: str = "") -> dict:
    return {"entries": _guard(connections.browse, _conn(name), path)}


@api.get("/api/connections/{name}/glob")
def glob(name: str, bucket: str, pattern: str = "**") -> dict:
    files = _guard(connections.glob_files, _conn(name), bucket, pattern)
    return {"files": files[:500], "count": len(files)}


# ── static + mount ───────────────────────────────────────────────────────
api.mount("/static", StaticFiles(directory=STATIC), name="static")
if ui_kit_dir().is_dir():
    api.mount("/ui-kit", StaticFiles(directory=ui_kit_dir()), name="ui-kit")


@api.middleware("http")
async def guard(request, call_next):
    path = request.url.path
    if path.startswith((PREFIX + "/api/", "/api/")) and request.method not in ("GET", "HEAD", "OPTIONS") \
            and request.headers.get("x-studio") != "1":
        return JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)
    # principal switch: the signed-in user (Studio gate header) and their Studio
    # session cookie — Studio verifies the cookie before it issues a token for a
    # named Google identity, so a forged header alone gets nothing
    user = (request.headers.get("x-auth-user") or "").strip()
    if user:
        from bsp_common import gcp
        with gcp.acting(user, session=request.cookies.get("studio_session", "")):
            resp = await call_next(request)
    else:
        resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if "/api/" in path:
        resp.headers.setdefault("Cache-Control", "no-store")
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


@app.on_event("startup")
def _startup() -> None:
    seeded = store.seed_examples()
    if seeded:
        log.info("seeded example pipelines: %s", ", ".join(seeded))
    if os.environ.get("CANVAS_SCHEDULER", "on").lower() not in ("0", "off", "false"):
        scheduler.start()
