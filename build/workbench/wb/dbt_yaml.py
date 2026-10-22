"""dbt Studio — read and write dbt properties YAML (model docs, config,
column tests, sources) without the analyst ever hand-authoring YAML.

Round-trips with ruamel.yaml so comments, key order, quoting and any
entries the form does not manage (custom tests, meta, other models) are
kept as they were. Tests are written in the dbt ≥ 1.10 ``arguments:`` form;
both that and the older inline form are read.
"""
from __future__ import annotations

import io
import os
import re
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .dbt_project import (SKIP_DIRS, StudioError, check_ident, check_model_name, find_node,
                          model_file, model_paths, safe_path)

MATERIALIZATIONS = ("view", "table", "incremental", "ephemeral")
MANAGED_TESTS = ("unique", "not_null", "accepted_values", "relationships")
REF_RE = re.compile(r"""^\s*ref\(\s*['"]([A-Za-z0-9_]+)['"]\s*\)\s*$""")
NUM_RE = re.compile(r"^-?\d+(\.\d+)?$")
JINJA_RE = re.compile(r"\{[{%#]|[}%#]\}")


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    y.brace_single_entry_mapping_in_flow_sequence = True
    return y


def load(path: Path):
    if not path.is_file():
        return None
    data = _yaml().load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, CommentedMap) else None


def dumps(data) -> str:
    buf = io.StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def save(path: Path, data) -> None:
    text = dumps(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _clean_text(s, what: str, limit: int = 4000) -> str:
    s = "" if s is None else str(s)
    if len(s) > limit:
        raise StudioError(f"{what} is too long.")
    if JINJA_RE.search(s):
        raise StudioError(f"{what} may not contain Jinja delimiters ({{{{ }}}}, {{% %}}, {{# #}}).")
    return s.strip()


def yml_files(project: Path) -> list[Path]:
    out = []
    for mp in model_paths(project):
        base = safe_path(project, mp)
        if not base.is_dir():
            continue
        for p in sorted(list(base.rglob("*.yml")) + list(base.rglob("*.yaml"))):
            rel = p.relative_to(project).parts
            if p.is_file() and not any(part in SKIP_DIRS or part.startswith(".") for part in rel):
                out.append(p.resolve())
    return out


def _entries(data, key: str) -> list:
    seq = data.get(key) if isinstance(data, dict) else None
    return seq if isinstance(seq, list) else []


def _find_entry(data, key: str, name: str):
    for e in _entries(data, key):
        if isinstance(e, dict) and e.get("name") == name:
            return e
    return None


# ── model properties ─────────────────────────────────────────────────
def props_file_for(project: Path, name: str) -> tuple[Path, bool]:
    """(path, exists_with_entry). Prefer the file dbt says documents the model,
    then any YAML in the model paths that has it, then the folder's
    ``_<folder>.yml`` (or another models-YAML in that folder)."""
    check_model_name(name)
    n = find_node(project, name)
    if n and n.get("patch_path"):
        rel = n["patch_path"].split("://", 1)[-1]
        p = safe_path(project, rel)
        if p.is_file() and _find_entry(load(p), "models", name) is not None:
            return p, True
    for p in yml_files(project):
        try:
            if _find_entry(load(p), "models", name) is not None:
                return p, True
        except Exception:  # noqa: BLE001  (a broken YAML elsewhere must not block this model)
            continue
    sql = model_file(project, name)
    if sql is None:
        raise StudioError(f"Model {name} not found.", 404)
    folder = sql.parent
    own = folder / f"_{folder.name}.yml"
    if own.is_file():
        return own, False
    for p in sorted(folder.glob("*.yml")):
        try:
            if isinstance(load(p), dict) and "models" in load(p):
                return p.resolve(), False
        except Exception:  # noqa: BLE001
            continue
    return own, False


def _parse_test(entry) -> tuple[str | None, dict, dict]:
    """→ (kind, arguments, config) for a test entry in any supported form."""
    if isinstance(entry, str):
        return entry, {}, {}
    if isinstance(entry, dict) and len(entry) >= 1:
        kind = next(iter(entry))
        body = entry.get(kind)
        if not isinstance(body, dict):
            return kind, {}, {}
        args = body.get("arguments") if isinstance(body.get("arguments"), dict) else {
            k: v for k, v in body.items() if k not in ("config", "arguments", "name", "severity", "description")}
        cfg = dict(body.get("config") or {}) if isinstance(body.get("config"), dict) else {}
        if "severity" in body and "severity" not in cfg:
            cfg["severity"] = body["severity"]
        return kind, dict(args), cfg
    return None, {}, {}


def _sev(cfg: dict) -> str:
    return "warn" if str(cfg.get("severity", "")).lower() == "warn" else "error"


def _read_column(col) -> dict:
    tests_key = "data_tests" if "data_tests" in col else "tests"
    out = {"name": str(col.get("name")), "description": str(col.get("description") or ""),
           "tests": {}, "other_tests": []}
    for t in col.get(tests_key) or []:
        kind, args, cfg = _parse_test(t)
        if kind == "unique" or kind == "not_null":
            out["tests"][kind] = {"severity": _sev(cfg)}
        elif kind == "accepted_values":
            vals = args.get("values") or []
            out["tests"][kind] = {"values": [str(v) for v in vals], "severity": _sev(cfg)}
        elif kind == "relationships":
            m = REF_RE.match(str(args.get("to") or ""))
            out["tests"][kind] = {"to": m.group(1) if m else str(args.get("to") or ""),
                                  "field": str(args.get("field") or ""), "severity": _sev(cfg)}
        elif kind:
            out["other_tests"].append(kind)
    return out


def get_model_props(project: Path, name: str) -> dict:
    path, exists = props_file_for(project, name)
    entry = _find_entry(load(path), "models", name) if exists else None
    entry = entry or {}
    cfg = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    tags = cfg.get("tags", entry.get("tags")) or []
    if isinstance(tags, str):
        tags = [tags]
    return {
        "file": path.relative_to(project).as_posix(),
        "documented": exists,
        "description": str(entry.get("description") or ""),
        "materialized": cfg.get("materialized"),
        "unique_key": cfg.get("unique_key") if isinstance(cfg.get("unique_key"), str) else None,
        "tags": [str(t) for t in tags],
        "columns": [_read_column(c) for c in entry.get("columns") or [] if isinstance(c, dict) and c.get("name")],
    }


def _values(raw) -> tuple[list, bool]:
    vals = [str(v).strip() for v in (raw or []) if str(v).strip() != ""]
    if not vals:
        raise StudioError("accepted_values needs at least one value.")
    for v in vals:
        _clean_text(v, "An accepted value", 200)
    if all(NUM_RE.match(v) for v in vals):
        return [float(v) if "." in v else int(v) for v in vals], False
    return vals, True


def _apply_sev(body: CommentedMap, sev: str) -> None:
    body.pop("severity", None)
    cfg = body.get("config")
    if sev == "warn":
        if not isinstance(cfg, dict):
            cfg = body["config"] = CommentedMap()
        cfg["severity"] = "warn"
    elif isinstance(cfg, dict):
        cfg.pop("severity", None)
        if not cfg:
            del body["config"]


def _new_test(kind: str, spec: dict):
    sev = spec.get("severity") or "error"
    if kind in ("unique", "not_null"):
        if sev != "warn":
            return kind
        body = CommentedMap()
    else:
        body = CommentedMap()
        args = body["arguments"] = CommentedMap()
        if kind == "accepted_values":
            vals, quote = _values(spec.get("values"))
            seq = CommentedSeq(vals)
            seq.fa.set_flow_style()
            args["values"] = seq
            if not quote:
                args["quote"] = False
        else:
            args["to"] = f"ref('{check_model_name(spec.get('to') or '')}')"
            args["field"] = check_ident(spec.get("field") or "", "relationship column")
    _apply_sev(body, sev)
    m = CommentedMap()
    m[kind] = body
    m.fa.set_flow_style()
    return m


def _update_test(entry, kind: str, spec: dict):
    """Update a managed test in place (keeps config / extra keys); returns the entry."""
    if isinstance(entry, str):
        return _new_test(kind, spec)
    body = entry.get(kind)
    if not isinstance(body, dict):
        return _new_test(kind, spec)
    if kind == "accepted_values":
        vals, quote = _values(spec.get("values"))
        target = body["arguments"] if isinstance(body.get("arguments"), dict) else body
        seq = CommentedSeq(vals)
        seq.fa.set_flow_style()
        target["values"] = seq
        if quote:
            target.pop("quote", None)
        else:
            target["quote"] = False
    elif kind == "relationships":
        target = body["arguments"] if isinstance(body.get("arguments"), dict) else body
        target["to"] = f"ref('{check_model_name(spec.get('to') or '')}')"
        target["field"] = check_ident(spec.get("field") or "", "relationship column")
    _apply_sev(body, spec.get("severity") or "error")
    if kind in ("unique", "not_null") and not body:
        return kind
    return entry


def _same_test(old, kind: str, spec: dict) -> bool:
    k, args, cfg = _parse_test(old)
    if k != kind or _sev(cfg) != (spec.get("severity") or "error"):
        return False
    if kind == "accepted_values":
        return [str(v) for v in args.get("values") or []] == [str(v).strip() for v in spec.get("values") or [] if str(v).strip()]
    if kind == "relationships":
        m = REF_RE.match(str(args.get("to") or ""))
        return (m.group(1) if m else None) == spec.get("to") and str(args.get("field") or "") == spec.get("field")
    return True


def _file_tests_key(data) -> str:
    for m in _entries(data, "models"):
        for c in (m.get("columns") or []) if isinstance(m, dict) else []:
            if isinstance(c, dict):
                if "data_tests" in c:
                    return "data_tests"
                if "tests" in c:
                    return "tests"
    return "data_tests"


def _apply_column(col: CommentedMap, spec: dict, default_key: str) -> bool:
    changed = False
    desc = _clean_text(spec.get("description"), f"Description of {col.get('name')}")
    if desc != str(col.get("description") or ""):
        changed = True
        if desc:
            col["description"] = desc
        else:
            col.pop("description", None)
    key = "data_tests" if "data_tests" in col else "tests" if "tests" in col else default_key
    tests = col.get(key)
    if not isinstance(tests, list):
        tests = CommentedSeq()
    wanted = {k: v for k, v in (spec.get("tests") or {}).items() if k in MANAGED_TESTS and v is not None and v is not False}
    out = CommentedSeq()
    seen = set()
    for t in tests:
        kind, _, _ = _parse_test(t)
        if kind in MANAGED_TESTS:
            if kind not in wanted or kind in seen:
                changed = True
                continue
            seen.add(kind)
            spec_k = wanted[kind] if isinstance(wanted[kind], dict) else {}
            if _same_test(t, kind, spec_k):
                out.append(t)
            else:
                out.append(_update_test(t, kind, spec_k))
                changed = True
        else:
            out.append(t)
    for kind in MANAGED_TESTS:
        if kind in wanted and kind not in seen:
            out.append(_new_test(kind, wanted[kind] if isinstance(wanted[kind], dict) else {}))
            changed = True
    if changed:
        if out:
            if isinstance(tests, CommentedSeq) and tests.fa.flow_style() and all(isinstance(x, str) for x in out):
                out.fa.set_flow_style()
            col[key] = out
        else:
            col.pop(key, None)
            col.pop("tests" if key == "data_tests" else "data_tests", None)
    return changed


def set_model_props(project: Path, name: str, props: dict) -> dict:
    path, exists = props_file_for(project, name)
    data = load(path) if path.is_file() else None
    if data is None:
        data = CommentedMap()
        data["version"] = 2
    models = data.get("models")
    if not isinstance(models, list):
        models = data["models"] = CommentedSeq()
    entry = _find_entry(data, "models", name)
    if entry is None:
        entry = CommentedMap()
        entry["name"] = name
        models.append(entry)

    if "description" in props:
        desc = _clean_text(props.get("description"), "Model description")
        if desc:
            entry["description"] = desc
        else:
            entry.pop("description", None)

    cfg = entry.get("config")
    if not isinstance(cfg, dict):
        cfg = CommentedMap()
    if "materialized" in props:
        mat = props.get("materialized") or None
        if mat is not None and mat not in MATERIALIZATIONS:
            raise StudioError(f"Materialization must be one of {', '.join(MATERIALIZATIONS)}.")
        if mat:
            cfg["materialized"] = mat
        else:
            cfg.pop("materialized", None)
    if "unique_key" in props:
        uk = props.get("unique_key") or None
        if uk:
            cfg["unique_key"] = check_ident(uk, "unique key")
        else:
            cfg.pop("unique_key", None)
    if "tags" in props:
        tags = [check_ident(str(t).strip(), "tag") for t in props.get("tags") or [] if str(t).strip()]
        entry.pop("tags", None)
        if tags:
            seq = CommentedSeq(tags)
            seq.fa.set_flow_style()
            cfg["tags"] = seq
        else:
            cfg.pop("tags", None)
    if cfg:
        entry["config"] = cfg
    else:
        entry.pop("config", None)

    if "columns" in props:
        default_key = _file_tests_key(data)
        cols = entry.get("columns")
        if not isinstance(cols, list):
            cols = CommentedSeq()
        by_name = {str(c.get("name")): c for c in cols if isinstance(c, dict)}
        for spec in props.get("columns") or []:
            cname = check_ident(str(spec.get("name") or ""), "column name")
            col = by_name.get(cname)
            has_content = bool(str(spec.get("description") or "").strip()) or any(
                v is not None and v is not False for v in (spec.get("tests") or {}).values())
            if col is None:
                if not has_content:
                    continue
                col = CommentedMap()
                col["name"] = cname
                cols.append(col)
                by_name[cname] = col
            if _apply_column(col, spec, default_key) and col.fa.flow_style():
                col.fa.set_block_style()
        if cols:
            entry["columns"] = cols

    save(path, data)
    return get_model_props(project, name)


# ── sources ──────────────────────────────────────────────────────────
def add_source(project: Path, source: str, schema: str, tables: list, description: str = "") -> dict:
    check_ident(source, "source name")
    check_ident(schema, "schema")
    names = []
    for t in tables or []:
        tname = t.get("name") if isinstance(t, dict) else t
        names.append(check_ident(str(tname or ""), "table name"))
    if not names:
        raise StudioError("Pick at least one table.")
    desc = _clean_text(description, "Source description")

    target, data = None, None
    for p in yml_files(project):
        d = load(p)
        if d is not None and _find_entry(d, "sources", source) is not None:
            target, data = p, d
            break
    if target is None:
        for p in yml_files(project):
            d = load(p)
            if d is not None and isinstance(d.get("sources"), list):
                target, data = p, d
                break
    if target is None:
        base = safe_path(project, model_paths(project)[0])
        target = base / "_sources.yml"
        data = CommentedMap()
        data["version"] = 2
    srcs = data.get("sources")
    if not isinstance(srcs, list):
        srcs = data["sources"] = CommentedSeq()
    entry = _find_entry(data, "sources", source)
    if entry is None:
        entry = CommentedMap()
        entry["name"] = source
        if desc:
            entry["description"] = desc
        entry["schema"] = schema
        entry["tables"] = CommentedSeq()
        srcs.append(entry)
    elif desc and not entry.get("description"):
        entry["description"] = desc
    tbls = entry.get("tables")
    if not isinstance(tbls, list):
        tbls = entry["tables"] = CommentedSeq()
    have = {str(t.get("name")) for t in tbls if isinstance(t, dict)}
    added = []
    for n in names:
        if n not in have:
            t = CommentedMap()
            t["name"] = n
            tbls.append(t)
            added.append(n)
    save(target, data)
    return {"file": target.relative_to(project).as_posix(), "source": source, "added": added,
            "existing": [n for n in names if n not in added]}
