"""Power BI REST API v1.0 with the same Entra app as Microsoft Graph
(scope https://analysis.windows.net/powerbi/api/.default). The tenant must
allow service principals to use Power BI APIs (Admin portal → Developer
settings) and the app's security group must be a member of each workspace.
POWERBI_BASE_URL can point at the mock (…/connectors/powerbi/v1.0/myorg)."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from .._common import NotFound, env
from .._http import POWERBI_SCOPE, entra_token, paged, request


def base() -> str:
    return env("POWERBI_BASE_URL", "https://api.powerbi.com/v1.0/myorg").rstrip("/")


def _token(force: bool = False) -> str:
    if "/connectors/" in base():
        return "mock-token"
    return entra_token(POWERBI_SCOPE, force)


def _req(method: str, path: str, **kw: Any) -> Any:
    return request(method, base() + path, token=_token, system="powerbi", **kw)


def list_workspaces() -> list[dict]:
    return list(paged(base() + "/groups", token=_token, system="powerbi"))


def _ws_id(ws: str | None) -> str:
    ws = ws or env("POWERBI_WORKSPACE_ID")
    if not ws:
        raise NotFound("no workspace given and POWERBI_WORKSPACE_ID unset", status=400, system="powerbi")
    if len(ws) == 36 and ws.count("-") == 4:
        return ws
    for w in list_workspaces():
        if w["name"].lower() == ws.lower():
            return w["id"]
    raise NotFound(f"workspace {ws!r} not found", status=404, system="powerbi")


def list_reports(ws: str | None) -> list[dict]:
    return list(paged(base() + f"/groups/{_ws_id(ws)}/reports", token=_token, system="powerbi"))


def list_datasets(ws: str | None) -> list[dict]:
    return list(paged(base() + f"/groups/{_ws_id(ws)}/datasets", token=_token, system="powerbi"))


def _ds_id(wsid: str, ds: str) -> str:
    if len(ds) == 36 and ds.count("-") == 4:
        return ds
    for d in list_datasets(wsid):
        if d["name"].lower() == ds.lower():
            return d["id"]
    raise NotFound(f"dataset {ds!r} not found", status=404, system="powerbi")


def refresh_dataset(ws: str | None, ds: str) -> dict:
    wsid = _ws_id(ws)
    dsid = _ds_id(wsid, ds)
    r = _req("POST", f"/groups/{wsid}/datasets/{quote(dsid)}/refreshes", json={"notifyOption": "NoNotification"},
             raw=True)
    return {"status": "accepted", "requestId": r.headers.get("RequestId") or r.headers.get("x-ms-request-id"),
            "workspace": wsid, "dataset": dsid, "mode": "live"}


def refresh_history(ws: str | None, ds: str, top: int = 10) -> list[dict]:
    wsid = _ws_id(ws)
    return (_req("GET", f"/groups/{wsid}/datasets/{_ds_id(wsid, ds)}/refreshes", params={"$top": top}) or {}).get(
        "value", [])


def execute_dax(ws: str | None, ds: str, dax: str) -> list[dict]:
    wsid = _ws_id(ws)
    res = _req("POST", f"/groups/{wsid}/datasets/{_ds_id(wsid, ds)}/executeQueries",
               json={"queries": [{"query": dax}], "serializerSettings": {"includeNulls": True}}) or {}
    results = res.get("results") or [{}]
    if results[0].get("error"):
        from .._common import ConnectorError
        raise ConnectorError(f"DAX error: {results[0]['error']}", status=400, system="powerbi")
    tables = results[0].get("tables") or [{}]
    return tables[0].get("rows", [])
