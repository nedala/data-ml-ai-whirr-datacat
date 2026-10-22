import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path[:0] = [str(HERE.parent), str(ROOT / "common"), str(ROOT / "connectors")]


@pytest.fixture(autouse=True)
def mcp_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_DATA_DIR", str(tmp_path / "mcp"))
    monkeypatch.setenv("CONNECTORS_STATE_DIR", str(tmp_path / "connectors"))
    monkeypatch.setenv("MCP_AUDIT_DB", "0")
    monkeypatch.setenv("SAP_BRIM_MODE", "mock")
    for k in ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET", "MCP_M365_WRITE", "MCP_GROUPS", "MCP_DISABLE_GROUPS"):
        monkeypatch.delenv(k, raising=False)
    yield
