"""MCP resources: the warehouse schema and dbt docs (when dbt_project/target/manifest.json exists)."""
from __future__ import annotations

import json
import os
from pathlib import Path


def dbt_manifest_path() -> Path | None:
    cands = [os.environ.get("DBT_MANIFEST", ""), os.environ.get("DBT_PROJECT_DIR", "") and
             str(Path(os.environ["DBT_PROJECT_DIR"]) / "target/manifest.json"),
             "/opt/dbt_project/target/manifest.json", str(Path.home() / "work/dbt_project/target/manifest.json"),
             str(Path(__file__).resolve().parents[3] / "dbt_project/target/manifest.json")]
    for c in cands:
        if c and Path(c).is_file():
            return Path(c)
    return None


def dbt_docs_markdown(max_models: int = 300) -> str:
    p = dbt_manifest_path()
    if p is None:
        return "dbt manifest not found — run `dbt docs generate` (or `dbt build`) in dbt_project first."
    m = json.loads(p.read_text())
    out = [f"# dbt project docs ({m.get('metadata', {}).get('project_name', 'bsp_edw')})", ""]
    nodes = [n for n in m.get("nodes", {}).values() if n.get("resource_type") in ("model", "seed", "snapshot")]
    for n in sorted(nodes, key=lambda x: (x.get("schema", ""), x.get("name", "")))[:max_models]:
        out.append(f"## {n.get('schema')}.{n.get('alias') or n.get('name')} ({n.get('resource_type')})")
        if n.get("description"):
            out.append(n["description"])
        cols = n.get("columns") or {}
        if cols:
            out += [f"- `{c}`: {v.get('description', '')}" for c, v in cols.items()]
        deps = (n.get("depends_on") or {}).get("nodes") or []
        if deps:
            out.append("depends on: " + ", ".join(d.split(".")[-1] for d in deps))
        out.append("")
    return "\n".join(out)


def register(mcp) -> None:
    @mcp.resource("warehouse://schema", name="warehouse_schema", mime_type="text/markdown",
                  description="Every schema, table and column in the local warehouse (Postgres bsp_edw)")
    def warehouse_schema() -> str:
        from .tools.warehouse import schema_markdown  # noqa: PLC0415

        try:
            return schema_markdown()
        except Exception as e:  # noqa: BLE001
            return f"warehouse not reachable: {e}"

    @mcp.resource("dbt://docs", name="dbt_docs", mime_type="text/markdown",
                  description="dbt models with descriptions, columns and dependencies (from target/manifest.json)")
    def dbt_docs() -> str:
        return dbt_docs_markdown()
