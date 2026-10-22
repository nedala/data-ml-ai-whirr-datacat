"""Safe, vectorised expressions for the Filter and Derive transforms.

Python-like syntax over column names; no attribute access, subscripts,
lambdas or imports — only the whitelisted functions below. Columns with
spaces or symbols are written in backticks: `Revenue (USD)` / 1000.

    amount_due > 100 and status == 'open'
    state in ['OH', 'NC']
    round(revenue / subscribers, 2)
    if_(truck_roll, 'dispatch', 'remote')
    regex_extract(from_address, '^ban(\\d{10})@')
"""

from __future__ import annotations

import ast
import re
from typing import Any, Callable

import numpy as np
import pandas as pd


class ExprError(ValueError):
    pass


def _s(x: Any, index) -> pd.Series:
    return x if isinstance(x, pd.Series) else pd.Series([x] * len(index), index=index)


def _str(x: Any, index) -> Any:
    return _s(x, index).astype("string")


FUNCS: dict[str, Callable[..., Any]] = {}


def fn(name: str):
    def deco(f):
        FUNCS[name] = f
        return f
    return deco


@fn("lower")
def _lower(ix, x): return _str(x, ix).str.lower()
@fn("upper")
def _upper(ix, x): return _str(x, ix).str.upper()
@fn("strip")
def _strip(ix, x): return _str(x, ix).str.strip()
@fn("length")
def _len(ix, x): return _str(x, ix).str.len()
@fn("contains")
def _contains(ix, x, sub): return _str(x, ix).str.contains(str(sub), case=False, regex=False, na=False)
@fn("startswith")
def _sw(ix, x, p): return _str(x, ix).str.startswith(str(p), na=False)
@fn("endswith")
def _ew(ix, x, p): return _str(x, ix).str.endswith(str(p), na=False)
@fn("replace")
def _replace(ix, x, a, b): return _str(x, ix).str.replace(str(a), str(b), regex=False)
@fn("regex_extract")
def _rx(ix, x, pat): return _str(x, ix).str.extract(str(pat), expand=False) if re.compile(str(pat)).groups else \
    _str(x, ix).str.extract("(" + str(pat) + ")", expand=False)
@fn("substr")
def _substr(ix, x, start, n=None): return _str(x, ix).str.slice(int(start), None if n is None else int(start) + int(n))
@fn("concat")
def _concat(ix, *xs):
    out = _str(xs[0], ix).fillna("")
    for x in xs[1:]:
        out = out + _str(x, ix).fillna("")
    return out
@fn("round")
def _round(ix, x, n=0): return pd.to_numeric(_s(x, ix), errors="coerce").round(int(n))
@fn("abs")
def _abs(ix, x): return pd.to_numeric(_s(x, ix), errors="coerce").abs()
@fn("to_number")
def _num(ix, x): return pd.to_numeric(_s(x, ix), errors="coerce")
@fn("to_string")
def _tostr(ix, x): return _str(x, ix)
@fn("to_date")
def _date(ix, x): return pd.to_datetime(_s(x, ix), errors="coerce", utc=False).dt.date
@fn("to_datetime")
def _dt(ix, x): return pd.to_datetime(_s(x, ix), errors="coerce", utc=True)
@fn("year")
def _year(ix, x): return pd.to_datetime(_s(x, ix), errors="coerce").dt.year
@fn("month")
def _month(ix, x): return pd.to_datetime(_s(x, ix), errors="coerce").dt.month
@fn("date_trunc_month")
def _dtm(ix, x): return pd.to_datetime(_s(x, ix), errors="coerce").dt.to_period("M").dt.to_timestamp().dt.date
@fn("isnull")
def _isnull(ix, x): return _s(x, ix).isna()
@fn("notnull")
def _notnull(ix, x): return _s(x, ix).notna()
@fn("coalesce")
def _coalesce(ix, *xs):
    out = _s(xs[0], ix)
    for x in xs[1:]:
        out = out.where(out.notna(), _s(x, ix))
    return out
@fn("if_")
def _if(ix, cond, a, b): return pd.Series(np.where(_s(cond, ix).fillna(False).astype(bool), _s(a, ix), _s(b, ix)), index=ix)
@fn("now")
def _now(ix): return _s(pd.Timestamp.now(tz="UTC"), ix)
@fn("today")
def _today(ix): return _s(pd.Timestamp.now(tz="UTC").date(), ix)


BIN = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
       ast.Div: lambda a, b: a / b, ast.Mod: lambda a, b: a % b, ast.FloorDiv: lambda a, b: a // b,
       ast.Pow: lambda a, b: a ** b}
CMP = {ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Lt: lambda a, b: a < b,
       ast.LtE: lambda a, b: a <= b, ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b}


def _prep(expr: str) -> tuple[str, dict[str, str]]:
    cols: dict[str, str] = {}

    def sub(m: re.Match) -> str:
        key = f"__c{len(cols)}"
        cols[key] = m.group(1)
        return key
    return re.sub(r"`([^`]+)`", sub, expr), cols


def parse(expr: str) -> tuple[ast.AST, dict[str, str]]:
    src, cols = _prep(expr.strip())
    if not src:
        raise ExprError("empty expression")
    if len(src) > 2000:
        raise ExprError("expression too long")
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"syntax error: {e.msg}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Lambda, ast.Starred, ast.NamedExpr, ast.comprehension,
                             ast.ListComp, ast.DictComp, ast.SetComp, ast.GeneratorExp, ast.Dict, ast.Await,
                             ast.JoinedStr, ast.FormattedValue)):
            raise ExprError(f"{type(node).__name__} is not allowed")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCS:
                name = getattr(node.func, "id", "?")
                raise ExprError(f"unknown function {name}() — allowed: {', '.join(sorted(FUNCS))}")
            if node.keywords:
                raise ExprError("keyword arguments are not allowed")
    return tree, cols


def columns_used(expr: str) -> set[str]:
    tree, cols = parse(expr)
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            if node.id in FUNCS or node.id in ("true", "false"):
                continue
            out.add(cols.get(node.id, node.id))
    return out


def evaluate(df: pd.DataFrame, expr: str) -> Any:
    tree, cols = parse(expr)
    ix = df.index

    def ev(n: ast.AST) -> Any:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant):
            return n.value
        if isinstance(n, (ast.List, ast.Tuple, ast.Set)):
            return [ev(e) for e in n.elts]
        if isinstance(n, ast.Name):
            name = cols.get(n.id, n.id)
            if name in df.columns:
                return df[name]
            if n.id in ("true", "false"):
                return n.id == "true"
            raise ExprError(f"unknown column {name!r} (columns: {', '.join(map(str, df.columns[:30]))})")
        if isinstance(n, ast.BoolOp):
            vals = [_s(ev(v), ix).fillna(False).astype(bool) for v in n.values]
            out = vals[0]
            for v in vals[1:]:
                out = (out & v) if isinstance(n.op, ast.And) else (out | v)
            return out
        if isinstance(n, ast.UnaryOp):
            v = ev(n.operand)
            if isinstance(n.op, ast.Not):
                return ~_s(v, ix).fillna(False).astype(bool)
            if isinstance(n.op, ast.USub):
                return -v
            return v
        if isinstance(n, ast.BinOp):
            op = BIN.get(type(n.op))
            if op is None:
                raise ExprError(f"operator {type(n.op).__name__} not allowed")
            return op(ev(n.left), ev(n.right))
        if isinstance(n, ast.Compare):
            left = ev(n.left)
            out = None
            for op, comp in zip(n.ops, n.comparators):
                right = ev(comp)
                if isinstance(op, (ast.In, ast.NotIn)):
                    r = _s(left, ix).isin(right if isinstance(right, list) else [right])
                    r = ~r if isinstance(op, ast.NotIn) else r
                elif isinstance(op, (ast.Is, ast.IsNot)):
                    r = _s(left, ix).isna() if right is None else _s(left, ix) == right
                    r = ~r if isinstance(op, ast.IsNot) else r
                else:
                    r = CMP[type(op)](left, right)
                out = r if out is None else (out & r)
                left = right
            return out
        if isinstance(n, ast.IfExp):
            return FUNCS["if_"](ix, ev(n.test), ev(n.body), ev(n.orelse))
        if isinstance(n, ast.Call):
            return FUNCS[n.func.id](ix, *[ev(a) for a in n.args])
        raise ExprError(f"{type(n).__name__} is not allowed")

    try:
        return ev(tree)
    except ExprError:
        raise
    except Exception as e:
        raise ExprError(f"{type(e).__name__}: {e}") from None
