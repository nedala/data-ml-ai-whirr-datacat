"""Fixtures: shape, coverage, no label leakage; parsers for every format."""
import json
import re

import pytest

from bsp_ccai import parsers, taxonomy
from bsp_seed import ban_for
from conftest import FIXTURES, GROUND_TRUTH


def gt():
    return json.loads(GROUND_TRUTH.read_text())["calls"]


def test_fixture_volume_and_layout():
    files = [p for p in FIXTURES.rglob("*") if p.is_file()]
    calls = [p for p in files if p.suffix in (".json", ".txt", ".vtt")]
    assert 190 <= len(calls) <= 260
    assert all(re.match(r"\d{4}/\d{2}/\d{2}$", p.parent.relative_to(FIXTURES).as_posix()) for p in files)
    assert {p.suffix for p in calls} == {".json", ".txt", ".vtt"}
    assert any(p.name.startswith("manifest_") for p in files)


def test_ground_truth_covers_taxonomy_and_multi_intent():
    truth = gt()
    ids = set(taxonomy.load().ids)
    assert {t["primary"] for t in truth.values()} == ids
    assert sum(1 for t in truth.values() if t["secondary"]) >= 30
    bans = {ban_for(i) for i in range(20000)}   # the bsp_seed build(customers=20000) accounts
    assert all(re.match(r"\d{10}$", t["ban"]) and t["ban"] in bans for t in truth.values())


def test_no_labels_in_fixtures():
    ids = taxonomy.load().ids
    blob = "\n".join(p.read_text(errors="ignore") for p in FIXTURES.rglob("*") if p.is_file())
    assert not [i for i in ids if i in blob]


@pytest.mark.parametrize("fmt", ["json", "txt", "vtt"])
def test_parse_each_format(fmt):
    truth = gt()
    cid, t = next((k, v) for k, v in truth.items() if v["format"] == fmt)
    kind, rec = parsers.parse(t["file"], (FIXTURES / t["file"]).read_bytes())
    assert kind == "call" and rec["call_id"] == cid
    assert rec["ban"] == t["ban"]
    assert rec["turns"] and {x["role"] for x in rec["turns"]} == {"agent", "customer"}
    m = rec["metrics"]
    assert rec["duration_sec"] > 30 and m["agent_talk_pct"] and m["silence_sec"] is not None
    assert m["avg_customer_sentiment"] is not None and rec["started_at"]


def test_all_calls_parse_and_bans_detected():
    truth = gt()
    ok = 0
    for cid, t in truth.items():
        _, rec = parsers.parse(t["file"], (FIXTURES / t["file"]).read_bytes())
        ok += rec["ban"] == t["ban"]
    assert ok / len(truth) >= 0.97


def test_manifest_and_corrupt_file():
    man = next(FIXTURES.rglob("manifest_*.csv"))
    kind, rows = parsers.parse(man.name, man.read_bytes())
    assert kind == "manifest" and rows and "conversation_id" in rows[0]
    bad = next(FIXTURES.rglob("conv_partial_upload.json"))
    with pytest.raises(parsers.ParseError):
        parsers.parse(bad.name, bad.read_bytes())


def test_find_ban_variants():
    assert parsers.find_ban(["yeah it's 3100000008"]) == "3100000008"
    assert parsers.find_ban(["sure, it's 310 000 0008"]) == "3100000008"
    assert parsers.find_ban(["um it's three one zero oh oh oh oh oh oh eight"]) == "3100000008"
    assert parsers.find_ban(["call me at 555 123 4567"]) is None
