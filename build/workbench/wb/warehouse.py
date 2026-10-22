"""The local Postgres warehouse (``bsp_edw``): catalogue, read-only queries
and bulk loads into the ``landing`` schema.

psycopg2 only — pandas.to_sql/read_sql don't work with the image's
SQLAlchemy 1.4. Loads stream CSV through ``COPY … FROM STDIN``.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from typing import Any, Iterator

from . import config
from .tabular import jsonable

LANDING = "landing"
SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
READ_KEYWORDS = {"select", "with", "table", "values", "explain", "show"}


class WarehouseError(RuntimeError):
    """A warehouse problem worth showing to the analyst."""


def _psycopg2():
    import psycopg2
    import psycopg2.extras  # noqa: F401
    import psycopg2.sql  # noqa: F401
    return psycopg2


def connect(*, readonly: bool = False, timeout_s: int | None = None):
    pg = _psycopg2()
    p = config.pg_params()
    if not p["password"]:
        raise WarehouseError("PG_PASS is not set — start the workspace from Studio so the warehouse password is injected")
    opts = []
    if readonly:
        opts.append("-c default_transaction_read_only=on")
    if timeout_s:
        opts.append(f"-c statement_timeout={int(timeout_s) * 1000}")
    try:
        return pg.connect(host=p["host"], port=p["port"], dbname=p["dbname"], user=p["user"],
                          password=p["password"], connect_timeout=5, application_name="workbench",
                          options=" ".join(opts) or None)
    except pg.OperationalError as exc:
        raise WarehouseError(f"can't reach the warehouse at {p['host']}:{p['port']} — {_pgmsg(exc)}") from exc


def _pgmsg(exc: Exception) -> str:
    msg = getattr(exc, "pgerror", None) or str(exc)
    msg = re.sub(r"password=\S+", "password=***", str(msg))
    return msg.strip().splitlines()[0][:500] if msg.strip() else type(exc).__name__


def friendly(exc: Exception) -> str:
    return _pgmsg(exc)


@contextmanager
def readonly_cursor(timeout_s: int | None = None, name: str | None = None) -> Iterator[Any]:
    """A cursor inside a READ ONLY transaction with a statement timeout."""
    conn = connect(readonly=True, timeout_s=timeout_s or config.QUERY_TIMEOUT_S)
    try:
        with conn.cursor() as c:
            c.execute("SET TRANSACTION READ ONLY")
            c.execute("SET LOCAL statement_timeout = %s", (f"{int(timeout_s or config.QUERY_TIMEOUT_S)}s",))
        cur = conn.cursor(name=name) if name else conn.cursor()
        try:
            yield cur
        finally:
            cur.close()
        conn.rollback()
    finally:
        conn.close()


def status() -> dict:
    p = config.pg_params()
    out = {"host": p["host"], "port": p["port"], "database": p["dbname"], "user": p["user"],
           "configured": bool(p["password"]), "ok": False}
    if not p["password"]:
        out["error"] = "PG_PASS is not set"
        return out
    try:
        with readonly_cursor(timeout_s=5) as c:
            c.execute("SELECT current_setting('server_version')")
            out["version"] = c.fetchone()[0]
            out["ok"] = True
    except WarehouseError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = friendly(exc)
    return out


# ── catalogue ───────────────────────────────────────────────────────────
_REL_SQL = """
SELECT n.nspname, c.relname, c.relkind,
       CASE WHEN c.relkind IN ('r','p','m') THEN
            COALESCE(NULLIF(c.reltuples, -1)::bigint, s.n_live_tup) END AS approx_rows,
       CASE WHEN c.relkind IN ('r','p','m') THEN pg_total_relation_size(c.oid) END AS bytes,
       obj_description(c.oid, 'pg_class') AS comment
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
 WHERE c.relkind IN ('r','p','v','m','f')
   AND n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
   AND n.nspname NOT LIKE 'pg\\_%%'
   AND has_schema_privilege(n.oid, 'USAGE')
   {extra}
 ORDER BY n.nspname, c.relname
"""
KINDS = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}


def schemas() -> list[dict]:
    with readonly_cursor(timeout_s=20) as c:
        c.execute("""SELECT n.nspname FROM pg_namespace n
                      WHERE n.nspname NOT IN ('pg_catalog','information_schema','pg_toast')
                        AND n.nspname NOT LIKE 'pg\\_%%' AND has_schema_privilege(n.oid, 'USAGE')
                      ORDER BY 1""")
        names = [r[0] for r in c.fetchall()]
        c.execute(_REL_SQL.format(extra=""))
        rels = c.fetchall()
    by: dict[str, dict] = {n: {"schema": n, "tables": 0, "views": 0, "bytes": 0} for n in names}
    for sch, _name, kind, _rows, size, _cm in rels:
        d = by.setdefault(sch, {"schema": sch, "tables": 0, "views": 0, "bytes": 0})
        d["views" if kind == "v" else "tables"] += 1
        d["bytes"] += size or 0
    return list(by.values())


def tables(schema: str | None = None) -> list[dict]:
    with readonly_cursor(timeout_s=20) as c:
        if schema:
            c.execute(_REL_SQL.format(extra="AND n.nspname = %s"), (schema,))
        else:
            c.execute(_REL_SQL.format(extra=""))
        rows = c.fetchall()
    return [{"schema": s, "name": n, "kind": KINDS.get(k, k), "approx_rows": r, "bytes": b, "comment": cm}
            for s, n, k, r, b, cm in rows]


def table_exists(schema: str, name: str) -> bool:
    with readonly_cursor(timeout_s=10) as c:
        c.execute("SELECT to_regclass(%s) IS NOT NULL", (f'{_q(schema)}.{_q(name)}',))
        return bool(c.fetchone()[0])


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def columns(schema: str, name: str) -> list[dict]:
    with readonly_cursor(timeout_s=10) as c:
        c.execute("""SELECT column_name, data_type, is_nullable = 'YES'
                       FROM information_schema.columns
                      WHERE table_schema = %s AND table_name = %s
                      ORDER BY ordinal_position""", (schema, name))
        return [{"name": n, "type": t, "nullable": nl} for n, t, nl in c.fetchall()]


def table_detail(schema: str, name: str, rows: int = 50, count: bool = True) -> dict:
    pg = _psycopg2()
    ident = pg.sql.SQL("{}.{}").format(pg.sql.Identifier(schema), pg.sql.Identifier(name))
    cols = columns(schema, name)
    if not cols:
        raise WarehouseError(f"{schema}.{name} not found (or you can't read it)")
    out: dict[str, Any] = {"schema": schema, "name": name, "columns": cols, "row_count": None}
    with readonly_cursor(timeout_s=30) as c:
        c.execute(pg.sql.SQL("SELECT * FROM {} LIMIT %s").format(ident), (int(rows),))
        out["rows"] = [[jsonable(v) for v in r] for r in c.fetchall()]
    if count:
        try:
            with readonly_cursor(timeout_s=10) as c:
                c.execute(pg.sql.SQL("SELECT count(*) FROM {}").format(ident))
                out["row_count"] = c.fetchone()[0]
        except Exception:  # noqa: BLE001 — big views can time out; the estimate is shown instead
            out["row_count"] = None
    return out


# ── read-only SQL ───────────────────────────────────────────────────────
def check_select(sql: str) -> str:
    """Accept exactly one read statement (SELECT / WITH / TABLE / VALUES /
    EXPLAIN / SHOW). Comments and string literals are skipped when looking
    for a second statement, so ``SELECT 1; DROP …`` is rejected."""
    if not isinstance(sql, str) or not sql.strip():
        raise WarehouseError("write a SELECT query first")
    if len(sql) > 200_000:
        raise WarehouseError("query is too long")
    code = _strip(sql).strip()
    body = code.rstrip().rstrip(";").rstrip()
    if ";" in body:
        raise WarehouseError("run one statement at a time (remove the extra ';')")
    m = re.match(r"\(*\s*([A-Za-z]+)", body)
    if not m or m.group(1).lower() not in READ_KEYWORDS:
        raise WarehouseError("only read queries are allowed here (SELECT … / WITH …). "
                             "Use a notebook or dbt to change data.")
    return sql.strip().rstrip(";").rstrip()


def _strip(sql: str) -> str:
    """Remove comments and blank out string / quoted-identifier contents."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            continue
        if sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            out.append(" ")
            continue
        if ch in ("'", '"'):
            j = i + 1
            while j < n:
                if sql[j] == ch:
                    if j + 1 < n and sql[j + 1] == ch:
                        j += 2
                        continue
                    break
                j += 1
            out.append(ch + ch)
            i = j + 1
            continue
        if ch == "$":
            m = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                j = sql.find(tag, i + len(tag))
                out.append("''")
                i = n if j < 0 else j + len(tag)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def table_sql(schema: str, name: str) -> str:
    return f"SELECT * FROM {_q(schema)}.{_q(name)}"


def run_query(sql: str, limit: int = 200) -> dict:
    sql = check_select(sql)
    limit = max(1, min(int(limit), 5000))
    pg = _psycopg2()
    try:
        with readonly_cursor() as c:
            c.execute(sql)
            if c.description is None:
                return {"columns": [], "rows": [], "truncated": False}
            cols = [{"name": d.name, "type": _type_name(d.type_code)} for d in c.description]
            data = c.fetchmany(limit + 1)
    except pg.Error as exc:
        raise WarehouseError(friendly(exc)) from exc
    return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in data[:limit]],
            "truncated": len(data) > limit}


_OIDS = {16: "boolean", 20: "bigint", 21: "smallint", 23: "integer", 25: "text", 700: "real", 701: "double",
         1043: "varchar", 1082: "date", 1114: "timestamp", 1184: "timestamptz", 1700: "numeric",
         114: "json", 3802: "jsonb", 1083: "time", 2950: "uuid", 18: "char", 1042: "char"}


def _type_name(oid: int) -> str:
    return _OIDS.get(oid, "other")


def stream_query(sql: str, batch: int = 5000, timeout_s: int | None = None) -> Iterator[tuple[list[dict], list[tuple]]]:
    """Yield (columns, rows) batches from a read-only server-side cursor."""
    sql = check_select(sql)
    pg = _psycopg2()
    try:
        with readonly_cursor(timeout_s=timeout_s or config.EXPORT_TIMEOUT_S, name="wb_export") as c:
            c.itersize = batch
            c.execute(sql)
            cols = None
            while True:
                rows = c.fetchmany(batch)
                if cols is None:
                    cols = [{"name": d.name, "type": _type_name(d.type_code)} for d in (c.description or [])]
                if not rows:
                    if cols is not None:
                        yield cols, []
                    return
                yield cols, rows
    except pg.Error as exc:
        raise WarehouseError(friendly(exc)) from exc


# ── loads into landing ─────────────────────────────────────────────────
def check_table_name(name: str) -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise WarehouseError("table names use lower-case letters, digits and _ and start with a letter (max 63)")
    return name


def drop_landing_table(name: str) -> None:
    pg = _psycopg2()
    name = check_table_name(name)
    conn = connect()
    try:
        with conn, conn.cursor() as c:
            c.execute(pg.sql.SQL("DROP TABLE {}.{}").format(pg.sql.Identifier(LANDING), pg.sql.Identifier(name)))
    except pg.errors.DependentObjectsStillExist as exc:
        raise WarehouseError(f"other objects (for example dbt models) depend on landing.{name}; "
                             "drop or change those first") from exc
    except pg.Error as exc:
        raise WarehouseError(friendly(exc)) from exc
    finally:
        conn.close()


class Loader:
    """Create/replace/append a landing table and COPY CSV data into it, in
    one transaction (readers keep seeing the old table until commit)."""

    def __init__(self, table: str, columns: list[tuple[str, str]], mode: str, schema: str = LANDING):
        if mode not in ("replace", "append"):
            raise WarehouseError("mode must be replace or append")
        self.pg = _psycopg2()
        self.schema, self.table, self.mode = schema, check_table_name(table), mode
        self.columns = columns  # [(name, postgres type)]
        self.conn = None
        self.rows = 0

    def __enter__(self):
        sql = self.pg.sql
        self.conn = connect()
        c = self.conn.cursor()
        tid = sql.SQL("{}.{}").format(sql.Identifier(self.schema), sql.Identifier(self.table))
        try:
            c.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            c.execute("SELECT column_name, data_type FROM information_schema.columns "
                      "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                      (self.schema, self.table))
            existing = c.fetchall()
            wanted = [(n, t) for n, t in self.columns]
            if self.mode == "append" and existing:
                have = {n for n, _ in existing}
                missing = [n for n, _ in wanted if n not in have]
                if missing:
                    raise WarehouseError(f"{self.schema}.{self.table} has no column(s) {', '.join(missing)} — "
                                         "choose 'Replace' or rename the columns to match")
            elif existing and self.mode == "replace" and _same_shape(existing, wanted):
                c.execute(sql.SQL("TRUNCATE {}").format(tid))  # keeps dependent dbt views intact
            else:
                if existing:
                    c.execute(sql.SQL("DROP TABLE {}").format(tid))
                c.execute(sql.SQL("CREATE TABLE {} ({})").format(tid, sql.SQL(", ").join(
                    sql.SQL("{} {}").format(sql.Identifier(n), sql.SQL(t)) for n, t in wanted)))
        except self.pg.errors.DependentObjectsStillExist as exc:
            self.conn.rollback()
            self.conn.close()
            raise WarehouseError(f"the columns of {self.schema}.{self.table} changed and other objects "
                                 "(e.g. dbt views) depend on it — keep the same columns, or drop those views first") from exc
        except self.pg.Error as exc:
            self.conn.rollback()
            self.conn.close()
            raise WarehouseError(friendly(exc)) from exc
        except Exception:
            self.conn.rollback()
            self.conn.close()
            raise
        self._cur = c
        self._tid = tid
        return self

    def copy_csv(self, fh) -> int:
        """COPY one CSV stream (no header; NULL = unquoted empty)."""
        sql = self.pg.sql
        stmt = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT csv)").format(
            self._tid, sql.SQL(", ").join(sql.Identifier(n) for n, _ in self.columns))
        try:
            self._cur.copy_expert(stmt.as_string(self.conn), fh, size=1 << 20)
        except self.pg.Error as exc:
            raise WarehouseError(friendly(exc)) from exc
        n = self._cur.rowcount if self._cur.rowcount and self._cur.rowcount > 0 else 0
        self.rows += n
        return n

    def __exit__(self, et, ev, tb):
        try:
            if et is None:
                c = self._cur
                c.execute(self.pg.sql.SQL("ANALYZE {}").format(self._tid))
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
        return False


def _same_shape(existing: list[tuple[str, str]], wanted: list[tuple[str, str]]) -> bool:
    norm = {"double precision": "double precision", "bigint": "bigint", "text": "text", "boolean": "boolean",
            "date": "date", "timestamp": "timestamp without time zone", "jsonb": "jsonb"}
    return [(n, t) for n, t in existing] == [(n, norm.get(t, t)) for n, t in wanted]
