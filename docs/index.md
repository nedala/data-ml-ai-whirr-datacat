# Data & AI Appliance

A self-hosted, Docker-based workspace for data engineering, analytics, MLOps and
AI agents. One control plane (**Studio**) manages single sign-on, an encrypted
vault, a single HTTPS front door, allowlisted egress and all service containers.
Three personas share one workspace image:

| Persona | For | Included |
|---|---|---|
| **Data Professional** | analytics / data engineers, stewards | integration canvas · dbt on Postgres, BigQuery and Trino · data catalog · Superset · Power BI |
| **ML Scientist** | data scientists | EDA → features → experiments → registry → serving → drift → retraining |
| **AI Engineer** | AI / automation engineers | LLM gateway · MCP tool server · RAG · Open WebUI · browser automation |

Connectors are **mock-or-live**. The appliance ships with synthetic demo data and
safe mock responses; real credentials are added in Studio when you are ready.

## Deep dive

[Download the 24-page deep-dive PDF](datacat-deep-dive.pdf) — business purpose, the three personas, key functions with inputs and outputs, and the real tooling screens.

## Quick start

Requirements: Docker Engine 24+ with Compose v2.20+, at least 16 GB RAM per
appliance, and a host with ports 31201–31203 free (or edit the overlay files).

```bash
git clone <this-repo> appliance
cd appliance
cp .env.example .env        # non-secret settings only
make build                  # workspace + Linux Desktop images
make up APPLIANCE=data      # data | ml | ai
make open APPLIANCE=data    # https://localhost:31201/studio/
```

Studio opens on first run. Create the admin account and the vault passphrase,
review the preselected persona on **Compose**, then press **Start**.

| Appliance | Compose project | Studio | Data root |
|---|---|---|---|
| data | `bsp_data` | https://localhost:31201/ | `./data/data` |
| ml | `bsp_ml` | https://localhost:31202/ | `./data/ml` |
| ai | `bsp_ai` | https://localhost:31203/ | `./data/ai` |

Useful targets: `make ps`, `make logs`, `make down`, `make up-all`, `make test`.

## Configuration

- **`.env`** holds non-secret settings (ports, hosts, regions, feature flags).
  Copy it from `.env.example`; it is ignored by git.
- **`appliances/{data,ml,ai}.env`** are per-appliance overlays (ports and data
  root). They contain no secrets and are safe to edit.
- **Secrets never go in git.** Service passwords and enterprise keys are stored
  in Studio's encrypted vault and injected at container start:
  - cloud credentials / service-account JSON
  - Atlan and BigQuery API tokens
  - Microsoft 365 / Power BI client secrets
  - SAP BRIM credentials
  - LLM provider keys
  - `MCP_TOKEN` for the tool server

Set non-secret endpoints in `.env` or Studio (for example
`ATLAN_BASE_URL=https://<your-tenant>.atlan.com` and `GCP_PROJECT_ID=<project>`).
Set the corresponding secret in **Studio → Credentials**.

## MCP tool server

The server speaks streamable HTTP MCP:

```text
http://127.0.0.1:${MCP_PORT:-8765}/mcp          all enabled groups
http://127.0.0.1:${MCP_PORT:-8765}/mcp/<group>  one group (atlan, bigquery, …)
```

Authenticate with `Authorization: Bearer $MCP_TOKEN` (Studio → Credentials →
MCP_TOKEN). Public discovery pages: `/mcp/info`, `/mcp/tools.json`,
`/mcp/tools/status` (add `?probe=1` for a live credential check).

### Codex / Claude / IDE clients

```toml
# ~/.codex/config.toml
[mcp_servers.appliance]
url = "http://127.0.0.1:8765/mcp"
bearer_token_env_var = "MCP_TOKEN"
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Export the token in the shell that launches the client:

```bash
export MCP_TOKEN='paste-from-Studio-Credentials'
```

If the client runs on another machine, use the appliance facade
(`https://<appliance-host>:31201/mcp`) or forward port 8765 over SSH. Clients
that only support stdio can launch `python -m bsp_mcp.stdio_bridge` with
`MCP_URL` and `MCP_TOKEN` set.

### API vs UI tools

The browser-backed UI tools work without enterprise API tokens:

- `atlan_ui_search`, `atlan_ui_asset`, `atlan_discover_assets`, `atlan_ui_join_map`
- `bigquery_ui_status`, `bigquery_ui_query`, `bigquery_ui_results`, `bigquery_ui_export`

The API tools are hidden while credentials are missing (`MCP_API_TOOLS=off`).
When tokens are provisioned, set `MCP_API_TOOLS=on` (or `auto` with
`MCP_BQ_API_ENABLED=1` for BigQuery) and restart the MCP service. The current
roster, disabled set and credential state are at `/mcp/tools/status`.

The **Atlan API** supports a true multi-term Elasticsearch-DSL query through
`atlan_multi_search(terms=[...], match="any"|"all")` when a token is available.
The Atlan **UI** command palette does not reliably implement boolean OR, so the
UI fallback searches one term at a time for coverage.

## AI agents and the Playground

Studio includes an AI Playground with a model/route selector, system prompt and
MCP tool groups. Two ready-made agent presets are available:

- **SQL Analyst** — discovery → fully qualified SQL → run → grounded answer.
- **BigQuery Analyst** — Atlan UI discovery → BigQuery Studio UI → run/peek/export,
  with no warehouse fallback and no hardcoded SQL.

For BigQuery questions, select **BigQuery Analyst** and enable the `atlan`,
`bigquery` and `browser` groups. The agent decomposes the question, discovers
assets in Atlan, reads their real columns, builds the SQL dynamically and runs
it in the logged-in BigQuery Studio tab.

## Data and catalog

- **Postgres warehouse** — local marts and raw tables, keyed by account id.
- **Trino** — federated queries across the warehouse, Oracle, SQL Server, a lake
  and BigQuery catalogs.
- **BigQuery** — read-only REST tools and a Studio UI fallback using the
  logged-in desktop session.
- **Atlan** — API search/lineage/glossary, plus UI search/schema fallbacks.
- **DataHub** — optional side stack for catalog and lineage.
- **RAG / Open WebUI** — knowledge-base ingestion, retrieval and citations.
- **MLflow** — experiments, registry, deployments and drift telemetry.

## Security and backups

- `.env`, `data/`, service-account JSON, TLS keys and browser profiles are
  git-ignored. Never commit them.
- Use the Studio vault for secrets; rotate a credential immediately if it is
  exposed.
- Back up `./data/<appliance>` (vault, Studio settings, service data) and the
  host TLS material. Test restore on a separate appliance before relying on it.
- Keep egress allowlists narrow; the proxy is deny-by-default.
- The MCP server audits tool calls; review **Studio → Audit** periodically.

## Troubleshooting

| Symptom | Check |
|---|---|
| Studio not reachable | `make ps`; the facade port in `appliances/*.env`; TLS trust |
| MCP client sees no tools | `MCP_TOKEN` exported; URL ends in `/mcp`; token has not been rotated |
| API tools missing | expected while `MCP_API_TOOLS=off`; use the UI tools or enable after provisioning |
| BigQuery UI query returns Access Denied | the signed-in identity lacks `bigquery.tables.getData` on that table/project |
| Atlan UI search empty | the desktop session is signed out, or the guest role cannot see the asset type |

## Repository layout

```text
build/          services: studio, mcp, harness, connectors, nginx, browser, rag, mlops, …
config/         non-secret catalog / AI routing configuration
appliances/     per-appliance port and data-root overlays
dbt_project/    dbt models, macros and tests
stacks/         optional side stacks (DataHub, Trino, …)
work_templates/ notebooks and scripts for each persona
data/           runtime data and saved credentials (git-ignored)
```

## License

Add the license that fits your distribution. Do not publish credentials,
customer data or tenant-specific configuration with the code.
