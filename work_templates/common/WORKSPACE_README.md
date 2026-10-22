# Your workspace

This folder (`~/work`, on the host under `<data root>/<user>/work`) is yours:
it survives restarts and upgrades, and it is the same folder in JupyterLab,
VS Code, the Linux Desktop and every terminal.

| Path | What it is |
|---|---|
| `notebooks/` | Starter notebooks — begin with `00_getting_started.ipynb` |
| `dbt_project/` | The `bsp_edw` dbt project (staging → intermediate → marts); also in dbt Studio (`/dbt/`) |
| `lake/` | The shared lake (`/data/lake`): `landing/ reference/ exports/ samples/` |
| `.mcp.json` | MCP tools for Claude Code and other MCP clients (needs the AI capability) |
| `.templates-updates/` | Newer versions of starter files you have changed (never overwritten) |

## Connections (inside the appliance)

| What | How |
|---|---|
| Local warehouse | `postgres:5432`, database `bsp_edw`, user `analyst`, password in `$PG_PASS` |
| BigQuery | `bsp_common.gcp` — uses the identity assigned in Studio → Cloud identities (BigQuery · read/write); "not connected" until then |
| Trino | `trino:8080` (`trino` Python client), when the Trino capability runs |
| LLM gateway | `http://harness:8096/ai/v1` (OpenAI-compatible; model aliases such as `chat-default`) |
| Linux Desktop browser (CDP) | `http://127.0.0.1:9222` here, `http://browser:9223` from services |

```python
from bsp_common import db
df = db.query_df("select * from marts.dim_customer limit 10")
```

Starter files are copied in once and never overwritten. Delete one and it
stays deleted; when an upstream template changes and you have edited your
copy, the new version is placed in `.templates-updates/` for you to compare.

Need help? Open **Docs** from the Studio launchpad.
