#!/usr/bin/env python3
"""Metadata-only AI annotator for the DataHub catalog — a thin CLI over
bsp_common.catalog.annotate (the harness and MCP use the same function).

Runs where bsp_common, DataHub (DATAHUB_TOKEN / DATAHUB_GMS_URL) and the LLM
gateway are reachable — the harness container:

    make -C stacks/datahub annotate [CATALOGS="bq_rpt_enterprise_dv warehouse"] [MAX=300] [FORCE=1] [DRY=1]
    docker exec -i bsp_data-harness-1 python - --catalog bq_tsa_dv --max 50 < stacks/datahub/scripts/annotate.py

Picks Trino datasets (catalog.schema.table) that have no description and no
``ai-annotated`` tag (with --force: also those whose text is AI-suggested),
up to --max tables per run, and annotates them. The model sees METADATA ONLY
(names, types, existing descriptions / tags / terms); --dry-run prints the
exact payload for the first table instead of calling the model.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, "/opt/common")
from bsp_common import catalog  # noqa: E402


def candidates(catalogs: list[str], force: bool, schema: str | None) -> list[str]:
    out: list[str] = []
    for cat in catalogs:
        for t in catalog.tables(cat):
            if schema and not t["name"].lower().startswith(schema.lower() + "."):
                continue
            human = bool(t["description"]) and not catalog.is_ai_text(t["description"])
            if t["ai_annotated"] and not force:
                continue
            if human and not force:
                continue
            out.append(t["urn"])
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--catalog", action="append", help="Trino catalog (repeatable); default: every catalogued one")
    ap.add_argument("--schema", help="only this schema")
    ap.add_argument("--max", type=int, default=300, help="tables per run (default 300)")
    ap.add_argument("--force", action="store_true", help="also redo AI-suggested text (never human text)")
    ap.add_argument("--model", default=None, help="gateway alias (default chat-default)")
    ap.add_argument("--dry-run", action="store_true", help="print the metadata payload for the first table; no writes")
    ap.add_argument("--pii-only", action="store_true",
                    help="no model: tag personal-looking column names 'PII (suspected)' on every table of the catalogs")
    a = ap.parse_args()
    if catalog.backend() != "datahub":
        print("DataHub is not configured here (DATAHUB_TOKEN)", file=sys.stderr)
        return 2
    cats = a.catalog or [c["name"] for c in catalog.tree()["catalogs"] if c["name"] != "bigquery"]
    if a.pii_only:
        urns = [t["urn"] for cat in cats for t in catalog.tables(cat)
                if not a.schema or t["name"].lower().startswith(a.schema.lower() + ".")]
        print(f"{len(urns)} table(s) in {', '.join(cats)}: PII name tags only")
        print(json.dumps(catalog.tag_pii(urns, log=print)))
        return 0
    urns = candidates(cats, a.force, a.schema)
    print(f"{len(urns)} candidate table(s) in {', '.join(cats)}; cap {a.max}")
    if a.dry_run:
        if urns:
            d = catalog.describe(urns[0])
            print(json.dumps(catalog.metadata_for_llm(d), indent=1))
        return 0
    t0 = time.time()
    counts = catalog.annotate(urns[: a.max], force=a.force, model=a.model, max_tables=a.max, log=print)
    print(json.dumps(counts | {"seconds": round(time.time() - t0)}))
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
