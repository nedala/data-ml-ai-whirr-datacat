"""BigQuery (the enterprise EDW) — thin wrappers over google-cloud-bigquery.

Live when GCP_SA_JSON (or GOOGLE_APPLICATION_CREDENTIALS) is configured
(BIGQUERY_MODE=mock|live overrides); credentials come from
bsp_common.gcp.credentials() and stay in memory. Mock: DuckDB over the local
lake parquet / warehouse presented as project GCP_PROJECT_ID.

    list_datasets() -> [{dataset_id, project, full_id, location}]
    list_tables(dataset) -> [{table_id, dataset_id, project, type, full_id}]
    table_schema(dataset, table) -> [{name, type, mode, description}]
    query(sql, max_rows=1000) -> [ {column: value} ]      (JSON-safe values)
"""

from __future__ import annotations

import contextlib
import contextvars
import datetime as dt
import decimal
from typing import Any

from .._common import ConnectorError, NotFound, env, mode_of
from . import mock as _mock

_client = None
_clients: dict = {}       # per named cloud identity (Studio → Cloud identities)
_identity: contextvars.ContextVar = contextvars.ContextVar("bq_identity", default="")
# principal switch: (identity, data project, execution project) — credentials come
# from Studio for the acting user (bsp_common.gcp.acting); access list checked there
_principal: contextvars.ContextVar = contextvars.ContextVar("bq_principal", default=None)


@contextlib.contextmanager
def using_principal(identity: str | None, project: str | None = None, exec_project: str | None = None):
    """Run BigQuery calls as a named Studio cloud identity for the user set by
    bsp_common.gcp.acting(...); '' = the assigned identity (unchanged)."""
    tok = _principal.set((identity, project or "", exec_project or "") if identity else None)
    try:
        yield
    finally:
        _principal.reset(tok)


@contextlib.contextmanager
def using_identity(name: str | None):
    """Run BigQuery calls as the named cloud identity (GCP_IDENTITIES); '' = the assigned one."""
    tok = _identity.set(name or "")
    try:
        yield
    finally:
        _identity.reset(tok)


def mode() -> str:
    return mode_of("bigquery")


def _live() -> bool:
    return mode_of("bigquery") == "live"


def client():
    """The google.cloud.bigquery.Client (live mode only)."""
    global _client
    if pr := _principal.get():
        from google.cloud import bigquery as bq

        from bsp_common import gcp
        creds = gcp.credentials("bigquery_read", identity=pr[0])     # new Client per call: no cross-user reuse
        execp = pr[2] or pr[1] or getattr(creds, "bsp_default_project", "") or exec_project()
        return bq.Client(project=execp, credentials=creds, location=env("BQ_LOCATION", "US"))
    if name := _identity.get():
        if name not in _clients:
            from google.cloud import bigquery as bq

            from bsp_common import gcp
            i = gcp.identity_info(name)
            _clients[name] = bq.Client(project=i.get("project_id") or i.get("quota_project_id") or project(),
                                       credentials=gcp.from_info(i), location=env("BQ_LOCATION", "US"))
        return _clients[name]
    if _client is None:
        from google.cloud import bigquery as bq

        from bsp_common import gcp
        creds = gcp.credentials("bigquery")
        if creds is None:
            raise ConnectorError("BigQuery credentials not configured (Studio → Cloud identities)", system="bigquery")
        # jobs run (and are billed) in the EXECUTION project; data lives in project() / data_projects()
        _client = bq.Client(project=exec_project(), credentials=creds, location=env("BQ_LOCATION", "US"))
    return _client


def project() -> str:
    """The (first) DATA project — Studio → Cloud identities → BigQuery · read."""
    from bsp_common import gcp
    if pr := _principal.get():
        own = pr[1] or gcp.identity_meta(pr[0]).get("project", "")
        if own:
            return own
    return gcp.project("bigquery", env("GCP_PROJECT_ID", "to-be-named-soon"))


def exec_project() -> str:
    from bsp_common import gcp
    return gcp.exec_project("bigquery", env("GCP_PROJECT_ID", "to-be-named-soon"))


def data_projects() -> list[str]:
    from bsp_common import gcp
    return gcp.data_projects("bigquery", env("GCP_PROJECT_ID", "to-be-named-soon"))


def _jsonable(v: Any) -> Any:
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, bytes):
        return v.hex()
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def list_datasets() -> list[dict]:
    if not _live():
        return _mock.list_datasets()
    c = client()
    projects = [project()] if (_identity.get() or _principal.get()) else data_projects()
    first = projects[0] if projects else None
    # datasets of extra data projects are addressed as "<project>.<dataset>"
    return [{"dataset_id": d.dataset_id if d.project == first else f"{d.project}.{d.dataset_id}", "project": d.project,
             "full_id": f"{d.project}:{d.dataset_id}", "location": getattr(d, "location", None) or env("BQ_LOCATION", "US")}
            for p in projects for d in c.list_datasets(project=p)]


def list_tables(dataset: str) -> list[dict]:
    if not _live():
        return _mock.list_tables(dataset)
    from google.api_core.exceptions import NotFound as GNotFound
    try:
        return [{"table_id": t.table_id, "dataset_id": t.dataset_id, "project": t.project, "type": t.table_type,
                 "full_id": f"{t.project}.{t.dataset_id}.{t.table_id}"}
                for t in client().list_tables(dataset if "." in dataset else f"{project()}.{dataset}")]
    except GNotFound as e:
        raise NotFound(str(e), status=404, system="bigquery") from e


def table_schema(dataset: str, table: str) -> list[dict]:
    if not _live():
        return _mock.table_schema(dataset, table)
    from google.api_core.exceptions import NotFound as GNotFound
    try:
        t = client().get_table(f"{project()}.{dataset}.{table}" if dataset.count(".") == 0 else f"{dataset}.{table}")
    except GNotFound as e:
        raise NotFound(str(e), status=404, system="bigquery") from e
    return [{"name": f.name, "type": f.field_type, "mode": f.mode, "description": f.description or ""}
            for f in t.schema]


def query(sql: str, max_rows: int = 1000) -> list[dict]:
    if not _live():
        return _mock.query(sql, max_rows=max_rows)
    from google.api_core.exceptions import GoogleAPIError
    try:
        rows = client().query(sql).result(max_results=max_rows or None)
    except GoogleAPIError as e:
        raise ConnectorError(f"BigQuery query failed: {e}", status=400, system="bigquery") from e
    return [{k: _jsonable(v) for k, v in dict(r).items()} for r in rows]


def query_df(sql: str, max_rows: int | None = None):
    """Pandas convenience (mock and live)."""
    import pandas as pd
    return pd.DataFrame(query(sql, max_rows=max_rows or 0))


def load_dataframe(df, dataset: str, table: str, write_disposition: str = "WRITE_APPEND") -> dict:
    """Write a DataFrame to BigQuery (live) — in mock mode lands in Postgres schema `<dataset>` instead."""
    if not _live():
        from bsp_common import db
        if_exists = "replace" if write_disposition == "WRITE_TRUNCATE" else "append"
        with db.engine().begin() as conn:
            conn.exec_driver_sql(f'create schema if not exists "{dataset}"')
            df.to_sql(table, conn, schema=dataset, if_exists=if_exists, index=False, method="multi", chunksize=2000)
        _mock.reset()
        return {"rows": len(df), "table": f"{project()}.{dataset}.{table}", "mode": "mock"}
    from google.cloud import bigquery as bq
    c = client()
    c.create_dataset(f"{project()}.{dataset}", exists_ok=True)
    job = c.load_table_from_dataframe(df, f"{project()}.{dataset}.{table}",
                                      job_config=bq.LoadJobConfig(write_disposition=write_disposition))
    job.result()
    return {"rows": len(df), "table": f"{project()}.{dataset}.{table}", "mode": "live"}


__all__ = ["list_datasets", "list_tables", "table_schema", "query", "query_df", "load_dataframe", "mode",
           "NotFound", "ConnectorError"]
