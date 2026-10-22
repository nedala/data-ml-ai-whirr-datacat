"""bsp_rag — vector store + RAG service for the DataPlatform AI Engineer appliance.

Ingest (upload · lake · SharePoint · URL) → parse → structure-aware chunks →
embed (gateway `embed-default` or the offline hashing embedder) → Qdrant or
pgvector → hybrid retrieval (dense + BM25/full-text, RRF, MMR, rerank) →
grounded answers with numbered citations → evals. Front-office demos (agent
assist, runbook copilot, design Q&A) and MCP-ready tools live in
`bsp_rag.frontoffice` / `bsp_rag.tools`.
"""

__version__ = "1.0.0"
