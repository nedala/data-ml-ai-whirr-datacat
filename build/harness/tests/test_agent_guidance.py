"""The agent tells the model which tools it has and to retry after earlier failures."""
from bsp_harness.agent import with_guidance


def test_guidance_mentions_browsing_and_retry():
    tools = [{"name": "browser_navigate", "group": "browser"}, {"name": "warehouse_query", "group": "warehouse"}]
    out = with_guidance([{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "hi"}], tools)
    sys = out[0]["content"]
    assert out[0]["role"] == "system" and sys.startswith("You are helpful.")
    assert "CAN browse the internet" in sys and "try it again" in sys and "warehouse" in sys
    assert out[1] == {"role": "user", "content": "hi"}


def test_no_tools_no_change():
    msgs = [{"role": "user", "content": "hi"}]
    assert with_guidance(msgs, []) == msgs


def test_inserts_system_when_missing():
    out = with_guidance([{"role": "user", "content": "hi"}], [{"name": "rag_search", "group": "rag"}])
    assert out[0]["role"] == "system" and "knowledge base" in out[0]["content"]
