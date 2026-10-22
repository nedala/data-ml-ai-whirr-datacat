"""CCAI Call Insights — FastAPI app, served natively under /ccai (nginx passes the prefix through).

    python -m bsp_ccai            # :8100

Pages: /ccai/ (single-page portal). API: /ccai/api/*. Health: /ccai/healthz.
State-changing API calls require the `X-Studio: 1` header (CSRF guard; BSP.api sends it).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, exports, ingest, source, tagger, taxonomy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("ccai")
P = config.PREFIX


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(db.init_schema)
    tax = taxonomy.load()
    errs = taxonomy.validate(tax)
    if errs:
        log.error("taxonomy %s has %d validation problem(s): %s", tax.path, len(errs), errs[:5])
    await asyncio.to_thread(taxonomy.sync_to_db, tax)
    await asyncio.to_thread(ingest.mark_interrupted)
    import threading
    threading.Thread(target=tagger.offline_model, args=(tax,), daemon=True, name="ccai-warmup").start()
    if config.env("CCAI_SCHEDULER", "1") not in ("0", "false", "no"):
        ingest.SCHEDULER.start()
    yield
    ingest.SCHEDULER.stop()


app = FastAPI(title="CCAI Call Insights", version=__version__, lifespan=lifespan,
              docs_url=f"{P}/api/docs", openapi_url=f"{P}/api/openapi.json", redoc_url=None)
SAFE = {"GET", "HEAD", "OPTIONS"}


class Guard:
    """Pure ASGI (keeps SSE streaming intact): prefix normalisation, CSRF header, security headers."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        path = scope.get("path", "")
        if not path.startswith(P):  # direct access on :8100 without the prefix
            scope = dict(scope, path=P + path if path != "/" else P + "/")
            path = scope["path"]
        is_api = path.startswith(f"{P}/api/")
        if is_api and scope["method"] not in SAFE:
            hdrs = dict(scope.get("headers") or [])
            if hdrs.get(b"x-studio") != b"1":
                return await JSONResponse({"detail": "Missing X-Studio header (CSRF guard)."}, status_code=403)(scope, receive, send)
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin")]
        if is_api:
            extra.append((b"cache-control", b"no-store"))
            if path.endswith("/events"):
                extra.append((b"x-accel-buffering", b"no"))
        elif "/static/" in path or "/ui-kit/" in path:
            extra.append((b"cache-control", b"no-cache"))

        async def wrapped(message):
            if message["type"] == "http.response.start":
                have = {k.lower() for k, _ in message.get("headers", [])}
                message["headers"] = list(message.get("headers", [])) + [(k, v) for k, v in extra if k not in have]
            await send(message)

        return await self.inner(scope, receive, wrapped)


app.add_middleware(Guard)
if config.UI_KIT_DIR.is_dir():
    app.mount(f"{P}/ui-kit", StaticFiles(directory=str(config.UI_KIT_DIR)), name="ui-kit")
app.mount(f"{P}/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")

r = APIRouter(prefix=P)


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def _clean(row: dict) -> dict:
    return {k: _iso(v) for k, v in row.items()}


def _user(request: Request, fallback: str | None = None) -> str:
    for h in ("x-studio-user", "x-forwarded-user", "x-auth-request-email", "x-remote-user", "x-user"):
        if request.headers.get(h):
            return request.headers[h][:120]
    return (fallback or "ccai-manager")[:120]


# ── pages + health ───────────────────────────────────────────────────────
@r.get("", include_in_schema=False)
def _root_redirect():
    return RedirectResponse(f"{P}/")


@r.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html", headers={"cache-control": "no-cache"})


@r.get("/healthz")
def healthz():
    return {"ok": True, "service": "ccai", "version": __version__, "db": db.healthy()}


# ── overview / status ────────────────────────────────────────────────────
def _counts() -> dict:
    f = db.one("""select count(*) as discovered,
                  count(*) filter (where status in ('downloaded','parsed')) as downloaded,
                  count(*) filter (where status = 'parsed' and kind = 'call') as parsed_files,
                  count(*) filter (where kind = 'manifest') as manifests,
                  count(*) filter (where status = 'error') as errors,
                  coalesce(sum(size),0) as bytes from ccai.source_files""") or {}
    c = db.one("""select count(*) as calls, count(*) filter (where status = 'tagged') as tagged,
                  count(*) filter (where status = 'parsed') as pending_tag, count(*) filter (where status = 'tag_error') as tag_errors,
                  coalesce(sum(duration_sec),0) as talk_seconds, min(started_at) as first_call, max(started_at) as last_call
                  from ccai.calls""") or {}
    t = db.one("""select count(*) as tags, avg(confidence) as avg_conf,
                  count(*) filter (where churn_risk) as churn,
                  count(*) filter (where confidence < %s and reviewed_at is null) as needs_review,
                  count(*) filter (where reviewed_at is not null) as reviewed,
                  count(*) filter (where route = 'llm') as llm, count(*) filter (where route <> 'llm') as offline,
                  avg(case when resolution_status = 'resolved' then 1.0 else 0 end) as fcr
                  from ccai.call_tags where is_current""",
               (float(ingest.current_settings()["confidence_threshold"]),)) or {}
    return {"files": _clean(f), "calls": _clean(c), "tags": _clean(t)}


@r.get("/api/status")
def status():
    st = ingest.current_settings()
    tax = taxonomy.load()
    last = db.one("select * from ccai.ingest_runs order by run_id desc limit 1")
    last_ok = db.one("select finished_at from ccai.ingest_runs where status in ('succeeded','partial') order by run_id desc limit 1")
    tp = db.one("""select sum(calls_tagged) as n, sum(tag_ms) as ms, sum(calls_parsed) as p, sum(parse_ms + download_ms) as pms
                   from (select * from ccai.ingest_runs where status in ('succeeded','partial') order by run_id desc limit 5) x""") or {}
    nd = ingest.SCHEDULER.compute_next(st) if st.get("schedule_enabled") else None
    return {"counts": _counts(), "progress": ingest.RUNNER.progress, "last_run": _clean(last) if last else None,
            "last_sync": _iso(last_ok["finished_at"]) if last_ok else None, "next_run": _iso(nd),
            "throughput": {"tag_calls_per_min": round(60000 * tp["n"] / tp["ms"], 1) if tp.get("n") and tp.get("ms") else None,
                           "ingest_files_per_min": round(60000 * tp["p"] / tp["pms"], 1) if tp.get("p") and tp.get("pms") else None},
            "source": source.describe(), "llm": {"available": tagger.llm_available(), "route": st["route"],
                                                "classify_alias": st["classify_alias"], "embed_alias": st["embed_alias"]},
            "taxonomy": {"version": tax.version, "leaves": len(tax.leaves), "digest": tax.digest},
            "settings": st, "version": __version__}


@r.get("/api/events")
async def events(request: Request):
    """Server-sent events: the live progress object whenever it changes (+ a heartbeat every 15 s)."""
    async def gen():
        seen, beat = -1, time.time()
        yield "retry: 3000\n\n"
        while not await request.is_disconnected():
            v = ingest.RUNNER.version
            if v != seen:
                seen = v
                yield f"event: progress\ndata: {json.dumps(ingest.RUNNER.progress, default=str)}\n\n"
                beat = time.time()
            elif time.time() - beat > 15:
                yield ": keep-alive\n\n"
                beat = time.time()
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")


@r.get("/api/overview")
def overview():
    top = db.q("""select coalesce(override_leaf, primary_leaf) as leaf, count(*) as n, avg(confidence) as conf,
                  count(*) filter (where churn_risk) as churn from ccai.call_tags where is_current
                  group by 1 order by n desc limit 10""")
    by = taxonomy.load().by_id
    for t in top:
        lf = by.get(t["leaf"])
        t.update(name=lf.name if lf else t["leaf"], l1=lf.l1_name if lf else "")
    daily = db.q("""select to_char(date_trunc('day', c.started_at), 'YYYY-MM-DD') as day, count(*) as calls,
                    count(*) filter (where t.churn_risk) as churn, avg(t.confidence) as conf
                    from ccai.calls c left join ccai.call_tags t on t.call_id = c.call_id and t.is_current
                    where c.started_at is not null group by 1 order by 1""")
    res = db.q("select resolution_status as k, count(*) as n from ccai.call_tags where is_current group by 1 order by 2 desc")
    sent = db.q("select sentiment_trajectory as k, count(*) as n from ccai.call_tags where is_current group by 1 order by 2 desc")
    l1 = {}
    for row in db.q("select coalesce(override_leaf, primary_leaf) as leaf, count(*) as n from ccai.call_tags where is_current group by 1"):
        lf = by.get(row["leaf"])
        k = lf.l1_name if lf else "Unknown"
        l1[k] = l1.get(k, 0) + row["n"]
    return {"top_reasons": top, "daily": daily, "resolution": res, "sentiment": sent,
            "l1": sorted(({"k": k, "n": v} for k, v in l1.items()), key=lambda x: -x["n"])}


# ── runs ─────────────────────────────────────────────────────────────────
@r.get("/api/runs")
def runs(limit: int = 100):
    return [_clean(x) for x in db.q("select * from ccai.ingest_runs order by run_id desc limit %s", (limit,))]


@r.post("/api/runs")
def start_run(body: dict = Body(default={})):
    try:
        rid = ingest.RUNNER.start(trigger=body.get("trigger", "manual"), mode=body.get("mode", "incremental"),
                                  retry_run_id=body.get("retry_run_id"))
    except ingest.Busy as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"run_id": rid, "progress": ingest.RUNNER.progress}


@r.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    run = db.one("select * from ccai.ingest_runs where run_id = %s", (run_id,))
    if not run:
        raise HTTPException(404, "run not found")
    files = db.q("""select item_id, path, name, size, kind, format, status, error, call_id, last_modified, updated_at
                    from ccai.source_files where last_run_id = %s order by status = 'error' desc, path""", (run_id,))
    tags = db.q("""select route, count(*) as n, avg(confidence) as conf, count(*) filter (where confidence_band = 'low') as low
                   from ccai.call_tags where run_id = %s group by route""", (run_id,))
    return {"run": _clean(run), "files": [_clean(f) for f in files], "tags": tags,
            "errors": [_clean(f) for f in files if f["status"] == "error"]}


@r.post("/api/runs/{run_id}/retry")
def retry_run(run_id: int):
    try:
        rid = ingest.RUNNER.start(trigger="retry", mode="retry_failed", retry_run_id=run_id)
    except ingest.Busy as e:
        raise HTTPException(409, str(e))
    return {"run_id": rid}


@r.post("/api/runs/cancel")
def cancel_run():
    return {"cancelled": ingest.RUNNER.cancel()}


# ── calls ────────────────────────────────────────────────────────────────
SORTS = {"started_at": "c.started_at", "confidence": "t.confidence", "duration": "c.duration_sec",
         "sentiment": "c.avg_customer_sentiment", "leaf": "coalesce(t.override_leaf, t.primary_leaf)"}


@r.get("/api/calls")
def calls(q: str | None = None, leaf: str | None = None, l1: str | None = None, l2: str | None = None,
          min_conf: float | None = None, max_conf: float | None = None, band: str | None = None,
          resolution: str | None = None, trajectory: str | None = None, churn: bool | None = None,
          reviewed: bool | None = None, run_id: int | None = None, date_from: str | None = None, date_to: str | None = None,
          status: str | None = None, sort: str = "started_at", desc: bool = True,
          limit: int = Query(50, le=1000), offset: int = 0):
    where, params = ["true"], []
    if q:
        where.append("(c.call_id ilike %s or c.ban ilike %s or c.agent_id ilike %s or c.transcript_text ilike %s "
                     "or t.summary ilike %s or c.file_name ilike %s)")
        params += [f"%{q}%"] * 6
    if leaf:
        where.append("coalesce(t.override_leaf, t.primary_leaf) = %s")
        params.append(leaf)
    for pref in (l2, l1):
        if pref:
            where.append("coalesce(t.override_leaf, t.primary_leaf) like %s")
            params.append(pref + ".%")
    if min_conf is not None:
        where.append("t.confidence >= %s")
        params.append(min_conf)
    if max_conf is not None:
        where.append("t.confidence < %s")
        params.append(max_conf)
    for col, val in (("t.confidence_band", band), ("t.resolution_status", resolution), ("t.sentiment_trajectory", trajectory),
                     ("c.status", status)):
        if val:
            where.append(f"{col} = %s")
            params.append(val)
    if churn is not None:
        where.append("coalesce(t.churn_risk, false) = %s")
        params.append(churn)
    if reviewed is not None:
        where.append("t.reviewed_at is not null" if reviewed else "t.reviewed_at is null")
    if run_id:
        where.append("(c.run_id = %s or t.run_id = %s)")
        params += [run_id, run_id]
    if date_from:
        where.append("c.started_at >= %s::date")
        params.append(date_from)
    if date_to:
        where.append("c.started_at < %s::date + 1")
        params.append(date_to)
    order = SORTS.get(sort, "c.started_at") + (" desc" if desc else " asc") + " nulls last"
    base = f"from ccai.calls c left join ccai.call_tags t on t.call_id = c.call_id and t.is_current where {' and '.join(where)}"
    total = db.one(f"select count(*) as n {base}", params)["n"]
    rows_ = db.q(f"""select c.call_id, c.started_at, c.ban, c.agent_id, c.queue, c.format, c.duration_sec, c.status,
                     c.avg_customer_sentiment, c.file_name, t.primary_leaf, t.override_leaf, t.secondary, t.confidence,
                     t.confidence_band, t.sentiment_trajectory, t.resolution_status, t.churn_risk, t.route, t.reviewed_by
                     {base} order by {order} limit %s offset %s""", params + [limit, offset])
    by = taxonomy.load().by_id
    for x in rows_:
        lf = by.get(x["override_leaf"] or x["primary_leaf"] or "")
        x["leaf"] = (x["override_leaf"] or x["primary_leaf"])
        x["leaf_name"] = lf.name if lf else None
        x["l1_name"] = lf.l1_name if lf else None
    return {"total": total, "rows": [_clean(x) for x in rows_]}


@r.get("/api/calls/{call_id}")
def call_detail(call_id: str):
    c = db.one("select * from ccai.calls where call_id = %s", (call_id,))
    if not c:
        raise HTTPException(404, "call not found")
    tags = db.q("select * from ccai.call_tags where call_id = %s order by tag_id desc", (call_id,))
    by = taxonomy.load().by_id

    def leaf_info(i):
        lf = by.get(i or "")
        return {"id": i, "name": lf.name, "l1": lf.l1_name, "l2": lf.l2_name, "definition": lf.definition,
                "churn_risk": lf.churn_risk} if lf else {"id": i, "name": i}

    cur = next((t for t in tags if t["is_current"]), None)
    if cur:
        cur = _clean(cur)
        cur["primary"] = leaf_info(cur["primary_leaf"])
        cur["override"] = leaf_info(cur["override_leaf"]) if cur.get("override_leaf") else None
        cur["secondary"] = [{**s, **leaf_info(s["leaf"])} for s in (cur.get("secondary") or [])]
        cur["candidates"] = [{**s, "name": leaf_info(s["leaf"]).get("name")} for s in (cur.get("candidates") or [])]
    c.pop("transcript_text", None)
    return {"call": _clean(c), "tag": cur, "history": [_clean({k: t[k] for k in ("tag_id", "created_at", "primary_leaf", "confidence",
                                                                                 "route", "model", "taxonomy_version", "override_leaf",
                                                                                 "reviewed_by", "run_id")}) for t in tags]}


@r.post("/api/calls/{call_id}/review")
def review(call_id: str, request: Request, body: dict = Body(...)):
    """Reviewer feedback: confirm the tag (override_leaf null) or override it with another leaf."""
    tax = taxonomy.load()
    ov = body.get("override_leaf") or None
    if ov and ov not in tax.by_id:
        raise HTTPException(400, f"unknown leaf id {ov!r}")
    cur = db.one("select tag_id, primary_leaf from ccai.call_tags where call_id = %s and is_current", (call_id,))
    if not cur:
        raise HTTPException(404, "call has no tag yet")
    if ov == cur["primary_leaf"]:
        ov = None
    who = _user(request, body.get("reviewer"))
    db.ex("""update ccai.call_tags set reviewed_by = %s, reviewed_at = now(), override_leaf = %s, review_note = %s
             where tag_id = %s""", (who, ov, (body.get("note") or "")[:500] or None, cur["tag_id"]))
    return {"ok": True, "call_id": call_id, "override_leaf": ov, "reviewed_by": who}


# ── taxonomy ─────────────────────────────────────────────────────────────
@r.get("/api/taxonomy")
def taxonomy_tree(date_from: str | None = None, date_to: str | None = None):
    tax = taxonomy.load()
    where, params = ["t.is_current"], []
    if date_from:
        where.append("c.started_at >= %s::date")
        params.append(date_from)
    if date_to:
        where.append("c.started_at < %s::date + 1")
        params.append(date_to)
    stats = {x["leaf"]: x for x in db.q(f"""select coalesce(t.override_leaf, t.primary_leaf) as leaf, count(*) as n,
                    avg(t.confidence) as conf, count(*) filter (where t.churn_risk) as churn,
                    count(*) filter (where t.confidence_band = 'low') as low,
                    avg(c.avg_customer_sentiment) as sentiment
                    from ccai.call_tags t join ccai.calls c on c.call_id = t.call_id
                    where {' and '.join(where)} group by 1""", params)}
    sec = {x["leaf"]: x["n"] for x in db.q("""select s->>'leaf' as leaf, count(*) as n from ccai.call_tags t,
                    jsonb_array_elements(t.secondary) s where t.is_current group by 1""")}
    total = sum(x["n"] for x in stats.values()) or 0

    def agg(nodes):
        n = sum(x["n"] for x in nodes)
        conf = sum((x["avg_confidence"] or 0) * x["n"] for x in nodes) / n if n else None
        return n, conf, sum(x["churn"] for x in nodes)

    out = []
    for d in tax.domains:
        groups = []
        for g in d["groups"]:
            leaves = []
            for lf in g["leaves"]:
                s = stats.get(lf["id"], {})
                leaves.append({"id": lf["id"], "name": lf["name"], "n": s.get("n", 0), "avg_confidence": s.get("conf"),
                               "churn": s.get("churn", 0), "low": s.get("low", 0), "secondary_n": sec.get(lf["id"], 0),
                               "sentiment": s.get("sentiment"), "churn_risk": bool(lf.get("churn_risk"))})
            n, conf, churn = agg(leaves)
            groups.append({"id": g["id"], "name": g["name"], "n": n, "avg_confidence": conf, "churn": churn, "leaves": leaves})
        n, conf, churn = agg(groups)
        out.append({"id": d["id"], "name": d["name"], "n": n, "avg_confidence": conf, "churn": churn, "groups": groups})
    return {"version": tax.version, "digest": tax.digest, "total": total, "leaves": len(tax.leaves),
            "validation": taxonomy.validate(tax), "tree": out}


@r.get("/api/taxonomy/leaves")
def taxonomy_leaves():
    return [x.to_dict() for x in taxonomy.load().leaves]


@r.get("/api/taxonomy/leaves/{leaf_id}")
def taxonomy_leaf(leaf_id: str):
    lf = taxonomy.load().by_id.get(leaf_id)
    if not lf:
        raise HTTPException(404, "leaf not found")
    return lf.to_dict()


# ── quality ──────────────────────────────────────────────────────────────
@r.get("/api/quality")
def quality():
    st = ingest.current_settings()
    thr = float(st["confidence_threshold"])
    hist = db.q("""select least(width_bucket(confidence, 0, 1, 10), 10) as b, count(*) as n,
                   count(*) filter (where reviewed_at is not null) as reviewed,
                   count(*) filter (where reviewed_at is not null and override_leaf is null) as correct
                   from ccai.call_tags where is_current group by 1 order by 1""")
    bins = [{"lo": i / 10, "hi": (i + 1) / 10, "n": 0, "reviewed": 0, "correct": 0} for i in range(10)]
    for h in hist:
        i = max(1, min(10, int(h["b"]))) - 1
        bins[i].update(n=h["n"], reviewed=h["reviewed"], correct=h["correct"])
    bands = db.q("""select confidence_band as band, count(*) as n, count(*) filter (where reviewed_at is not null) as reviewed,
                    count(*) filter (where reviewed_at is not null and override_leaf is null) as correct
                    from ccai.call_tags where is_current group by 1""")
    summary = db.one("""select count(*) filter (where reviewed_at is not null) as reviewed,
                        count(*) filter (where reviewed_at is not null and override_leaf is null) as confirmed,
                        count(*) filter (where override_leaf is not null) as overridden,
                        count(*) filter (where reviewed_at is null and confidence < %s) as queue,
                        count(*) as total from ccai.call_tags where is_current""", (thr,))
    by = taxonomy.load().by_id
    l1acc: dict[str, dict] = {}
    for x in db.q("""select primary_leaf, override_leaf from ccai.call_tags where is_current and reviewed_at is not null"""):
        k = by[x["primary_leaf"]].l1_name if x["primary_leaf"] in by else "?"
        a = l1acc.setdefault(k, {"l1": k, "reviewed": 0, "correct": 0})
        a["reviewed"] += 1
        a["correct"] += x["override_leaf"] is None
    confusions = db.q("""select primary_leaf, override_leaf, count(*) as n from ccai.call_tags
                         where is_current and override_leaf is not null group by 1, 2 order by 3 desc limit 10""")
    routes = db.q("select route, count(*) as n, avg(confidence) as conf from ccai.call_tags where is_current group by 1")
    acc = (summary["confirmed"] / summary["reviewed"]) if summary and summary["reviewed"] else None
    return {"threshold": thr, "histogram": bins, "bands": bands, "summary": {**summary, "accuracy": acc},
            "by_l1": sorted(l1acc.values(), key=lambda a: -a["reviewed"]), "confusions": confusions, "routes": routes,
            "calibration": tagger.load_calibration()}


@r.get("/api/quality/queue")
def review_queue(threshold: float | None = None, limit: int = 200):
    thr = float(threshold if threshold is not None else ingest.current_settings()["confidence_threshold"])
    rows_ = db.q("""select c.call_id, c.started_at, c.ban, c.queue, t.primary_leaf, t.confidence, t.secondary,
                    t.candidates, t.summary, t.route from ccai.call_tags t join ccai.calls c on c.call_id = t.call_id
                    where t.is_current and t.reviewed_at is null and t.confidence < %s order by t.confidence asc limit %s""",
                 (thr, limit))
    by = taxonomy.load().by_id
    for x in rows_:
        x["leaf_name"] = by[x["primary_leaf"]].name if x["primary_leaf"] in by else x["primary_leaf"]
        x["candidates"] = [{**cnd, "name": by[cnd["leaf"]].name if cnd["leaf"] in by else cnd["leaf"]}
                           for cnd in (x["candidates"] or [])[:5]]
    return {"threshold": thr, "rows": [_clean(x) for x in rows_]}


@r.post("/api/quality/recalibrate")
def recalibrate():
    rows_ = db.q("""select signals, route, override_leaf is null as correct from ccai.call_tags
                    where is_current and reviewed_at is not null""")
    data = [{"features": (x["signals"] or {}).get("features"), "route": x["route"], "correct": x["correct"]} for x in rows_]
    cal = tagger.fit_calibration(data)
    msg = (f"Refitted on {cal['fitted_on']} reviewed calls — re-tag to apply to existing calls" if cal["fitted_on"]
           else "Not enough reviewed calls yet (needs ≥ 20 per route with both confirmed and overridden examples)")
    return {"calibration": cal, "message": msg, "reviewed": len(data)}


# ── exports ──────────────────────────────────────────────────────────────
@r.get("/api/exports")
def export(format: str = "csv", date_from: str | None = None, date_to: str | None = None, run_id: int | None = None,
           min_conf: float | None = None):
    try:
        data, name, n = exports.build(format, date_from, date_to, run_id, min_conf)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return Response(data, media_type=exports.FORMATS[format][0],
                    headers={"content-disposition": f'attachment; filename="{name}"', "x-row-count": str(n)})


@r.get("/api/exports/preview")
def export_preview(date_from: str | None = None, date_to: str | None = None, run_id: int | None = None,
                   min_conf: float | None = None, limit: int = 8):
    df = exports.rows(date_from, date_to, run_id, min_conf)
    head = json.loads(df.head(limit).to_json(orient="records", date_format="iso"))
    return {"rows": len(df), "columns": [{"name": c, "meaning": m} for c, m in exports.COLUMNS], "sample": head}


@r.post("/api/exports/publish")
def export_publish(body: dict = Body(default={})):
    try:
        return exports.publish(body.get("format", "xlsx"), body.get("date_from"), body.get("date_to"), body.get("run_id"),
                               body.get("min_conf"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"publish to SharePoint failed: {e}")


@r.get("/api/exports/history")
def export_history():
    return exports.history()


# ── settings ─────────────────────────────────────────────────────────────
@r.get("/api/settings")
def get_settings():
    return {"settings": ingest.current_settings(), "defaults": config.DEFAULT_SETTINGS, "source": source.describe(),
            "taxonomy_path": str(config.TAXONOMY_PATH), "data_dir": str(config.DATA_DIR)}


@r.put("/api/settings")
def put_settings(body: dict = Body(...)):
    clean: dict[str, Any] = {}
    for k, v in body.items():
        if k not in config.DEFAULT_SETTINGS:
            continue
        d = config.DEFAULT_SETTINGS[k]
        try:
            v = bool(v) if isinstance(d, bool) else int(v) if isinstance(d, int) else float(v) if isinstance(d, float) else str(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"invalid value for {k}")
        clean[k] = v
    if "route" in clean and clean["route"] not in ("auto", "llm", "offline"):
        raise HTTPException(400, "route must be auto, llm or offline")
    if "confidence_threshold" in clean and not 0 < clean["confidence_threshold"] < 1:
        raise HTTPException(400, "confidence_threshold must be between 0 and 1")
    if "schedule_minutes" in clean and not 5 <= clean["schedule_minutes"] <= 7 * 24 * 60:
        raise HTTPException(400, "schedule_minutes must be between 5 and 10080")
    if "concurrency" in clean and not 1 <= clean["concurrency"] <= 16:
        raise HTTPException(400, "concurrency must be between 1 and 16")
    if "publish_format" in clean and clean["publish_format"] not in exports.FORMATS:
        raise HTTPException(400, "publish_format must be csv, xlsx or parquet")
    db.put_settings(clean)
    tagger._llm_state["at"] = 0  # re-probe the gateway with the new route
    return {"settings": ingest.current_settings(), "saved": list(clean)}


app.include_router(r)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": f"{exc.__class__.__name__}: {exc}"}, status_code=500)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
