# ---
# jupyter:
#   jupytext:
#     formats: ipynb,py:percent
#     text_representation:
#       extension: .py
#       format_name: percent
#   kernelspec:
#     display_name: Python 3
#     language: python
#     name: python3
# ---

# %% [markdown]
# # 03 · MCP client in Python
#
# The appliance MCP server (`http://mcp:8095/mcp`, bearer `MCP_TOKEN`) exposes tools for the
# warehouse, Trino, BigQuery, Atlan, Power BI, Microsoft 365, MLflow, SAP BRIM, the desktop browser
# and the knowledge base. Here we call them directly with the FastMCP client.

# %%
import json
import os

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

URL = os.environ.get("MCP_URL", "http://mcp:8095/mcp")
HEADERS = {"Authorization": f"Bearer {os.environ.get('MCP_TOKEN', '')}"}


def client(url=URL):
    return Client(StreamableHttpTransport(url, headers=HEADERS))


async def call(name, **args):
    async with client() as c:
        r = await c.call_tool(name, args, raise_on_error=False)
    data = json.loads(r.content[0].text) if r.content and r.content[0].text.startswith("{") else r.content[0].text
    if r.is_error:
        print("ERROR:", data)
    return data

# %%
async with client() as c:
    tools = await c.list_tools()
    prompts = await c.list_prompts()
    resources = await c.list_resources()
groups = {}
for t in tools:
    groups.setdefault(t.name.split("_")[0], []).append(t.name)
for g, names in groups.items():
    print(f"{g:10s} {len(names):2d}  {', '.join(names)}")
print("prompts:", [p.name for p in prompts])
print("resources:", [str(r.uri) for r in resources])

# %% [markdown]
# ## Warehouse (read-only SQL)

# %%
r = await call("warehouse_query", sql="select state, count(*) as customers from raw.customers group by 1 order by 2 desc limit 5")
print(r["summary"])

# %% [markdown]
# Every account table is keyed by `ban` (10-digit billing account number); invoices are `<ban>-<yymm>`.

# %%
sql = ("select c.ban, c.customer_name, l.technology, s.plan_id, i.invoice_id, i.amount_due, i.status "
       "from raw.customers c join raw.service_locations l using (ban) join raw.subscriptions s using (ban) "
       "join raw.invoices i using (ban) where c.ban = '3100000016' order by i.invoice_date desc limit 3")
print((await call("warehouse_query", sql=sql))["summary"])

# %%
print((await call("warehouse_query", sql="delete from raw.customers"))) # rejected: read-only

# %% [markdown]
# ## Atlan catalog (mock until ATLAN_API_TOKEN is set)

# %%
r = await call("atlan_search_assets", query="churn")
print(r["summary"])
print((await call("atlan_glossary_lookup", term="churn"))["summary"])

# %% [markdown]
# ## BigQuery: dry run first, then a capped SELECT

# %%
print((await call("bigquery_dry_run", sql="select ban, state from raw.customers"))["summary"])
print((await call("bigquery_run_query", sql="select ban, state from raw.customers limit 5"))["summary"])

# %% [markdown]
# ## Microsoft 365 (mock until the Entra app is configured)

# %%
print((await call("m365_mail_search", query="late fee"))["summary"])
print((await call("m365_mail_create_draft", to=["customer@example.net"], subject="Re: your bill",
                  body="We are reviewing your dispute."))["summary"])   # preview only

# %% [markdown]
# ## Resources: the warehouse schema as markdown (great context for SQL generation)

# %%
async with client() as c:
    schema = (await c.read_resource("warehouse://schema"))[0].text
print(schema[:1500])
