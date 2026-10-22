"""Alerts and the incident workflow (mlops.alerts).

    raise_alert(kind, title, severity, details, dedupe_key)   open (or refresh) an alert
    acknowledge(id, by) / resolve(id, by, resolution)          the on-call workflow
    open_alerts() / history()

Raised by: the drift monitor (breach → retrain trigger), the delayed-label
evaluation (realised AUC below expectation), the release gates (a blocked
candidate), the serving reconciliation (registry ≠ routing) and the SLO check.
Delivery to people (Teams / e-mail / paging) is delegated — on GCP to Cloud
Monitoring alerting policies on the same signals; here the control tower is
the console. Every transition is also written to mlops.audit_log.
"""

from __future__ import annotations

from . import db

SEVERITIES = ("info", "warning", "critical")
OWNERS = {"drift": "Data Science (model owner)", "performance": "Data Science (model owner)",
          "serving_divergence": "ML Platform on-call", "gate_failure": "Data Science (model owner)", "slo": "ML Platform on-call"}


def raise_alert(kind: str, title: str, severity: str = "warning", details: dict | None = None, dedupe_key: str | None = None,
                retrain_id: int | None = None) -> dict | None:
    try:
        db.ensure_schema()
        if dedupe_key:
            cur = db.fetchone("select id from mlops.alerts where dedupe_key=%s and status <> 'resolved' order by id desc limit 1",
                              (dedupe_key,))
            if cur:
                db.execute("update mlops.alerts set details=%s, title=%s where id=%s", (db.jdump(details or {}), title, cur["id"]))
                return {"id": cur["id"], "deduplicated": True}
        row = db.fetchone("""insert into mlops.alerts (kind, severity, title, details, owner, dedupe_key, retrain_id)
                             values (%s,%s,%s,%s,%s,%s,%s) returning id""",
                          (kind, severity if severity in SEVERITIES else "warning", title[:500], db.jdump(details or {}),
                           OWNERS.get(kind, "ML Platform on-call"), dedupe_key, retrain_id))
        db.audit("alert.raise", f"alert:{row['id']}", "system", "service", "automation", kind=kind, severity=severity, title=title[:200])
        return {"id": row["id"], "deduplicated": False}
    except Exception:
        return None


def acknowledge(alert_id: int, by: str, role: str | None = None, source: str | None = None) -> dict:
    db.execute("update mlops.alerts set status='acknowledged', acked_by=%s, acked_at=now() where id=%s and status='open'", (by, alert_id))
    db.audit("alert.acknowledge", f"alert:{alert_id}", by, role, source)
    return db.fetchone("select * from mlops.alerts where id=%s", (alert_id,))


def resolve(alert_id: int, by: str, resolution: str = "", role: str | None = None, source: str | None = None) -> dict:
    db.execute("update mlops.alerts set status='resolved', resolved_by=%s, resolved_at=now(), resolution=%s where id=%s and status <> 'resolved'",
               (by, resolution[:1000], alert_id))
    db.audit("alert.resolve", f"alert:{alert_id}", by, role, source, resolution=resolution[:300])
    return db.fetchone("select * from mlops.alerts where id=%s", (alert_id,))


def open_alerts(limit: int = 100) -> list[dict]:
    return db.fetchall("select * from mlops.alerts where status <> 'resolved' order by id desc limit %s", (limit,))


def history(limit: int = 200) -> list[dict]:
    return db.fetchall("select * from mlops.alerts order by id desc limit %s", (limit,))
