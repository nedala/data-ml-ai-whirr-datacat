"""Microsoft 365 mock store — Outlook mailboxes from .eml fixtures and SharePoint
drives from plain folders, with mutable state (read flags, moves, uploads)
under ${CONNECTORS_STATE_DIR}/m365/. Shapes match Microsoft Graph v1.0.

Fixtures (read-only seed, picked up on every call — drop new files in any time):
    fixtures/m365/mailboxes/<mailbox>/<folder>/*.eml
    fixtures/m365/sites/<site-or-default>/<folder path>/<file>
    fixtures/m365/lists/<site-or-default>/<List name>.json   ([{field: value}, …])
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
import shutil
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from .._common import NotFound, fixtures_dir, public_url, read_json, state_dir, write_json

for _ext, _mt in {".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                 ".parquet": "application/vnd.apache.parquet", ".json": "application/json", ".csv": "text/csv",
                 ".txt": "text/plain", ".md": "text/markdown", ".pdf": "application/pdf", ".eml": "message/rfc822",
                 ".vtt": "text/vtt", ".wav": "audio/wav", ".mp3": "audio/mpeg"}.items():
    mimetypes.add_type(_mt, _ext)  # slim images ship no /etc/mime.types

WELL_KNOWN = {"inbox": "Inbox", "archive": "Archive", "deleteditems": "Deleted Items", "drafts": "Drafts",
              "sentitems": "Sent Items", "junkemail": "Junk Email"}


def _hash(*parts: str, n: int = 32) -> str:
    return hashlib.sha1("\x1f".join(parts).encode()).hexdigest()[:n]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_since(since: str | None) -> datetime | None:
    if not since:
        return None
    s = since.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s if "T" in s or " " in s else s + "T00:00:00")
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def folder_key(folder: str) -> str:
    """Case/space-insensitive folder identity: "Needs review" == "needs-review" == "needsreview"."""
    k = re.sub(r"[\s_\-]+", "", (folder or "inbox").strip().strip("/").lower())
    return {"deleted": "deleteditems", "sent": "sentitems", "junk": "junkemail"}.get(k, k)


def folder_display(folder: str) -> str:
    k = folder_key(folder)
    if k in WELL_KNOWN:
        return WELL_KNOWN[k]
    f = folder.strip().strip("/")
    return f[:1].upper() + f[1:] if f.islower() else f


# ── mailboxes ────────────────────────────────────────────────────────────
def _mailbox_dir(mailbox: str) -> Path | None:
    root = fixtures_dir("m365") / "mailboxes"
    if not root.is_dir():
        return None
    want = mailbox.strip().lower()
    for d in root.iterdir():
        if d.is_dir() and d.name.lower() == want:
            return d
    return None


def _mb_state_path(mailbox: str) -> Path:
    return state_dir("m365") / "mailboxes" / f"{mailbox.strip().lower()}.json"


def _mb_state(mailbox: str) -> dict:
    st = read_json(_mb_state_path(mailbox), {})
    st.setdefault("messages", {})
    st.setdefault("folders", [])
    return st


_parse_cache: dict[tuple[str, float], dict] = {}


def _norm_subject(s: str) -> str:
    return re.sub(r"^\s*((re|fw|fwd|aw)\s*:\s*)+", "", s or "", flags=re.I).strip().lower()


def _parse_eml(path: Path) -> dict:
    key = (str(path), path.stat().st_mtime)
    if key in _parse_cache:
        return _parse_cache[key]
    msg: EmailMessage = BytesParser(policy=policy.default).parsebytes(path.read_bytes())  # type: ignore[assignment]

    def addr(header: str) -> list[dict]:
        h = msg.get(header)
        if not h:
            return []
        try:
            return [{"name": a.display_name or a.addr_spec, "address": a.addr_spec} for a in h.addresses]
        except Exception:
            return [{"name": str(h), "address": str(h)}]

    try:
        received = parsedate_to_datetime(msg["date"]) if msg["date"] else None
    except (TypeError, ValueError):
        received = None
    if received is None:
        received = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)

    body_part = msg.get_body(preferencelist=("html", "plain"))
    text_part = msg.get_body(preferencelist=("plain",))
    content = body_part.get_content() if body_part is not None else ""
    ctype = "html" if body_part is not None and body_part.get_content_subtype() == "html" else "text"
    if text_part is not None:
        plain = text_part.get_content()
    else:
        plain = re.sub(r"<[^>]+>", " ", content)
    preview = re.sub(r"\s+", " ", plain).strip()[:255]

    attachments = []
    for i, part in enumerate(msg.iter_attachments()):
        data = part.get_payload(decode=True) or b""
        name = part.get_filename() or f"attachment-{i + 1}"
        attachments.append({"name": name, "contentType": part.get_content_type(), "size": len(data),
                            "isInline": part.get_content_disposition() == "inline", "_data": data})
    frm = addr("from")
    parsed = {
        "subject": str(msg.get("subject", "") or ""),
        "from": frm[0] if frm else {"name": "", "address": ""},
        "toRecipients": [{"emailAddress": a} for a in addr("to")],
        "ccRecipients": [{"emailAddress": a} for a in addr("cc")],
        "receivedDateTime": _iso(received),
        "sentDateTime": _iso(received),
        "internetMessageId": str(msg.get("message-id", "") or ""),
        "bodyPreview": preview,
        "body": {"contentType": ctype, "content": content},
        "hasAttachments": any(not a["isInline"] for a in attachments),
        "conversationId": "AAQk" + _hash(_norm_subject(str(msg.get("subject", ""))), n=28),
        "importance": "high" if str(msg.get("importance", "")).lower() == "high" or
                                str(msg.get("x-priority", "")).startswith("1") else "normal",
        "_read_default": str(msg.get("x-mock-read", "")).lower() in ("1", "true", "yes"),
        "_attachments": attachments,
    }
    _parse_cache[key] = parsed
    return parsed


def _scan_mailbox(mailbox: str) -> dict[str, dict]:
    """id → {path, base_folder, folder, isRead, parsed}."""
    d = _mailbox_dir(mailbox)
    if d is None:
        raise NotFound(f"mailbox {mailbox!r} not found (no fixtures/m365/mailboxes/{mailbox}/)", status=404,
                       system="m365")
    st = _mb_state(mailbox)
    out: dict[str, dict] = {}
    for fdir in sorted(p for p in d.iterdir() if p.is_dir()):
        for eml in sorted(fdir.rglob("*.eml")):
            rel = eml.relative_to(d).as_posix()
            mid = "AAMk" + _hash(mailbox.lower(), rel, n=40)
            s = st["messages"].get(mid, {})
            parsed = _parse_eml(eml)
            out[mid] = {"path": eml, "base_folder": fdir.name, "folder": s.get("folder", fdir.name),
                        "isRead": s.get("isRead", parsed["_read_default"]), "parsed": parsed}
    return out


def _message_json(mailbox: str, mid: str, rec: dict, with_attachments: bool = False) -> dict:
    p = rec["parsed"]
    m = {k: v for k, v in p.items() if not k.startswith("_")}
    m.update({"id": mid, "isRead": bool(rec["isRead"]), "parentFolderId": folder_display(rec["folder"]),
              "folder": folder_display(rec["folder"]),
              "webLink": public_url(f"/connectors/m365/graph/v1.0/users/{quote(mailbox)}/messages/{mid}")})
    if with_attachments:
        m["attachments"] = [{"id": f"{mid}-att{i}", "name": a["name"], "contentType": a["contentType"],
                             "size": a["size"], "isInline": a["isInline"]}
                            for i, a in enumerate(p["_attachments"])]
    return m


def list_mailboxes() -> list[str]:
    root = fixtures_dir("m365") / "mailboxes"
    return sorted(d.name for d in root.iterdir() if d.is_dir()) if root.is_dir() else []


def list_folders(mailbox: str) -> list[dict]:
    msgs = _scan_mailbox(mailbox)
    names: dict[str, str] = {"inbox": "Inbox"}
    for rec in msgs.values():
        names.setdefault(folder_key(rec["folder"]), folder_display(rec["folder"]))
        names.setdefault(folder_key(rec["base_folder"]), folder_display(rec["base_folder"]))
    for f in _mb_state(mailbox)["folders"]:
        names.setdefault(folder_key(f), folder_display(f))
    counts: dict[str, list[int]] = {}
    for rec in msgs.values():
        c = counts.setdefault(folder_key(rec["folder"]), [0, 0])
        c[0] += 1
        c[1] += 0 if rec["isRead"] else 1
    return [{"id": k, "displayName": v, "totalItemCount": counts.get(k, [0, 0])[0],
             "unreadItemCount": counts.get(k, [0, 0])[1], "childFolderCount": 0} for k, v in names.items()]


def create_folder(mailbox: str, name: str) -> dict:
    _scan_mailbox(mailbox)
    st = _mb_state(mailbox)
    if folder_key(name) not in {folder_key(f) for f in st["folders"]}:
        st["folders"].append(folder_display(name))
        write_json(_mb_state_path(mailbox), st)
    return {"id": folder_key(name), "displayName": folder_display(name)}


def list_messages(mailbox: str, folder: str = "inbox", since: str | None = None, unread_only: bool = False,
                  top: int = 50, skip: int = 0) -> list[dict]:
    want = folder_key(folder)
    since_dt = parse_since(since)
    rows = []
    for mid, rec in _scan_mailbox(mailbox).items():
        if folder_key(rec["folder"]) != want:
            continue
        if unread_only and rec["isRead"]:
            continue
        if since_dt and parse_since(rec["parsed"]["receivedDateTime"]) < since_dt:
            continue
        rows.append(_message_json(mailbox, mid, rec))
    rows.sort(key=lambda m: m["receivedDateTime"], reverse=True)
    return rows[skip: skip + top] if top else rows[skip:]


def _get(mailbox: str, message_id: str) -> dict:
    rec = _scan_mailbox(mailbox).get(message_id)
    if rec is None:
        raise NotFound(f"message {message_id} not found in {mailbox}", status=404, system="m365")
    return rec


def get_message(mailbox: str, message_id: str) -> dict:
    return _message_json(mailbox, message_id, _get(mailbox, message_id), with_attachments=True)


def get_attachment(mailbox: str, message_id: str, attachment_id: str) -> bytes:
    rec = _get(mailbox, message_id)
    for i, a in enumerate(rec["parsed"]["_attachments"]):
        if attachment_id in (f"{message_id}-att{i}", a["name"]):
            return a["_data"]
    raise NotFound(f"attachment {attachment_id} not found on {message_id}", status=404, system="m365")


def attachment_meta(mailbox: str, message_id: str, attachment_id: str) -> dict:
    for a in get_message(mailbox, message_id)["attachments"]:
        if attachment_id in (a["id"], a["name"]):
            return a
    raise NotFound(f"attachment {attachment_id} not found", status=404, system="m365")


def _update(mailbox: str, message_id: str, **changes: Any) -> None:
    _get(mailbox, message_id)
    st = _mb_state(mailbox)
    st["messages"].setdefault(message_id, {}).update(changes)
    write_json(_mb_state_path(mailbox), st)


def mark_read(mailbox: str, message_id: str, is_read: bool = True) -> None:
    _update(mailbox, message_id, isRead=is_read)


def move_message(mailbox: str, message_id: str, folder: str) -> None:
    create_folder(mailbox, folder)
    _update(mailbox, message_id, folder=folder_display(folder))


def reset_mailbox(mailbox: str) -> None:
    """Forget read flags and moves (demo reset)."""
    p = _mb_state_path(mailbox)
    if p.exists():
        p.unlink()


# ── SharePoint / OneDrive ────────────────────────────────────────────────
def resolve_site(site: str | None) -> str:
    """Map a site reference (None, "root", a name, "host:/sites/X", a URL, a mock id) to a fixture site."""
    roots = [fixtures_dir("m365") / "sites", state_dir("m365") / "sites"]
    names = {p.name.lower(): p.name for r in roots if r.is_dir() for p in r.iterdir() if p.is_dir()}
    s = (site or "").strip()
    if not s or s.lower() in ("root", "default"):
        return "default"
    if s.lower() in names:
        return names[s.lower()]
    tail = urlparse(s).path if "://" in s else s.split(":", 1)[-1]
    seg = [x for x in re.split(r"[/:]", tail) if x]
    if seg and seg[-1].lower() in names:
        return names[seg[-1].lower()]
    return "default"


def _site_roots(site: str) -> list[Path]:
    """Fixture root then state (uploads) root; later wins on name clashes."""
    return [fixtures_dir("m365") / "sites" / site, state_dir("m365") / "sites" / site]


def _norm_folder(folder: str | None) -> str:
    f = (folder or "").replace("\\", "/").strip().strip("/")
    if f.lower().startswith("shared documents/"):
        f = f[len("shared documents/"):]
    elif f.lower() in ("shared documents", "root"):
        f = ""
    if ".." in f.split("/"):
        raise NotFound("invalid folder path", status=400, system="m365")
    return f


def item_id(site: str, rel: str) -> str:
    return "01" + base64.b32encode(hashlib.sha1(f"{site}\x1f{rel}".encode()).digest()).decode()[:30]


def _item(site: str, rel: str, path: Path) -> dict:
    parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
    st = path.stat()
    d = {"id": item_id(site, rel), "name": path.name,
         "lastModifiedDateTime": _iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)),
         "createdDateTime": _iso(datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)),
         "webUrl": public_url(f"/connectors/m365/sites/{quote(site)}/files/{quote(rel)}"),
         "parentReference": {"driveId": "b!" + _hash(site, n=20), "path": "/drive/root:" + ("/" + parent if parent else ""),
                             "siteId": site},
         "parentPath": parent}
    if path.is_dir():
        d["size"] = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
        d["folder"] = {"childCount": sum(1 for _ in path.iterdir())}
    else:
        d["size"] = st.st_size
        d["file"] = {"mimeType": mimetypes.guess_type(path.name)[0] or "application/octet-stream"}
    return d


def _children(site: str, folder: str) -> dict[str, tuple[str, Path]]:
    out: dict[str, tuple[str, Path]] = {}
    found = False
    for root in _site_roots(site):
        d = root / folder if folder else root
        if not d.is_dir():
            continue
        found = True
        for p in sorted(d.iterdir()):
            if p.name.startswith("."):
                continue
            rel = f"{folder}/{p.name}" if folder else p.name
            out[p.name] = (rel, p)
    if not found and folder:
        raise NotFound(f"folder {folder!r} not found in site {site!r}", status=404, system="m365")
    return out


def list_children(site: str | None, folder: str = "") -> list[dict]:
    s = resolve_site(site)
    return [_item(s, rel, p) for rel, p in _children(s, _norm_folder(folder)).values()]


def list_files(site: str | None, folder: str = "", recursive: bool = False,
               include_folders: bool = False) -> list[dict]:
    s = resolve_site(site)
    out, queue = [], [_norm_folder(folder)]
    while queue:
        f = queue.pop(0)
        for rel, p in _children(s, f).values():
            if p.is_dir():
                if include_folders:
                    out.append(_item(s, rel, p))
                if recursive:
                    queue.append(rel)
            else:
                out.append(_item(s, rel, p))
    return out


def _find(site: str, item: str) -> tuple[str, Path]:
    for root in reversed(_site_roots(site)):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if item_id(site, rel) == item:
                return rel, p
    raise NotFound(f"item {item} not found in site {site!r}", status=404, system="m365")


def get_item(site: str | None, item: str) -> dict:
    s = resolve_site(site)
    rel, p = _find(s, item)
    return _item(s, rel, p)


def item_by_path(site: str | None, path: str) -> dict:
    s = resolve_site(site)
    rel = _norm_folder(path)
    for root in reversed(_site_roots(s)):
        if (root / rel).exists():
            return _item(s, rel, root / rel)
    raise NotFound(f"{path!r} not found in site {s!r}", status=404, system="m365")


def download_file(site: str | None, item: str) -> bytes:
    s = resolve_site(site)
    _, p = _find(s, item)
    if p.is_dir():
        raise NotFound(f"item {item} is a folder", status=400, system="m365")
    return p.read_bytes()


def upload_file(site: str | None, folder: str, name: str, data: bytes) -> dict:
    s = resolve_site(site)
    f = _norm_folder(folder)
    if "/" in name or name in ("", ".", ".."):
        raise NotFound("invalid file name", status=400, system="m365")
    dest = state_dir("m365") / "sites" / s / f / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return _item(s, f"{f}/{name}" if f else name, dest)


def reset_uploads(site: str | None = None) -> None:
    root = state_dir("m365") / "sites"
    target = root / resolve_site(site) if site else root
    shutil.rmtree(target, ignore_errors=True)


# ── SharePoint lists ─────────────────────────────────────────────────────
def _lists_dir(site: str) -> Path:
    return fixtures_dir("m365") / "lists" / site


def list_lists(site: str | None) -> list[dict]:
    s = resolve_site(site)
    d = _lists_dir(s)
    return [{"id": _hash(s, p.stem, n=36), "name": p.stem, "displayName": p.stem,
             "webUrl": public_url(f"/connectors/m365/sites/{quote(s)}/lists/{quote(p.stem)}")}
            for p in sorted(d.glob("*.json"))] if d.is_dir() else []


def list_items(site: str | None, list_name: str) -> list[dict]:
    s = resolve_site(site)
    for meta in list_lists(s):
        if list_name in (meta["id"], meta["name"]) or list_name.lower() == meta["name"].lower():
            rows = json.loads((_lists_dir(s) / f"{meta['name']}.json").read_text())
            return [{"id": str(r.get("id", i + 1)), "fields": r} for i, r in enumerate(rows)]
    raise NotFound(f"list {list_name!r} not found in site {s!r}", status=404, system="m365")
