"""Graph-shaped mock under /connectors/m365/graph/v1.0/… (any bearer token is
accepted). It implements the subset of Microsoft Graph the live client uses,
so `GRAPH_BASE_URL=http://connectors:8092/connectors/m365/graph/v1.0` runs the
live code path against fixtures."""

from __future__ import annotations

import mimetypes
import re
from typing import Any
from urllib.parse import unquote

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from .._common import NotFound
from . import mock

router = APIRouter(tags=["m365 (Graph mock)"])
G = "/graph/v1.0"


def _nf(e: Exception) -> HTTPException:
    status = getattr(e, "status", None) or 404
    return HTTPException(status, detail={"error": {"code": "itemNotFound" if status == 404 else "invalidRequest",
                                                   "message": str(e)}})


def _page(request: Request, items: list, top: int, skip: int) -> dict:
    out: dict[str, Any] = {"value": items[skip: skip + top]}
    if skip + top < len(items):
        q = dict(request.query_params)
        q["$skip"] = str(skip + top)
        q["$top"] = str(top)
        out["@odata.nextLink"] = str(request.url.replace_query_params(**q))
    return out


# ── friendly helpers ─────────────────────────────────────────────────────
@router.get("/mailboxes", summary="Mock mailboxes (fixture folders)")
def mailboxes() -> dict:
    return {"value": [{"mailbox": m, "folders": mock.list_folders(m)} for m in mock.list_mailboxes()]}


@router.post("/mailboxes/{mailbox}/reset", summary="Forget read flags and moves for a mock mailbox")
def reset(mailbox: str) -> dict:
    mock.reset_mailbox(mailbox)
    return {"ok": True}


@router.get("/sites/{site}/files/{path:path}", summary="Download a mock SharePoint file by path (webUrl)")
def file_by_path(site: str, path: str) -> Response:
    try:
        item = mock.item_by_path(site, path)
        data = mock.download_file(site, item["id"])
    except NotFound as e:
        raise _nf(e)
    return Response(data, media_type=item.get("file", {}).get("mimeType", "application/octet-stream"),
                    headers={"Content-Disposition": f'inline; filename="{item["name"]}"'})


# ── Graph: mail ──────────────────────────────────────────────────────────
@router.get(G + "/users/{mailbox}/mailFolders")
def mail_folders(mailbox: str) -> dict:
    try:
        return {"value": mock.list_folders(mailbox)}
    except NotFound as e:
        raise _nf(e)


@router.post(G + "/users/{mailbox}/mailFolders", status_code=201)
def create_mail_folder(mailbox: str, body: dict = Body(...)) -> dict:
    try:
        return mock.create_folder(mailbox, body.get("displayName", ""))
    except NotFound as e:
        raise _nf(e)


@router.get(G + "/users/{mailbox}/mailFolders/{folder}/childFolders")
def child_folders(mailbox: str, folder: str) -> dict:
    return {"value": []}


_FILTER_SINCE = re.compile(r"receivedDateTime\s+ge\s+([0-9T:\-\.Z+]+)", re.I)
_FILTER_UNREAD = re.compile(r"isRead\s+eq\s+false", re.I)


@router.get(G + "/users/{mailbox}/mailFolders/{folder}/messages")
def folder_messages(request: Request, mailbox: str, folder: str,
                    top: int = Query(50, alias="$top"), skip: int = Query(0, alias="$skip"),
                    filter: str = Query("", alias="$filter")) -> dict:
    m = _FILTER_SINCE.search(filter or "")
    since = m.group(1) if m and not m.group(1).startswith("1900") else None
    try:
        rows = mock.list_messages(mailbox, folder, since=since, unread_only=bool(_FILTER_UNREAD.search(filter or "")),
                                  top=0)
    except NotFound as e:
        raise _nf(e)
    return _page(request, [_graph_msg(m) for m in rows], top, skip)


@router.get(G + "/users/{mailbox}/messages")
def all_messages(request: Request, mailbox: str, top: int = Query(50, alias="$top"),
                 skip: int = Query(0, alias="$skip")) -> dict:
    try:
        rows = [m for f in mock.list_folders(mailbox) for m in mock.list_messages(mailbox, f["id"], top=0)]
    except NotFound as e:
        raise _nf(e)
    rows.sort(key=lambda m: m["receivedDateTime"], reverse=True)
    return _page(request, [_graph_msg(m) for m in rows], top, skip)


def _graph_msg(m: dict) -> dict:
    m = dict(m)
    m["from"] = {"emailAddress": m["from"]}
    m.pop("attachments", None)
    return m


@router.get(G + "/users/{mailbox}/messages/{message_id}")
def message(mailbox: str, message_id: str) -> dict:
    try:
        return _graph_msg(mock.get_message(mailbox, message_id))
    except NotFound as e:
        raise _nf(e)


@router.patch(G + "/users/{mailbox}/messages/{message_id}")
def patch_message(mailbox: str, message_id: str, body: dict = Body(...)) -> dict:
    try:
        if "isRead" in body:
            mock.mark_read(mailbox, message_id, bool(body["isRead"]))
        return _graph_msg(mock.get_message(mailbox, message_id))
    except NotFound as e:
        raise _nf(e)


@router.post(G + "/users/{mailbox}/messages/{message_id}/move", status_code=201)
def move(mailbox: str, message_id: str, body: dict = Body(...)) -> dict:
    try:
        mock.move_message(mailbox, message_id, body.get("destinationId", ""))
        return _graph_msg(mock.get_message(mailbox, message_id))
    except NotFound as e:
        raise _nf(e)


@router.get(G + "/users/{mailbox}/messages/{message_id}/attachments")
def attachments(mailbox: str, message_id: str) -> dict:
    try:
        return {"value": [{"@odata.type": "#microsoft.graph.fileAttachment", **a}
                          for a in mock.get_message(mailbox, message_id)["attachments"]]}
    except NotFound as e:
        raise _nf(e)


@router.get(G + "/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}/$value")
def attachment_value(mailbox: str, message_id: str, attachment_id: str) -> Response:
    try:
        meta = mock.attachment_meta(mailbox, message_id, attachment_id)
        return Response(mock.get_attachment(mailbox, message_id, attachment_id), media_type=meta["contentType"])
    except NotFound as e:
        raise _nf(e)


@router.get(G + "/users/{mailbox}/messages/{message_id}/attachments/{attachment_id}")
def attachment(mailbox: str, message_id: str, attachment_id: str) -> dict:
    import base64
    try:
        meta = mock.attachment_meta(mailbox, message_id, attachment_id)
        data = mock.get_attachment(mailbox, message_id, attachment_id)
    except NotFound as e:
        raise _nf(e)
    return {"@odata.type": "#microsoft.graph.fileAttachment", **meta, "contentBytes": base64.b64encode(data).decode()}


# ── Graph: sites / drives / lists (one catch-all: Graph paths embed ':' segments) ──
def _split_site(rest: str) -> tuple[str, str]:
    """'contoso.sharepoint.com:/sites/Finance:/drive/…' → ('contoso…/sites/Finance', '/drive/…')."""
    if ":/" in rest:
        m = re.match(r"^([^/]+:/.*?):(/.*)?$", rest)
        if m:
            return m.group(1), m.group(2) or ""
    head, _, tail = rest.partition("/")
    return head, ("/" + tail) if tail else ""


@router.get(G + "/sites/{rest:path}", operation_id="graph_sites_get")
@router.put(G + "/sites/{rest:path}", operation_id="graph_sites_put")
async def sites(request: Request, rest: str) -> Any:
    rest = unquote(rest)
    site_ref, sub = _split_site(rest)
    site = mock.resolve_site(site_ref)
    top = int(request.query_params.get("$top", "200"))
    skip = int(request.query_params.get("$skip", "0"))
    try:
        if sub in ("", "/"):
            return {"id": site, "name": site, "displayName": site, "webUrl": mock.public_url(f"/connectors/m365/sites/{site}")}
        if sub == "/drive":
            return {"id": "b!" + mock._hash(site, n=20), "driveType": "documentLibrary", "name": "Documents"}
        if sub == "/drive/root/children":
            return _page(request, mock.list_children(site, ""), top, skip)
        m = re.match(r"^/drive/root:/(.*?):/children$", sub)
        if m:
            return _page(request, mock.list_children(site, m.group(1)), top, skip)
        m = re.match(r"^/drive/root:/(.*?):/content$", sub)
        if m and request.method == "PUT":
            path = m.group(1).strip("/")
            folder, _, name = path.rpartition("/")
            return JSONResponse(mock.upload_file(site, folder, name, await request.body()), status_code=201)
        m = re.match(r"^/drive/root:/(.*?):?$", sub)
        if m and request.method == "GET":
            return mock.item_by_path(site, m.group(1))
        m = re.match(r"^/drive/items/([^/]+)/children$", sub)
        if m:
            item = mock.get_item(site, m.group(1))
            path = (item["parentPath"] + "/" if item["parentPath"] else "") + item["name"]
            return _page(request, mock.list_children(site, path), top, skip)
        m = re.match(r"^/drive/items/([^/]+)/content$", sub)
        if m:
            item = mock.get_item(site, m.group(1))
            return Response(mock.download_file(site, m.group(1)),
                            media_type=mimetypes.guess_type(item["name"])[0] or "application/octet-stream")
        m = re.match(r"^/drive/items/([^/]+)$", sub)
        if m:
            return mock.get_item(site, m.group(1))
        if sub == "/lists":
            return {"value": mock.list_lists(site)}
        m = re.match(r"^/lists/([^/]+)/items$", sub)
        if m:
            return _page(request, mock.list_items(site, m.group(1)), top, skip)
    except NotFound as e:
        raise _nf(e)
    raise HTTPException(404, detail={"error": {"code": "notSupported", "message": f"mock does not implement {sub}"}})
