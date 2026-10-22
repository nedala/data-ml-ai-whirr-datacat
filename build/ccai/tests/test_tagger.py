"""Offline tagger accuracy vs ground truth, confidence meaning, and the LLM route (stubbed gateway)."""
import json

import pytest

from bsp_ccai import parsers, tagger, taxonomy
from conftest import FIXTURES, GROUND_TRUTH

ST = {"confidence_threshold": 0.6, "classify_alias": "classify-default", "embed_alias": "embed-default"}


@pytest.fixture(scope="module")
def tagged():
    tax = taxonomy.load()
    truth = json.loads(GROUND_TRUTH.read_text())["calls"]
    out = []
    for cid, t in truth.items():
        _, rec = parsers.parse(t["file"], (FIXTURES / t["file"]).read_bytes())
        out.append((t, rec, tagger.tag_call(rec, tax, ST, use_llm=False)))
    return out


def test_offline_accuracy(tagged):
    n = len(tagged)
    top1 = sum(r["primary_leaf"] == t["primary"] for t, _, r in tagged) / n
    top3 = sum(t["primary"] in [c["leaf"] for c in r["candidates"][:3]] for t, _, r in tagged) / n
    assert top1 >= 0.75, top1
    assert top3 >= 0.88, top3


def test_confidence_is_meaningful(tagged):
    hi = [r["primary_leaf"] == t["primary"] for t, _, r in tagged if r["confidence"] >= 0.6]
    lo = [r["primary_leaf"] == t["primary"] for t, _, r in tagged if r["confidence"] < 0.6]
    assert hi and lo and sum(hi) / len(hi) > sum(lo) / len(lo) + 0.2


def test_tag_shape(tagged):
    tax = taxonomy.load()
    for _, rec, r in tagged[:20]:
        assert r["primary_leaf"] in tax.by_id and 0 <= r["confidence"] <= 1 and r["route"] == "offline"
        assert len(r["secondary"]) <= 2 and all(s["leaf"] in tax.by_id for s in r["secondary"])
        assert r["summary"] and r["resolution_status"] in tagger.RES_TEXT and r["sentiment_trajectory"]
        assert all(0 <= i < len(rec["turns"]) for i in r["evidence_turns"])


def test_llm_route_with_stubbed_gateway(monkeypatch, tagged):
    from bsp_common import llm
    tax = taxonomy.load()
    t, rec, off = tagged[0]
    replies = iter(["not json at all", json.dumps({"primary": t["primary"], "secondary": ["NOT.A.LEAF"], "confidence": 0.9,
                                                    "rationale": "test", "evidence_turn_ids": [5, 999], "summary": "s",
                                                    "resolution_status": "resolved"})])
    monkeypatch.setattr(llm, "chat", lambda *a, **k: next(replies))
    monkeypatch.setattr(llm, "embed", lambda texts, model="": [[float(len(x) % 7), 1.0, 0.5] for x in texts])
    r = tagger.tag_call(rec, tax, ST, use_llm=True)
    assert r["route"] == "llm" and r["primary_leaf"] == t["primary"] and r["secondary"] == []
    assert r["evidence_turns"] == [5] and r["signals"]["attempts"] == 2 and 0 < r["confidence"] <= 1


def test_llm_failure_falls_back_offline(monkeypatch, tagged):
    from bsp_common import llm
    tax = taxonomy.load()
    _, rec, off = tagged[1]
    monkeypatch.setattr(llm, "chat", lambda *a, **k: '{"primary": "BOGUS.ID"}')
    monkeypatch.setattr(llm, "embed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    r = tagger.tag_call(rec, tax, ST, use_llm=True)
    assert r["route"] == "offline-fallback" and r["primary_leaf"] == off["primary_leaf"]
    assert "fallback_reason" in r["signals"]
