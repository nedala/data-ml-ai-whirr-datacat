"""stdio ↔ streamable-HTTP bridge for MCP clients that only launch local (stdio) servers.

    MCP_URL=http://127.0.0.1:8765/mcp MCP_TOKEN=… python -m bsp_mcp.stdio_bridge
    python -m bsp_mcp.stdio_bridge --url http://127.0.0.1:8765/mcp/warehouse

Needs only `pip install fastmcp` on the laptop (no Node). The token is read from the
environment (never pass it on the command line, where it lands in shell history / ps).
"""
from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    ap = argparse.ArgumentParser(prog="bsp_mcp.stdio_bridge")
    ap.add_argument("--url", default=os.environ.get("MCP_URL", "http://127.0.0.1:8765/mcp"))
    ap.add_argument("--token-env", default="MCP_TOKEN", help="env var holding the bearer token")
    a = ap.parse_args()
    tok = os.environ.get(a.token_env, "")
    if not tok:
        print(f"bsp_mcp.stdio_bridge: ${a.token_env} is empty — get the token in Studio → Credentials", file=sys.stderr)
    from fastmcp import Client, FastMCP  # noqa: PLC0415
    from fastmcp.client.transports import StreamableHttpTransport  # noqa: PLC0415

    transport = StreamableHttpTransport(a.url, headers={"Authorization": f"Bearer {tok}"} if tok else {})
    FastMCP.as_proxy(Client(transport), name="dataplatform").run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
