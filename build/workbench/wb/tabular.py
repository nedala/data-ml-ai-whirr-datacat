"""Read tabular files (CSV/TSV, Parquet, Excel, JSON) through DuckDB for
preview, type inference and loading.

Column types shown to analysts are a small, friendly set; each maps to a
DuckDB cast (used while reading) and a Postgres column type (used when
creating the landing table).
"""
from __future__ import annotations

import datetime as dt
import decimal
import math
import re
from pathlib import Path
from typing import Any

FORMATS = {
    ".csv": "csv", ".tsv": "csv", ".txt": "csv",
    ".parquet": "parquet", ".pq": "parquet",
    ".xlsx": "excel", ".xlsm": "excel", ".xls": "excel",
    ".json": "json", ".jsonl": "json", ".ndjson": "json",
}

# friendly type → (DuckDB cast target, Postgres type)
TYPES: dict[str, tuple[str, str]] = {
    "text": ("VARCHAR", "text"),
    "integer": ("BIGINT", "bigint"),
    "number": ("DOUBLE", "double precision"),
    "boolean": ("BOOLEAN", "boolean"),
    "date": ("DATE", "date"),
    "timestamp": ("TIMESTAMP", "timestamp"),
    "json": ("JSON", "jsonb"),
}
ENCODINGS = ("utf-8", "latin-1", "utf-16")
DELIMS = {"auto": None, "comma": ",", "semicolon": ";", "tab": "\t", "pipe": "|"}
EXCEL_MAX_BYTES = 200 * 1024 ** 2


class TabularError(ValueError):
    pass


def fmt_of(path: Path) -> str | None:
    return FORMATS.get(path.suffix.lower())


def friendly_type(duck_type: str) -> str:
    t = str(duck_type).upper()
    if any(k in t for k in ("STRUCT", "MAP", "[]", "LIST", "UNION")) or t == "JSON":
        return "json"
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT"):
        return "integer"
    if t.startswith(("DECIMAL", "NUMERIC")) or t in ("FLOAT", "DOUBLE", "REAL"):
        return "number"
    if t == "BOOLEAN":
        return "boolean"
    if t == "DATE":
        return "date"
    if t.startswith("TIMESTAMP"):
        return "timestamp"
    return "text"


def lit(value: str) -> str:
    """A DuckDB string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def sql_name(name: str, used: set[str] | None = None) -> str:
    """A Postgres-friendly snake_case identifier (≤ 63 chars, unique in *used*)."""
    s = re.sub(r"[^0-9a-zA-Z]+", "_", str(name or "").strip()).strip("_").lower()
    s = re.sub(r"_+", "_", s) or "column"
    if s[0].isdigit():
        s = f"c_{s}"
    s = s[:63]
    if used is not None:
        base, i = s, 2
        while s in used:
            suffix = f"_{i}"
            s = base[: 63 - len(suffix)] + suffix
            i += 1
        used.add(s)
    return s


def table_name(name: str) -> str:
    return sql_name(Path(str(name)).stem if "." in str(name) else name)


def excel_sheets(path: Path) -> list[str]:
    if path.suffix.lower() == ".xls":
        import pandas as pd
        return list(pd.ExcelFile(path).sheet_names)
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def _excel_rows_estimate(path: Path, sheet: str) -> int | None:
    if path.suffix.lower() == ".xls":
        return None
    try:
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return wb[sheet].max_row
        finally:
            wb.close()
    except Exception:  # noqa: BLE001
        return None


def _normalise_frame(df):
    """Make an Excel frame safe for DuckDB: unique string column names and
    mixed-type object columns turned into text."""
    import pandas as pd
    cols, used = [], set()
    for i, c in enumerate(df.columns):
        name = str(c).strip() if c is not None and not (isinstance(c, float) and math.isnan(c)) else ""
        if not name or name.startswith("Unnamed:"):
            name = f"column_{i + 1}"
        base, n = name, 2
        while name in used:
            name = f"{base}_{n}"
            n += 1
        used.add(name)
        cols.append(name)
    df.columns = cols
    for c in df.columns:
        if df[c].dtype == object:
            kinds = {type(v) for v in df[c].dropna().tolist()[:5000]}
            if len(kinds) > 1 or (kinds and not kinds <= {str, dt.datetime, dt.date, bool}):
                df[c] = df[c].map(lambda v: None if v is None or (isinstance(v, float) and math.isnan(v)) else str(v))
            elif kinds == {dt.date}:
                df[c] = pd.to_datetime(df[c])
    return df


def relation(con, path: Path, fmt: str | None = None, *, sheet: str | None = None, header: bool = True,
             delimiter: str = "auto", encoding: str = "utf-8", skip_rows: int = 0, nrows: int | None = None):
    """A DuckDB relation over the file (nothing is read until it is queried)."""
    fmt = fmt or fmt_of(path)
    if fmt is None:
        raise TabularError(f"{path.suffix or 'this file type'} isn't a table format "
                           "(use CSV, TSV, Parquet, Excel or JSON)")
    if fmt == "csv":
        if encoding not in ENCODINGS:
            raise TabularError(f"encoding must be one of {', '.join(ENCODINGS)}")
        args = [lit(str(path)), f"header={'true' if header else 'false'}"]
        sep = DELIMS.get(delimiter) if delimiter in DELIMS else None
        if sep is None and path.suffix.lower() == ".tsv":
            sep = "\t"
        if sep:
            args.append(f"delim={lit(sep)}")
        if encoding != "utf-8":
            args.append(f"encoding={lit(encoding)}")
        if skip_rows:
            args.append(f"skip={int(skip_rows)}")
        try:
            return con.sql(f"SELECT * FROM read_csv({', '.join(args)})")
        except Exception as exc:  # noqa: BLE001
            raise TabularError(_friendly(exc, encoding)) from exc
    if fmt == "parquet":
        try:
            return con.sql(f"SELECT * FROM read_parquet({lit(str(path))})")
        except Exception as exc:  # noqa: BLE001
            raise TabularError(f"couldn't read this Parquet file: {_first_line(exc)}") from exc
    if fmt == "json":
        try:
            return con.sql(f"SELECT * FROM read_json_auto({lit(str(path))})")
        except Exception as exc:  # noqa: BLE001
            raise TabularError(f"couldn't read this JSON as a table: {_first_line(exc)}") from exc
    if fmt == "excel":
        if path.stat().st_size > EXCEL_MAX_BYTES:
            raise TabularError("Excel files over 200 MB are too large — save the sheet as CSV first")
        import pandas as pd
        sheets = excel_sheets(path)
        if not sheets:
            raise TabularError("this workbook has no sheets")
        sh = sheet if sheet in sheets else sheets[0]
        df = pd.read_excel(path, sheet_name=sh, header=0 if header else None,
                           skiprows=int(skip_rows) or None, nrows=nrows)
        df = _normalise_frame(df)
        return con.from_df(df)
    raise TabularError(f"unsupported format {fmt}")


def _first_line(exc: Exception) -> str:
    return str(exc).strip().splitlines()[0][:400] if str(exc).strip() else type(exc).__name__


def _friendly(exc: Exception, encoding: str) -> str:
    msg = str(exc)
    if "unicode" in msg.lower() or "utf-8" in msg.lower() or "encoding" in msg.lower():
        return ("the file isn't valid " + encoding.upper() + " text — try another encoding "
                "(files saved from Excel are often Latin-1)")
    return f"couldn't read this CSV: {_first_line(exc)}"


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, decimal.Decimal):
        f = float(v)
        return f if math.isfinite(f) else str(v)
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    return str(v)


def inspect(path: Path, *, sheet: str | None = None, header: bool = True, delimiter: str = "auto",
            encoding: str = "utf-8", skip_rows: int = 0, rows: int = 50, count: bool = True) -> dict:
    """Columns with inferred types, the first *rows* rows, and (when cheap) the row count."""
    import duckdb
    fmt = fmt_of(path)
    if fmt is None:
        raise TabularError(f"{path.suffix or 'this file type'} isn't a table format")
    size = path.stat().st_size
    out: dict[str, Any] = {"format": fmt, "size": size, "sheets": None, "sheet": None}
    if fmt == "excel":
        out["sheets"] = excel_sheets(path)
        out["sheet"] = sheet if sheet in out["sheets"] else (out["sheets"][0] if out["sheets"] else None)
    con = duckdb.connect()
    try:
        rel = relation(con, path, fmt, sheet=out["sheet"], header=header, delimiter=delimiter,
                       encoding=encoding, skip_rows=skip_rows, nrows=max(rows, 1000) if fmt == "excel" else None)
        names, types = list(rel.columns), [str(t) for t in rel.types]
        try:
            data = rel.limit(rows).fetchall()
        except Exception as exc:  # noqa: BLE001
            raise TabularError(_friendly(exc, encoding) if fmt == "csv" else _first_line(exc)) from exc
        total = None
        if count:
            if fmt == "parquet" or (fmt in ("csv", "json") and size <= 200 * 1024 ** 2):
                try:
                    total = rel.aggregate("count(*)").fetchone()[0]
                except Exception:  # noqa: BLE001
                    total = None
            elif fmt == "excel":
                est = _excel_rows_estimate(path, out["sheet"])
                total = max(0, est - (1 if header else 0) - int(skip_rows or 0)) if est else None
    finally:
        con.close()
    used: set[str] = set()
    out["columns"] = [{"name": n, "source_type": t, "type": friendly_type(t), "target": sql_name(n, used)}
                      for n, t in zip(names, types)]
    out["rows"] = [[jsonable(v) for v in r] for r in data]
    out["total_rows"] = total
    return out


def select_list(columns: list[dict], source_types: dict[str, str]) -> tuple[str, list[tuple[str, str, str]]]:
    """Build the DuckDB projection for the chosen columns.
    columns: [{name, target, type, include}] → (sql, [(source, target, friendly)])"""
    parts, chosen, used = [], [], set()
    for c in columns:
        if not c.get("include", True):
            continue
        src = c.get("name")
        if src not in source_types:
            raise TabularError(f"unknown column {src!r}")
        ty = c.get("type") or friendly_type(source_types[src])
        if ty not in TYPES:
            raise TabularError(f"unknown type {ty!r} for column {src!r}")
        tgt = c.get("target") or src
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", tgt):
            raise TabularError(f"column name {tgt!r} must be lower-case letters, digits and _ (max 63)")
        if tgt in used:
            raise TabularError(f"column name {tgt!r} is used twice")
        used.add(tgt)
        parts.append(f"{cast_expr(qident(src), source_types[src], ty)} AS {qident(tgt)}")
        chosen.append((src, tgt, ty))
    if not parts:
        raise TabularError("choose at least one column")
    return ", ".join(parts), chosen


def cast_expr(col_sql: str, source_type: str, ty: str) -> str:
    if ty == "json":
        return f"CAST(to_json({col_sql}) AS VARCHAR)"
    if friendly_type(source_type) == "json" and ty == "text":
        return f"CAST(to_json({col_sql}) AS VARCHAR)"
    if ty == "text" and friendly_type(source_type) == "text":
        return col_sql
    if ty == "boolean" and friendly_type(source_type) == "text":
        # accept the usual spellings analysts meet in spreadsheets
        return (f"CASE WHEN lower(trim({col_sql})) IN ('true','t','yes','y','1') THEN TRUE "
                f"WHEN lower(trim({col_sql})) IN ('false','f','no','n','0') THEN FALSE ELSE NULL END")
    return f"TRY_CAST({col_sql} AS {TYPES[ty][0]})"
