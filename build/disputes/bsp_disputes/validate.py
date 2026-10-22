"""Warehouse enrichment + validation of extracted dispute fields
(Postgres `bsp_edw`, schema raw, keyed by the 10-digit BAN — see
plan/architecture.md §3: raw.customers, raw.invoices, raw.transactions,
raw.disputes, raw.subscriptions, raw.customer_monthly)."""

from __future__ import annotations

import logging
from typing import Any

from .config import get

log = logging.getLogger("bsp_disputes.validate")

WEIGHTS = {"ban": 0.3, "category": 0.2, "disputed_amount": 0.2, "invoice_id": 0.15, "contact_email": 0.15}


def _q(cur, sql: str, params: tuple) -> list[dict]:
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _name_match(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    ta, tb = set(a.lower().replace(",", " ").split()), set(b.lower().replace(",", " ").split())
    return len(ta & tb) >= min(2, len(ta), len(tb))


def enrich(fields: dict[str, Any], conf: dict[str, float], ev: dict[str, Any]) -> dict[str, Any]:
    """Mutates fields/conf/ev (fills gaps from the warehouse); returns validation report."""
    checks: list[dict] = []
    ctx: dict[str, Any] = {}

    def check(name: str, status: str, detail: str):
        checks.append({"check": name, "status": status, "detail": detail})

    def fill(k: str, v: Any, c: float, why: str):
        if v in (None, "") or fields.get(k) not in (None, ""):
            return
        fields[k], conf[k] = v, c
        ev[k] = {"source": "warehouse", "text": why}

    try:
        from bsp_common.db import connect

        with connect() as conn, conn.cursor() as cur:
            cust = None
            if fields.get("ban"):
                rows = _q(cur, "select * from raw.customers where ban = %s", (fields["ban"],))
                cust = rows[0] if rows else None
                if cust:
                    check("account_exists", "pass", f"{cust['ban']} found ({cust['customer_name']})")
                else:
                    check("account_exists", "fail", f"{fields['ban']} not in raw.customers")
                    conf["ban"] = min(conf.get("ban", 0.5), 0.3)
            if not cust and fields.get("invoice_id"):
                rows = _q(cur, "select c.* from raw.invoices i join raw.customers c using (ban) "
                               "where i.invoice_id = %s", (fields["invoice_id"],))
                if rows:
                    cust = rows[0]
                    fields["ban"], conf["ban"] = cust["ban"], 0.85
                    ev["ban"] = {"source": "warehouse", "text": f"owner of invoice {fields['invoice_id']}"}
                    check("account_exists", "pass", f"resolved from invoice {fields['invoice_id']}")
            if not cust and fields.get("contact_email"):
                rows = _q(cur, "select * from raw.customers where lower(email) = lower(%s)", (fields["contact_email"],))
                if rows:
                    cust = rows[0]
                    fields["ban"], conf["ban"] = cust["ban"], 0.8
                    ev["ban"] = {"source": "warehouse", "text": f"sender e-mail {fields['contact_email']} on file"}
                    check("account_exists", "pass", "resolved from sender e-mail on file")
            if not cust and fields.get("customer_name"):
                rows = _q(cur, "select * from raw.customers where customer_name = %s limit 3", (fields["customer_name"],))
                if len(rows) == 1:
                    cust = rows[0]
                    fields["ban"], conf["ban"] = cust["ban"], 0.5
                    ev["ban"] = {"source": "warehouse", "text": "unique name match (weak)"}
                    check("account_exists", "warn", "resolved from customer name only — verify")
            if not cust:
                if not any(c["check"] == "account_exists" for c in checks):
                    check("account_exists", "fail", "no account id found in mail, attachments or warehouse")
                return {"checks": checks, "context": ctx}
            acct = cust["ban"]
            ctx.update(customer_name=cust["customer_name"], autopay=bool(cust.get("autopay")),
                       segment=cust.get("segment"), city=cust.get("city"), state=cust.get("state"))
            if fields.get("customer_name"):
                check("name_matches", "pass" if _name_match(fields["customer_name"], cust["customer_name"]) else "warn",
                      f"mail: {fields['customer_name']} / account: {cust['customer_name']}")
            else:
                fill("customer_name", cust["customer_name"], 0.8, "customer name on account")
            fill("contact_phone", cust.get("phone"), 0.7, "phone on account")
            fill("contact_email", cust.get("email"), 0.6, "e-mail on account")
            fill("service_address", f"{cust.get('city')}, {cust.get('state')} {cust.get('zip')}", 0.6, "service location on account")
            # invoice
            inv = None
            if fields.get("invoice_id"):
                rows = _q(cur, "select * from raw.invoices where invoice_id = %s", (fields["invoice_id"],))
                inv = rows[0] if rows else None
                if not inv:
                    check("invoice_exists", "fail", f"{fields['invoice_id']} not in raw.invoices")
                    conf["invoice_id"] = min(conf.get("invoice_id", 0.5), 0.3)
                elif inv["ban"] != acct:
                    check("invoice_belongs_to_account", "fail", f"{inv['invoice_id']} belongs to {inv['ban']}")
                    conf["invoice_id"] = min(conf.get("invoice_id", 0.5), 0.3)
                    inv = None
                else:
                    check("invoice_belongs_to_account", "pass", f"{inv['invoice_id']} · {inv['bill_cycle']} · ${inv['amount_due']:.2f}")
            if inv is None:
                cands = _q(cur, "select * from raw.invoices where ban = %s order by bill_cycle desc limit 6", (acct,))
                cat, amt = fields.get("category"), fields.get("disputed_amount")
                pick = None
                if cat:  # a dispute already logged by care / the IVR for this BAN and category
                    d = _q(cur, "select invoice_id from raw.disputes where ban = %s and category = %s "
                                "order by opened_date desc limit 1", (acct, cat))
                    if d:
                        hit = _q(cur, "select * from raw.invoices where invoice_id = %s", (d[0]["invoice_id"],))
                        pick = hit[0] if hit else None
                if not pick and fields.get("billing_period"):
                    pick = next((c for c in cands if c["bill_cycle"] == fields["billing_period"]), None)
                if not pick and amt is not None:
                    key = {"late_fee_autopay": "late_fee"}.get(cat, "amount_due")
                    pick = next((c for c in cands if abs(float(c[key] or 0) - float(amt)) < 0.01), None)
                if not pick and cat == "late_fee_autopay":
                    pick = next((c for c in cands if c["late_fee"]), None)
                if not pick and cands and cat not in ("equipment_fee", "final_bill"):
                    pick = cands[0]
                if not pick and cands:
                    pick = cands[0]
                if pick:
                    inv = pick
                    fill("invoice_id", pick["invoice_id"], 0.55, f"inferred: invoice {pick['bill_cycle']} of account")
                    check("invoice_inferred", "warn", f"no invoice in mail; using {pick['invoice_id']} ({pick['bill_cycle']})")
            if inv:
                fill("billing_period", inv["bill_cycle"], 0.7, "bill cycle of invoice")
                ctx["invoice"] = {k: (float(v) if k in ("amount_due", "recurring_charges", "one_time_charges", "taxes_fees", "credits", "late_fee") else str(v))
                                  for k, v in inv.items()}
                cat = fields.get("category")
                if fields.get("disputed_amount") in (None, ""):
                    v = {"late_fee_autopay": inv["late_fee"] or None,
                         "duplicate_charge": inv["amount_due"], "final_bill": inv["amount_due"]}.get(cat)
                    if v:
                        fill("disputed_amount", round(float(v), 2), 0.55, f"from invoice {inv['invoice_id']}")
                amt = fields.get("disputed_amount")
                if amt not in (None, ""):
                    amt, due = float(amt), float(inv["amount_due"])
                    exact = {"late_fee_autopay": float(inv["late_fee"] or 0),
                             "duplicate_charge": due, "final_bill": due}.get(cat)
                    on_file = _q(cur, "select disputed_amount from raw.disputes where invoice_id = %s", (inv["invoice_id"],))
                    if any(abs(float(d["disputed_amount"]) - amt) < 0.01 for d in on_file):
                        check("amount_plausible", "pass", f"${amt:.2f} matches the dispute on file for the invoice")
                    elif exact is not None and abs(exact - amt) < 0.01:
                        check("amount_plausible", "pass", f"${amt:.2f} matches invoice")
                    elif amt <= max(3 * due, 200):
                        check("amount_plausible", "pass" if exact is None else "warn",
                              f"${amt:.2f} vs invoice total ${due:.2f}")
                    else:
                        check("amount_plausible", "fail", f"${amt:.2f} exceeds plausible range (invoice ${due:.2f})")
                        conf["disputed_amount"] = min(conf.get("disputed_amount", 0.5), 0.4)
                if cat == "late_fee_autopay":
                    check("late_fee_on_invoice", "pass" if inv["late_fee"] else "warn",
                          f"late fee on invoice: ${float(inv['late_fee'] or 0):.2f}")
                txns = _q(cur, "select txn_id, txn_date, txn_type, amount, payment_method, reason from raw.transactions "
                               "where invoice_id = %s order by txn_date", (inv["invoice_id"],))
                pays = [t for t in txns if t["txn_type"] == "payment"]
                ctx["transactions"] = [{**t, "txn_date": str(t["txn_date"]), "amount": float(t["amount"])} for t in txns]
                if cat == "duplicate_charge":
                    check("payments_on_invoice", "pass" if len(pays) >= 2 else "warn",
                          f"{len(pays)} payment(s) posted against invoice in raw.transactions")
                if cat == "install_fee":
                    check("one_time_charge_on_invoice", "pass" if float(inv["one_time_charges"] or 0) else "warn",
                          f"one-time charges on invoice: ${float(inv['one_time_charges'] or 0):.2f}")
            if fields.get("category") == "late_fee_autopay":
                check("autopay_enrolled", "pass" if cust.get("autopay") else "warn",
                      "autopay on" if cust.get("autopay") else "autopay NOT on file")
            rows = _q(cur, "select bool_or(recent_price_increase) as up, bool_or(promo_expired) as promo from "
                           "(select * from raw.customer_monthly where ban = %s order by snapshot_month desc limit 3) s",
                      (acct,))
            ctx["recent_price_increase"] = bool(rows and rows[0]["up"])
            ctx["promo_expired"] = bool(rows and rows[0]["promo"])
            if fields.get("category") in ("promo_expired", "price_increase"):
                on_file = ctx["recent_price_increase"] if fields["category"] == "price_increase" else (
                    ctx["promo_expired"] or ctx["recent_price_increase"])
                check("price_change_on_file", "pass" if on_file else "warn",
                      "recent price increase / promo roll-off on file" if on_file else "no recent price change on file")
            cr = _q(cur, "select txn_id, invoice_id, txn_date, amount, reason from raw.transactions "
                         "where ban = %s and txn_type in ('credit', 'adjustment', 'refund') order by txn_date desc limit 10",
                    (acct,))
            ctx["prior_credits"] = [{**a, "txn_date": str(a["txn_date"]), "amount": float(a["amount"])} for a in cr]
            if len(cr) >= 3:
                check("prior_credits", "warn", f"{len(cr)} prior credits on account")
            dp = _q(cur, "select dispute_id, invoice_id, opened_date, channel, category, disputed_amount, status "
                         "from raw.disputes where ban = %s order by opened_date desc limit 10", (acct,))
            ctx["prior_disputes"] = [{**d, "opened_date": str(d["opened_date"]), "disputed_amount": float(d["disputed_amount"])}
                                     for d in dp]
            same_inv = [d for d in dp if fields.get("invoice_id") and d["invoice_id"] == fields["invoice_id"]]
            if same_inv:
                d = same_inv[0]
                check("dispute_on_file", "pass" if d["category"] == fields.get("category") else "warn",
                      f"{d['dispute_id']} on file for {d['invoice_id']}: {d['category']} ${float(d['disputed_amount']):.2f} ({d['status']})")
            loc = _q(cur, "select l.technology, l.clli, s.plan_id from raw.service_locations l "
                          "left join raw.subscriptions s using (ban) where l.ban = %s limit 1", (acct,))
            if loc:
                ctx.update(technology=loc[0]["technology"], clli=loc[0]["clli"], plan_id=loc[0]["plan_id"])
            sub = _q(cur, "select status, plan_id, end_date from raw.subscriptions where ban = %s", (acct,))
            if sub:
                ctx["subscription"] = {k: str(v) for k, v in sub[0].items()}
                if fields.get("category") == "final_bill":
                    check("account_cancelled", "pass" if sub[0]["status"] == "cancelled" else "warn",
                          f"subscription {sub[0]['status']}")
    except Exception as e:  # noqa: BLE001
        log.warning("warehouse validation unavailable: %s", e)
        check("warehouse", "warn", f"warehouse unavailable: {type(e).__name__}")
    return {"checks": checks, "context": ctx}


def overall(fields: dict, conf: dict, report: dict) -> float:
    s = sum(w * (conf.get(k, 0.0) if fields.get(k) not in (None, "") else 0.0) for k, w in WEIGHTS.items())
    # amount may legitimately be absent (e.g. outage credit "some credit"): do not punish twice
    if fields.get("disputed_amount") in (None, "") and fields.get("category") == "outage_credit":
        s += WEIGHTS["disputed_amount"] * 0.5
    mult = 1.0
    for c in report.get("checks", []):
        if c["status"] == "fail":
            mult *= 0.6
        elif c["status"] == "warn" and c["check"] in ("invoice_inferred", "name_matches", "warehouse"):
            mult *= 0.9
    return round(min(1.0, s * mult), 3)


def auto_approvable(fields: dict, report: dict, score: float) -> bool:
    if score < get().auto_approve_threshold:
        return False
    if not fields.get("ban") or not fields.get("category") or fields.get("category") == "other":
        return False
    return not any(c["status"] == "fail" for c in report.get("checks", []))
