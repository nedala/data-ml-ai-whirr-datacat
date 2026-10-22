"""Job endpoints (list / detail / log / SSE stream / cancel), mounted under
both /pkg/api/jobs and /files/api/jobs."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from .jobs import MANAGER


def make_router(prefix: str, default_queues: set[str]) -> APIRouter:
    r = APIRouter(prefix=prefix)

    @r.get("")
    def list_jobs(queue: str | None = None, limit: int = 50):
        qs = set(queue.split(",")) if queue else default_queues
        return {"jobs": MANAGER.list(qs, min(max(limit, 1), 200)),
                "active": {q: [j.id for j in MANAGER.active(q)] for q in qs}}

    @r.get("/{job_id}")
    def get_job(job_id: str):
        j = MANAGER.get(job_id)
        if j:
            return j.summary()
        for rec in MANAGER.history:
            if rec.get("id") == job_id:
                return {k: v for k, v in rec.items() if k != "log_tail"}
        raise HTTPException(404, "job not found")

    @r.get("/{job_id}/log")
    def get_log(job_id: str):
        j = MANAGER.get(job_id)
        if j:
            lines, _ = j.lines_from(0)
            return {"lines": lines, "status": j.status}
        tail = MANAGER.history_log(job_id)
        if tail is None:
            raise HTTPException(404, "job not found")
        return {"lines": tail, "status": "finished"}

    @r.post("/{job_id}/cancel")
    def cancel(job_id: str):
        j = MANAGER.get(job_id)
        if not j:
            raise HTTPException(404, "job not found")
        if not j.done:
            j.cancel()
        return j.summary()

    @r.get("/{job_id}/stream")
    async def stream(job_id: str, request: Request, start: int = 0):
        j = MANAGER.get(job_id)
        if not j:
            tail = MANAGER.history_log(job_id)
            if tail is None:
                raise HTTPException(404, "job not found")

            async def finished():
                for i, ln in enumerate(tail):
                    yield f"id: {i + 1}\nevent: log\ndata: {json.dumps(ln)}\n\n"
                yield "event: end\ndata: {}\n\n"
            return StreamingResponse(finished(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        last = request.headers.get("last-event-id")
        pos = int(last) if last and last.isdigit() else max(0, start)

        async def gen():
            nonlocal pos
            last_state = None
            beat = time.time()
            yield "retry: 2000\n\n"
            while True:
                lines, total = j.lines_from(pos)
                if lines:
                    buf = []
                    for ln in lines:
                        pos += 1
                        buf.append(f"id: {pos}\nevent: log\ndata: {json.dumps(ln)}\n\n")
                    yield "".join(buf)
                    pos = total
                state = (j.status, json.dumps(j.progress, sort_keys=True))
                if state != last_state:
                    last_state = state
                    yield f"event: status\ndata: {json.dumps(j.summary(), default=str)}\n\n"
                if j.done and pos >= total:
                    yield f"event: end\ndata: {json.dumps(j.summary(), default=str)}\n\n"
                    return
                if await request.is_disconnected():
                    return
                if time.time() - beat > 15:
                    beat = time.time()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return r
