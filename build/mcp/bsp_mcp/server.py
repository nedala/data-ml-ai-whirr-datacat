"""DataPlatform MCP server — Streamable HTTP.

    MCP_TOKEN=… python -m bsp_mcp            # 0.0.0.0:${MCP_PORT:-8095}

Routes
    POST/GET/DELETE /mcp              all enabled tool groups (+ prompts, resources)   bearer MCP_TOKEN
    POST/GET/DELETE /mcp/<group>      one group (warehouse | trino | bigquery | atlan | powerbi | m365 | mlflow | sap | browser | rag)
    GET /mcp/info                     landing page: endpoints, client snippets, tools         public, no data
    GET /mcp/tools.json               tool roster                                              public, no data
    GET /mcp/healthz, /healthz        liveness                                                 public

Stateless streamable HTTP with JSON responses (no sticky sessions) — works behind nginx
with the path kept, on the laptop loopback port, and for server-side clients such as
Open WebUI and the harness playground.
"""
from __future__ import annotations

import contextlib
import functools
import hmac
import html
import json
import logging
import os
import sys
import time
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)

from starlette.applications import Starlette  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402

from . import __version__, audit, prompts, readiness, resources  # noqa: E402
from . import tools as _tools  # noqa: E402,F401  (registers tool modules)
from .common import REGISTRY, UserError, enabled_groups, threaded  # noqa: E402
from .tools import rag as _rag  # noqa: E402

log = logging.getLogger("bsp_mcp")

INSTRUCTIONS = """DataPlatform (US internet provider) data & AI tools. Groups (tool-name prefixes):
warehouse_* (local Postgres warehouse bsp_edw: raw → staging → marts, read-only SQL), trino_* (federated read-only SQL),
bigquery_* (enterprise EDW: SELECT-only, dry-run cost first), atlan_* (data catalog: search, lineage, glossary),
powerbi_* (workspaces, DAX, refresh status), m365_* (Outlook mail/calendar, Teams, SharePoint/OneDrive files, people —
read-only by default; write tools preview first), mlflow_* (experiments, leaderboards, registered models),
sap_* (SAP BRIM dispute lookup), browser_* (drive the appliance desktop's Chromium, visible live in noVNC; take a
browser_snapshot to find elements by role/name before clicking), rag_* (front-office knowledge base, when installed).
Data model: DataPlatform sells copper (DSL) and fiber (FTTH) internet only. Account tables are keyed by `ban`
(10-digit billing account number, e.g. 3100000016), wire centers by `clli` (e.g. RLGHNCXA), invoice ids are
<ban>-<yymm>; raw.transactions is the payment / late-fee / credit ledger, raw.disputes the billing disputes.
Connectors run in MOCK mode with synthetic data until live credentials are configured — results carry "mode".
Never present output as a pricing, legal or financial decision; cite the tables/assets you used."""


def caller() -> str:
    try:
        from fastmcp.server.dependencies import get_http_headers  # noqa: PLC0415

        h = get_http_headers() or {}
        return (h.get("x-caller") or h.get("x-auth-user") or h.get("user-agent") or "")[:120]
    except Exception:  # noqa: BLE001
        return ""


def _audited(spec, ToolError):
    fn = threaded(spec.fn)

    @functools.wraps(fn)
    async def run(*a, **kw):
        t0 = time.monotonic()
        try:
            out = await fn(*a, **kw)
        except UserError as e:
            audit.record(spec.name, spec.group, kw, "error", int((time.monotonic() - t0) * 1000), str(e), caller=caller())
            raise ToolError(str(e)) from None
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", spec.name)
            msg = f"{type(e).__name__}: {str(e)[:400]}"
            audit.record(spec.name, spec.group, kw, "error", int((time.monotonic() - t0) * 1000), msg, caller=caller())
            raise ToolError(msg) from None
        size = len(out) if isinstance(out, str) else len(json.dumps(out, default=str)) if isinstance(out, dict) else 0
        audit.record(spec.name, spec.group, kw, "ok", int((time.monotonic() - t0) * 1000), result_chars=size, caller=caller())
        return out

    run.__signature__ = fn.__signature__  # type: ignore[attr-defined]
    run.__annotations__ = getattr(fn, "__annotations__", {})
    if hasattr(run, "__wrapped__"):
        del run.__wrapped__
    return run


def all_groups() -> list[str]:
    _rag.register()
    return REGISTRY.groups()


def build(group: str | None = None):
    """A FastMCP server with every enabled group (group=None) or one group's tools."""
    from fastmcp import FastMCP  # noqa: PLC0415
    from fastmcp.exceptions import ToolError  # noqa: PLC0415
    from fastmcp.tools.tool import Tool  # noqa: PLC0415
    from mcp.types import ToolAnnotations  # noqa: PLC0415

    groups = enabled_groups(all_groups())
    disabled = readiness.disabled_tools()
    mcp = FastMCP(name="dataplatform" if group is None else f"dataplatform-{group}", instructions=INSTRUCTIONS,
                  version=__version__)
    for spec in REGISTRY.tools:
        if spec.name in disabled or spec.group not in groups or (group is not None and spec.group != group):
            continue
        t = Tool.from_function(_audited(spec, ToolError), name=spec.name, title=spec.title, tags={spec.group},
                               output_schema=None,
                               annotations=ToolAnnotations(title=spec.title, readOnlyHint=spec.read_only,
                                                           destructiveHint=False, idempotentHint=spec.read_only,
                                                           openWorldHint=spec.open_world))
        mcp.add_tool(t)
    if group is None:
        resources.register(mcp)
        prompts.register(mcp)
    return mcp


def _token() -> str:
    tok = os.environ.get("MCP_TOKEN", "")
    if not tok and os.environ.get("MCP_ALLOW_OPEN", "") != "1":
        raise SystemExit("MCP_TOKEN is empty — refusing to start an unauthenticated MCP server "
                         "(set MCP_ALLOW_OPEN=1 only for local tests)")
    return tok


def _json(status: int, body: dict, extra: dict | None = None):
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store", **(extra or {})})


def roster() -> list[dict]:
    groups = enabled_groups(all_groups())
    disabled = readiness.disabled_tools()
    return [{"name": t.name, "group": t.group, "title": t.title, "read_only": t.read_only, "summary": t.summary}
            for t in REGISTRY.tools if t.group in groups and t.name not in disabled]


async def healthz(request):
    groups = enabled_groups(all_groups())
    return _json(200, {"status": "ok", "service": "mcp", "version": __version__, "groups": groups,
                       "tools": len(roster()), "prompts": len(prompts.PROMPTS),
                       "api_tools_mode": readiness.api_mode(), "disabled_tools": len(readiness.disabled_tools())})


async def tools_status(request):
    body = readiness.status()
    if (request.query_params.get("probe") or "").strip().lower() in ("1", "true", "yes", "on"):
        body["probe"] = readiness.probe()
    return _json(200, body)


async def tools_json(request):
    return _json(200, {"tools": roster()})


async def info(request):
    rows = "".join(f"<tr><td><code>{html.escape(t['name'])}</code></td><td>{html.escape(t['group'])}</td>"
                   f"<td>{'read-only' if t['read_only'] else 'write (gated)'}</td><td>{html.escape(t['summary'])}</td></tr>"
                   for t in roster())
    port = os.environ.get("MCP_HOST_PORT", "8765")
    body = f"""<!doctype html><meta charset="utf-8"><title>DataPlatform MCP tools</title>
<link rel="stylesheet" href="/mcp/ui-kit/bsp.css"><body style="padding:24px;max-width:1100px;margin:auto;font-family:system-ui">
<h1>DataPlatform MCP tools <small>v{__version__}</small></h1>
<p>Streamable HTTP, bearer token (<b>Studio → Credentials → MCP_TOKEN</b>).</p>
<ul><li>In the appliance: <code>http://mcp:8095/mcp</code></li>
<li>On this laptop: <code>http://127.0.0.1:{port}/mcp</code></li>
<li>One group only: <code>/mcp/&lt;group&gt;</code></li></ul>
<pre>claude mcp add --transport http dataplatform http://127.0.0.1:{port}/mcp --header "Authorization: Bearer $MCP_TOKEN"</pre>
<p>Full client guide: <a href="/docs/ai-engineer/mcp-clients/">docs → AI Engineer → MCP clients</a></p>
<table class="bsp-table"><tr><th>tool</th><th>group</th><th>access</th><th>what it does</th></tr>{rows}</table>"""
    return HTMLResponse(body)


def create_app(token: str | None = None):
    token = _token() if token is None else token
    groups = enabled_groups(all_groups())
    servers = {"": build(None)}
    servers.update({g: build(g) for g in groups})
    mcp_apps = {}
    for g, srv in servers.items():
        path = "/mcp" + (f"/{g}" if g else "")
        mcp_apps[path] = srv.http_app(path=path, transport="http", stateless_http=True, json_response=True)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            for a in mcp_apps.values():
                await stack.enter_async_context(a.router.lifespan_context(a))
            log.info("bsp-mcp %s ready: groups=%s tools=%d auth=%s", __version__, ",".join(groups), len(roster()),
                     "bearer" if token else "OPEN (tests only)")
            yield

    routes = [Route("/", info), Route("/mcp/info", info), Route("/mcp/tools.json", tools_json),
              Route("/mcp/tools/status", tools_status),
              Route("/mcp/healthz", healthz), Route("/healthz", healthz)]
    kit = os.environ.get("UI_KIT_DIR", "/opt/ui-kit")
    if os.path.isdir(kit):
        from starlette.routing import Mount  # noqa: PLC0415
        from starlette.staticfiles import StaticFiles  # noqa: PLC0415

        routes.append(Mount("/mcp/ui-kit", app=StaticFiles(directory=kit), name="ui-kit"))
    pages = Starlette(routes=routes, lifespan=lifespan)
    expected = f"Bearer {token}".encode() if token else b""

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return await pages(scope, receive, send)
        path = scope.get("path", "/")
        norm = path.rstrip("/") or "/"
        if norm in mcp_apps:
            if token:
                got = dict(scope.get("headers") or []).get(b"authorization", b"")
                if not hmac.compare_digest(got.strip(), expected):
                    resp = _json(401, {"error": "unauthorized",
                                       "message": "Send 'Authorization: Bearer <MCP_TOKEN>' (Studio → Credentials → MCP_TOKEN)"},
                                 {"WWW-Authenticate": 'Bearer realm="bsp-mcp"'})
                    return await resp(scope, receive, send)
            sub = dict(scope)
            sub["path"] = norm
            sub["raw_path"] = norm.encode()
            sub["root_path"] = ""
            return await mcp_apps[norm](sub, receive, send)
        if norm.startswith("/.well-known/"):
            return await _json(404, {"error": "not_found", "message": "no OAuth here — use a bearer token"})(scope, receive, send)
        if norm.startswith("/mcp/") and norm.count("/") == 2 and norm.split("/")[2] not in ("info", "tools.json", "healthz", "ui-kit"):
            return await _json(404, {"error": "unknown_group", "groups": groups})(scope, receive, send)
        scope = dict(scope)
        if not norm.startswith("/mcp/ui-kit/"):
            scope["path"] = norm
        return await pages(scope, receive, send)

    app.mcp_servers = servers  # type: ignore[attr-defined]
    return app


def main() -> None:
    logging.basicConfig(level=os.environ.get("MCP_LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        stream=sys.stderr)
    for noisy in ("docket", "fakeredis", "mcp.server", "fastmcp", "httpx", "sse_starlette"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    import uvicorn  # noqa: PLC0415

    app = create_app()
    port = int(os.environ.get("MCP_PORT", "8095"))
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "0.0.0.0"), port=port, log_level="warning", proxy_headers=True,
                forwarded_allow_ips="*", access_log=False)


if __name__ == "__main__":
    main()
