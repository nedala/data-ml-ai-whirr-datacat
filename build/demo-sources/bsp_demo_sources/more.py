"""Append one more batch to a demo system so the next incremental run has new rows.

    python -m bsp_demo_sources.more minio    # next month's billing/YYYY-MM.parquet
    python -m bsp_demo_sources.more mssql    # +250 care orders, 40 open orders completed (updated_at = now)
    python -m bsp_demo_sources.more oracle   # next bill cycle into INVOICES / TRANSACTIONS
    python -m bsp_demo_sources.more all

From the host: docker exec <project>-demo-seed-1 python -m bsp_demo_sources.more minio
"""

from __future__ import annotations

import sys

from .common import dataset, log
from .targets import MORE


def main(argv: list[str]) -> int:
    if not argv or any(a not in (*MORE, "all") for a in argv):
        print(__doc__.strip())
        return 2
    targets = list(MORE) if "all" in argv else argv
    ds = dataset()
    for t in targets:
        log(f"{t}: {MORE[t](ds)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
