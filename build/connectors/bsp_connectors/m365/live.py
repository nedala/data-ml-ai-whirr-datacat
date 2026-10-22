"""Microsoft Graph v1.0, app-only (client credentials). Needs these Graph
application permissions (least privilege — see docs/data-pro/m365-app-registration.md):
Mail.ReadWrite (restricted by an ApplicationAccessPolicy to the disputes
mailbox) and Sites.Selected (granted per site). GRAPH_BASE_URL can point at
the in-appliance mock (…/connectors/m365/graph/v1.0) for parity tests."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from .._common import NotFound, env
from .._http import GRAPH_SCOPE, entra_token, paged, request
from .mock import WELL_KNOWN, folder_key

SELECT = ("id,subject,from,toRecipients,ccRecipients,receivedDateTime,sentDateTime,bodyPreview,body,"
          "hasAttachments,isRead,conversationId,internetMessageId,importance,parentFolderId,webLink")
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 10 * 320 * 1024  # multiple of 320 KiB, as Graph requires


def base() -> str:
    return env("GRAPH_BASE_URL", "https://graph.microsoft.com/v1.0").rstrip("/")


def _token(force: bool = False) -> str:
    if "/connectors/" in base():  # the in-appliance mock accepts any bearer
        return "mock-token"
    return entra_token(GRAPH_SCOPE, force)


def _req(method: str, path: str, **kw: Any) -> Any:
    return request(method, base() + path, token=_token, system="m365", **kw)


def _u(mailbox: str) -> str:
    return f"/users/{quote(mailbox)}"


# ── mail ─────────────────────────────────────────────────────────────────
_folder_ids: dict[tuple[str, str], str] = {}


def folder_id(mailbox: str, folder: str, create: bool = False) -> str:
    k = folder_key(folder)
    if k in WELL_KNOWN:
        return k
    if (mailbox, k) in _folder_ids:
        return _folder_ids[(mailbox, k)]
    for path in ("/mailFolders", "/mailFolders/inbox/childFolders"):
        for f in paged(base() + _u(mailbox) + path, token=_token, params={"$top": "100"}, system="m365"):
            if folder_key(f.get("displayName", "")) == k:
                _folder_ids[(mailbox, k)] = f["id"]
                return f["id"]
    if not create:
        raise NotFound(f"mail folder {folder!r} not found in {mailbox}", status=404, system="m365")
    f = _req("POST", _u(mailbox) + "/mailFolders", json={"displayName": folder})
    _folder_ids[(mailbox, k)] = f["id"]
    return f["id"]


def _shape(m: dict) -> dict:
    frm = (m.get("from") or {}).get("emailAddress") or {}
    m["from"] = {"name": frm.get("name", ""), "address": frm.get("address", "")}
    return m


def list_messages(mailbox: str, folder: str = "inbox", since: str | None = None, unread_only: bool = False,
                  top: int = 50) -> list[dict]:
    filters = []
    if since:
        s = since if "T" in since else since + "T00:00:00Z"
        filters.append(f"receivedDateTime ge {s if s.endswith('Z') or '+' in s else s + 'Z'}")
    if unread_only:
        if not filters:  # Graph wants the $orderby property to lead the $filter
            filters.append("receivedDateTime ge 1900-01-01T00:00:00Z")
        filters.append("isRead eq false")
    params = {"$top": str(min(top or 50, 100)), "$orderby": "receivedDateTime desc", "$select": SELECT}
    if filters:
        params["$filter"] = " and ".join(filters)
    url = base() + _u(mailbox) + f"/mailFolders/{quote(folder_id(mailbox, folder))}/messages"
    return [_shape(m) for m in paged(url, token=_token, params=params, limit=top or None, system="m365")]


def get_message(mailbox: str, message_id: str) -> dict:
    m = _shape(_req("GET", _u(mailbox) + f"/messages/{quote(message_id)}", params={"$select": SELECT}))
    atts = paged(base() + _u(mailbox) + f"/messages/{quote(message_id)}/attachments", token=_token,
                 params={"$select": "id,name,contentType,size,isInline"}, system="m365")
    m["attachments"] = [{k: a.get(k) for k in ("id", "name", "contentType", "size", "isInline")} for a in atts]
    return m


def get_attachment(mailbox: str, message_id: str, attachment_id: str) -> bytes:
    r = _req("GET", _u(mailbox) + f"/messages/{quote(message_id)}/attachments/{quote(attachment_id)}/$value",
             raw=True)
    return r.content


def mark_read(mailbox: str, message_id: str) -> None:
    _req("PATCH", _u(mailbox) + f"/messages/{quote(message_id)}", json={"isRead": True})


def move_message(mailbox: str, message_id: str, folder: str) -> None:
    dest = folder_id(mailbox, folder, create=True)
    _req("POST", _u(mailbox) + f"/messages/{quote(message_id)}/move", json={"destinationId": dest})


# ── SharePoint / drives ──────────────────────────────────────────────────
_site_ids: dict[str, str] = {}


def site_id(site: str | None) -> str:
    ref = (site or env("SHAREPOINT_SITE") or "root").strip()
    if ref in _site_ids:
        return _site_ids[ref]
    if "://" in ref:  # https://contoso.sharepoint.com/sites/Finance → contoso.sharepoint.com:/sites/Finance:
        u = urlparse(ref)
        path = f"{u.hostname}:{u.path.rstrip('/')}:" if u.path.strip("/") else u.hostname
    elif ":/" in ref and not ref.endswith(":"):
        path = ref + ":"
    else:
        path = ref
    sid = _req("GET", f"/sites/{path}", params={"$select": "id,displayName,webUrl"})["id"]
    _site_ids[ref] = sid
    return sid


def _folder_path(folder: str | None) -> str:
    f = (folder or "").replace("\\", "/").strip().strip("/")
    if f.lower().startswith("shared documents/"):
        f = f[len("shared documents/"):]
    elif f.lower() in ("shared documents", "root"):
        f = ""
    return f


def _norm_item(d: dict) -> dict:
    p = (d.get("parentReference") or {}).get("path", "") or ""
    d["parentPath"] = p.split("root:", 1)[1].strip("/") if "root:" in p else ""
    return d


def _children_url(sid: str, folder: str) -> str:
    f = _folder_path(folder)
    return base() + (f"/sites/{sid}/drive/root:/{quote(f)}:/children" if f else f"/sites/{sid}/drive/root/children")


def list_files(site: str | None, folder: str = "", recursive: bool = False,
               include_folders: bool = False) -> list[dict]:
    sid = site_id(site)
    sel = {"$select": "id,name,size,lastModifiedDateTime,createdDateTime,webUrl,file,folder,parentReference",
           "$top": "200"}
    out, queue = [], [_children_url(sid, folder)]
    while queue:
        for d in paged(queue.pop(0), token=_token, params=dict(sel), system="m365"):
            d = _norm_item(d)
            if "folder" in d:
                if include_folders:
                    out.append(d)
                if recursive:
                    queue.append(base() + f"/sites/{sid}/drive/items/{d['id']}/children")
            else:
                out.append(d)
    return out


def download_file(site: str | None, item_id: str) -> bytes:
    return _req("GET", f"/sites/{site_id(site)}/drive/items/{quote(item_id)}/content", raw=True).content


def upload_file(site: str | None, folder: str, name: str, data: bytes) -> dict:
    sid = site_id(site)
    f = _folder_path(folder)
    target = quote(f"{f}/{name}" if f else name)
    if len(data) <= SIMPLE_UPLOAD_MAX:
        return _norm_item(_req("PUT", f"/sites/{sid}/drive/root:/{target}:/content", content=data,
                               headers={"Content-Type": "application/octet-stream"}))
    sess = _req("POST", f"/sites/{sid}/drive/root:/{target}:/createUploadSession",
                json={"item": {"@microsoft.graph.conflictBehavior": "replace"}})
    url, total, res = sess["uploadUrl"], len(data), None
    for start in range(0, total, CHUNK):
        chunk = data[start:start + CHUNK]
        res = request("PUT", url, content=chunk, system="m365",  # pre-authenticated URL: no bearer
                      headers={"Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{total}"})
    return _norm_item(res or {})


def list_lists(site: str | None) -> list[dict]:
    return list(paged(base() + f"/sites/{site_id(site)}/lists", token=_token,
                      params={"$select": "id,name,displayName,webUrl"}, system="m365"))


def list_items(site: str | None, list_name: str) -> list[dict]:
    sid = site_id(site)
    lid = next((x["id"] for x in list_lists(site) if list_name in (x["id"], x.get("name"), x.get("displayName"))),
               None)
    if lid is None:
        raise NotFound(f"list {list_name!r} not found", status=404, system="m365")
    return [{"id": r["id"], "fields": r.get("fields", {})}
            for r in paged(base() + f"/sites/{sid}/lists/{lid}/items", token=_token,
                           params={"$expand": "fields", "$top": "200"}, system="m365")]
