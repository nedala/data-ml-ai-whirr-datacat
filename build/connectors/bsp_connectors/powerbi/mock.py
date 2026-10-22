"""Power BI mock: workspaces "Revenue & Churn", "Network Ops", "Customer Care"
(fixtures/powerbi/workspaces.yaml), refresh history in state, and a small DAX
engine for executeQueries:

    EVALUATE 'Table'                                   EVALUATE TOPN(10, 'Table'[, 'Table'[col][, DESC]])
    EVALUATE SUMMARIZECOLUMNS('T'[c1], …, "Name", [Measure], …)
    EVALUATE ROW("Name", [Measure], …)                 EVALUATE FILTER('T', 'T'[col] = "x")

Table data comes from the warehouse via the BigQuery mock (DuckDB), falling
back to the fixture's sample rows. Result keys follow Power BI:
"Table[column]" for columns, "[Name]" for measures.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import yaml

from .._common import ConnectorError, NotFound, fixtures_dir, read_json, state_dir, write_json

_lock = threading.Lock()
_tables: dict[tuple[str, str], tuple[float, Any]] = {}
TTL = 120.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _fixture() -> dict:
    return yaml.safe_load((fixtures_dir("powerbi") / "workspaces.yaml").read_text()) or {"workspaces": []}


def _ws(ws: str) -> dict:
    for w in _fixture()["workspaces"]:
        if ws in (w["id"], w["name"]) or ws.lower() == w["name"].lower():
            return w
    raise NotFound(f"workspace {ws!r} not found", status=404, system="powerbi")


def _ds(ws: str, ds: str) -> tuple[dict, dict]:
    w = _ws(ws)
    for d in w.get("datasets", []):
        if ds in (d["id"], d["name"]) or ds.lower() == d["name"].lower():
            return w, d
    raise NotFound(f"dataset {ds!r} not found in workspace {w['name']!r}", status=404, system="powerbi")


def _refresh_log() -> dict:
    return read_json(state_dir("powerbi") / "refreshes.json", {})


def list_workspaces() -> list[dict]:
    return [{"id": w["id"], "name": w["name"], "isReadOnly": False, "isOnDedicatedCapacity": False, "type": "Workspace",
             "description": w.get("description", "")} for w in _fixture()["workspaces"]]


def list_reports(ws: str) -> list[dict]:
    w = _ws(ws)
    ds_ids = {d["name"]: d["id"] for d in w.get("datasets", [])}
    return [{"id": r["id"], "name": r["name"], "reportType": "PowerBIReport", "datasetId": ds_ids.get(r.get("dataset")),
             "webUrl": f"https://app.powerbi.com/groups/{w['id']}/reports/{r['id']}",
             "embedUrl": f"https://app.powerbi.com/reportEmbed?reportId={r['id']}&groupId={w['id']}"}
            for r in w.get("reports", [])]


def list_datasets(ws: str) -> list[dict]:
    w = _ws(ws)
    log = _refresh_log()
    out = []
    for d in w.get("datasets", []):
        last = (log.get(d["id"]) or [{}])[-1]
        out.append({"id": d["id"], "name": d["name"], "configuredBy": f"{d.get('owner', 'bi-team')}@example.net",
                    "isRefreshable": True, "isEffectiveIdentityRequired": False, "targetStorageMode": "Import",
                    "description": d.get("description", ""),
                    "tables": [{"name": t, "columns": list((cfg.get("sample") or [{}])[0].keys())}
                               for t, cfg in d.get("tables", {}).items()],
                    "measures": list(d.get("measures", {}).keys()),
                    "lastRefresh": last.get("endTime"),
                    "webUrl": f"https://app.powerbi.com/groups/{w['id']}/datasets/{d['id']}"})
    return out


def refresh_dataset(ws: str, ds: str) -> dict:
    w, d = _ds(ws, ds)
    rid = str(uuid.uuid4())
    t0 = _now()
    with _lock:
        for k in [k for k in _tables if k[0] == d["id"]]:
            _tables.pop(k, None)
    rows = {t: len(_table(d, t)) for t in d.get("tables", {})}
    rec = {"requestId": rid, "id": int(time.time()), "refreshType": "ViaApi", "startTime": t0, "endTime": _now(),
           "status": "Completed", "tables": rows}
    log = _refresh_log()
    log.setdefault(d["id"], []).append(rec)
    log[d["id"]] = log[d["id"]][-20:]
    write_json(state_dir("powerbi") / "refreshes.json", log)
    return {"status": "accepted", "requestId": rid, "workspace": w["name"], "dataset": d["name"], "mode": "mock"}


def refresh_history(ws: str, ds: str) -> list[dict]:
    _, d = _ds(ws, ds)
    return list(reversed(_refresh_log().get(d["id"], [])))


# ── data + DAX ───────────────────────────────────────────────────────────
def _table(d: dict, name: str):
    import pandas as pd
    cfg = None
    for t, c in d.get("tables", {}).items():
        if t.lower() == name.lower():
            name, cfg = t, c
    if cfg is None:
        raise ConnectorError(f"table {name!r} not in dataset {d['name']!r}", status=400, system="powerbi")
    key = (d["id"], name)
    with _lock:
        hit = _tables.get(key)
        if hit and time.time() - hit[0] < TTL:
            return hit[1]
    df = None
    if cfg.get("sql"):
        try:
            from ..bigquery import mock as bq
            rows = bq.query(cfg["sql"], max_rows=0)
            if rows:
                df = pd.DataFrame(rows)
        except Exception:
            df = None
    if df is None:
        df = pd.DataFrame(cfg.get("sample") or [])
    with _lock:
        _tables[key] = (time.time(), df)
    return df


_TBL = r"(?:'([^']+)'|([A-Za-z_][\w ]*?))"
_COL = re.compile(_TBL + r"\s*\[([^\]]+)\]")


def _split_args(s: str) -> list[str]:
    out, depth, cur, q = [], 0, "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
            continue
        if ch in "\"'":
            q = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


def _tname(s: str) -> str:
    s = s.strip()
    return s[1:-1] if s.startswith("'") and s.endswith("'") else s


def _rows(df, table: str, rename: bool = True) -> list[dict]:
    import pandas as pd
    df = df.astype(object).where(pd.notna(df), None)
    recs = df.to_dict(orient="records")
    return [{f"{table}[{k}]" if rename else k: v for k, v in r.items()} for r in recs]


def execute_dax(ws: str, ds: str, dax: str) -> list[dict]:
    import duckdb
    _, d = _ds(ws, ds)
    q = re.sub(r"//[^\n]*", "", dax).strip()
    m = re.match(r"(?is)^EVALUATE\s+(.*)$", q)
    if not m:
        raise ConnectorError("mock DAX: query must start with EVALUATE", status=400, system="powerbi")
    expr = m.group(1).strip().rstrip(";")
    fn = re.match(r"(?is)^([A-Z]+)\s*\((.*)\)$", expr)
    measures = {k.lower(): (k, v) for k, v in d.get("measures", {}).items()}

    def measure(ref: str) -> tuple[str, dict]:
        name = ref.strip().strip("[]")
        if name.lower() not in measures:
            raise ConnectorError(f"mock DAX: unknown measure [{name}]", status=400, system="powerbi")
        return measures[name.lower()]

    if not fn:  # EVALUATE 'Table'
        t = _tname(expr)
        return _rows(_table(d, t), t)
    func, args = fn.group(1).upper(), _split_args(fn.group(2))
    con = duckdb.connect(":memory:")
    try:
        if func == "TOPN":
            n, t = int(args[0]), _tname(args[1])
            df = _table(d, t)
            if len(args) > 2:
                cm = _COL.search(args[2])
                col = cm.group(3) if cm else None
                if col is None:
                    _, mcfg = measure(args[2])
                    raise ConnectorError("mock DAX: TOPN order by a column, not a measure", status=400, system="powerbi")
                asc = len(args) > 3 and args[3].strip().upper() in ("ASC", "1")
                df = df.sort_values(col, ascending=asc)
            return _rows(df.head(n), t)
        if func == "FILTER":
            t = _tname(args[0])
            df = _table(d, t)
            cond = args[1]
            for cm in _COL.finditer(cond):
                cond = cond.replace(cm.group(0), f'"{cm.group(3)}"')
            cond = re.sub(r'"([^"]*)"(?=\s*(\)|$|&&|\|\||AND|OR))', r"'\1'", cond)
            cond = cond.replace("&&", " AND ").replace("||", " OR ").replace("==", "=")
            con.register("t", df)
            return _rows(con.execute(f"select * from t where {cond}").df(), t)
        if func in ("SUMMARIZECOLUMNS", "ROW"):
            groups: list[tuple[str, str]] = []
            meas: list[tuple[str, str, str]] = []  # alias, table, sql
            i = 0
            while i < len(args):
                a = args[i]
                if a.startswith('"'):
                    alias = a.strip('"')
                    mname, mcfg = measure(args[i + 1])
                    meas.append((alias, mcfg["table"], mcfg["sql"]))
                    i += 2
                    continue
                cm = _COL.fullmatch(a.strip())
                if cm:
                    groups.append(((cm.group(1) or cm.group(2)).strip(), cm.group(3)))
                elif a.strip().startswith("["):
                    mname, mcfg = measure(a)
                    meas.append((mname, mcfg["table"], mcfg["sql"]))
                i += 1
            tables = {g[0] for g in groups} | {m_[1] for m_ in meas}
            if len(tables) != 1:
                raise ConnectorError("mock DAX: columns and measures must come from one table", status=400,
                                     system="powerbi")
            t = tables.pop()
            con.register("t", _table(d, t))
            gcols = ", ".join(f'"{c}" as "{t}[{c}]"' for _, c in groups)
            mcols = ", ".join(f'{sql} as "[{alias}]"' for alias, _, sql in meas)
            sel = ", ".join(x for x in (gcols, mcols) if x)
            sql = f"select {sel} from t"
            if groups:
                gb = ", ".join(f'"{c}"' for _, c in groups)
                sql += f" group by {gb} order by {gb}"
            return _rows(con.execute(sql).df(), t, rename=False)
    except ConnectorError:
        raise
    except Exception as e:
        raise ConnectorError(f"mock DAX failed: {e}", status=400, system="powerbi") from e
    finally:
        con.close()
    raise ConnectorError(f"mock DAX: {func}() not supported (EVALUATE table | TOPN | FILTER | SUMMARIZECOLUMNS | ROW)",
                         status=400, system="powerbi")
