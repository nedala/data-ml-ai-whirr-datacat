"""Evals: small YAML golden sets (packaged evalsets/ + $HARNESS_DATA_DIR/evals/*.yaml)
run against chosen routes; scores stored in ai.eval_runs (JSONL fallback)."""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from . import router as rt
from . import telemetry
from .config import data_dir

PACKAGED = Path(__file__).parent / "evalsets"
JOBS: dict[str, dict] = {}


def suites() -> dict[str, dict]:
    out = {}
    for d in (PACKAGED, data_dir() / "evals"):
        if d.is_dir():
            for p in sorted(d.glob("*.yaml")):
                try:
                    s = yaml.safe_load(p.read_text())
                    s["_path"] = str(p)
                    out[s["suite"]] = s
                except Exception:  # noqa: BLE001
                    continue
    return out


# ── scoring ──────────────────────────────────────────────────────────────────
def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2)
    s = str(v).strip().lower()
    if s in ("", "null", "none", "n/a"):
        return None
    try:
        return round(float(s.replace("$", "").replace(",", "")), 2)
    except ValueError:
        return re.sub(r"\s+", " ", s)


def score_extract(text: str, expected: dict) -> tuple[float, dict]:
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        got = json.loads(m.group(0) if m else text)
    except (ValueError, TypeError):
        return 0.0, {"error": "not JSON"}
    hits = {k: _norm(got.get(k)) == _norm(v) for k, v in expected.items()}
    return sum(hits.values()) / max(len(hits), 1), {"got": got, "fields": hits}


def score_classify(text: str, expected: str, labels: list[str]) -> tuple[float, dict]:
    t = (text or "").strip().strip("`'\". ").upper()
    if t == expected.upper():
        return 1.0, {"got": t}
    found = [lab for lab in labels if lab.upper() in t]
    return (1.0 if found == [expected] else 0.0), {"got": t[:80]}


def _strip_sql(text: str) -> str:
    m = re.search(r"```(?:sql)?\s*(.*?)```", text or "", re.S | re.I)
    s = (m.group(1) if m else text or "").strip()
    return s.rstrip(";").strip()


def _run_sql(sql: str, limit: int = 2000) -> list[tuple]:
    from psycopg2 import sql as _  # noqa: F401,PLC0415

    with telemetry.connect() as conn:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 15000")
            cur.execute("SET LOCAL search_path TO raw, public")
            cur.execute(f"SELECT * FROM ({sql}) q LIMIT {limit}")
            return [tuple(str(x) if x is not None else None for x in r) for r in cur.fetchall()]


def score_sql(text: str, case: dict) -> tuple[float, dict]:
    sql = _strip_sql(text)
    if not re.match(r"^\s*(select|with)\b", sql, re.I):
        return 0.0, {"sql": sql[:300], "error": "not a SELECT"}
    try:
        exp = _run_sql(case["expected_sql"])
        try:
            got = _run_sql(sql)
        except Exception as e:  # noqa: BLE001
            return 0.0, {"sql": sql[:300], "error": f"SQL error: {str(e).splitlines()[0][:200]}", "mode": "execute"}
        same = sorted(map(str, exp)) == sorted(map(str, got))
        return (1.0 if same else 0.0), {"sql": sql[:300], "mode": "execute", "rows": len(got)}
    except Exception:  # noqa: BLE001 — warehouse unreachable → token check
        toks = case.get("must_contain") or []
        low = sql.lower()
        hit = [t for t in toks if t.lower() in low]
        return (len(hit) / max(len(toks), 1)), {"sql": sql[:300], "mode": "keywords", "matched": hit}


def _messages(suite: dict, case: dict) -> tuple[list[dict], dict]:
    sys = suite.get("system", "")
    extra: dict[str, Any] = {}
    if suite["type"] == "extract":
        sys += "\nJSON schema:\n" + json.dumps(suite.get("schema") or {})
        extra = {"temperature": 0, "response_format": {"type": "json_object"}}
    elif suite["type"] == "classify":
        labels = suite.get("labels") or {}
        sys += "\nLabels:\n" + "\n".join(f"- {k}: {v}" for k, v in labels.items())
        extra = {"temperature": 0, "max_tokens": 32}
    else:
        extra = {"temperature": 0}
    return [{"role": "system", "content": sys}, {"role": "user", "content": case["input"]}], extra


async def run_case(suite: dict, case: dict, route: str) -> dict:
    msgs, extra = _messages(suite, case)
    t0 = time.monotonic()
    try:
        res, info = await rt.chat({"model": route, "messages": msgs, **extra}, caller=f"eval:{suite['suite']}")
        text = res["message"].get("content") or ""
        served = f"{info['provider']}/{info['model']}"
    except rt.RouteError as e:
        return {"id": case["id"], "score": 0.0, "error": str(e)[:300], "ms": int((time.monotonic() - t0) * 1000)}
    ms = int((time.monotonic() - t0) * 1000)
    if suite["type"] == "extract":
        sc, det = score_extract(text, case["expected"])
    elif suite["type"] == "classify":
        sc, det = score_classify(text, case["expected"], list((suite.get("labels") or {}).keys()))
    else:
        sc, det = await asyncio.to_thread(score_sql, text, case)
    return {"id": case["id"], "score": round(sc, 3), "ms": ms, "served_by": served, "output": text[:400], **det}


def _store(row: dict) -> str:
    if telemetry.ensure_schema():
        try:
            with telemetry.connect() as conn, conn.cursor() as cur:
                cur.execute("INSERT INTO ai.eval_runs (ts, run_id, suite, route, n_cases, score, latency_p50_ms, errors, details) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                            (row["ts"], row["run_id"], row["suite"], row["route"], row["n_cases"], row["score"],
                             row["latency_p50_ms"], row["errors"], json.dumps(row["details"], default=str)))
            return "postgres"
        except Exception:  # noqa: BLE001
            pass
    p = data_dir() / "evals" / "runs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    return "jsonl"


async def run_suite(name: str, routes: list[str], job_id: str | None = None, limit: int | None = None) -> list[dict]:
    s = suites().get(name)
    if not s:
        raise ValueError(f"unknown suite {name}")
    cases = s["cases"][:limit] if limit else s["cases"]
    job = JOBS.setdefault(job_id or uuid.uuid4().hex[:10], {"status": "running", "done": 0, "total": len(cases) * len(routes)})
    out = []
    for route in routes:
        results = []
        for c in cases:
            results.append(await run_case(s, c, route))
            job["done"] = job.get("done", 0) + 1
        lat = sorted(r["ms"] for r in results)
        row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "run_id": job_id or "", "suite": name, "route": route,
               "n_cases": len(results), "score": round(sum(r["score"] for r in results) / max(len(results), 1), 4),
               "latency_p50_ms": lat[len(lat) // 2] if lat else None, "errors": sum(1 for r in results if r.get("error")),
               "details": results}
        row["stored_in"] = _store(row)
        out.append(row)
    return out


def start_job(name: str, routes: list[str], limit: int | None = None) -> str:
    jid = uuid.uuid4().hex[:10]
    s = suites().get(name)
    if not s:
        raise ValueError(f"unknown suite {name}")
    n = len(s["cases"][:limit] if limit else s["cases"])
    JOBS[jid] = {"status": "running", "done": 0, "total": n * len(routes), "suite": name, "routes": routes}

    async def go():
        try:
            JOBS[jid]["results"] = await run_suite(name, routes, jid, limit)
            JOBS[jid]["status"] = "done"
        except Exception as e:  # noqa: BLE001
            JOBS[jid].update(status="error", error=f"{type(e).__name__}: {e}")

    JOBS[jid]["task"] = asyncio.get_event_loop().create_task(go())
    return jid


def job_view(jid: str) -> dict | None:
    j = JOBS.get(jid)
    return None if j is None else {k: v for k, v in j.items() if k != "task"}


def runs(limit: int = 200) -> list[dict]:
    rows: list[dict] = []
    if telemetry.ensure_schema():
        try:
            with telemetry.connect() as conn, conn.cursor() as cur:
                cur.execute("SELECT ts, run_id, suite, route, n_cases, score, latency_p50_ms, errors FROM ai.eval_runs "
                            "ORDER BY ts DESC LIMIT %s", (limit,))
                for r in cur.fetchall():
                    rows.append({"ts": r[0].isoformat(), "run_id": r[1], "suite": r[2], "route": r[3], "n_cases": r[4],
                                 "score": r[5], "latency_p50_ms": r[6], "errors": r[7]})
            return rows
        except Exception:  # noqa: BLE001
            pass
    p = data_dir() / "evals" / "runs.jsonl"
    if p.exists():
        for line in p.read_text().splitlines()[-limit:]:
            try:
                r = json.loads(line)
                r.pop("details", None)
                rows.append(r)
            except ValueError:
                continue
    return sorted(rows, key=lambda r: r["ts"], reverse=True)


def leaderboard() -> list[dict]:
    best: dict[tuple, dict] = {}
    for r in runs(1000):
        k = (r["suite"], r["route"])
        if k not in best or r["ts"] > best[k]["ts"]:   # latest run per suite × route
            best[k] = r
    return sorted(best.values(), key=lambda r: (r["suite"], -(r["score"] or 0)))
