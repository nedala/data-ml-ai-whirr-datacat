"""Exports of the current call tags: CSV · Excel · Parquet, for a date range or a run;
optional publish back to SharePoint (m365.upload_file → CCAI/Tagged/)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone

import pandas as pd

from . import config, db, source, taxonomy

FORMATS = {"csv": ("text/csv", "csv"),
           "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"),
           "parquet": ("application/vnd.apache.parquet", "parquet")}

COLUMNS = [
    ("call_id", "CCAI conversation id"), ("started_at", "Call start (UTC)"), ("source_path", "File in the SharePoint folder"),
    ("format", "Source format"), ("agent_id", "Agent id"), ("queue", "Queue"), ("ban", "BAN (billing account number)"),
    ("duration_sec", "Call duration (s)"), ("l1", "L1 domain"), ("l2", "L2 intent group"), ("primary_leaf", "Tagger primary leaf id"),
    ("primary_name", "Tagger primary leaf name"), ("confidence", "Calibrated confidence 0–1"),
    ("confidence_band", "high / medium / low vs the threshold"), ("secondary_leaves", "Secondary leaf ids (| separated)"),
    ("final_leaf", "Reviewer override if any, else the tagger primary"), ("final_name", "Name of final_leaf"),
    ("reviewed_by", "Reviewer"), ("override_leaf", "Reviewer override"), ("sentiment_trajectory", "Customer sentiment trajectory"),
    ("avg_customer_sentiment", "Mean customer sentiment −1…1"), ("agent_talk_pct", "Agent share of talk time %"),
    ("silence_pct", "Silence %"), ("resolution_status", "resolved / pending_followup / escalated / unresolved / unknown"),
    ("churn_risk", "Churn-risk flag"), ("churn_reasons", "Why the churn flag was raised"), ("summary", "Call summary"),
    ("route", "llm / offline / offline-fallback"), ("model", "Model alias or offline model"),
    ("taxonomy_version", "Taxonomy version"), ("sap_reason", "SAP interaction reason code (final leaf)"),
    ("ticket_category", "Service-ticket category (final leaf)"), ("tagged_at", "Tagged at (UTC)"), ("run_id", "Ingest run"),
]


def rows(date_from: str | None = None, date_to: str | None = None, run_id: int | None = None,
         min_conf: float | None = None) -> pd.DataFrame:
    where, params = ["t.is_current"], []
    if date_from:
        where.append("c.started_at >= %s::date")
        params.append(date_from)
    if date_to:
        where.append("c.started_at < %s::date + 1")
        params.append(date_to)
    if run_id:
        where.append("(t.run_id = %s or c.run_id = %s)")
        params += [run_id, run_id]
    if min_conf is not None:
        where.append("t.confidence >= %s")
        params.append(min_conf)
    data = db.q(f"""select c.call_id, c.started_at, c.source_path, c.format, c.agent_id, c.queue, c.ban, c.duration_sec,
                    t.primary_leaf, t.confidence, t.confidence_band, t.secondary, t.reviewed_by, t.override_leaf,
                    t.sentiment_trajectory, c.avg_customer_sentiment, c.agent_talk_pct, c.silence_pct, t.resolution_status,
                    t.churn_risk, t.churn_reasons, t.summary, t.route, t.model, t.taxonomy_version, c.tagged_at, t.run_id
                    from ccai.calls c join ccai.call_tags t on t.call_id = c.call_id
                    where {' and '.join(where)} order by c.started_at""", params)
    by = taxonomy.load().by_id
    for r in data:
        leaf = by.get(r["primary_leaf"])
        final = r["override_leaf"] or r["primary_leaf"]
        fl = by.get(final)
        r["l1"] = fl.l1_name if fl else None
        r["l2"] = fl.l2_name if fl else None
        r["primary_name"] = leaf.name if leaf else None
        r["final_leaf"] = final
        r["final_name"] = fl.name if fl else None
        r["secondary_leaves"] = "|".join(s["leaf"] for s in (r.pop("secondary") or []))
        r["churn_reasons"] = "; ".join(r["churn_reasons"] or [])
        r["sap_reason"] = fl.mapping.get("sap_reason") if fl else None
        r["ticket_category"] = (f"{fl.mapping.get('ticket_category')}/{fl.mapping.get('ticket_subcategory')}" if fl else None)
        for k in ("started_at", "tagged_at"):
            if isinstance(r[k], datetime):
                r[k] = r[k].astimezone(timezone.utc).replace(tzinfo=None)
    df = pd.DataFrame(data, columns=[c for c, _ in COLUMNS])
    return df


def render(df: pd.DataFrame, fmt: str) -> bytes:
    buf = io.BytesIO()
    if fmt == "csv":
        return df.to_csv(index=False).encode("utf-8")
    if fmt == "parquet":
        df.to_parquet(buf, index=False, engine="pyarrow")
        return buf.getvalue()
    if fmt == "xlsx":
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="Call tags", index=False)
            vol = (df.groupby(["l1", "l2", "final_leaf", "final_name"], dropna=False)
                   .agg(calls=("call_id", "count"), avg_confidence=("confidence", "mean"),
                        churn_risk_calls=("churn_risk", "sum")).reset_index().sort_values("calls", ascending=False)
                   if len(df) else pd.DataFrame(columns=["l1", "l2", "final_leaf", "final_name", "calls"]))
            vol.to_excel(xw, sheet_name="Volume by reason", index=False)
            pd.DataFrame(COLUMNS, columns=["column", "meaning"]).to_excel(xw, sheet_name="Data dictionary", index=False)
            for ws in xw.book.worksheets:
                ws.freeze_panes = "A2"
                for col in ws.columns:
                    width = min(60, max(10, *(len(str(c.value or "")) for c in list(col)[:200])))
                    ws.column_dimensions[col[0].column_letter].width = width + 2
        return buf.getvalue()
    raise ValueError(f"unknown format {fmt!r}")


def filename(fmt: str, date_from=None, date_to=None, run_id=None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope = f"run{run_id}" if run_id else f"{date_from or 'all'}_{date_to or 'latest'}"
    return f"ccai_call_tags_{scope}_{stamp}.{FORMATS[fmt][1]}"


def build(fmt: str, date_from=None, date_to=None, run_id=None, min_conf=None) -> tuple[bytes, str, int]:
    if fmt not in FORMATS:
        raise ValueError(f"format must be one of {list(FORMATS)}")
    df = rows(date_from, date_to, run_id, min_conf)
    data = render(df, fmt)
    name = filename(fmt, date_from, date_to, run_id)
    (config.data_path("exports") / name).write_bytes(data)
    _log({"action": "download", "name": name, "format": fmt, "rows": len(df), "bytes": len(data),
          "date_from": date_from, "date_to": date_to, "run_id": run_id})
    return data, name, len(df)


def publish(fmt: str = "xlsx", date_from=None, date_to=None, run_id=None, min_conf=None) -> dict:
    data, name, n = build(fmt, date_from, date_to, run_id, min_conf)
    src = source.get()
    item = src.upload_file(config.sharepoint_site(), config.publish_folder(), name, data)
    rec = {"action": "publish", "name": name, "format": fmt, "rows": n, "bytes": len(data), "folder": config.publish_folder(),
           "web_url": item.get("webUrl"), "item_id": item.get("id"), "adapter": f"{src.kind}:{src.mode}",
           "date_from": date_from, "date_to": date_to, "run_id": run_id}
    _log(rec)
    return rec


def _log(rec: dict) -> None:
    rec = {"at": datetime.now(timezone.utc).isoformat(), **rec}
    with open(config.data_path("exports") / "exports.log.jsonl", "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def history(limit: int = 30) -> list[dict]:
    p = config.data_path("exports") / "exports.log.jsonl"
    if not p.exists():
        return []
    lines = p.read_text().splitlines()[-limit:]
    return [json.loads(x) for x in reversed(lines) if x.strip()]
