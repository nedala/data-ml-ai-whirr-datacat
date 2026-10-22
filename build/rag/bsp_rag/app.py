"""FastAPI app for the `rag` service (port 8101, prefix /rag — hosted natively
under the prefix so the same URLs work behind nginx and service-to-service).

  /rag/                 UI (Collections · Ingest · Explore · Ask · Front office · Evals · Settings)
  /rag/api/*            JSON API used by the UI
  /rag/tools/*          OpenAPI tool server (Open WebUI "tool servers", any OpenAPI agent)
  /rag/chat/            redirect to Open WebUI on its dedicated port
  /rag/healthz          liveness
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, answer as answer_mod, config, embeddings, evals, frontoffice, gateway, ingest, meta as meta_mod
from . import retrieval, stores, tools as tools_mod

os.environ.setdefault("BSP_RAG_IN_PROCESS", "1")
log = logging.getLogger("bsp_rag")
logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")

PREFIX = "/rag"
STATIC = Path(__file__).parent / "static"
UI_KIT = Path(os.environ.get("UI_KIT_DIR", "/opt/ui-kit"))
if not UI_KIT.exists():
    UI_KIT = Path(__file__).resolve().parents[2] / "ui-kit"

STATE: dict[str, Any] = {"ready": False, "error": None, "seed": None}

app = FastAPI(title="DataPlatform Knowledge & RAG", version=__version__, docs_url=PREFIX + "/docs",
              openapi_url=PREFIX + "/openapi.json", redoc_url=None)
api = APIRouter(prefix=PREFIX + "/api")


def user_of(request: Request) -> str:
    return request.headers.get("x-auth-user") or "anonymous"


def _err(e: Exception) -> HTTPException:
    if isinstance(e, embeddings.MixedModelError):
        return HTTPException(409, str(e))
    if isinstance(e, embeddings.EmbedderUnavailable):
        return HTTPException(503, str(e))
    if isinstance(e, ingest.IngestError):
        return HTTPException(400, str(e))
    return HTTPException(500, f"{type(e).__name__}: {e}")


def _need_ready() -> None:
    if not STATE["ready"]:
        try:
            meta_mod.get()
            STATE["ready"], STATE["error"] = True, None
            return
        except Exception:  # noqa: BLE001
            pass
        raise HTTPException(503, STATE["error"] or "rag service is starting (waiting for Postgres / Qdrant)")


# ── startup ──────────────────────────────────────────────────────────────
def _startup() -> None:
    cfg = config.get()
    while not STATE["ready"]:          # Postgres may start long after us — keep trying
        try:
            meta_mod.get()
            STATE["ready"], STATE["error"] = True, None
        except Exception as e:  # noqa: BLE001
            STATE["error"] = f"metadata store not reachable yet: {e}"
            time.sleep(3)
    if not cfg.autoseed:
        return
    spec = os.environ.get("RAG_AUTOSEED_COLLECTIONS", f"{cfg.default_collection}:{cfg.backend}")
    results = []
    for item in [s.strip() for s in spec.split(",") if s.strip()]:
        name, _, backend = item.partition(":")
        for attempt in range(60):
            try:
                if meta_mod.get().collection(name) is None:
                    results.append(ingest.ensure_demo(name, backend or None, "auto"))
                break
            except Exception as e:  # noqa: BLE001 — Qdrant may still be starting
                STATE["seed"] = f"seeding {name}: waiting ({type(e).__name__}: {e})"
                time.sleep(3)
    STATE["seed"] = results or "collections already present"


@app.on_event("startup")
def on_startup() -> None:
    threading.Thread(target=_startup, daemon=True, name="rag-startup").start()


# ── health / UI ──────────────────────────────────────────────────────────
@app.get(PREFIX + "/healthz")
def healthz():
    return {"ok": True, "ready": STATE["ready"], "version": __version__}


@app.get(PREFIX)
def root_redirect():
    return RedirectResponse(PREFIX + "/")


@app.get(PREFIX + "/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get(PREFIX + "/chat/")
@app.get(PREFIX + "/chat")
def chat_redirect(request: Request):
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or config.get().public_host).split(":")[0]
    return RedirectResponse(f"https://{host}:{config.get().openwebui_port}/")


app.mount(PREFIX + "/static", StaticFiles(directory=STATIC), name="static")
if UI_KIT.exists():
    app.mount(PREFIX + "/ui-kit", StaticFiles(directory=UI_KIT), name="ui-kit")


# ── status / settings ────────────────────────────────────────────────────
@api.get("/status")
def status():
    cfg = config.get()
    out: dict[str, Any] = {"ready": STATE["ready"], "error": STATE["error"], "seed": STATE["seed"], "version": __version__,
                           "default_backend": cfg.backend, "default_collection": cfg.default_collection,
                           "llm_gateway": {"url": os.environ.get("LLM_GATEWAY_URL", ""), "available": gateway.available(),
                                           "use_llm": cfg.use_llm, "chat_alias": cfg.chat_alias,
                                           "embed_alias": cfg.gateway_embed_alias},
                           "embedder_default": cfg.embedder, "openwebui_port": cfg.openwebui_port,
                           "sharepoint_folder": cfg.sharepoint_folder, "lake_dir": str(cfg.lake_dir)}
    backends = {}
    for b in ("qdrant", "pgvector"):
        try:
            backends[b] = stores.get(b).status()
        except Exception as e:  # noqa: BLE001
            backends[b] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    out["backends"] = backends
    if STATE["ready"]:
        out["meta_store"] = {"kind": meta_mod.get().kind, "pgvector_extension": meta_mod.get().pgvector}
    try:
        e = embeddings.get("auto")
        out["embedder_auto"] = {"name": e.name, "dim": e.dim, "offline": e.offline}
    except Exception as ex:  # noqa: BLE001
        out["embedder_auto"] = {"error": str(ex)}
    return out


# ── collections ──────────────────────────────────────────────────────────
class CollectionIn(BaseModel):
    name: str
    backend: str | None = None
    embedder: str | None = "auto"
    chunker: str = "structure"
    chunk_size: int = Field(380, ge=80, le=2000)
    description: str = ""


@api.get("/collections")
def list_collections():
    _need_ready()
    return {"collections": meta_mod.get().collections()}


@api.post("/collections")
def create_collection(body: CollectionIn):
    _need_ready()
    try:
        return ingest.create_collection(body.name, body.backend, body.embedder, body.chunker, body.chunk_size, body.description)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@api.get("/collections/{name}")
def get_collection(name: str):
    _need_ready()
    try:
        coll = ingest.get_collection(name)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    m = meta_mod.get()
    docs = m.documents(int(coll["id"]))
    try:
        coll["vector_count"] = stores.get(coll["backend"]).count(coll)
    except Exception as e:  # noqa: BLE001
        coll["vector_count"] = None
        coll["backend_error"] = str(e)
    by_type: dict[str, int] = {}
    for d in docs:
        by_type[d.get("doc_type") or "?"] = by_type.get(d.get("doc_type") or "?", 0) + 1
    return {"collection": coll, "documents": docs, "by_type": by_type}


@api.delete("/collections/{name}")
def delete_collection(name: str):
    _need_ready()
    try:
        ingest.drop_collection(name)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    return {"ok": True}


@api.get("/collections/{name}/documents/{did}")
def get_document(name: str, did: int):
    _need_ready()
    m = meta_mod.get()
    d = m.document(did)
    if not d:
        raise HTTPException(404, "no such document")
    return {"document": d, "chunks": m.doc_chunks(did)}


@api.delete("/collections/{name}/documents/{did}")
def delete_document(name: str, did: int):
    _need_ready()
    try:
        ingest.delete_document(ingest.get_collection(name), did)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    return {"ok": True}


# ── ingestion ────────────────────────────────────────────────────────────
def _coll(name: str) -> dict[str, Any]:
    _need_ready()
    try:
        return ingest.get_collection(name)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@api.post("/collections/{name}/ingest/upload")
async def ingest_upload(name: str, files: list[UploadFile] = File(...), doc_type: str = Form(""), system: str = Form(""),
                        owner: str = Form(""), version: str = Form(""), effective_date: str = Form(""),
                        acl_group: str = Form(""), tags: str = Form("")):
    coll = _coll(name)
    payload = [(f.filename or "upload.txt", await f.read()) for f in files]
    meta = {k: v for k, v in {"doc_type": doc_type, "system": system, "owner": owner, "version": version,
                              "effective_date": effective_date, "acl_group": acl_group, "tags": tags}.items() if v}
    job = ingest.start_job("upload", name, f"{len(payload)} file(s)", lambda j: ingest.ingest_uploads(j, coll, payload, meta))
    return job.to_dict(full=False)


@api.post("/collections/{name}/ingest/corpus")
def ingest_corpus(name: str):
    coll = _coll(name)
    return ingest.start_job("corpus", name, "built-in demo corpus", lambda j: ingest.ingest_corpus(j, coll)).to_dict(False)


@api.post("/collections/{name}/ingest/lake")
def ingest_lake(name: str, body: dict = Body(...)):
    coll = _coll(name)
    path = str(body.get("path") or "")
    try:
        ingest.safe_lake_path(path)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    return ingest.start_job("lake", name, path or "/", lambda j: ingest.ingest_lake(j, coll, path)).to_dict(False)


@api.post("/collections/{name}/ingest/sharepoint")
def ingest_sharepoint(name: str, body: dict = Body(default={})):
    coll = _coll(name)
    folder, site = body.get("folder") or config.get().sharepoint_folder, body.get("site") or None
    return ingest.start_job("sharepoint", name, f"{site or 'default'}:/{folder}",
                            lambda j: ingest.ingest_sharepoint(j, coll, folder, site)).to_dict(False)


@api.post("/collections/{name}/ingest/url")
def ingest_url(name: str, body: dict = Body(...)):
    coll = _coll(name)
    url = str(body.get("url") or "")
    return ingest.start_job("url", name, url, lambda j: ingest.ingest_url(j, coll, url)).to_dict(False)


@api.post("/collections/{name}/reindex")
def reindex(name: str):
    coll = _coll(name)
    return ingest.start_job("reindex", name, "re-chunk + re-embed", lambda j: ingest.reindex(j, coll)).to_dict(False)


@api.get("/jobs")
def jobs():
    return {"jobs": [j.to_dict(full=False) for j in sorted(ingest.JOBS.values(), key=lambda j: j.started, reverse=True)]}


@api.get("/jobs/{jid}")
def job(jid: str):
    j = ingest.JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return j.to_dict()


@api.get("/lake")
def lake(path: str = ""):
    try:
        p = ingest.safe_lake_path(path)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    if not p.is_dir():
        return {"path": path, "exists": False, "dirs": [], "files": 0}
    dirs = sorted(c.name for c in p.iterdir() if c.is_dir() and not c.name.startswith("."))
    files = sum(1 for c in p.iterdir() if c.is_file())
    return {"path": path, "exists": True, "dirs": dirs, "files": files}


@api.get("/sharepoint")
def sharepoint(folder: str = ""):
    try:
        from bsp_connectors import m365
        folder = folder or config.get().sharepoint_folder
        items = m365.list_files(None, folder, recursive=True)
        return {"mode": m365.mode() if hasattr(m365, "mode") else "?", "folder": folder,
                "files": [{"name": i.get("name"), "size": i.get("size"), "lastModifiedDateTime": i.get("lastModifiedDateTime"),
                           "parentPath": i.get("parentPath")} for i in items if not i.get("name", "").endswith(".meta.yaml")]}
    except Exception as e:  # noqa: BLE001
        return {"mode": "unavailable", "error": f"{type(e).__name__}: {e}", "files": []}


# ── retrieval / answers ──────────────────────────────────────────────────
class SearchIn(BaseModel):
    query: str
    collection: str | None = None
    k: int = Field(8, ge=1, le=30)
    mode: str = "hybrid"
    filters: dict[str, Any] | None = None
    rewrite: bool = True
    mmr: bool = True
    mmr_lambda: float = 0.7
    rerank: str = "auto"
    use_llm: bool = True


@api.post("/search")
def search(body: SearchIn, request: Request):
    _need_ready()
    try:
        res = retrieval.search(body.collection or config.get().default_collection, body.query, k=body.k, filters=body.filters,
                               mode=body.mode, rewrite=body.rewrite, use_mmr=body.mmr, mmr_lambda=body.mmr_lambda,
                               rerank=body.rerank, use_llm=body.use_llm)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e
    retrieval.log_search(res, user=user_of(request), mode="search")
    return res


class AskIn(BaseModel):
    question: str
    collection: str | None = None
    k: int = Field(6, ge=1, le=12)
    filters: dict[str, Any] | None = None
    mode: str = "hybrid"
    use_llm: bool = True


@api.post("/ask")
def ask(body: AskIn, request: Request):
    _need_ready()
    try:
        return answer_mod.answer(body.collection or config.get().default_collection, body.question, k=body.k,
                                 filters=body.filters, mode=body.mode, use_llm=body.use_llm, user=user_of(request))
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


class AssistIn(BaseModel):
    text: str
    channel: str = "email"
    collection: str | None = None
    use_llm: bool = True


@api.post("/frontoffice/agent-assist")
def agent_assist(body: AssistIn, request: Request):
    _need_ready()
    try:
        return frontoffice.agent_assist(body.text, body.collection, body.channel, body.use_llm, user_of(request))
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


class CopilotIn(BaseModel):
    text: str
    collection: str | None = None
    use_llm: bool = True


@api.post("/frontoffice/runbook")
def runbook(body: CopilotIn, request: Request):
    _need_ready()
    try:
        return frontoffice.runbook_copilot(body.text, body.collection, body.use_llm, user_of(request))
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@api.post("/frontoffice/design")
def design(body: CopilotIn, request: Request):
    _need_ready()
    try:
        return frontoffice.design_qa(body.text, body.collection, body.use_llm, user_of(request))
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@api.get("/frontoffice/samples")
def samples():
    return {"agent_assist": frontoffice.SAMPLES, "runbook": frontoffice.RUNBOOK_SAMPLES, "design": frontoffice.DESIGN_SAMPLES}


# ── evals / logs ─────────────────────────────────────────────────────────
class EvalIn(BaseModel):
    collection: str | None = None
    presets: list[str] = ["hybrid+rerank+mmr"]
    k: int = 5
    with_answers: bool = True
    use_llm: bool = True


@api.post("/evals/run")
def run_evals(body: EvalIn):
    _need_ready()
    name = body.collection or config.get().default_collection
    coll = _coll(name)
    bad = [p for p in body.presets if p not in evals.PRESETS]
    if bad:
        raise HTTPException(400, f"unknown presets {bad}; choose from {list(evals.PRESETS)}")

    def work(j: ingest.Job) -> None:
        j.total = len(body.presets)
        for p in body.presets:
            j.current = p
            r = evals.run(coll["name"], p, k=body.k, with_answers=body.with_answers, use_llm=body.use_llm)
            j.results.append({"name": p, "status": "done", "eval_id": r["id"], **{k: v for k, v in r["metrics"].items()}})
            j.note(f"{p}: hit@5={r['metrics']['hit@5']} mrr={r['metrics']['mrr']}")
            j.done += 1

    return ingest.start_job("eval", name, ", ".join(body.presets), work).to_dict(False)


@api.get("/evals")
def list_evals():
    _need_ready()
    return {"runs": meta_mod.get().evals(), "presets": list(evals.PRESETS), "golden": len(evals.load_golden())}


@api.get("/evals/leaderboard")
def leaderboard():
    _need_ready()
    return {"rows": evals.leaderboard()}


@api.get("/evals/{rid}")
def eval_run(rid: int):
    _need_ready()
    r = meta_mod.get().eval_run(rid)
    if not r:
        raise HTTPException(404, "no such eval run")
    return r


@api.get("/golden")
def golden():
    return {"questions": evals.load_golden()}


@api.get("/queries")
def queries(limit: int = 100):
    _need_ready()
    return {"queries": meta_mod.get().queries(min(limit, 500))}


app.include_router(api)


# ── OpenAPI tool server (/rag/tools) ─────────────────────────────────────
tools_app = FastAPI(title="DataPlatform Knowledge (RAG) tools", version=__version__,
                    description="Governed retrieval over DataPlatform front-office runbooks, policies and design docs "
                                "(synthetic demo corpus). Answers carry numbered citations; 'I don't know' when not covered.")


def _tools_auth(request: Request) -> None:
    tok = config.get().tools_token
    if tok and request.headers.get("authorization", "") != f"Bearer {tok}":
        raise HTTPException(401, "bearer token required")


class TSearch(BaseModel):
    query: str = Field(..., description="Question or keywords, e.g. 'late fee reversal autopay failure'")
    collection: str = Field(tools_mod.DEFAULT_COLLECTION, description="Knowledge collection")
    k: int = Field(6, ge=1, le=10, description="Number of passages")
    doc_type: str = Field("", description="Optional filter: runbook | policy | design | faq | reference")


class TAnswer(BaseModel):
    question: str = Field(..., description="Question in plain language")
    collection: str = Field(tools_mod.DEFAULT_COLLECTION, description="Knowledge collection")
    doc_type: str = Field("", description="Optional filter: runbook | policy | design | faq | reference")


class TAssist(BaseModel):
    customer_message: str = Field(..., description="Customer email / chat / call transcript text")
    channel: str = Field("email", description="email | chat | call | ticket")
    collection: str = Field(tools_mod.DEFAULT_COLLECTION, description="Knowledge collection")


class TCopilot(BaseModel):
    situation: str = Field(..., description="Incident description, e.g. 'mass LOS in wire center RLGHNCXA, 6,000 customers'")
    collection: str = Field(tools_mod.DEFAULT_COLLECTION, description="Knowledge collection")


def _tool(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        raise _err(e) from e


@tools_app.post("/search", operation_id="search_knowledge", summary="Search the front-office knowledge base",
                description=tools_mod.rag_search.__doc__)
def t_search(body: TSearch, request: Request):
    _tools_auth(request)
    _need_ready()
    return _tool(tools_mod.rag_search, body.query, body.collection, body.k, body.doc_type)


@tools_app.post("/answer", operation_id="answer_with_citations", summary="Answer from runbooks/policies with citations",
                description=tools_mod.rag_answer.__doc__)
def t_answer(body: TAnswer, request: Request):
    _tools_auth(request)
    _need_ready()
    return _tool(tools_mod.rag_answer, body.question, body.collection, body.doc_type)


@tools_app.get("/collections", operation_id="list_knowledge_collections", summary="List knowledge collections",
               description=tools_mod.rag_list_collections.__doc__)
def t_collections(request: Request):
    _tools_auth(request)
    _need_ready()
    return _tool(tools_mod.rag_list_collections)


@tools_app.post("/agent-assist", operation_id="agent_assist", summary="Agent assist for a customer message",
                description=tools_mod.rag_agent_assist.__doc__)
def t_assist(body: TAssist, request: Request):
    _tools_auth(request)
    _need_ready()
    return _tool(tools_mod.rag_agent_assist, body.customer_message, body.channel, body.collection)


@tools_app.post("/runbook-copilot", operation_id="runbook_copilot", summary="Outage / incident runbook copilot",
                description=tools_mod.rag_runbook_copilot.__doc__)
def t_copilot(body: TCopilot, request: Request):
    _tools_auth(request)
    _need_ready()
    return _tool(tools_mod.rag_runbook_copilot, body.situation, body.collection)


app.mount(PREFIX + "/tools", tools_app)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):  # pragma: no cover
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)
