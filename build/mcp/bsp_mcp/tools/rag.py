"""Knowledge base (build/rag, bsp_rag) — wraps bsp_rag.tools when it is importable.

`bsp_rag.tools` exposes TOOLS (rag_search, rag_answer, rag_list_collections,
rag_agent_assist, rag_runbook_copilot — plain functions calling the rag service). They are re-registered here as MCP tools with our audit/error
handling; when bsp_rag is absent the group is simply not registered."""
from __future__ import annotations

import importlib
import inspect
import logging

from ..common import REGISTRY, ToolSpec

G = "rag"
log = logging.getLogger("bsp_mcp.rag")
NAMES = ("rag_search", "rag_answer", "rag_list_collections")
TITLES = {"rag_search": "Search the knowledge base", "rag_answer": "Answer from the knowledge base (with citations)",
          "rag_list_collections": "List knowledge-base collections"}


_done: list[str] | None = None


def register() -> list[str]:
    global _done
    if _done is not None:
        return _done
    _done = []
    try:
        mod = importlib.import_module("bsp_rag.tools")
    except Exception as e:  # noqa: BLE001 — absent or broken: skip the group cleanly
        log.info("rag tools not registered (bsp_rag.tools unavailable: %s)", type(e).__name__)
        return []
    added = []
    fns = list(getattr(mod, "TOOLS", None) or [getattr(mod, n, None) for n in NAMES])
    for fn in fns:
        name = getattr(fn, "__name__", "")
        if not callable(fn) or not name.startswith("rag_") or any(t.name == name for t in REGISTRY.tools):
            continue

        doc = inspect.getdoc(fn) or TITLES.get(name, name)
        REGISTRY.tools.append(ToolSpec(G, fn, name, TITLES.get(name, name.replace("_", " ").capitalize()), True, False,
                                       doc.split("\n\n")[0]))
        added.append(name)
    _done = added
    return added
