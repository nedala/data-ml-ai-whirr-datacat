"""Write a `Dataset` to the local warehouse (Postgres schema `raw`), to
parquet under the lake, or to BigQuery (dataset `raw` in GCP_PROJECT_ID)."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd

from .generate import Dataset

def _pg_type(s: pd.Series) -> str:
    # pandas 2 and 3 differ in datetime resolution (ns/us) and string dtypes
    if pd.api.types.is_datetime64_any_dtype(s):
        return "timestamp"
    if pd.api.types.is_bool_dtype(s):
        return "boolean"
    if pd.api.types.is_integer_dtype(s):
        return "bigint"
    if pd.api.types.is_float_dtype(s):
        return "double precision"
    sample = s.dropna().head(1)
    if len(sample):
        v = sample.iat[0]
        if isinstance(v, pd.Timestamp):
            return "timestamp"
        if hasattr(v, "isoformat") and not isinstance(v, str):
            return "date"
    return "text"


def to_postgres(ds: Dataset, schema: str = "raw", dsn: str | None = None) -> dict[str, int]:
    import psycopg2

    dsn = dsn or (f"host={os.environ.get('PG_HOST', 'postgres')} port={os.environ.get('PG_PORT', '5432')} "
                  f"dbname={os.environ.get('PG_DB', 'bsp_edw')} user={os.environ.get('PG_USER', 'analyst')} "
                  f"password={os.environ.get('PG_PASS', '')}")
    out = {}
    with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(f'create schema if not exists "{schema}"')
        for name, df in ds.tables.items():
            cols = ", ".join(f'"{c}" {_pg_type(df[c])}' for c in df.columns)
            cur.execute(f'drop table if exists "{schema}"."{name}" cascade')
            cur.execute(f'create table "{schema}"."{name}" ({cols})')
            buf = io.StringIO()
            df.to_csv(buf, index=False, header=False, na_rep="\\N")
            buf.seek(0)
            cur.copy_expert(f'copy "{schema}"."{name}" from stdin with (format csv, null \'\\N\')', buf)
            out[name] = len(df)
    return out


def to_parquet(ds: Dataset, root: str | Path | None = None) -> Path:
    root = Path(root or Path(os.environ.get("LAKE_DIR", "/data/lake")) / "samples" / "bsp")
    root.mkdir(parents=True, exist_ok=True)
    for name, df in ds.tables.items():
        df.to_parquet(root / f"{name}.parquet", index=False)
    return root


def to_bigquery(ds: Dataset, dataset: str = "raw", project: str | None = None) -> dict[str, int]:
    """Needs google-cloud-bigquery and credentials (BigQuery · write identity, GCP_SA_JSON or ADC)."""
    from google.cloud import bigquery

    from bsp_common.gcp import credentials

    project = project or os.environ.get("GCP_PROJECT_ID", "to-be-named-soon")
    creds = credentials("bigquery_write")   # Studio → Cloud identities: BigQuery · write
    client = bigquery.Client(project=project, credentials=creds, location=os.environ.get("BQ_LOCATION", "US"))
    client.create_dataset(f"{project}.{dataset}", exists_ok=True)
    out = {}
    for name, df in ds.tables.items():
        job = client.load_table_from_dataframe(df, f"{project}.{dataset}.{name}",
                                               job_config=bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE"))
        job.result()
        out[name] = len(df)
    return out
