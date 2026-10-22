"""Power BI — workspaces, reports, datasets, refresh and DAX queries.

Live with the M365 Entra app (POWERBI_MODE=mock|live overrides); mock serves
"Revenue & Churn", "Network Ops" and "Customer Care" over the local warehouse.
Workspaces and datasets may be given by id or by name.

    list_workspaces() -> [{id, name, …}]
    list_reports(ws) / list_datasets(ws) -> [{id, name, …}]
    refresh_dataset(ws, ds) -> {status: "accepted", requestId, …}
    execute_dax(ws, ds, dax) -> [ {"Table[col]" | "[Measure]": value} ]
"""

from __future__ import annotations

from .._common import mode_of
from . import mock as _mock


def _impl():
    if mode_of("powerbi") == "live":
        from . import live
        return live
    return _mock


def mode() -> str:
    return mode_of("powerbi")


def list_workspaces() -> list[dict]:
    return _impl().list_workspaces()


def list_reports(ws: str) -> list[dict]:
    return _impl().list_reports(ws)


def list_datasets(ws: str) -> list[dict]:
    return _impl().list_datasets(ws)


def refresh_dataset(ws: str, ds: str) -> dict:
    return _impl().refresh_dataset(ws, ds)


def refresh_history(ws: str, ds: str) -> list[dict]:
    return _impl().refresh_history(ws, ds)


def execute_dax(ws: str, ds: str, dax: str) -> list[dict]:
    return _impl().execute_dax(ws, ds, dax)
