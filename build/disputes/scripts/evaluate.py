"""Field-level accuracy of the Dispute Desk vs the fixture generator's ground truth.

    python build/disputes/scripts/evaluate.py [--url http://disputes:8099/disputes] [--json out.json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

GT = Path(__file__).resolve().parents[1] / "tests" / "ground_truth.json"
FIELDS = ["category", "ban", "invoice_id", "disputed_amount", "customer_name", "contact_email", "contact_phone",
          "requested_resolution", "billing_period", "urgency"]
KEY = ["category", "ban", "invoice_id", "disputed_amount"]


def same(k, a, b) -> bool:
    if k == "disputed_amount":
        if a in (None, "") or b in (None, ""):
            return a in (None, "") and b in (None, "")
        return abs(float(a) - float(b)) < 0.01
    if a is None or b is None:
        return a is None and b is None
    return str(a).strip().lower() == str(b).strip().lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8099/disputes")
    ap.add_argument("--json")
    a = ap.parse_args()
    gt = json.loads(GT.read_text())["messages"]
    c = httpx.Client(base_url=a.url, timeout=30)
    msgs = {m["internet_message_id"]: m for m in c.get("/api/messages").json()}
    res = {k: [0, 0] for k in ["triage"] + FIELDS + ["invoice_id (in text)", "ban (in text)"]}
    auto, auto_ok, misses = 0, 0, []
    for t in gt:
        m = msgs.get(t["message_id"])
        if not m or not m.get("case_id"):
            misses.append({"file": t["file"], "error": "not ingested"})
            continue
        case = c.get(f"/api/cases/{m['case_id']}").json()
        f = case["original_fields"] or case["fields"]
        pred_dispute = case["status"] != "not_dispute"
        res["triage"][1] += 1
        res["triage"][0] += pred_dispute == t["is_dispute"]
        if not t["is_dispute"]:
            continue
        wrong = []
        for k in FIELDS:
            res[k][1] += 1
            ok = same(k, f.get(k), t.get(k))
            res[k][0] += ok
            if not ok:
                wrong.append(f"{k}: got {f.get(k)!r} want {t.get(k)!r}")
        for k, flag in (("invoice_id", "invoice_in_text"), ("ban", "ban_in_text")):
            if t.get(flag):
                res[f"{k} (in text)"][1] += 1
                res[f"{k} (in text)"][0] += same(k, f.get(k), t.get(k))
        if case["auto_approved"]:
            auto += 1
            auto_ok += all(same(k, f.get(k), t.get(k)) for k in KEY)
        if wrong:
            misses.append({"file": t["file"], "case": case["case_ref"], "status": case["status"], "wrong": wrong})
    report = {"fields": {k: {"correct": v[0], "total": v[1], "accuracy": round(v[0] / v[1], 3) if v[1] else None}
                         for k, v in res.items()},
              "auto_approved": auto, "auto_approved_all_key_fields_correct": auto_ok, "misses": misses}
    for k, v in report["fields"].items():
        print(f"{k:24s} {v['correct']:3d}/{v['total']:<3d} {100 * (v['accuracy'] or 0):5.1f}%")
    print(f"auto-approved: {auto} (all key fields correct: {auto_ok})")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
