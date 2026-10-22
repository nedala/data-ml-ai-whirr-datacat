"""python -m bsp_seed [--target postgres|parquet|bigquery|summary] [--customers N] [--months M]"""

import argparse
import json
import os
import time

from .generate import build


def main() -> None:
    ap = argparse.ArgumentParser(prog="bsp_seed")
    ap.add_argument("--target", action="append", choices=["postgres", "parquet", "bigquery", "summary"])
    ap.add_argument("--customers", type=int, default=int(os.environ.get("SEED_CUSTOMERS", "20000")))
    ap.add_argument("--months", type=int, default=24)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", help="parquet directory (default $LAKE_DIR/samples/bsp)")
    a = ap.parse_args()
    t0 = time.time()
    ds = build(customers=a.customers, months=a.months, seed=a.seed)
    print(f"generated in {time.time() - t0:.1f}s: {json.dumps(ds.summary())}")
    for target in a.target or ["summary"]:
        if target == "postgres":
            from .load import to_postgres
            print("postgres raw.*:", to_postgres(ds))
        elif target == "parquet":
            from .load import to_parquet
            print("parquet:", to_parquet(ds, a.out))
        elif target == "bigquery":
            from .load import to_bigquery
            print("bigquery raw.*:", to_bigquery(ds))


if __name__ == "__main__":
    main()
