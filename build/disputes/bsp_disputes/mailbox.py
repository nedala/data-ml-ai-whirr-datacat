"""Mailbox access: the connectors SDK (`bsp_connectors.m365`, mock or live
Graph) — or, when that package is not importable in this container, a
local reader over the same .eml fixtures (DISPUTES_EML_DIR) with read/moved
state kept in ${DISPUTES_DATA_DIR}/mock_mailbox.json."""

from __future__ import annotations

import email
import json
import logging
import os
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from .config import get

log = logging.getLogger("bsp_disputes.mailbox")


class EmlFolder:
    name = "eml-folder"

    def __init__(self, root: Path):
        self.root = root
        self.state_file = get().data_dir / "mock_mailbox.json"

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"read": [], "moved": {}}

    def _save(self, st: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(st))

    def _parse(self, p: Path):
        return email.message_from_bytes(p.read_bytes(), policy=policy.default)

    def _dir(self, mailbox: str, folder: str) -> Path:
        return self.root / mailbox / folder.lower()

    def _to_dict(self, mid: str, m, st: dict, full: bool = False) -> dict:
        body = m.get_body(("plain", "html"))
        content = body.get_content() if body else ""
        ctype = "text" if body is None or body.get_content_type() == "text/plain" else "html"
        atts = [a for a in m.iter_attachments()]
        d = {"id": mid, "subject": m["Subject"] or "", "internetMessageId": m["Message-ID"],
             "from": {"name": m["From"].addresses[0].display_name if m["From"] else "",
                      "address": m["From"].addresses[0].addr_spec if m["From"] else ""},
             "receivedDateTime": parsedate_to_datetime(m["Date"]).isoformat() if m["Date"] else None,
             "bodyPreview": content[:255], "body": {"contentType": ctype, "content": content},
             "hasAttachments": bool(atts), "isRead": mid in st["read"], "conversationId": mid,
             "internetMessageHeaders": [{"name": k, "value": str(v)} for k, v in m.items()]}
        if full:
            d["attachments"] = [{"id": str(i), "name": a.get_filename() or f"att{i}", "contentType": a.get_content_type(),
                                 "size": len(a.get_payload(decode=True) or b"")} for i, a in enumerate(atts)]
        return d

    def list_messages(self, mailbox: str, folder: str = "inbox", since: str | None = None,
                      unread_only: bool = False, top: int = 50) -> list[dict]:
        st = self._state()
        out = []
        for p in sorted(self._dir(mailbox, "inbox").glob("*.eml")):
            mid = p.stem
            where = st["moved"].get(mid, "inbox")
            if where.lower() != folder.lower() or (unread_only and mid in st["read"]):
                continue
            out.append(self._to_dict(mid, self._parse(p), st))
            if len(out) >= top:
                break
        return out

    def get_message(self, mailbox: str, message_id: str) -> dict:
        return self._to_dict(message_id, self._parse(self._dir(mailbox, "inbox") / f"{message_id}.eml"),
                             self._state(), full=True)

    def get_attachment(self, mailbox: str, message_id: str, attachment_id: str) -> bytes:
        m = self._parse(self._dir(mailbox, "inbox") / f"{message_id}.eml")
        return list(m.iter_attachments())[int(attachment_id)].get_payload(decode=True)

    def mark_read(self, mailbox: str, message_id: str) -> None:
        st = self._state()
        if message_id not in st["read"]:
            st["read"].append(message_id)
        self._save(st)

    def move_message(self, mailbox: str, message_id: str, folder: str) -> None:
        st = self._state()
        st["moved"][message_id] = folder
        self._save(st)

    def reset(self) -> None:
        self._save({"read": [], "moved": {}})


def client() -> Any:
    """`bsp_connectors.m365` when importable (unless DISPUTES_EML_DIR forces the
    local reader), else the local .eml reader."""
    s = get()
    if not s.eml_dir:
        try:
            from bsp_connectors import m365  # noqa: F401
            _ = m365.list_messages
            m365.name = "m365"  # type: ignore[attr-defined]
            return m365
        except Exception as e:  # noqa: BLE001
            log.info("bsp_connectors.m365 unavailable (%s); using local .eml reader", e)
    root = Path(s.eml_dir or os.environ.get("CONNECTORS_FIXTURES_DIR", "/opt/connectors/fixtures") + "/m365/mailboxes")
    if root.name != "mailboxes" and (root / "m365" / "mailboxes").exists():
        root = root / "m365" / "mailboxes"
    return EmlFolder(root)
