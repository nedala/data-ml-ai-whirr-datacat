"""Shared plumbing: tool registry, errors, SQL guard, JSON-safe results, group flags."""
from __future__ import annotations

import datetime as dt
import decimal
import functools
import inspect
import os
import re
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MAX_ROWS = int(os.environ.get("MCP_MAX_ROWS", "500"))


class UserError(ValueError):
    """A problem the caller can fix — surfaced to the model as a tool error."""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def data_dir() -> Path:
    for base in (env("MCP_DATA_DIR"), "/data/mcp", str(Path(os.environ.get("TMPDIR", "/tmp")) / "bsp-mcp")):
        if not base:
            continue
        p = Path(base)
        try:
            p.mkdir(parents=True, exist_ok=True)
            if os.access(p, os.W_OK):
                return p
        except OSError:
            continue
    raise RuntimeError("no writable data dir for the MCP server")


# ── tool registry ────────────────────────────────────────────────────────────
@dataclass
class ToolSpec:
    group: str
    fn: Callable[..., Any]
    name: str
    title: str
    read_only: bool = True
    open_world: bool = False
    summary: str = ""


@dataclass
class Registry:
    tools: list[ToolSpec] = field(default_factory=list)

    def tool(self, group: str, title: str, *, read_only: bool = True, open_world: bool = False):
        def deco(fn):
            doc = inspect.getdoc(fn) or ""
            if not fn.__name__.startswith(group + "_"):
                raise ValueError(f"tool {fn.__name__} must be prefixed with its group '{group}_'")
            self.tools.append(ToolSpec(group, fn, fn.__name__, title, read_only, open_world,
                                       doc.split("\n\n")[0].replace("\n", " ")))
            return fn

        return deco

    def groups(self) -> list[str]:
        seen: list[str] = []
        for t in self.tools:
            if t.group not in seen:
                seen.append(t.group)
        return seen


REGISTRY = Registry()
tool = REGISTRY.tool


def enabled_groups(all_groups: list[str]) -> list[str]:
    """MCP_GROUPS=atlan,warehouse (allow-list, default all) minus MCP_DISABLE_GROUPS=browser,…"""
    allow = [g.strip() for g in env("MCP_GROUPS").split(",") if g.strip()]
    deny = {g.strip() for g in env("MCP_DISABLE_GROUPS").split(",") if g.strip()}
    return [g for g in all_groups if (not allow or g in allow) and g not in deny]


def threaded(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Async twin of a blocking tool (same resolved signature), run in a worker thread."""
    import anyio  # noqa: PLC0415

    hints = typing.get_type_hints(fn, include_extras=True)
    sig = inspect.signature(fn)
    params = [p.replace(annotation=hints.get(p.name, p.annotation)) for p in sig.parameters.values()]
    sig = sig.replace(parameters=params, return_annotation=hints.get("return", sig.return_annotation))

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            return await fn(*args, **kwargs)
    else:
        @functools.wraps(fn)
        async def run(*args, **kwargs):
            return await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))

    run.__signature__ = sig  # type: ignore[attr-defined]
    run.__annotations__ = {**{p.name: p.annotation for p in params}, "return": sig.return_annotation}
    del run.__wrapped__
    return run


# ── output helpers ───────────────────────────────────────────────────────────
def jsonable(v: Any) -> Any:
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [jsonable(x) for x in v]
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return None
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    try:
        import numpy as np  # noqa: PLC0415

        if isinstance(v, np.generic):
            return jsonable(v.item())
    except ImportError:
        pass
    return str(v)


def md_table(rows: list[dict], max_rows: int = 15) -> str:
    if not rows:
        return "_(no rows)_"
    cols = list(rows[0].keys())

    def cell(x):
        s = "" if x is None else str(x)
        return s.replace("|", "/").replace("\n", " ")[:60]

    out = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    out += ["| " + " | ".join(cell(r.get(c)) for c in cols) + " |" for r in rows[:max_rows]]
    if len(rows) > max_rows:
        out.append(f"_… {len(rows) - max_rows} more rows_")
    return "\n".join(out)


def result(summary: str, mode: str | None = None, **data: Any) -> dict:
    out: dict[str, Any] = {"summary": summary}
    out.update({k: jsonable(v) for k, v in data.items()})
    if mode:
        out["mode"] = mode
    return out


def clamp(v: Any, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(v)))


_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]{0,127}$")


def ident(name: str, what: str = "identifier") -> str:
    if not _IDENT.match(name or ""):
        raise UserError(f"invalid {what} {name!r}")
    return name


# ── read-only SQL guard (Postgres / Trino / BigQuery) ───────────────────────
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)
_FORBIDDEN = re.compile(r"\b(insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|copy|call|do|vacuum|"
                        r"analyze|refresh|lock|listen|notify|set|reset|comment|security|prepare|execute|export|load|"
                        r"declare|begin|commit|rollback|pg_read_file|pg_read_binary_file|pg_ls_dir|lo_import|lo_export|"
                        r"dblink|pg_sleep|pg_terminate_backend|pg_cancel_backend|set_config)\b", re.I)


def check_select(sql: str) -> str:
    """Exactly one SELECT / WITH statement (comments stripped, one optional trailing ';')."""
    s = _COMMENT.sub(" ", sql or "").strip()
    if s.endswith(";"):
        s = s[:-1].rstrip()
    if not s:
        raise UserError("empty query")
    no_strings = re.sub(r"'(?:[^']|'')*'", "''", s)
    if ";" in no_strings:
        raise UserError("only a single statement is allowed")
    if not re.match(r"^\(?\s*(select|with|values|table|show|describe)\b", s, re.I):
        raise UserError("read-only: the query must start with SELECT or WITH")
    m = _FORBIDDEN.search(re.sub(r'"[^"]*"|`[^`]*`', '""', no_strings))
    if m:
        raise UserError(f"read-only: '{m.group(0)}' is not allowed here")
    return s


def connectors(system: str):
    """Lazily import a bsp_connectors system (mock-or-live)."""
    try:
        import bsp_connectors  # noqa: PLC0415

        return getattr(bsp_connectors, system)
    except ImportError as e:
        raise UserError(f"the connectors SDK (bsp_connectors.{system}) is not installed in this container: {e}") from None


def connector_mode(system: str) -> str:
    try:
        from bsp_connectors import mode_of  # noqa: PLC0415

        return mode_of(system)
    except Exception:  # noqa: BLE001
        return "unknown"
