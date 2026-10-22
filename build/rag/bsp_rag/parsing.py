"""Document parsing → a flat list of structural blocks.

Every parser returns a `ParsedDoc`: title, metadata (front matter / document
properties / sidecar `<file>.meta.yaml`) and `blocks` — headings, paragraphs,
numbered steps, bullets, table rows and code — so the chunker can keep
runbook steps and tables intact. Supported: md, txt, html, pdf, docx, pptx,
xlsx (and csv as a table).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import Any

import yaml

SUPPORTED = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx", ".pptx", ".xlsx", ".csv"}


@dataclass
class Block:
    kind: str            # heading | para | step | bullet | table | code | quote
    text: str
    level: int = 0       # heading level, or list nesting
    header: str = ""     # for table rows: the table header line (repeated per chunk)


@dataclass
class ParsedDoc:
    title: str
    blocks: list[Block]
    meta: dict[str, Any] = field(default_factory=dict)
    text: str = ""       # full plain text (for hashing / previews)


class UnsupportedFormat(ValueError):
    pass


def ext_of(name: str) -> str:
    return PurePath(name).suffix.lower()


def parse(data: bytes, filename: str, meta: dict[str, Any] | None = None) -> ParsedDoc:
    ext = ext_of(filename)
    if ext not in SUPPORTED:
        raise UnsupportedFormat(f"unsupported file type {ext or '(none)'} for {filename}")
    fn = {".md": _md, ".markdown": _md, ".txt": _md, ".html": _html, ".htm": _html, ".pdf": _pdf,
          ".docx": _docx, ".pptx": _pptx, ".xlsx": _xlsx, ".csv": _csv}[ext]
    doc = fn(data, filename)
    if meta:
        doc.meta = {**doc.meta, **{k: v for k, v in meta.items() if v not in (None, "")}}
    doc.title = str(doc.meta.get("title") or doc.title or PurePath(filename).stem)
    doc.text = "\n".join(b.text for b in doc.blocks)
    return doc


# ── markdown / text ──────────────────────────────────────────────────────
_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)
_H = re.compile(r"^(#{1,6})\s+(.*)$")
_STEP = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_BUL = re.compile(r"^(\s*)[-*+•]\s+(.*)$")
_TROW = re.compile(r"^\s*\|.*\|\s*$")
_TSEP = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")


def front_matter(text: str) -> tuple[dict[str, Any], str]:
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), text[m.end():]


def md_blocks(text: str) -> tuple[str, list[Block]]:
    """Line-oriented markdown → blocks. Continuation lines join the open block."""
    blocks: list[Block] = []
    title = ""
    lines = text.replace("\r\n", "\n").split("\n")
    i, para = 0, []
    table_header = ""

    def flush_para():
        if para:
            blocks.append(Block("para", " ".join(s.strip() for s in para).strip()))
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush_para()
            code = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            blocks.append(Block("code", "\n".join(code)))
            i += 1
            continue
        if not line.strip():
            flush_para()
            table_header = ""
            i += 1
            continue
        if m := _H.match(line):
            flush_para()
            lvl, txt = len(m.group(1)), m.group(2).strip().strip("#").strip()
            if lvl == 1 and not title:
                title = txt
            blocks.append(Block("heading", txt, level=lvl))
        elif _TROW.match(line):
            flush_para()
            if i + 1 < len(lines) and _TSEP.match(lines[i + 1]) and not table_header:
                table_header = line.strip()
                i += 2
                continue
            if _TSEP.match(line):
                i += 1
                continue
            blocks.append(Block("table", line.strip(), header=table_header))
        elif m := _STEP.match(line):
            flush_para()
            blocks.append(Block("step", f"{m.group(2)}. {m.group(3).strip()}", level=len(m.group(1)) // 2))
        elif m := _BUL.match(line):
            flush_para()
            blocks.append(Block("bullet", "• " + m.group(2).strip(), level=len(m.group(1)) // 2))
        elif line.lstrip().startswith(">"):
            flush_para()
            blocks.append(Block("quote", line.lstrip()[1:].strip()))
        elif blocks and blocks[-1].kind in ("step", "bullet", "quote") and line.startswith(("   ", "\t")) and not para:
            blocks[-1].text += " " + line.strip()      # wrapped list item
        elif blocks and blocks[-1].kind == "quote" and not para and lines[i - 1].lstrip().startswith(">"):
            blocks[-1].text += " " + line.strip()
        else:
            para.append(line)
        i += 1
    flush_para()
    # merge consecutive quote lines into one block
    merged: list[Block] = []
    for b in blocks:
        if merged and b.kind == "quote" and merged[-1].kind == "quote":
            merged[-1].text += " " + b.text
        else:
            merged.append(b)
    return title, merged


def _md(data: bytes, filename: str) -> ParsedDoc:
    text = data.decode("utf-8", errors="replace")
    fm, body = front_matter(text)
    title, blocks = md_blocks(body)
    return ParsedDoc(title=str(fm.get("title") or title), blocks=blocks, meta=fm)


# ── HTML ─────────────────────────────────────────────────────────────────
def _html(data: bytes, filename: str) -> ParsedDoc:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for t in soup(["script", "style", "nav", "footer", "header", "noscript", "form"]):
        t.decompose()
    title = (soup.title.string.strip() if soup.title and soup.title.string else "")
    meta: dict[str, Any] = {}
    for m in soup.find_all("meta"):
        if m.get("name") in ("description", "author", "keywords") and m.get("content"):
            meta[{"author": "owner"}.get(m["name"], m["name"])] = m["content"]
    blocks: list[Block] = []
    root = soup.find("main") or soup.find("article") or soup.body or soup
    for el in root.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "table", "pre", "blockquote"]):
        if el.find_parent(["table", "pre"]) is not None and el.name not in ("table",):
            continue
        if el.name == "li" and el.find_parent("li") is not None:
            continue
        txt = " ".join(el.get_text(" ", strip=True).split())
        if el.name.startswith("h"):
            if txt:
                blocks.append(Block("heading", txt, level=int(el.name[1])))
                title = title or txt
        elif el.name == "p" and txt and el.find_parent("li") is None:
            blocks.append(Block("para", txt))
        elif el.name == "li" and txt:
            parent = el.find_parent(["ol", "ul"])
            if parent is not None and parent.name == "ol":
                n = len(el.find_previous_siblings("li")) + 1
                blocks.append(Block("step", f"{n}. {txt}"))
            else:
                blocks.append(Block("bullet", "• " + txt))
        elif el.name == "table":
            rows = el.find_all("tr")
            cells = [[" ".join(c.get_text(" ", strip=True).split()) for c in r.find_all(["th", "td"])] for r in rows]
            cells = [c for c in cells if any(c)]
            if cells:
                header = "| " + " | ".join(cells[0]) + " |"
                for r in cells[1:]:
                    blocks.append(Block("table", "| " + " | ".join(r) + " |", header=header))
        elif el.name == "pre" and txt:
            blocks.append(Block("code", el.get_text()))
        elif el.name == "blockquote" and txt:
            blocks.append(Block("quote", txt))
    return ParsedDoc(title=title, blocks=blocks, meta=meta)


# ── PDF ──────────────────────────────────────────────────────────────────
_NUM_HEAD = re.compile(r"^(\d{1,2}(\.\d{1,2})*)\.?\s+[A-Z][^.]{2,80}$")


def _pdf(data: bytes, filename: str) -> ParsedDoc:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    meta: dict[str, Any] = {}
    info = reader.metadata or {}
    if info.get("/Title"):
        meta["title"] = str(info["/Title"])
    if info.get("/Author"):
        meta["owner"] = str(info["/Author"])
    if info.get("/Subject"):
        meta["subject"] = str(info["/Subject"])
    lines: list[str] = []
    for page in reader.pages:
        lines.extend((page.extract_text() or "").splitlines())
        lines.append("")
    # heuristics: numbered section titles / short title-case lines become headings
    md: list[str] = []
    for ln in lines:
        s = ln.strip()
        if not s:
            md.append("")
        elif _NUM_HEAD.match(s) and not s.endswith((".", ",", ";", ":")) and len(s.split()) <= 9 \
                and "." not in s.split(" ", 1)[1]:
            depth = s.split(" ", 1)[0].count(".") + 2
            md.append("#" * min(depth, 6) + " " + s)
        elif s.isupper() and 3 < len(s) < 70:
            md.append("## " + s.title())
        elif md and md[-1] and not md[-1].startswith("#") and not md[-1].rstrip().endswith((".", ":", "!", "?")) \
                and not _STEP.match(s) and not _BUL.match(s):
            md[-1] = md[-1] + " " + s          # soft-wrapped line → join
        else:
            md.append(s)
    title, blocks = md_blocks("\n".join(md))
    return ParsedDoc(title=meta.get("title") or title, blocks=blocks, meta=meta)


# ── DOCX ─────────────────────────────────────────────────────────────────
def _docx(data: bytes, filename: str) -> ParsedDoc:
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(io.BytesIO(data))
    cp = d.core_properties
    meta: dict[str, Any] = {}
    if cp.title:
        meta["title"] = cp.title
    if cp.author:
        meta["owner"] = cp.author
    if cp.subject:
        meta["subject"] = cp.subject
    if cp.keywords:
        meta["tags"] = [k.strip() for k in cp.keywords.replace(";", ",").split(",") if k.strip()]
    if cp.version:
        meta["version"] = cp.version
    blocks: list[Block] = []
    title = ""
    step_n = 0
    body = d.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, d)
            txt = p.text.strip()
            style = (p.style.name if p.style is not None else "") or ""
            if not txt:
                continue
            if style.startswith("Title"):
                title = title or txt
                blocks.append(Block("heading", txt, level=1))
                step_n = 0
            elif style.startswith("Heading"):
                lvl = int(re.sub(r"\D", "", style) or 1)
                blocks.append(Block("heading", txt, level=min(lvl + 1, 6)))
                step_n = 0
            elif "List Number" in style:
                step_n += 1
                blocks.append(Block("step", f"{step_n}. {txt}"))
            elif "List" in style:
                blocks.append(Block("bullet", "• " + txt))
            elif m := _STEP.match(txt):
                blocks.append(Block("step", f"{m.group(2)}. {m.group(3)}"))
            else:
                blocks.append(Block("para", txt))
        elif tag == "tbl":
            t = Table(child, d)
            rows = [[c.text.strip() for c in r.cells] for r in t.rows]
            if rows:
                header = "| " + " | ".join(rows[0]) + " |"
                for r in rows[1:]:
                    blocks.append(Block("table", "| " + " | ".join(r) + " |", header=header))
    return ParsedDoc(title=meta.get("title") or title, blocks=blocks, meta=meta)


# ── PPTX ─────────────────────────────────────────────────────────────────
def _pptx(data: bytes, filename: str) -> ParsedDoc:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    cp = prs.core_properties
    meta: dict[str, Any] = {}
    if cp.title:
        meta["title"] = cp.title
    if cp.author:
        meta["owner"] = cp.author
    blocks: list[Block] = []
    for n, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text.strip() if slide.shapes.title is not None and slide.shapes.title.has_text_frame else ""
        blocks.append(Block("heading", f"Slide {n}: {title}" if title else f"Slide {n}", level=2))
        for shp in slide.shapes:
            if shp == slide.shapes.title:
                continue
            if shp.has_text_frame:
                for para in shp.text_frame.paragraphs:
                    txt = "".join(r.text for r in para.runs).strip()
                    if txt:
                        blocks.append(Block("bullet", "• " + txt, level=para.level))
            if getattr(shp, "has_table", False) and shp.has_table:
                rows = [[c.text.strip() for c in r.cells] for r in shp.table.rows]
                if rows:
                    header = "| " + " | ".join(rows[0]) + " |"
                    blocks.extend(Block("table", "| " + " | ".join(r) + " |", header=header) for r in rows[1:])
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                blocks.append(Block("para", "Speaker notes: " + " ".join(notes.split())))
    return ParsedDoc(title=meta.get("title", ""), blocks=blocks, meta=meta)


# ── XLSX / CSV ───────────────────────────────────────────────────────────
def _xlsx(data: bytes, filename: str) -> ParsedDoc:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    meta: dict[str, Any] = {}
    if wb.properties.title:
        meta["title"] = wb.properties.title
    if wb.properties.creator:
        meta["owner"] = wb.properties.creator
    blocks: list[Block] = []
    for ws in wb.worksheets:
        rows = [[("" if v is None else str(v).strip()) for v in r] for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(r)]
        if not rows:
            continue
        blocks.append(Block("heading", f"Sheet: {ws.title}", level=2))
        blocks.extend(_table_blocks(rows))
    return ParsedDoc(title=meta.get("title", ""), blocks=blocks, meta=meta)


def _csv(data: bytes, filename: str) -> ParsedDoc:
    import csv

    rows = [r for r in csv.reader(io.StringIO(data.decode("utf-8", errors="replace"))) if any(c.strip() for c in r)]
    return ParsedDoc(title="", blocks=_table_blocks(rows))


def _table_blocks(rows: list[list[str]]) -> list[Block]:
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    header_cells = rows[0]
    header = "| " + " | ".join(header_cells) + " |"
    return [Block("table", "| " + " | ".join(r) + " |", header=header) for r in rows[1:]]


def sidecar_meta(data: bytes) -> dict[str, Any]:
    try:
        doc = yaml.safe_load(data.decode("utf-8")) or {}
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}
