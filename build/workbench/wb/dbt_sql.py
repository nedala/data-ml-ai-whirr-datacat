"""dbt Studio — turn a "New model" wizard spec into dbt SQL.

Spec (all identifiers validated, values rendered as SQL literals)::

    {
      "layer": "intermediate", "name": "int_open_tickets", "description": "...",
      "materialized": "table" | null,
      "from": {"kind": "ref", "model": "stg_tickets"}
            | {"kind": "source", "source": "raw", "table": "invoices"},
      "joins": [{"kind": "ref", "model": "stg_policies", "type": "left",
                 "left": "policy_id", "right": "policy_id"}],
      "columns": [{"rel": "base" | "j1" ..., "column": "claim_id", "alias": "claim_ref"}],
      "filters": [{"rel": "base", "column": "status", "op": "=", "value": "Open"}],
      "group_by": [{"rel": "base", "column": "state", "alias": null}],
      "aggregates": [{"fn": "sum", "rel": "base", "column": "amount_due", "alias": "total_billed"}]
    }

The generated SQL uses ``{{ ref() }}`` / ``{{ source() }}`` so lineage,
environments and docs work exactly as for hand-written models.
"""
from __future__ import annotations

import re

from .dbt_project import LAYERS, StudioError, check_ident, check_model_name

OPS = {"=", "!=", ">", ">=", "<", "<=", "in", "not in", "like", "not like", "is null", "is not null"}
AGGS = {"sum": "sum({})", "avg": "avg({})", "min": "min({})", "max": "max({})",
        "count": "count({})", "count_distinct": "count(distinct {})"}
JOINS = {"left": "left join", "inner": "inner join", "right": "right join", "full": "full outer join"}
NUM_RE = re.compile(r"^-?\d+(\.\d+)?([eE][-+]?\d+)?$")
JINJA_RE = re.compile(r"\{[{%#]|[}%#]\}")
# words that must be quoted when used as a bare column name (Postgres + BigQuery + Trino)
RESERVED = {
    "all", "analyse", "analyze", "and", "any", "array", "as", "asc", "asymmetric", "both", "case", "cast",
    "check", "collate", "column", "constraint", "create", "current_date", "current_role", "current_time",
    "current_timestamp", "current_user", "default", "deferrable", "desc", "distinct", "do", "else", "end",
    "except", "false", "fetch", "for", "foreign", "from", "grant", "group", "having", "in", "initially",
    "intersect", "into", "lateral", "leading", "limit", "localtime", "localtimestamp", "not", "null",
    "offset", "on", "only", "or", "order", "placing", "primary", "references", "returning", "select",
    "session_user", "some", "symmetric", "table", "then", "to", "trailing", "true", "union", "unique",
    "user", "using", "variadic", "when", "where", "window", "with", "qualify", "sample", "connect",
    "increment", "minus", "regexp", "rlike", "start", "trigger", "values", "view",
}


def q(col: str) -> str:
    check_ident(col, "column")
    return f'"{col}"' if col.lower() in RESERVED or col != col.lower() else col


def literal(v) -> str:
    s = "" if v is None else str(v).strip()
    if JINJA_RE.search(s):
        raise StudioError("Filter values may not contain Jinja delimiters.")
    if len(s) > 500:
        raise StudioError("Filter value is too long.")
    if NUM_RE.match(s):
        return s
    if s.lower() in ("true", "false"):
        return s.lower()
    return "'" + s.replace("'", "''") + "'"


def relation(spec: dict) -> str:
    kind = (spec or {}).get("kind")
    if kind == "ref":
        return "{{ ref('%s') }}" % check_model_name(spec.get("model") or "")
    if kind == "source":
        return "{{ source('%s', '%s') }}" % (check_ident(spec.get("source") or "", "source name"),
                                               check_ident(spec.get("table") or "", "source table"))
    raise StudioError("Pick a source table or an upstream model to start from.")


def _label(spec: dict) -> str:
    return spec.get("model") if spec.get("kind") == "ref" else f"{spec.get('source')}.{spec.get('table')}"


def generate_sql(spec: dict) -> str:
    if not isinstance(spec, dict):
        raise StudioError("Invalid model spec.")
    layer = spec.get("layer")
    if layer not in LAYERS:
        raise StudioError("Layer must be staging, intermediate or marts.")
    name = check_model_name(spec.get("name") or "")
    base = spec.get("from") or {}
    rels = {"base": relation(base)}
    joins = spec.get("joins") or []
    if len(joins) > 6:
        raise StudioError("At most 6 joins.")
    join_sql = []
    for i, j in enumerate(joins, 1):
        alias = f"j{i}"
        rels[alias] = relation(j)
        jt = JOINS.get(j.get("type") or "left")
        if not jt:
            raise StudioError("Join type must be left, inner, right or full.")
        left_rel = j.get("left_rel") or "base"
        if left_rel not in rels or left_rel == alias:
            raise StudioError(f"Join {i}: unknown left-hand table.")
        pairs = j.get("on") or [{"left": j.get("left"), "right": j.get("right")}]
        conds = [f"{left_rel}.{q(p.get('left') or '')} = {alias}.{q(p.get('right') or '')}" for p in pairs]
        if not conds:
            raise StudioError(f"Join {i}: pick the key columns.")
        join_sql.append(f"{jt} {alias} on " + "\n    and ".join(conds))

    def col_ref(c: dict) -> str:
        rel = c.get("rel") or "base"
        if rel not in rels:
            raise StudioError(f"Unknown table alias {rel!r}.")
        return f"{rel}.{q(c.get('column') or '')}"

    def alias_of(c: dict, default: str | None) -> str | None:
        a = (c.get("alias") or "").strip()
        if a:
            check_ident(a, "column name")
            return a.lower() if a != a.lower() else a
        return default

    group_by = spec.get("group_by") or []
    aggs = spec.get("aggregates") or []
    select, group_exprs = [], []
    used: set[str] = set()

    def add(expr: str, out_name: str | None, src_name: str | None) -> None:
        final = out_name or src_name
        if final and final in used:
            raise StudioError(f"Column {final!r} appears twice — rename one of them.")
        if final:
            used.add(final)
        select.append(expr if not out_name or out_name == src_name else f"{expr} as {q(out_name)}")

    if aggs:
        for g in group_by:
            expr = col_ref(g)
            group_exprs.append(expr)
            add(expr, alias_of(g, None), g.get("column"))
        for a in aggs:
            fn = a.get("fn")
            if fn not in AGGS:
                raise StudioError("Aggregates: sum, avg, count, count_distinct, min or max.")
            if fn == "count" and not a.get("column"):
                expr, default = "count(*)", "row_count"
            else:
                expr, default = AGGS[fn].format(col_ref(a)), f"{fn}_{a.get('column')}"
            out = alias_of(a, default)
            if out in used:
                raise StudioError(f"Column {out!r} appears twice — rename one of them.")
            used.add(out)
            select.append(f"{expr} as {q(out)}")
    else:
        for c in spec.get("columns") or []:
            add(col_ref(c), alias_of(c, None), c.get("column"))
        if not select:
            select = ["base.*"]

    where = []
    for f in spec.get("filters") or []:
        op = (f.get("op") or "=").lower()
        if op not in OPS:
            raise StudioError(f"Unsupported filter operator {op!r}.")
        lhs = col_ref(f)
        if op in ("is null", "is not null"):
            where.append(f"{lhs} {op}")
        elif op in ("in", "not in"):
            raw = f.get("value")
            items = raw if isinstance(raw, list) else str(raw or "").split(",")
            items = [x for x in (str(i).strip() for i in items) if x]
            if not items:
                raise StudioError(f"Filter on {f.get('column')}: list at least one value.")
            where.append(f"{lhs} {op} (" + ", ".join(literal(i) for i in items) + ")")
        else:
            where.append(f"{lhs} {op} {literal(f.get('value'))}")

    desc = re.sub(r"\s+", " ", str(spec.get("description") or "")).strip()
    if JINJA_RE.search(desc):
        raise StudioError("Description may not contain Jinja delimiters.")
    header = [f"-- {layer.capitalize()}: {desc or name}",
              f"-- Built in dbt Studio from {_label(base)}" + (
                  " + " + ", ".join(_label(j) for j in joins) if joins else "") + ". Edit freely."]
    mat = spec.get("materialized")
    if mat:
        if mat not in ("view", "table", "incremental", "ephemeral"):
            raise StudioError("Materialization must be view, table, incremental or ephemeral.")
        header.append("{{ config(materialized='%s') }}" % mat)

    ctes = [f"{alias} as (\n    select * from {rel}\n)" for alias, rel in rels.items()]
    lines = header + ["", "with " + ",\n\n".join(ctes), "", "select"]
    lines.append(",\n".join(f"    {s}" for s in select))
    lines.append("from base")
    lines += join_sql
    if where:
        lines.append("where " + "\n  and ".join(where))
    if group_exprs:
        lines.append("group by " + ", ".join(group_exprs))
    return "\n".join(lines) + "\n"
