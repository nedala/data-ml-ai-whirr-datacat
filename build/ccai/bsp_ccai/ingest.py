"""Ingest runs: list SharePoint → download → parse → tag, with live progress.

* Incremental + idempotent: an item is (re)processed only when it is new or its
  lastModifiedDateTime / eTag / size changed since the last successful sync
  (state in ccai.source_files). Re-running the same folder is a no-op.
* Modes: incremental (default) · full (re-download + re-parse everything) ·
  retry_failed (only items in error, optionally of one run) · retag (re-tag every call).
* Resumable: every file and every call commits on its own; an interrupted run is
  marked `interrupted` on restart and the next run picks up downloaded-but-unparsed
  files and parsed-but-untagged calls.
* Concurrency: downloads and LLM tagging run on a bounded thread pool
  (settings.concurrency); the offline tagger runs in-process.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, parsers, source, tagger, taxonomy

log = logging.getLogger("ccai.ingest")


def current_settings() -> dict:
    return db.get_settings(config.DEFAULT_SETTINGS)


class Busy(RuntimeError):
    pass


class Runner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.progress: dict = {"active": False}
        self.version = 0  # bumps on every progress change (SSE)

    # ── control ──────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, trigger: str = "manual", mode: str = "incremental", retry_run_id: int | None = None,
              wait: bool = False) -> int:
        if mode not in ("incremental", "full", "retry_failed", "retag"):
            raise ValueError(f"unknown mode {mode!r}")
        with self._lock:
            if self.active:
                raise Busy(f"run {self.progress.get('run_id')} is still in progress")
            st = current_settings()
            src = source.get()
            tax = taxonomy.load()
            row = db.one("""insert into ccai.ingest_runs (trigger, mode, status, phase, source_mode, site, folder,
                            taxonomy_version, params) values (%s,%s,'running','starting',%s,%s,%s,%s,%s) returning run_id""",
                         (trigger, mode, f"{src.kind}:{src.mode}", config.sharepoint_site() or "default",
                          config.sharepoint_folder(), tax.version,
                          db.J({"retry_run_id": retry_run_id, "route": st["route"], "concurrency": st["concurrency"]})))
            run_id = int(row["run_id"])
            self._cancel.clear()
            self.progress = {"active": True, "run_id": run_id, "trigger": trigger, "mode": mode, "phase": "starting",
                             "started_at": _now(), "discovered": 0, "to_process": 0, "downloaded": 0, "parsed": 0,
                             "to_tag": 0, "tagged": 0, "errors": 0, "skipped": 0, "bytes": 0, "route": None,
                             "message": "", "rate_per_min": None, "eta_s": None, "percent": 0}
            self._bump()
            self._thread = threading.Thread(target=self._run, args=(run_id, mode, retry_run_id, st), daemon=True,
                                            name=f"ccai-run-{run_id}")
            self._thread.start()
        if wait:
            self._thread.join()
        return run_id

    def cancel(self) -> bool:
        if self.active:
            self._cancel.set()
            return True
        return False

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ── helpers ──────────────────────────────────────────────────────────
    def _bump(self, **kw) -> None:
        self.progress.update(kw)
        p = self.progress
        total = (p.get("to_process") or 0) + (p.get("to_tag") or 0)
        done = (p.get("parsed") or 0) + (p.get("tagged") or 0) + (p.get("errors") or 0)
        if p.get("phase") == "done":
            p["percent"] = 100
        elif p.get("phase") in ("listing", "starting"):
            p["percent"] = 2
        elif total:
            p["percent"] = min(99, int(5 + 95 * done / max(1, total + (p.get("errors") or 0))))
        el = time.time() - datetime.fromisoformat(p["started_at"]).timestamp() if p.get("started_at") else 0
        work = (p.get("parsed") or 0) + (p.get("tagged") or 0)
        if el > 1 and work:
            p["rate_per_min"] = round(60 * work / el, 1)
            remaining = max(0, total - done)
            p["eta_s"] = int(remaining / (work / el)) if work else None
        self.version += 1

    def _update_run(self, run_id: int, **cols) -> None:
        if not cols:
            return
        sets = ", ".join(f"{k} = %s" for k in cols)
        db.ex(f"update ccai.ingest_runs set {sets} where run_id = %s", (*cols.values(), run_id))

    # ── the run ──────────────────────────────────────────────────────────
    def _run(self, run_id: int, mode: str, retry_run_id: int | None, st: dict) -> None:
        t_start = time.time()
        counts = {"files_discovered": 0, "files_new": 0, "files_changed": 0, "files_skipped": 0, "files_downloaded": 0,
                  "manifests": 0, "calls_parsed": 0, "calls_tagged": 0, "errors": 0, "bytes_downloaded": 0}
        timings = {"list_ms": 0, "download_ms": 0, "parse_ms": 0, "tag_ms": 0}
        status, message = "succeeded", ""
        try:
            tax = taxonomy.load()
            taxonomy.sync_to_db(tax)
            src = source.get()
            site, folder = config.sharepoint_site(), config.sharepoint_folder()
            todo: list[dict] = []
            if mode != "retag":
                # 1 · list
                self._bump(phase="listing", message=f"Listing {folder} ({src.kind}, {src.mode})")
                self._update_run(run_id, phase="listing")
                t = time.time()
                items = [i for i in src.list_files(site, folder, recursive=True) if "folder" not in i]
                timings["list_ms"] = int((time.time() - t) * 1000)
                counts["files_discovered"] = len(items)
                known = {r["item_id"]: r for r in db.q("select item_id, last_modified, etag, size, status, last_run_id "
                                                         "from ccai.source_files")}
                for it in items:
                    k = known.get(it["id"])
                    lm = it.get("lastModifiedDateTime")
                    etag = it.get("eTag") or it.get("cTag")
                    changed = k is not None and (_ts(k["last_modified"]) != _ts(lm) or (etag and k["etag"] != etag)
                                                 or (k["size"] is not None and k["size"] != it.get("size")))
                    if mode == "full" or k is None or changed:
                        todo.append(it)
                        counts["files_new" if k is None else "files_changed"] += 1
                    elif mode == "retry_failed" and k["status"] == "error" and (not retry_run_id or k["last_run_id"] == retry_run_id):
                        todo.append(it)
                    elif k["status"] == "downloaded":  # resume: downloaded but never parsed
                        todo.append(it)
                    else:
                        counts["files_skipped"] += 1
                # manifests first, so their rows can enrich calls in this run
                todo.sort(key=lambda i: (0 if i["name"].lower().endswith(".csv") else 1, i.get("parentPath", ""), i["name"]))
                self._bump(phase="downloading", discovered=len(items), to_process=len(todo), skipped=counts["files_skipped"],
                           message=f"{len(todo)} new/changed of {len(items)} files")
                self._update_run(run_id, phase="downloading", **{k: counts[k] for k in
                                 ("files_discovered", "files_new", "files_changed", "files_skipped")})
                # 2 · download + parse (bounded concurrency for the network part)
                manifest_rows: dict[str, dict] = {r["conversation_id"]: r for r in _manifest_rows()}
                with ThreadPoolExecutor(max_workers=max(1, int(st.get("concurrency", 4)))) as pool:
                    futs = {pool.submit(self._download, src, site, it): it for it in todo}
                    for fut in as_completed(futs):
                        it = futs[fut]
                        if self._cancel.is_set():
                            break
                        try:
                            data, local, dl_ms = fut.result()
                            timings["download_ms"] += dl_ms
                            counts["files_downloaded"] += 1
                            counts["bytes_downloaded"] += len(data)
                            self._record_file(it, run_id, "downloaded", local_path=str(local), sha=_sha(data))
                            t = time.time()
                            kind, rec = parsers.parse(it["name"], data)
                            timings["parse_ms"] += int((time.time() - t) * 1000)
                            if kind == "manifest":
                                counts["manifests"] += 1
                                for r in rec:
                                    manifest_rows[r["conversation_id"]] = r
                                self._record_file(it, run_id, "parsed", kind="manifest", fmt="csv_manifest",
                                                  meta={"rows": len(rec)}, local_path=str(local), sha=_sha(data))
                            else:
                                self._store_call(rec, it, run_id, manifest_rows.get(rec["call_id"]), data)
                                counts["calls_parsed"] += 1
                                self._record_file(it, run_id, "parsed", kind="call", fmt=rec["format"], call_id=rec["call_id"],
                                                  local_path=str(local), sha=_sha(data))
                            self._bump(downloaded=counts["files_downloaded"], parsed=counts["calls_parsed"] + counts["manifests"],
                                       bytes=counts["bytes_downloaded"])
                        except Exception as e:
                            counts["errors"] += 1
                            msg = f"{e.__class__.__name__}: {e}"[:500]
                            log.warning("file %s failed: %s", it.get("name"), msg)
                            self._record_file(it, run_id, "error", error=msg,
                                              kind="unsupported" if isinstance(e, parsers.ParseError) and "unsupported" in msg else None)
                            self._bump(errors=counts["errors"])
                    if self._cancel.is_set():
                        for f in futs:
                            f.cancel()
                self._update_run(run_id, **{k: counts[k] for k in counts}, **timings)
            if self._cancel.is_set():
                raise _Cancelled()
            # 3 · tag
            if mode == "retag":
                db.ex("update ccai.calls set status = 'parsed' where status in ('tagged', 'tag_error')")
            pending = [r["call_id"] for r in db.q("select call_id from ccai.calls where status in ('parsed', 'tag_error') "
                                                   "order by started_at nulls last")]
            use_llm = tagger.should_use_llm(st)
            route = "llm" if use_llm else "offline"
            self._bump(phase="tagging", to_tag=len(pending), route=route,
                       message=f"Tagging {len(pending)} calls ({'LLM ' + st['classify_alias'] if use_llm else 'offline tagger'})")
            self._update_run(run_id, phase="tagging", route=route)
            t = time.time()
            if pending:
                tagger.offline_model(tax)  # train once before fanning out
            workers = max(1, int(st.get("concurrency", 4))) if use_llm else 1
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(self._tag_one, cid, tax, st, use_llm, run_id): cid for cid in pending}
                for fut in as_completed(futs):
                    if self._cancel.is_set():
                        for f in futs:
                            f.cancel()
                        break
                    try:
                        fut.result()
                        counts["calls_tagged"] += 1
                    except Exception as e:
                        counts["errors"] += 1
                        log.warning("tagging %s failed: %s", futs[fut], e)
                    self._bump(tagged=counts["calls_tagged"], errors=counts["errors"])
                    if counts["calls_tagged"] % 25 == 0:
                        self._update_run(run_id, calls_tagged=counts["calls_tagged"], errors=counts["errors"])
            timings["tag_ms"] = int((time.time() - t) * 1000)
            if self._cancel.is_set():
                raise _Cancelled()
            if counts["errors"]:
                status = "partial"
                message = f"{counts['errors']} item(s) failed — see the run's error list; use 'Re-run failed' after fixing"
            else:
                message = (f"{counts['calls_parsed']} new call(s) parsed, {counts['calls_tagged']} tagged"
                           if counts["calls_parsed"] or counts["calls_tagged"] else "Up to date — nothing new in the folder")
            if st.get("publish_on_complete") and counts["calls_tagged"]:
                try:
                    from . import exports
                    res = exports.publish(fmt=st.get("publish_format", "xlsx"), run_id=run_id)
                    message += f"; published {res['name']} to SharePoint"
                except Exception as e:
                    message += f"; publish failed: {e}"
        except _Cancelled:
            status, message = "cancelled", "Cancelled by user"
        except Exception as e:
            status, message = "failed", f"{e.__class__.__name__}: {e}"[:500]
            log.error("run %s failed: %s", run_id, traceback.format_exc())
        finally:
            dur = int((time.time() - t_start) * 1000)
            try:
                self._update_run(run_id, status=status, phase="done", finished_at=datetime.now(timezone.utc), duration_ms=dur,
                                 message=message, **{k: counts[k] for k in counts}, **timings)
            except Exception:
                log.exception("could not finalise run %s", run_id)
            self._bump(active=False, phase="done", status=status, message=message, percent=100, finished_at=_now())

    # ── steps ────────────────────────────────────────────────────────────
    def _download(self, src, site, it: dict):
        t = time.time()
        data = src.download_file(site, it["id"])
        parent = (it.get("parentPath") or "").strip("/")
        local = config.data_path("raw", parent) / it["name"] if parent else config.data_path("raw") / it["name"]
        Path(local).write_bytes(data)
        return data, local, int((time.time() - t) * 1000)

    def _record_file(self, it: dict, run_id: int, status: str, error: str | None = None, kind: str | None = None,
                     fmt: str | None = None, call_id: str | None = None, meta: dict | None = None,
                     local_path: str | None = None, sha: str | None = None) -> None:
        path = "/".join(x for x in (it.get("parentPath", "").strip("/"), it["name"]) if x)
        db.ex("""insert into ccai.source_files (item_id, site, path, name, size, last_modified, etag, web_url, kind, format,
                 status, error, local_path, sha256, call_id, meta, first_run_id, last_run_id, updated_at)
                 values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                 on conflict (item_id) do update set size = excluded.size, last_modified = excluded.last_modified,
                   etag = excluded.etag, web_url = excluded.web_url, status = excluded.status, error = excluded.error,
                   kind = coalesce(excluded.kind, ccai.source_files.kind), format = coalesce(excluded.format, ccai.source_files.format),
                   local_path = coalesce(excluded.local_path, ccai.source_files.local_path),
                   sha256 = coalesce(excluded.sha256, ccai.source_files.sha256),
                   call_id = coalesce(excluded.call_id, ccai.source_files.call_id),
                   meta = coalesce(excluded.meta, ccai.source_files.meta), last_run_id = excluded.last_run_id, updated_at = now()""",
              (it["id"], config.sharepoint_site() or "default", path, it["name"], it.get("size"), it.get("lastModifiedDateTime"),
               it.get("eTag") or it.get("cTag"), it.get("webUrl"), kind, fmt, status, error, local_path, sha, call_id,
               db.J(meta) if meta is not None else None, run_id, run_id))

    def _store_call(self, rec: dict, it: dict, run_id: int, manifest: dict | None, data: bytes) -> None:
        m = rec["metrics"]
        meta = dict(rec.get("meta") or {})
        if manifest:  # fill gaps from the daily manifest and verify integrity
            rec["agent_id"] = rec.get("agent_id") or manifest.get("agent_id")
            rec["queue"] = rec.get("queue") or manifest.get("queue")
            rec["ani_masked"] = rec.get("ani_masked") or manifest.get("ani_masked")
            rec["started_at"] = rec.get("started_at") or parsers._iso(manifest.get("start_time"))
            meta["manifest_sha256_match"] = (manifest.get("sha256") or "") == _sha(data) if manifest.get("sha256") else None
        path = "/".join(x for x in (it.get("parentPath", "").strip("/"), it["name"]) if x)
        text = "\n".join(f"{t['role']}: {t['text']}" for t in rec["turns"])
        db.ex("""insert into ccai.calls (call_id, item_id, file_name, source_path, format, run_id, started_at, duration_sec,
                 agent_id, queue, ani_masked, ban, language, turn_count, agent_talk_sec, customer_talk_sec,
                 agent_talk_pct, silence_sec, silence_pct, avg_customer_sentiment, sentiment_start, sentiment_end,
                 turns, meta, transcript_text, status, error, ingested_at, tagged_at)
                 values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'parsed',null, now(), null)
                 on conflict (call_id) do update set item_id = excluded.item_id, file_name = excluded.file_name,
                   source_path = excluded.source_path, format = excluded.format, run_id = excluded.run_id,
                   started_at = excluded.started_at, duration_sec = excluded.duration_sec, agent_id = excluded.agent_id,
                   queue = excluded.queue, ani_masked = excluded.ani_masked, ban = excluded.ban,
                   language = excluded.language, turn_count = excluded.turn_count, agent_talk_sec = excluded.agent_talk_sec,
                   customer_talk_sec = excluded.customer_talk_sec, agent_talk_pct = excluded.agent_talk_pct,
                   silence_sec = excluded.silence_sec, silence_pct = excluded.silence_pct,
                   avg_customer_sentiment = excluded.avg_customer_sentiment, sentiment_start = excluded.sentiment_start,
                   sentiment_end = excluded.sentiment_end, turns = excluded.turns, meta = excluded.meta,
                   transcript_text = excluded.transcript_text, status = 'parsed', error = null, ingested_at = now()""",
              (rec["call_id"], it["id"], it["name"], path, rec["format"], run_id, rec.get("started_at"), rec.get("duration_sec"),
               rec.get("agent_id"), rec.get("queue"), rec.get("ani_masked"), rec.get("ban"), rec.get("language"),
               len(rec["turns"]), m["agent_talk_sec"], m["customer_talk_sec"], m["agent_talk_pct"], m["silence_sec"],
               m["silence_pct"], m["avg_customer_sentiment"], m["sentiment_start"], m["sentiment_end"],
               db.J(rec["turns"]), db.J(meta), text))

    def _tag_one(self, call_id: str, tax, st: dict, use_llm: bool, run_id: int) -> None:
        row = db.one("select call_id, ban, queue, turns from ccai.calls where call_id = %s", (call_id,))
        call = {"call_id": row["call_id"], "ban": row["ban"], "queue": row["queue"], "turns": row["turns"]}
        try:
            res = tagger.tag_call(call, tax, st, use_llm)
        except Exception as e:
            db.ex("update ccai.calls set status = 'tag_error', error = %s where call_id = %s", (str(e)[:500], call_id))
            raise
        with db.conn() as c, c.cursor() as cur:
            cur.execute("update ccai.call_tags set is_current = false where call_id = %s and is_current", (call_id,))
            cur.execute("""insert into ccai.call_tags (call_id, run_id, is_current, primary_leaf, secondary, candidates, confidence,
                           confidence_band, signals, rationale, evidence_turns, summary, sentiment_trajectory, sentiment_series,
                           resolution_status, churn_risk, churn_reasons, model, route, taxonomy_version, latency_ms)
                           values (%s,%s,true,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (call_id, run_id, res["primary_leaf"], db.J(res["secondary"]), db.J(res["candidates"]), res["confidence"],
                         res["confidence_band"], db.J(res["signals"]), res["rationale"], db.J(res["evidence_turns"]),
                         res["summary"], res["sentiment_trajectory"], db.J(res["sentiment_series"]), res["resolution_status"],
                         res["churn_risk"], db.J(res["churn_reasons"]), res["model"], res["route"], res["taxonomy_version"],
                         res["latency_ms"]))
            cur.execute("update ccai.calls set status = 'tagged', error = null, tagged_at = now() where call_id = %s", (call_id,))


class _Cancelled(Exception):
    pass


def _manifest_rows() -> list[dict]:
    """Manifest rows from earlier runs (local copies), so incremental runs can still enrich calls."""
    out = []
    for r in db.q("select local_path from ccai.source_files where kind = 'manifest' and status = 'parsed'"):
        try:
            out += parsers.parse_manifest(Path(r["local_path"]).read_bytes(), r["local_path"])
        except Exception:
            continue
    return out


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return round(v.timestamp(), 3)
    try:
        return round(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp(), 3)
    except ValueError:
        return None


def mark_interrupted() -> None:
    db.ex("update ccai.ingest_runs set status = 'interrupted', phase = 'done', finished_at = now(), "
          "message = coalesce(message, '') || ' (service restarted mid-run — resumes on the next run)' where status = 'running'")


RUNNER = Runner()


# ── scheduler ────────────────────────────────────────────────────────────
class Scheduler:
    """Interval scheduler (portal Settings → schedule_enabled / schedule_minutes)."""

    def __init__(self, runner: Runner) -> None:
        self.runner = runner
        self.next_due: datetime | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True, name="ccai-scheduler").start()

    def stop(self) -> None:
        self._stop.set()

    def compute_next(self, st: dict) -> datetime | None:
        if not st.get("schedule_enabled"):
            return None
        last = db.one("select max(started_at) as t from ccai.ingest_runs")
        mins = max(5, int(st.get("schedule_minutes") or 60))
        if not last or not last["t"]:
            return datetime.now(timezone.utc)
        return datetime.fromtimestamp(last["t"].timestamp() + mins * 60, tz=timezone.utc)

    def _loop(self) -> None:
        self._stop.wait(float(config.env("CCAI_SCHEDULE_DELAY", "20")))
        while not self._stop.is_set():
            try:
                st = current_settings()
                self.next_due = self.compute_next(st)
                if self.next_due and datetime.now(timezone.utc) >= self.next_due and not self.runner.active:
                    self.runner.start(trigger="schedule")
            except Busy:
                pass
            except Exception as e:
                log.warning("scheduler: %s", e)
            self._stop.wait(15)


SCHEDULER = Scheduler(RUNNER)
