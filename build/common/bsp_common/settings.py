"""Connection facts every container shares (see plan/architecture.md §3).
Secrets come from the process environment (injected by Studio), never files."""

from __future__ import annotations

import os
from functools import lru_cache


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


class Settings:
    @property
    def pg_dsn(self) -> str:
        return (f"host={env('PG_HOST', 'postgres')} port={env('PG_PORT', '5432')} dbname={env('PG_DB', 'bsp_edw')} "
                f"user={env('PG_USER', 'analyst')} password={env('PG_PASS')}")

    @property
    def pg_url(self) -> str:
        return (f"postgresql+psycopg2://{env('PG_USER', 'analyst')}:{env('PG_PASS')}@{env('PG_HOST', 'postgres')}:"
                f"{env('PG_PORT', '5432')}/{env('PG_DB', 'bsp_edw')}")

    gcp_project = property(lambda self: env("GCP_PROJECT_ID", "to-be-named-soon"))
    gcp_region = property(lambda self: env("GCP_REGION", "us-central1"))
    bq_location = property(lambda self: env("BQ_LOCATION", "US"))
    lake_dir = property(lambda self: env("LAKE_DIR", "/data/lake"))
    connectors_url = property(lambda self: env("CONNECTORS_URL", "http://connectors:8092/connectors"))
    llm_gateway_url = property(lambda self: env("LLM_GATEWAY_URL", "http://harness:8096/ai/v1"))
    trino_host = property(lambda self: env("TRINO_HOST", "trino"))
    trino_port = property(lambda self: int(env("TRINO_PORT", "8080")))
    mlflow_uri = property(lambda self: env("MLFLOW_TRACKING_URI", "http://mlflow:5000/mlflow"))


@lru_cache(maxsize=1)
def get() -> Settings:
    return Settings()
