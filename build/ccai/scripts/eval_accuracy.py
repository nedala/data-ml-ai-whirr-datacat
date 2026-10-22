"""Measure tagger accuracy against build/ccai/tests/ground_truth.json.

    python build/ccai/scripts/eval_accuracy.py                  # offline tagger on the fixture files (no DB)
    python build/ccai/scripts/eval_accuracy.py --tags tags.json # score exported tags ({call_id: {primary_leaf, candidates, confidence}})

Reports top-1 / top-3 primary accuracy, secondary-tag precision/recall, resolution and
churn-flag agreement, and accuracy by confidence band (is confidence meaningful?).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[3]
sys.path.insert(0, str(ROOT / "build" / "ccai"))
sys.path.insert(0, str(ROOT / "build" / "common"))

FIX = ROOT / "build/connectors/fixtures/m365/sites/default/CCAI/CallLogs"
GT = ROOT / "build/ccai/tests/ground_truth.json"


def offline_tags() -> dict:
    from bsp_ccai import parsers, tagger, taxonomy

    tax = taxonomy.load()
    st = {"confidence_threshold": 0.6}
    out = {}
    for p in sorted(FIX.rglob("*")):
        if not p.is_file():
            continue
        try:
            kind, rec = parsers.parse(p.name, p.read_bytes())
        except parsers.ParseError:
            continue
        if kind != "call":
            continue
        out[rec["call_id"]] = tagger.tag_call(rec, tax, st, use_llm=False)
    return out


def score(tags: dict, truth: dict) -> dict:
    n = top1 = top3 = 0
    sec_tp = sec_fp = sec_fn = 0
    res_ok = churn_ok = 0
    bands: dict[str, list[int]] = {}
    buckets: dict[str, list[int]] = {}
    for cid, gt in truth.items():
        t = tags.get(cid)
        if not t:
            continue
        n += 1
        ok = t["primary_leaf"] == gt["primary"]
        top1 += ok
        cands = [c["leaf"] for c in t.get("candidates", [])][:3]
        top3 += gt["primary"] in ([t["primary_leaf"]] + cands)[:3] or gt["primary"] in cands
        pred_sec = {s["leaf"] for s in t.get("secondary", [])}
        true_sec = set(gt["secondary"])
        sec_tp += len(pred_sec & true_sec)
        sec_fp += len(pred_sec - true_sec)
        sec_fn += len(true_sec - pred_sec)
        res_ok += t.get("resolution_status") == gt["resolution"]
        churn_ok += bool(t.get("churn_risk")) == gt["churn_risk"]
        bands.setdefault(t["confidence_band"], []).append(ok)
        c = t["confidence"]
        b = "≥0.8" if c >= 0.8 else "0.6–0.8" if c >= 0.6 else "0.4–0.6" if c >= 0.4 else "<0.4"
        buckets.setdefault(b, []).append(ok)
    pct = lambda a, b: round(100 * a / b, 1) if b else None  # noqa: E731
    return {
        "calls_scored": n, "top1_pct": pct(top1, n), "top3_pct": pct(top3, n),
        "secondary_precision_pct": pct(sec_tp, sec_tp + sec_fp), "secondary_recall_pct": pct(sec_tp, sec_tp + sec_fn),
        "resolution_agreement_pct": pct(res_ok, n), "churn_flag_agreement_pct": pct(churn_ok, n),
        "accuracy_by_band": {k: {"n": len(v), "top1_pct": pct(sum(v), len(v))} for k, v in sorted(bands.items())},
        "accuracy_by_confidence": {k: {"n": len(v), "top1_pct": pct(sum(v), len(v))}
                                   for k, v in sorted(buckets.items(), key=lambda kv: kv[0])},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", help="JSON {call_id: tag} to score instead of running the offline tagger")
    args = ap.parse_args()
    truth = json.loads(GT.read_text())["calls"]
    tags = json.loads(Path(args.tags).read_text()) if args.tags else offline_tags()
    print(json.dumps(score(tags, truth), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
