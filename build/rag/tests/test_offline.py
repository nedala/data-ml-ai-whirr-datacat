"""Offline tests: SQLite metadata + memory vector store + hashing embedder, no
LLM. Run from the repo root inside a Python 3.11 container:

    PYTHONPATH=build/common:build/rag:build/connectors pytest -q build/rag/tests
"""

from __future__ import annotations

import os

import pytest

os.environ.update({"RAG_META_STORE": "sqlite", "RAG_SQLITE_PATH": ":memory:", "RAG_USE_LLM": "never",
                   "RAG_EMBEDDER": "hash", "RAG_DATA_DIR": "/tmp/bsp-rag-test", "LLM_GATEWAY_URL": "http://127.0.0.1:9/v1"})

from bsp_rag import answer, chunking, config, evals, frontoffice, ingest, meta, parsing, retrieval, stores  # noqa: E402

config.reset()


@pytest.fixture(scope="module")
def coll():
    meta.set_instance(meta.Meta("sqlite", ":memory:"))
    stores.reset()
    c = ingest.create_collection("fo-test", backend="memory", embedder="hash")
    job = ingest.start_job("corpus", "fo-test", "", lambda j: ingest.ingest_corpus(j, c), background=False)
    assert job.status == "done", job.to_dict()
    return c


def test_parse_markdown_steps_and_tables():
    data = open(config.get().corpus_dir / "runbooks" / "rb-billing-dispute-handling.md", "rb").read()
    doc = parsing.parse(data, "rb-billing-dispute-handling.md")
    assert doc.meta["doc_id"] == "RB-BIL-001"
    kinds = {b.kind for b in doc.blocks}
    assert {"heading", "step", "table", "para"} <= kinds
    chunks = chunking.chunk(doc)
    proc = [c for c in chunks if "3. Procedure" in c.section]
    assert proc and "1. **Verify the caller.**" in proc[0].body and "9. **Close the case**" in proc[0].body


def test_binary_formats_parse():
    base = config.get().corpus_dir / "binary"
    for name, needle in [("ds-xgs-pon-migration-plan.pdf", "1577"), ("rb-storm-restoration-playbook.docx", "generator"),
                         ("rb-wire-center-contacts.xlsx", "RLGHNCXA"), ("trn-front-office-onboarding.pptx", "Verify the caller")]:
        doc = parsing.parse((base / name).read_bytes(), name)
        assert needle in doc.text, name


def test_ingest_dedupe_and_unchanged(coll):
    m = meta.get()
    docs = m.documents(int(coll["id"]))
    assert len(docs) >= 30
    job = ingest.start_job("corpus", "fo-test", "", lambda j: ingest.ingest_corpus(j, coll), background=False)
    assert all(r["status"] == "unchanged" for r in job.results)
    data = (config.get().corpus_dir / "faq" / "faq-billing.md").read_bytes()
    r = ingest.ingest_bytes(coll, data, "copy.md", source="upload", uri="upload://copy.md")
    assert r["status"] == "duplicate"


def test_hybrid_search_finds_runbook(coll):
    res = retrieval.search("fo-test", "ONT red LOS light no internet", k=5, use_llm=False)
    assert res["results"][0]["doc_key"] in ("RB-TEC-003", "FAQ-TEC-002")
    assert any("dense_rank" in r for r in res["results"]) and any("lexical_rank" in r for r in res["results"])


def test_answer_cites_and_abstains(coll):
    a = answer.answer("fo-test", "What is the goodwill credit limit for a Tier 1 care agent?", use_llm=False)
    assert "$25" in a["answer"] and a["citations"]
    assert a["citations"][0]["doc_key"] == "POL-BIL-006"
    x = answer.answer("fo-test", "What is the dress code for the annual holiday party?", use_llm=False)
    assert x["answer"].startswith("I don't know")


def test_agent_assist_late_fee(coll):
    r = frontoffice.agent_assist(frontoffice.SAMPLES[0]["text"], "fo-test", use_llm=False)
    assert r["intent"]["intent"] in ("late_fee", "autopay")
    assert r["entities"]["bans"] == ["3100000008"]
    assert r["entities"]["invoices"] == ["3100000008-2609"]
    assert r["checklist"] and r["citations"]
    assert "Maria" in r["draft_reply"]


def test_entities_ban_invoice_clli():
    e = frontoffice.entities("BAN 3100000043, invoice 3100000008-2609, wire center MNFDOHXA, call 5550100123. DISPUTED")
    assert e["bans"] == ["3100000008", "3100000043"]
    assert e["invoices"] == ["3100000008-2609"]
    assert e["wire_centers"] == ["MNFDOHXA"]
    assert frontoffice._region("MNFDOHXA") == "Central" and frontoffice._region("TYLRTXXA") == "South"


def test_runbook_copilot(coll):
    r = frontoffice.runbook_copilot(frontoffice.RUNBOOK_SAMPLES[0], "fo-test", use_llm=False)
    assert r["wire_center"] == "RLGHNCXA" and r["region"] == "East" and r["severity"] == "SEV1"
    assert len(r["noc_steps"]) >= 4 and r["escalation"]
    assert r["local_contacts"].get("Field Ops supervisor") == "Jordan Ellis"


def test_evals(coll):
    r = evals.run("fo-test", "hybrid", with_answers=True, use_llm=False)
    assert r["metrics"]["hit@5"] >= 0.8
    assert r["metrics"]["abstain_accuracy"] == 1.0


def test_mixed_model_refused(coll):
    from bsp_rag import embeddings

    with pytest.raises(embeddings.MixedModelError):
        embeddings.for_collection("gateway:embed-default", 768)
