"""Warehouse (Postgres `bsp_edw`) helpers."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from . import settings


@contextmanager
def connect() -> Iterator[Any]:
    import psycopg2

    conn = psycopg2.connect(settings.get().pg_dsn)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def engine():
    from sqlalchemy import create_engine

    return create_engine(settings.get().pg_url, pool_pre_ping=True)


def query_df(sql: str, params: Any = None):
    import pandas as pd

    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)
