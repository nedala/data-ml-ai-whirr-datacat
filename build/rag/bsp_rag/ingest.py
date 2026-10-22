"""Collections + ingestion pipeline and sources.

Sources: file upload · lake folder · SharePoint folder (bsp_connectors.m365,
incremental by lastModifiedDateTime/size/eTag, delete propagation) · URL ·
the built-in demo corpus. Each document: parse → structure-aware chunks →
dedupe (document content hash; unchanged documents are skipped, identical
content under another URI is recorded as a duplicate) → embed (cached) →
upsert to the collection's backend; re-ingest replaces the old chunks.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from . import chunking, config, embeddings, meta as meta_mod, parsing, stores
from .stores import Point
from .textutil import sparse_doc

log = logging.getLogger("bsp_rag.ingest")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,47}$")
META_KEYS = ("doc_key", "title", "doc_type", "system", "owner", "version", "effective_date", "acl_group", "tags")
DOC_TYPES = ("runbook", "design", "policy", "faq", "reference")


class IngestError(RuntimeError):
    pass


# ── collections ──────────────────────────────────────────────────────────
def create_collection(name: str, backend: str | None = None, embedder: str | None = None, chunker: str = "structure",
                      chunk_size: int = 380, description: str = "") -> dict[str, Any]:
    m = meta_mod.get()
    name = name.strip().lower()
    if not NAME_RE.match(name):
        raise IngestError("collection name: 2–48 chars, lowercase letters, digits, - and _")
    if m.collection(name):
        raise IngestError(f"collection {name!r} already exists")
    backend = (backend or config.get().backend).lower()
    if chunker not in chunking.CHUNKERS:
        raise IngestError(f"chunker must be one of {chunking.CHUNKERS}")
    store = stores.get(backend)
    emb = embeddings.get(embedder or "auto")
    coll = m.create_collection(name, backend, emb.name, emb.dim, chunker, int(chunk_size), description,
                               {"embed_offline": emb.offline})
    try:
        store.ensure_collection(coll)
    except Exception:
        m.delete_collection(coll["id"])
        raise
    return coll


def drop_collection(name: str) -> None:
    m = meta_mod.get()
    coll = m.collection(name)
    if not coll:
        raise IngestError(f"no collection {name!r}")
    try:
        stores.get(coll["backend"]).drop_collection(coll)
    finally:
        m.delete_collection(coll["id"])


def get_collection(name: str) -> dict[str, Any]:
    coll = meta_mod.get().collection(name)
    if not coll:
        raise IngestError(f"no collection {name!r}")
    return coll


# ── metadata helpers ─────────────────────────────────────────────────────
def infer_doc_type(name: str) -> str:
    n = PurePosixPath(name).name.lower()
    for pfx, t in (("rb-", "runbook"), ("ds-", "design"), ("pol-", "policy"), ("faq-", "faq"), ("trn-", "reference")):
        if n.startswith(pfx):
            return t
    low = name.lower()
    for word, t in (("runbook", "runbook"), ("playbook", "runbook"), ("design", "design"), ("architecture", "design"),
                    ("policy", "policy"), ("faq", "faq")):
        if word in low:
            return t
    return "reference"


def norm_meta(raw: dict[str, Any], filename: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    out["doc_key"] = str(raw.get("doc_id") or raw.get("doc_key") or PurePosixPath(filename).stem)
    for k in ("title", "system", "owner", "version", "acl_group"):
        if raw.get(k) not in (None, ""):
            out[k] = str(raw[k])
    if raw.get("effective_date"):
        out["effective_date"] = str(raw["effective_date"])
    dt = str(raw.get("doc_type") or "").lower() or infer_doc_type(filename)
    out["doc_type"] = dt if dt in DOC_TYPES else "reference"
    tags = raw.get("tags") or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    out["tags"] = [str(t) for t in tags]
    out.setdefault("acl_group", "all")
    return out


def _clean_blocks(doc: parsing.ParsedDoc) -> bool:
    """Drop the 'SYNTHETIC EXAMPLE' banner from chunk text (kept as metadata)."""
    synthetic = False
    keep = []
    for b in doc.blocks:
        if b.kind in ("quote", "para") and b.text.upper().startswith("SYNTHETIC EXAMPLE"):
            synthetic = True
            continue
        keep.append(b)
    doc.blocks = keep
    return synthetic


def _raw_path(cid: int, content_hash: str, filename: str) -> Path:
    return config.get().data_dir / "raw" / str(cid) / (content_hash[:24] + parsing.ext_of(filename))


# ── core: ingest one document ────────────────────────────────────────────
def ingest_bytes(coll: dict[str, Any], data: bytes, filename: str, *, source: str, uri: str,
                 meta: dict[str, Any] | None = None, etag: str | None = None, last_modified: str | None = None,
                 force: bool = False) -> dict[str, Any]:
    m = meta_mod.get()
    cid = int(coll["id"])
    h = hashlib.sha256(data).hexdigest()
    existing = m.document_by_uri(cid, uri)
    if existing and existing.get("content_hash") == h and existing.get("status") == "indexed" and not force:
        return {"uri": uri, "status": "unchanged", "document_id": existing["id"], "n_chunks": existing.get("n_chunks", 0)}
    dup = m.one(f"SELECT id, uri FROM {m.t('documents')} WHERE collection_id=%s AND content_hash=%s AND uri<>%s AND status='indexed'",
                (cid, h, uri))
    if dup and not force:
        did = m.upsert_document(cid, {"uri": uri, "source": source, "title": PurePosixPath(filename).name, "content_hash": h,
                                      "status": "duplicate", "error": f"same content as {dup['uri']}", "n_chunks": 0,
                                      "doc_type": infer_doc_type(filename)})
        return {"uri": uri, "status": "duplicate", "document_id": did, "duplicate_of": dup["uri"]}

    doc = parsing.parse(data, filename, meta)
    synthetic = _clean_blocks(doc)
    md = norm_meta({**doc.meta, **(meta or {})}, filename)
    md["title"] = md.get("title") or doc.title
    chunks = chunking.chunk(doc, coll.get("chunker") or "structure", int(coll.get("chunk_size") or 380))
    if not chunks:
        raise IngestError(f"{filename}: no text extracted")

    store = stores.get(coll["backend"])
    emb = embeddings.for_collection(coll["embed_model"], int(coll["embed_dim"]))
    vectors = embeddings.cache().embed(emb, [c.text for c in chunks])

    doc_row = {**md, "uri": uri, "source": source, "content_hash": h, "etag": etag, "last_modified": last_modified,
               "meta": {"tags": md.get("tags", []), "synthetic": synthetic, "filename": PurePosixPath(filename).name,
                        "bytes": len(data), "extra": {k: v for k, v in doc.meta.items() if k not in META_KEYS and k != "doc_id"
                                                      and isinstance(v, (str, int, float, bool))}},
               "n_chunks": len(chunks), "status": "indexed", "error": None}
    did = m.upsert_document(cid, doc_row)
    if existing:
        store.delete_document(coll, did)
    rows, points = [], []
    for c, v in zip(chunks, vectors):
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"bsp-rag:{cid}:{uri}:{c.ordinal}"))
        rows.append({"id": pid, "ordinal": c.ordinal, "section": c.section, "kind": c.kind, "text": c.text,
                     "body": c.body, "tokens": c.tokens, "content_hash": c.hash, "meta": {}})
        points.append(Point(pid, v, sparse_doc(c.text), {
            "chunk_id": pid, "document_id": did, "doc_key": md["doc_key"], "title": md["title"], "section": c.section,
            "kind": c.kind, "text": c.body, "doc_type": md["doc_type"], "system": md.get("system", ""),
            "acl_group": md.get("acl_group", "all"), "tags": md.get("tags", []), "uri": uri, "source": source,
            "ordinal": c.ordinal, "version": md.get("version", ""), "effective_date": md.get("effective_date", "")}))
    m.replace_chunks(cid, did, rows)
    store.upsert(coll, points)
    try:  # keep the original for re-index (chunker changes)
        p = _raw_path(cid, h, filename)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            p.write_bytes(data)
    except OSError:
        pass
    return {"uri": uri, "status": "updated" if existing else "indexed", "document_id": did, "n_chunks": len(chunks),
            "doc_key": md["doc_key"], "title": md["title"]}


def delete_document(coll: dict[str, Any], did: int) -> None:
    m = meta_mod.get()
    stores.get(coll["backend"]).delete_document(coll, did)
    m.delete_document(did)


# ── jobs (live progress for the UI) ──────────────────────────────────────
class Job:
    def __init__(self, kind: str, collection: str, detail: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kind, self.collection, self.detail = kind, collection, detail
        self.status = "running"
        self.total = 0
        self.done = 0
        self.current = ""
        self.results: list[dict[str, Any]] = []
        self.log: list[str] = []
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.finished: str | None = None
        self.error: str | None = None

    def note(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.log[:-400]

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for r in self.results:
            counts[r.get("status", "?")] = counts.get(r.get("status", "?"), 0) + 1
        d = {"id": self.id, "kind": self.kind, "collection": self.collection, "detail": self.detail, "status": self.status,
             "total": self.total, "done": self.done, "current": self.current, "counts": counts, "started": self.started,
             "finished": self.finished, "error": self.error}
        if full:
            d["results"] = self.results[-500:]
            d["log"] = self.log[-200:]
        return d


JOBS: dict[str, Job] = {}


def start_job(kind: str, collection: str, detail: str, fn: Callable[[Job], None], background: bool = True) -> Job:
    job = Job(kind, collection, detail)
    JOBS[job.id] = job
    for old in sorted(JOBS.values(), key=lambda j: j.started)[:-50]:
        JOBS.pop(old.id, None)

    def run():
        try:
            fn(job)
            job.status = "done" if not any(r.get("status") == "error" for r in job.results) else "done_with_errors"
        except Exception as e:  # noqa: BLE001
            job.status, job.error = "failed", f"{type(e).__name__}: {e}"
            job.note("FAILED " + job.error)
            log.warning("job %s failed: %s", job.id, traceback.format_exc())
        job.finished = datetime.now(timezone.utc).isoformat(timespec="seconds")
        job.current = ""

    if background:
        threading.Thread(target=run, daemon=True, name=f"ingest-{job.id}").start()
    else:
        run()
    return job


def _one(job: Job, coll: dict[str, Any], data: bytes, name: str, **kw: Any) -> None:
    job.current = name
    try:
        r = ingest_bytes(coll, data, name, **kw)
    except Exception as e:  # noqa: BLE001
        r = {"uri": kw.get("uri"), "status": "error", "error": f"{type(e).__name__}: {e}"}
    job.results.append({"name": name, **r})
    job.done += 1
    job.note(f"{r['status']:<9} {name}" + (f" ({r.get('n_chunks')} chunks)" if r.get("n_chunks") else "")
             + (f" — {r.get('error')}" if r.get("error") else ""))


# ── sources ──────────────────────────────────────────────────────────────
def _sidecar(path: Path) -> dict[str, Any]:
    sc = path.with_name(path.name + ".meta.yaml")
    return parsing.sidecar_meta(sc.read_bytes()) if sc.exists() else {}


def _walk(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and parsing.ext_of(p.name) in parsing.SUPPORTED
                  and not p.name.endswith(".meta.yaml") and p.name.lower() != "readme.md")


def ingest_folder(job: Job, coll: dict[str, Any], root: Path, scheme: str, source: str, prune: bool = True) -> None:
    if not root.is_dir():
        raise IngestError(f"folder not found: {root}")
    files = _walk(root)
    job.total = len(files)
    job.note(f"{len(files)} files under {root}")
    seen = set()
    for p in files:
        rel = p.relative_to(root).as_posix()
        uri = f"{scheme}{rel}"
        seen.add(uri)
        mtime = datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        _one(job, coll, p.read_bytes(), rel, source=source, uri=uri, meta=_sidecar(p), last_modified=mtime)
    if prune:
        _prune(job, coll, scheme, seen)


def _prune(job: Job, coll: dict[str, Any], prefix: str, seen: set[str]) -> None:
    """Delete propagation: documents under `prefix` that the source no longer has."""
    m = meta_mod.get()
    for d in m.documents(int(coll["id"])):
        if d["uri"].startswith(prefix) and d["uri"] not in seen:
            delete_document(coll, int(d["id"]))
            job.results.append({"name": d["uri"], "uri": d["uri"], "status": "deleted"})
            job.note(f"deleted   {d['uri']} (gone from source)")


def ingest_corpus(job: Job, coll: dict[str, Any]) -> None:
    ingest_folder(job, coll, config.get().corpus_dir, "corpus://", "corpus")


def safe_lake_path(rel: str) -> Path:
    root = config.get().lake_dir.resolve()
    p = (root / rel.strip().lstrip("/")).resolve()
    if p != root and root not in p.parents:
        raise IngestError("path must stay inside the lake")
    return p


def ingest_lake(job: Job, coll: dict[str, Any], rel: str) -> None:
    root = safe_lake_path(rel)
    rel_root = root.relative_to(config.get().lake_dir.resolve()).as_posix()
    ingest_folder(job, coll, root, "lake://" + ("" if rel_root == "." else rel_root + "/"), "lake")


def ingest_sharepoint(job: Job, coll: dict[str, Any], folder: str | None = None, site: str | None = None) -> None:
    from bsp_connectors import m365  # concurrent component; mock-or-live

    folder = (folder or config.get().sharepoint_folder).strip("/")
    items = m365.list_files(site, folder, recursive=True)
    files = [i for i in items if parsing.ext_of(i.get("name", "")) in parsing.SUPPORTED
             and i.get("name", "").lower() != "readme.md"]
    sidecars = {(i.get("parentPath") or "", i["name"][: -len(".meta.yaml")]): i for i in items
                if i.get("name", "").endswith(".meta.yaml")}
    job.total = len(files)
    mode = m365.mode() if hasattr(m365, "mode") else "?"
    job.note(f"SharePoint ({mode}) {site or 'default site'} /{folder}: {len(files)} files")
    m = meta_mod.get()
    prefix = f"sharepoint://{site or 'default'}/{folder}/"
    seen = set()
    for it in files:
        parent = (it.get("parentPath") or "").strip("/")
        rel = (parent + "/" if parent else "") + it["name"]
        rel_in = rel[len(folder) + 1:] if rel.lower().startswith(folder.lower() + "/") else rel
        uri = prefix + rel_in
        seen.add(uri)
        change_key = it.get("eTag") or it.get("cTag") or f"{it.get('lastModifiedDateTime')}|{it.get('size')}"
        existing = m.document_by_uri(int(coll["id"]), uri)
        if existing and existing.get("etag") == change_key and existing.get("status") == "indexed":
            job.results.append({"name": rel_in, "uri": uri, "status": "unchanged", "document_id": existing["id"]})
            job.done += 1
            continue
        side = sidecars.get((it.get("parentPath") or "", it["name"]))
        md = parsing.sidecar_meta(m365.download_file(site, side["id"])) if side else {}
        md.setdefault("web_url", it.get("webUrl"))
        _one(job, coll, m365.download_file(site, it["id"]), rel_in, source="sharepoint", uri=uri, meta=md,
             etag=change_key, last_modified=it.get("lastModifiedDateTime"))
    _prune(job, coll, prefix, seen)


def ingest_url(job: Job, coll: dict[str, Any], url: str) -> None:
    import httpx

    if not re.match(r"^https?://", url):
        raise IngestError("URL must start with http:// or https://")
    job.total = 1
    r = httpx.get(url, follow_redirects=True, timeout=60, headers={"User-Agent": "bsp-rag-ingest/1.0"})
    r.raise_for_status()
    ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
    name = PurePosixPath(httpx.URL(url).path).name or "index.html"
    if parsing.ext_of(name) not in parsing.SUPPORTED:
        name += {"application/pdf": ".pdf", "text/plain": ".txt", "text/markdown": ".md"}.get(ctype, ".html")
    _one(job, coll, r.content, name, source="url", uri=url, meta={"web_url": url})


def ingest_uploads(job: Job, coll: dict[str, Any], files: list[tuple[str, bytes]], meta: dict[str, Any]) -> None:
    job.total = len(files)
    for name, data in files:
        _one(job, coll, data, name, source="upload", uri=f"upload://{name}", meta=dict(meta))


def reindex(job: Job, coll: dict[str, Any]) -> None:
    """Re-chunk + re-embed every document from its stored original."""
    m = meta_mod.get()
    docs = [d for d in m.documents(int(coll["id"])) if d["status"] == "indexed"]
    job.total = len(docs)
    for d in docs:
        full = m.document(int(d["id"])) or {}
        fn = (full.get("meta") or {}).get("filename") or d["uri"].rsplit("/", 1)[-1]
        p = _raw_path(int(coll["id"]), full.get("content_hash") or "", fn)
        if not p.exists():
            job.results.append({"name": d["uri"], "status": "error", "error": "original not stored; re-ingest from source"})
            job.done += 1
            continue
        md = {k: full.get(k) for k in ("doc_type", "system", "owner", "version", "effective_date", "acl_group", "title")}
        md["doc_id"] = full.get("doc_key")
        md["tags"] = (full.get("meta") or {}).get("tags")
        _one(job, coll, p.read_bytes(), fn, source=full.get("source") or "reindex", uri=d["uri"], meta=md,
             etag=full.get("etag"), last_modified=full.get("last_modified"), force=True)


def ensure_demo(collection: str | None = None, backend: str | None = None, embedder: str | None = None) -> dict[str, Any]:
    """Create the demo collection and ingest the corpus if it does not exist (startup autoseed)."""
    name = collection or config.get().default_collection
    m = meta_mod.get()
    coll = m.collection(name)
    if coll is None:
        coll = create_collection(name, backend=backend, embedder=embedder,
                                 description="Front-office runbooks, design docs, policies and FAQs (synthetic demo corpus)")
    job = start_job("corpus", name, str(config.get().corpus_dir), lambda j: ingest_corpus(j, coll), background=False)
    return job.to_dict(full=False)
