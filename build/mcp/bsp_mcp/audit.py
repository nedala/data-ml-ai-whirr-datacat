"""Audit log of every tool call: JSONL under $MCP_DATA_DIR/audit/ (always) and
Postgres ai.mcp_tool_calls (best effort). Arguments are stored truncated; tool
results are not stored (only size + status)."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time

from .common import data_dir

log = logging.getLogger("bsp_mcp.audit")
_lock = threading.Lock()
_db = {"ok": None, "checked": 0.0, "ddl": False}
_ddl_lock = threading.Lock()
DDL = """CREATE SCHEMA IF NOT EXISTS ai;
CREATE TABLE IF NOT EXISTS ai.mcp_tool_calls (
  id bigserial PRIMARY KEY, ts timestamptz NOT NULL DEFAULT now(), tool text, grp text, caller text,
  status text, error text, ms int, args jsonb, result_chars int);"""
_REDACT = ("password", "secret", "token", "authorization", "api_key")


def _clean_args(args: dict) -> dict:
    out = {}
    for k, v in (args or {}).items():
        if any(r in k.lower() for r in _REDACT):
            out[k] = "***"
        else:
            s = json.dumps(v, default=str)
            out[k] = v if len(s) <= 500 else s[:500] + "…"
    return out


def _db_write(row: dict) -> None:
    if os.environ.get("MCP_AUDIT_DB", "1") == "0" or not os.environ.get("PG_PASS"):
        return
    now = time.monotonic()
    if _db["ok"] is False and now - _db["checked"] < 60:
        return
    try:
        import psycopg2  # noqa: PLC0415

        with psycopg2.connect(host=os.environ.get("PG_HOST", "postgres"), port=int(os.environ.get("PG_PORT", "5432")),
                              dbname=os.environ.get("PG_DB", "bsp_edw"), user=os.environ.get("PG_USER", "analyst"),
                              password=os.environ.get("PG_PASS", ""), connect_timeout=3,
                              application_name="bsp-mcp-audit") as conn, conn.cursor() as cur:
            if not _db["ddl"]:
                with _ddl_lock:
                    if not _db["ddl"]:
                        cur.execute(DDL)
                        conn.commit()
                        _db["ddl"] = True
            cur.execute("INSERT INTO ai.mcp_tool_calls (ts, tool, grp, caller, status, error, ms, args, result_chars) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                        (row["ts"], row["tool"], row["group"], row["caller"], row["status"], row["error"], row["ms"],
                         json.dumps(row["args"], default=str), row["result_chars"]))
        _db.update(ok=True, checked=now)
    except Exception as e:  # noqa: BLE001
        _db.update(ok=False, checked=now)
        log.debug("audit DB write failed: %s", e)


def record(tool: str, group: str, args: dict, status: str, ms: int, error: str = "", result_chars: int = 0,
           caller: str = "") -> dict:
    row = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(), "tool": tool, "group": group, "caller": caller,
           "status": status, "error": error[:500], "ms": ms, "args": _clean_args(args), "result_chars": result_chars}
    try:
        p = data_dir() / "audit" / f"tool_calls-{row['ts'][:10]}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with _lock, p.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        log.warning("audit JSONL write failed: %s", e)
    threading.Thread(target=_db_write, args=(row,), daemon=True).start()
    log.info("tool %s %s %dms", tool, status, ms)
    return row


def recent(limit: int = 100) -> list[dict]:
    d = data_dir() / "audit"
    rows: list[dict] = []
    for p in sorted(d.glob("tool_calls-*.jsonl"), reverse=True):
        for line in reversed(p.read_text().splitlines()):
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
            if len(rows) >= limit:
                return rows
    return rows
