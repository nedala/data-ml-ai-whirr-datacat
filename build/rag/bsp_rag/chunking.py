"""Chunkers.

* `structure` (default): heading-aware sections; numbered runbook steps are
  kept together as one procedure (split only between steps, with the section
  breadcrumb repeated); tables are chunked by row groups with the header row
  repeated; every chunk carries its heading path ("section").
* `fixed`: sliding word windows with overlap — the naive baseline the evals
  leaderboard compares against.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from .parsing import Block, ParsedDoc

CHUNKERS = ("structure", "fixed")


@dataclass
class Chunk:
    ordinal: int
    text: str                 # what is embedded / indexed (breadcrumb + body)
    body: str                 # body only (what is shown and quoted)
    section: str              # heading path, e.g. "3. Procedure"
    kind: str                 # procedure | table | text | mixed
    tokens: int
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def approx_tokens(text: str) -> int:
    return max(1, int(len(text.split()) * 1.33))


def chunk(doc: ParsedDoc, method: str = "structure", max_tokens: int = 380, overlap: int = 40) -> list[Chunk]:
    if method == "fixed":
        return _fixed(doc, max_tokens=max_tokens, overlap=overlap)
    return _structure(doc, max_tokens=max_tokens)


# ── structure-aware ──────────────────────────────────────────────────────
def _structure(doc: ParsedDoc, max_tokens: int) -> list[Chunk]:
    sections: list[tuple[list[str], list[Block]]] = []
    path: list[tuple[int, str]] = []
    cur: list[Block] = []

    def close():
        if cur:
            sections.append(([t for _, t in path], list(cur)))
            cur.clear()

    for b in doc.blocks:
        if b.kind == "heading":
            close()
            if b.level <= 1:
                path = []          # the H1 is the document title
                continue
            path = [(lvl, t) for lvl, t in path if lvl < b.level] + [(b.level, b.text)]
        else:
            cur.append(b)
    close()

    out: list[Chunk] = []
    for heads, blocks in sections:
        section = " › ".join(heads)
        for body, kind in _pack(blocks, max_tokens):
            crumb = doc.title + (f" › {section}" if section else "")
            text = f"{crumb}\n{body}"
            out.append(Chunk(len(out), text, body, section, kind, approx_tokens(text)))
    return _merge_tiny(out, doc.title, max_tokens)


def _units(blocks: list[Block]) -> list[tuple[str, list[str]]]:
    """Group blocks into atomic units: a run of steps (with their sub-bullets),
    a run of table rows (header kept), or single paragraphs/bullets/code."""
    units: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b.kind == "step":
            lines = []
            while i < len(blocks) and (blocks[i].kind == "step" or (blocks[i].kind == "bullet" and blocks[i].level > 0)
                                       or (blocks[i].kind == "bullet" and lines and blocks[i - 1].kind in ("step", "bullet")
                                           and _steps_continue(blocks, i))):
                lines.append(("   " if blocks[i].kind == "bullet" else "") + blocks[i].text)
                i += 1
            units.append(("procedure", lines))
            continue
        if b.kind == "table":
            header = b.header
            rows = []
            while i < len(blocks) and blocks[i].kind == "table" and blocks[i].header == header:
                rows.append(blocks[i].text)
                i += 1
            units.append(("table", ([header] if header else []) + rows))
            continue
        units.append(("text", [b.text if b.kind != "code" else "```\n" + b.text + "\n```"]))
        i += 1
    return units


def _steps_continue(blocks: list[Block], i: int) -> bool:
    """A bullet directly under a step belongs to the step if another step follows."""
    j = i
    while j < len(blocks) and blocks[j].kind == "bullet":
        j += 1
    return j < len(blocks) and blocks[j].kind == "step"


def _pack(blocks: list[Block], max_tokens: int) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    buf: list[str] = []
    kinds: set[str] = set()

    def flush():
        if buf:
            out.append(("\n".join(buf), kinds.pop() if len(kinds) == 1 else "mixed"))
            buf.clear()
            kinds.clear()

    for kind, lines in _units(blocks):
        size = approx_tokens("\n".join(lines))
        if size > max_tokens:
            flush()
            if kind == "table":
                header, rows = (lines[0], lines[1:]) if lines and lines[0].startswith("|") else ("", lines)
                for grp in _split_lines(rows, max_tokens - approx_tokens(header)):
                    out.append(("\n".join(([header] if header else []) + grp), "table"))
            else:
                # procedures split between steps only; paragraphs by sentence
                parts = lines if kind == "procedure" else _sentences(" ".join(lines))
                for grp in _split_lines(parts, max_tokens):
                    out.append(("\n".join(grp) if kind == "procedure" else " ".join(grp), kind))
            continue
        if buf and approx_tokens("\n".join(buf)) + size > max_tokens:
            flush()
        buf.extend(lines)
        kinds.add(kind)
    flush()
    return out


def _split_lines(lines: list[str], budget: int) -> list[list[str]]:
    groups, cur, n = [], [], 0
    for ln in lines:
        t = approx_tokens(ln)
        if cur and n + t > budget:
            groups.append(cur)
            cur, n = [], 0
        cur.append(ln)
        n += t
    if cur:
        groups.append(cur)
    return groups


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _merge_tiny(chunks: list[Chunk], title: str, max_tokens: int, floor: int = 40) -> list[Chunk]:
    """Fold very small chunks (e.g. a one-line section) into their neighbour in
    the same section path prefix so they still retrieve with context."""
    out: list[Chunk] = []
    for c in chunks:
        if out and c.tokens < floor and out[-1].tokens + c.tokens <= max_tokens \
                and (out[-1].section.split(" › ")[0] == c.section.split(" › ")[0]):
            prev = out[-1]
            body = prev.body + f"\n\n{c.section.split(' › ')[-1]}:\n" + c.body if c.section != prev.section else prev.body + "\n" + c.body
            text = f"{title}" + (f" › {prev.section}" if prev.section else "") + "\n" + body
            out[-1] = Chunk(prev.ordinal, text, body, prev.section, "mixed" if prev.kind != c.kind else prev.kind, approx_tokens(text))
        else:
            out.append(Chunk(len(out), c.text, c.body, c.section, c.kind, c.tokens))
    return out


# ── fixed windows ────────────────────────────────────────────────────────
def _fixed(doc: ParsedDoc, max_tokens: int, overlap: int) -> list[Chunk]:
    words = doc.text.split()
    size = max(50, int(max_tokens / 1.33))
    step = max(10, size - int(overlap / 1.33))
    out: list[Chunk] = []
    for start in range(0, max(1, len(words)), step):
        body = " ".join(words[start:start + size])
        if not body:
            break
        text = f"{doc.title}\n{body}"
        out.append(Chunk(len(out), text, body, "", "text", approx_tokens(text)))
        if start + size >= len(words):
            break
    return out
