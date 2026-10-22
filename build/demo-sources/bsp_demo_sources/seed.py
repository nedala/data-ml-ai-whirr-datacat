"""Initial, idempotent load of all three demo systems (run by the demo-seed service).

    python -m bsp_demo_sources.seed [minio mssql oracle]
"""

from __future__ import annotations

import sys

from .common import dataset, log, wait_for
from .targets import SEEDERS


def main(argv: list[str]) -> int:
    targets = argv or list(SEEDERS)
    for t in targets:
        wait_for(t, SEEDERS[t][0])
    ds = dataset()
    failed = []
    for t in targets:
        try:
            SEEDERS[t][1](ds)
        except Exception as e:  # noqa: BLE001 — report every target, then fail
            log(f"{t}: seeding FAILED — {type(e).__name__}: {e}")
            failed.append(t)
    if failed:
        return 1
    log("all demo sources seeded")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
