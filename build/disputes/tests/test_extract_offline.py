"""Rules-only extraction vs the fixture ground truth (no LLM, no warehouse).

    PYTHONPATH=build/disputes:build/common:build/seed pytest build/disputes/tests
"""

from __future__ import annotations

import email
import json
import os
from email import policy
from pathlib import Path

import pytest

os.environ["DISPUTES_USE_LLM"] = "off"
from bsp_disputes import extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
GT = json.loads((Path(__file__).parent / "ground_truth.json").read_text())
BOX = ROOT / "build/connectors/fixtures/m365/mailboxes" / GT["mailbox"] / "inbox"


def _run(t: dict) -> dict:
    m = email.message_from_bytes((BOX / t["file"]).read_bytes(), policy=policy.default)
    fr = m["From"].addresses[0]
    atts = [{"name": p.get_filename(), "contentType": p.get_content_type(), "data": p.get_content()}
            for p in m.iter_attachments()]
    return extract.extract({"subject": m["Subject"], "from": {"name": fr.display_name, "address": fr.addr_spec},
                            "body_text": m.get_body(("plain",)).get_content(), "headers": dict(m.items())}, atts)


@pytest.fixture(scope="module")
def results() -> list[tuple[dict, dict]]:
    return [(t, _run(t)) for t in GT["messages"]]


def test_fixtures_use_bans_and_new_invoice_ids():
    import re
    for t in GT["messages"]:
        if t["is_dispute"]:
            assert re.fullmatch(r"\d{10}", t["ban"])
            assert t["invoice_id"].startswith(t["ban"] + "-") and re.fullmatch(r"\d{10}-\d{4}", t["invoice_id"])
            assert t["category"] in extract.Category.__args__
            assert t["technology"] in ("copper", "fiber")


def test_regexes():
    assert extract.RX_ACCT[0].search("BAN 3100000008").group(1) == "3100000008"
    m = extract.RX_ACCT[1].search("billing account 310 000 0008")
    assert m and m.group(1).replace(" ", "") == "3100000008"
    m = extract.RX_INV.search("invoice 3100000008-2609.")
    assert m and (m.group(1), m.group(2)) == ("3100000008", "2609")
    assert not extract.RX_INV.search("call +1-555-123-4567")


def _acc(results, field, flag=None):
    rows = [(t, r) for t, r in results if t["is_dispute"] and (flag is None or t.get(flag))]
    ok = sum(str(r.get(field)).lower() == str(t.get(field)).lower() if field != "disputed_amount" else
             (r.get(field) is None) == (t.get(field) is None) and (t.get(field) is None or abs(r[field] - t[field]) < 0.01)
             for t, r in rows)
    return ok / len(rows)


def test_triage(results):
    assert sum(r["is_dispute"] == t["is_dispute"] for t, r in results) / len(results) >= 0.95


@pytest.mark.parametrize("field,flag,floor", [("category", None, 0.9), ("ban", "ban_in_text", 0.95),
                                               ("invoice_id", "invoice_in_text", 0.95), ("disputed_amount", None, 0.85)])
def test_field_accuracy(results, field, flag, floor):
    assert _acc(results, field, flag) >= floor
