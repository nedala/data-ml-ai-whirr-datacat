"""Demo source systems for the Integration Canvas examples (capability `demo-sources`).

Seeds MinIO (bucket bsp-demo), SQL Server (database care) and Oracle (schema
billing) with DataPlatform sample data derived from `bsp_seed.build(customers=20000,
seed=7)` — the same keys as the warehouse (BAN, CLLI, `<ban>-<yymm>` invoice ids).

    python -m bsp_demo_sources.seed                 # idempotent initial load (run by the demo-seed service)
    python -m bsp_demo_sources.more minio|mssql|oracle   # append one more batch (incremental demos)
"""
