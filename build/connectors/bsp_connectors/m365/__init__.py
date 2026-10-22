"""Microsoft 365 (Outlook mail + SharePoint/OneDrive files) via Microsoft Graph.

Live when M365_TENANT_ID / M365_CLIENT_ID / M365_CLIENT_SECRET are set
(override with M365_MODE=mock|live), otherwise the in-appliance mock backed
by fixtures/m365/. Both return the same Graph-shaped dicts (plan §4.1).

Additive extras beyond §4.1: ``list_files(..., include_folders=False)``,
``list_folders``, ``list_lists`` / ``list_items`` (SharePoint lists).
"""

from __future__ import annotations

from .._common import env, mode_of
from . import mock as _mock


def _impl():
    if mode_of("m365") == "live":
        from . import live
        return live
    return _mock


def mode() -> str:
    return mode_of("m365")


# ── Outlook ──────────────────────────────────────────────────────────────
def list_messages(mailbox: str, folder: str = "inbox", since: str | None = None, unread_only: bool = False,
                  top: int = 50) -> list[dict]:
    """Newest first. Each: id, subject, from{name,address}, receivedDateTime, bodyPreview,
    body{contentType,content}, hasAttachments, isRead, conversationId (+ Graph extras)."""
    return _impl().list_messages(mailbox, folder=folder, since=since, unread_only=unread_only, top=top)


def get_message(mailbox: str, message_id: str) -> dict:
    """The message plus attachments: [{id, name, contentType, size}]."""
    return _impl().get_message(mailbox, message_id)


def get_attachment(mailbox: str, message_id: str, attachment_id: str) -> bytes:
    return _impl().get_attachment(mailbox, message_id, attachment_id)


def mark_read(mailbox: str, message_id: str) -> None:
    _impl().mark_read(mailbox, message_id)


def move_message(mailbox: str, message_id: str, folder: str) -> None:
    """Move to a folder by display name ("Processed", "Needs review"); created if missing."""
    _impl().move_message(mailbox, message_id, folder)


def list_folders(mailbox: str) -> list[dict]:
    impl = _impl()
    if impl is _mock:
        return _mock.list_folders(mailbox)
    from . import live
    from .._http import paged
    return list(paged(live.base() + live._u(mailbox) + "/mailFolders", token=live._token, system="m365"))


# ── SharePoint / OneDrive ────────────────────────────────────────────────
def list_files(site: str | None, folder: str, recursive: bool = False, include_folders: bool = False) -> list[dict]:
    """Files in a document-library folder (site None → SHAREPOINT_SITE → root/default).
    Each: id, name, size, lastModifiedDateTime, webUrl, file{mimeType}, parentPath."""
    return _impl().list_files(site, folder, recursive=recursive, include_folders=include_folders)


def download_file(site: str | None, item_id: str) -> bytes:
    return _impl().download_file(site, item_id)


def upload_file(site: str | None, folder: str, name: str, data: bytes) -> dict:
    return _impl().upload_file(site, folder, name, data)


def list_lists(site: str | None = None) -> list[dict]:
    return _impl().list_lists(site)


def list_items(site: str | None, list_name: str) -> list[dict]:
    """SharePoint list rows: [{id, fields{…}}]."""
    return _impl().list_items(site, list_name)


def default_mailbox() -> str:
    return env("DISPUTES_MAILBOX", "billing-disputes@example.net")
