"""Power BI REST-shaped mock under /connectors/powerbi/v1.0/myorg/… (any bearer
accepted), so POWERBI_BASE_URL=http://connectors:8092/connectors/powerbi/v1.0/myorg
exercises the live client against fixtures."""

from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import Response

from .._common import ConnectorError, NotFound
from . import mock

router = APIRouter(tags=["powerbi (mock)"])
P = "/v1.0/myorg"


def _err(e: ConnectorError) -> HTTPException:
    return HTTPException(e.status or 400, detail={"error": {"code": "ItemNotFound" if isinstance(e, NotFound)
                                                            else "BadRequest", "message": str(e)}})


@router.get(P + "/groups")
def groups() -> dict:
    return {"@odata.context": "groups", "value": mock.list_workspaces()}


@router.get(P + "/groups/{ws}/reports")
def reports(ws: str) -> dict:
    try:
        return {"value": mock.list_reports(ws)}
    except ConnectorError as e:
        raise _err(e)


@router.get(P + "/groups/{ws}/datasets")
def datasets(ws: str) -> dict:
    try:
        return {"value": mock.list_datasets(ws)}
    except ConnectorError as e:
        raise _err(e)


@router.post(P + "/groups/{ws}/datasets/{ds}/refreshes", status_code=202)
def refresh(ws: str, ds: str, body: dict = Body(default={})) -> Response:
    try:
        res = mock.refresh_dataset(ws, ds)
    except ConnectorError as e:
        raise _err(e)
    return Response(status_code=202, headers={"RequestId": res["requestId"]})


@router.get(P + "/groups/{ws}/datasets/{ds}/refreshes")
def refreshes(ws: str, ds: str) -> dict:
    try:
        return {"value": mock.refresh_history(ws, ds)}
    except ConnectorError as e:
        raise _err(e)


@router.post(P + "/groups/{ws}/datasets/{ds}/executeQueries")
def execute_queries(ws: str, ds: str, body: dict = Body(..., examples=[{
        "queries": [{"query": 'EVALUATE SUMMARIZECOLUMNS(\'Revenue\'[state], "Revenue", [Total Revenue])'}]}])) -> dict:
    results = []
    for q in body.get("queries", [])[:1]:  # Power BI allows one query per call
        try:
            rows = mock.execute_dax(ws, ds, q.get("query", ""))
            results.append({"tables": [{"rows": rows}]})
        except NotFound as e:
            raise _err(e)
        except ConnectorError as e:
            results.append({"error": {"code": "DAXQueryFailure", "message": str(e)}})
    return {"results": results}
