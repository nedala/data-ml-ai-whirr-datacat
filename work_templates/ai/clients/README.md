# MCP client configs

Copy the file for your client (paths and details: docs → AI Engineer → *Connect your own MCP client*).

| client | file | goes to |
|---|---|---|
| Claude Code | `../.mcp.json` | project root `.mcp.json` (or `claude mcp add --transport http …`) |
| OpenAI Codex CLI / IDE | `codex-config.toml` | `~/.codex/config.toml` |
| VS Code (Copilot agent) | `vscode-mcp.json` | `.vscode/mcp.json` |
| Cursor | `cursor-mcp.json` | `.cursor/mcp.json` or `~/.cursor/mcp.json` |
| Claude Desktop | `claude-desktop-config.json` | Settings → Developer → Edit config |

Token: Studio → Credentials → `MCP_TOKEN`; keep it in an environment variable, never in a committed file.
Laptop port: `MCP_PORT` of your appliance (8765 by default); inside the appliance use `http://mcp:8095/mcp`.
