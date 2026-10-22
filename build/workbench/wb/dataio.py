"""Import & Export: the job bodies behind /ingress/.

IN   file (lake / workspace / upload) → landing.<table>      load_file_job
     https URL → lake/landing/<file>                          fetch_url_job
     BigQuery table / query → landing.<table>                  bigquery_job
     Trino query (federated SQL) → landing.<table>             trino_job
OUT  warehouse SELECT → CSV / Excel / Parquet (download or lake/exports)  export_job
"""
from __future__ import annotations

import csv
import datetime as dt
import decimal
import io
import json
import os
import re
import ssl
import tempfile
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

from . import config, fsroots, tabular
from . import warehouse as W
from .jobs import Job, JobError

EXPORT_DIR = config.STATE_DIR / "exports"
EXPORT_TTL_S = 6 * 3600
FORMATS_OUT = {"csv": ".csv", "excel": ".xlsx", "parquet": ".parquet"}
EXCEL_MAX_ROWS = 1_048_575


class _Progress:
    """File wrapper that reports bytes read to a job (for COPY progress)."""

    def __init__(self, fh, job: Job, total: int, label: str):
        self.fh, self.job, self.total, self.label, self.done = fh, job, total, label, 0
        self._t = 0.0

    def read(self, n: int = -1):
        self.job.check_cancel()
        b = self.fh.read(n)
        self.done += len(b)
        if time.time() - self._t > 0.3:
            self._t = time.time()
            self.job.set_progress(self.done, self.total or None, self.label)
        return b

    def readline(self, n: int = -1):
        return self.read(n)


def _fmt_rows(n: int | None) -> str:
    return "?" if n is None else f"{n:,}"


# ── IN: file → landing ──────────────────────────────────────────────────
def load_file_job(root: str, rel: str, table: str, mode: str, columns: list[dict], options: dict):
    """Validate everything up front (errors become HTTP 422), then return the job body."""
    path = fsroots.resolve(root, rel)
    if not path.is_file():
        raise tabular.TabularError("choose a file, not a folder")
    fmt = tabular.fmt_of(path)
    if fmt is None:
        raise tabular.TabularError(f"{path.suffix or 'this file'} isn't a table format")
    table = W.check_table_name(table)
    if mode not in ("replace", "append"):
        raise tabular.TabularError("mode must be replace or append")
    opts = _read_opts(options)

    def fn(job: Job) -> dict:
        import duckdb
        job.log(f"==> Reading {fsroots.ROOT_LABELS[root]} › {rel}")
        con = duckdb.connect()
        tmpdir = tempfile.mkdtemp(prefix="wb-load-")
        tmp = Path(tmpdir) / "data.csv"
        try:
            rel_ = tabular.relation(con, path, fmt, **opts)
            src_types = dict(zip(rel_.columns, [str(t) for t in rel_.types]))
            used: set[str] = set()
            cols = columns or [{"name": n, "target": tabular.sql_name(n, used)} for n in rel_.columns]
            proj, chosen = tabular.select_list(cols, src_types)
            changed = [(s, t) for s, _tgt, t in chosen if t != tabular.friendly_type(src_types[s])]
            if changed:
                job.log("Checking type conversions…")
                checks = ", ".join(
                    f"count(*) FILTER (WHERE {tabular.qident(s)} IS NOT NULL AND "
                    f"({tabular.cast_expr(tabular.qident(s), src_types[s], t)}) IS NULL)" for s, t in changed)
                bad = rel_.aggregate(checks).fetchone()
                for (s, t), n in zip(changed, bad):
                    if n:
                        job.log(f"⚠ {n:,} value(s) in '{s}' couldn't be read as {t} and will be empty")
            job.check_cancel()
            job.log("Preparing data…")
            job.set_progress(0, None, "preparing")
            rel_.project(proj).write_csv(str(tmp), header=False)
            size = tmp.stat().st_size
            pg_cols = [(tgt, tabular.TYPES[t][1]) for _s, tgt, t in chosen]
            job.log(f"==> {'Replacing' if mode == 'replace' else 'Appending to'} landing.{table} "
                    f"({len(pg_cols)} columns)")
            with W.Loader(table, pg_cols, mode) as ld, open(tmp, "rb") as fh:
                ld.copy_csv(_Progress(fh, job, size, "loading"))
                rows = ld.rows
            job.set_progress(size, size, "done")
            job.log(f"✓ Loaded {rows:,} rows into landing.{table}")
            return {"table": f"{W.LANDING}.{table}", "rows": rows, "columns": [c for c, _ in pg_cols], "mode": mode}
        except (tabular.TabularError, W.WarehouseError) as exc:
            raise JobError(str(exc)) from exc
        finally:
            con.close()
            try:
                tmp.unlink(missing_ok=True)
                os.rmdir(tmpdir)
            except OSError:
                pass
    return fn


def _read_opts(o: dict | None) -> dict:
    o = o or {}
    enc = o.get("encoding") or "utf-8"
    if enc not in tabular.ENCODINGS:
        raise tabular.TabularError(f"encoding must be one of {', '.join(tabular.ENCODINGS)}")
    delim = o.get("delimiter") or "auto"
    if delim not in tabular.DELIMS:
        raise tabular.TabularError(f"delimiter must be one of {', '.join(tabular.DELIMS)}")
    skip = int(o.get("skip_rows") or 0)
    if not 0 <= skip <= 10000:
        raise tabular.TabularError("skip rows must be between 0 and 10000")
    sheet = o.get("sheet")
    return {"sheet": str(sheet) if sheet else None, "header": o.get("header", True) is not False,
            "delimiter": delim, "encoding": enc, "skip_rows": skip}


# ── IN: https URL → lake/landing ────────────────────────────────────────
def check_url(url: str) -> urllib.parse.SplitResult:
    if not isinstance(url, str) or len(url) > 4000:
        raise tabular.TabularError("enter a web address")
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise tabular.TabularError("only https:// addresses are supported")
    if u.username or u.password:
        raise tabular.TabularError("don't put credentials in the address")
    return u


def _url_filename(u: urllib.parse.SplitResult, cd: str | None) -> str:
    name = ""
    if cd:
        m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I) or re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            name = urllib.parse.unquote(m.group(1))
    if not name:
        name = urllib.parse.unquote(u.path.rstrip("/").rsplit("/", 1)[-1])
    return fsroots.safe_filename(name, default=f"{u.hostname}-download")


def fetch_url_job(url: str, folder: str = "landing"):
    u = check_url(url)
    dest_dir = fsroots.resolve("lake", folder)

    def fn(job: Job) -> dict:
        import httpx
        host = u.hostname
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
        verify: Any = ssl.create_default_context(cafile=cafile) if cafile and os.path.isfile(cafile) else True
        # Always through the egress proxy when there is one (NO_PROXY is ignored
        # on purpose: this must not become a way to reach internal services).
        kw: dict[str, Any] = {"follow_redirects": True, "timeout": httpx.Timeout(30.0, read=120.0), "verify": verify,
                              "headers": {"User-Agent": "DataPlatform-Workbench/1.0"}}
        if proxy:
            kw.update(proxy=proxy, trust_env=False)
        job.log(f"==> Downloading from {host}" + (" (via the egress proxy)" if proxy else ""))
        part = None
        try:
            with httpx.Client(**kw) as cli, cli.stream("GET", u.geturl()) as r:
                if r.status_code == 403 and proxy:
                    raise JobError(_blocked(host))
                if r.status_code >= 400:
                    raise JobError(f"{host} answered HTTP {r.status_code} {r.reason_phrase}")
                total = int(r.headers.get("content-length") or 0) or None
                if total and total > config.URL_MAX_BYTES:
                    raise JobError(f"file is {total / 1e9:.1f} GB — the limit is {config.URL_MAX_BYTES / 1e9:.1f} GB")
                name = _url_filename(urllib.parse.urlsplit(str(r.url)), r.headers.get("content-disposition"))
                fd, part = tempfile.mkstemp(dir=str(dest_dir), prefix=".download-", suffix=".part")
                got = 0
                last = 0.0
                with os.fdopen(fd, "wb") as fh:
                    for chunk in r.iter_bytes(1 << 20):
                        job.check_cancel()
                        got += len(chunk)
                        if got > config.URL_MAX_BYTES:
                            raise JobError("download is larger than the allowed size")
                        fh.write(chunk)
                        if time.time() - last > 0.3:
                            last = time.time()
                            job.set_progress(got, total, "downloading")
                final = fsroots.unique_name(dest_dir, name)
                os.replace(part, dest_dir / final)
                part = None
                os.chmod(dest_dir / final, 0o664)
                job.set_progress(got, got, "done")
                rel = f"{fsroots.clean_rel(folder)}/{final}"
                job.log(f"✓ Saved {got:,} bytes to Shared lake › {rel}")
                return {"root": "lake", "path": rel, "bytes": got, "name": final}
        except httpx.ProxyError as exc:
            raise JobError(_blocked(host) if "403" in str(exc) or "Forbidden" in str(exc)
                           else f"the egress proxy couldn't reach {host}: {exc}") from exc
        except httpx.ConnectError as exc:
            raise JobError(f"couldn't connect to {host}: {exc}") from exc
        except httpx.TimeoutException as exc:
            raise JobError(f"{host} took too long to answer") from exc
        finally:
            if part:
                try:
                    os.unlink(part)
                except OSError:
                    pass
    return fn


def _blocked(host: str) -> str:
    return (f"{host} is not on the egress allowlist, so the proxy blocked it. "
            "Ask your appliance admin to allow it in Studio → Egress, then try again.")


# ── IN: BigQuery / Trino → landing ──────────────────────────────────────
def _first(exc: Exception) -> str:
    s = str(exc).strip()
    return s.splitlines()[0][:400] if s else type(exc).__name__


def _pg_type_of_dtype(dtype) -> str:
    k = getattr(dtype, "kind", "O")
    if k in "iu":
        return "bigint"
    if k == "f":
        return "double precision"
    if k == "b":
        return "boolean"
    if k == "M":
        return "timestamp"
    return "text"


def _load_frames(job: Job, frames, table: str, mode: str, source: str) -> dict:
    """Stream pandas DataFrames into landing.<table> (COPY, one transaction)."""
    loader = None
    total = 0
    used: set[str] = set()
    try:
        for df in frames:
            job.check_cancel()
            if df is None or not len(df.columns):
                continue
            if loader is None:
                names = [tabular.sql_name(str(c), used) for c in df.columns]
                cols = [(n, _pg_type_of_dtype(df[c].dtype)) for n, c in zip(names, df.columns)]
                loader = W.Loader(table, cols, mode).__enter__()
                job.log(f"==> Writing to landing.{table} ({len(cols)} columns)")
            buf = io.StringIO()
            df.to_csv(buf, header=False, index=False)
            buf.seek(0)
            total += loader.copy_csv(buf)
            job.set_progress(total, None, f"{total:,} rows")
            job.log(f"  {total:,} rows")
        if loader is None:
            raise JobError("the query returned no rows — nothing to load")
        loader.__exit__(None, None, None)
    except BaseException as exc:
        if loader is not None and loader.conn is not None and not loader.conn.closed:
            loader.__exit__(type(exc), exc, None)
        if isinstance(exc, W.WarehouseError):
            raise JobError(str(exc)) from exc
        raise
    job.log(f"✓ Loaded {total:,} rows from {source} into landing.{table}")
    return {"table": f"{W.LANDING}.{table}", "rows": total, "mode": mode, "source": source}


def _check_target(table: str, mode: str) -> str:
    table = W.check_table_name(table)
    if mode not in ("replace", "append"):
        raise tabular.TabularError("mode must be replace or append")
    return table


_BQ_IDENT = re.compile(r"^[A-Za-z0-9_\-]{1,1024}$")


def _bq_ident(value: str, what: str) -> str:
    if not _BQ_IDENT.fullmatch(value or ""):
        raise W.WarehouseError(f"invalid BigQuery {what} name: {value!r}")
    return value


# BigQuery (enterprise EDW). Default: the ASSIGNED identity (Studio → Cloud
# identities → BigQuery · read: its key in this process's memory, its data
# projects and execution project). Principal switch: a named identity the
# signed-in user may act as — a short-lived token from Studio (access list
# checked there, the key never leaves Studio). `pr` = {identity, project,
# exec_project, user, session} from the request (dataio_api.principal()).
_PROJECT = re.compile(r"^[a-z][a-z0-9.:-]{3,62}$")


class PrincipalDenied(W.WarehouseError):
    """Studio refused the identity for this user (HTTP 403)."""
    status = 403


def bigquery_status() -> dict:
    p = config.bigquery_params()
    if not p:
        return {"configured": False, "hint": "An admin assigns a Google identity in Studio → Cloud identities → BigQuery · read"}
    return {"configured": True, **p}


def _project_arg(value: str | None) -> str:
    v = (value or "").strip()
    if v and not _PROJECT.fullmatch(v):
        raise W.WarehouseError(f"invalid Google Cloud project id: {v!r}")
    return v


def _bq_client(pr: dict | None = None):
    """(client, data project, acting-as metadata)."""
    pr = pr or {}
    p = config.bigquery_params()
    identity = (pr.get("identity") or "").strip()
    if not p and not identity:
        raise W.WarehouseError("BigQuery isn't connected — an admin assigns a Google identity in Studio → Cloud identities")
    try:
        from bsp_common import gcp
        from google.cloud import bigquery
    except ImportError as exc:
        raise W.WarehouseError(f"BigQuery client libraries are missing: {exc}") from exc
    project, exec_project = _project_arg(pr.get("project")), _project_arg(pr.get("exec_project"))
    if identity:
        try:
            with gcp.acting(pr.get("user", ""), session=pr.get("session", "")):
                creds = gcp.credentials("bigquery_read", identity=identity)
        except gcp.StudioTokenError as exc:
            if exc.status == 403:
                raise PrincipalDenied(str(exc)) from None
            raise W.WarehouseError(f"Studio could not issue a Google token: {exc}") from None
        meta = gcp.identity_meta(identity)
        project = project or getattr(creds, "bsp_default_project", "") or meta.get("project", "")
        exec_project = exec_project or project
        acting = {"identity": identity, "name": meta.get("name") or identity,
                  "principal": getattr(creds, "bsp_principal", "") or meta.get("principal", ""), "default": False}
    else:
        try:
            creds = gcp.credentials("bigquery_read")
        except Exception as exc:  # noqa: BLE001 — malformed key JSON, bad base64 …
            raise W.WarehouseError(f"the assigned Google identity is not usable: {_first(exc)}") from exc
        if creds is None:
            raise W.WarehouseError("BigQuery credentials could not be built (Studio → Cloud identities)")
        project = project or p["project"]
        exec_project = p["exec_project"] or project
        acting = {"identity": "", "name": p.get("identity", ""), "principal": p.get("principal", ""), "default": True}
    if not project:
        raise W.WarehouseError("no BigQuery project — pick one")
    try:
        client = bigquery.Client(project=exec_project, credentials=creds, location=(p or {}).get("location") or config._env("BQ_LOCATION", "US"))
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"BigQuery: {_first(exc)}") from exc
    return client, project, {**acting, "project": project, "exec_project": exec_project}


def bigquery_datasets(pr: dict | None = None) -> dict:
    client, project, acting = _bq_client(pr)
    try:
        return {"project": project, "acting": acting,
                "datasets": sorted(d.dataset_id for d in client.list_datasets(project=project, max_results=1000))}
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"BigQuery ({acting['principal'] or 'assigned identity'} on {project}): {_first(exc)}") from exc


def bigquery_tables(dataset: str, pr: dict | None = None) -> dict:
    client, project, acting = _bq_client(pr)
    _bq_ident(dataset, "dataset")
    try:
        out = []
        for t in client.list_tables(f"{project}.{dataset}", max_results=5000):
            out.append({"name": t.table_id, "type": t.table_type})
        return {"dataset": dataset, "project": project, "acting": acting, "tables": sorted(out, key=lambda t: t["name"])}
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"BigQuery: {_first(exc)}") from exc


def _bq_sql(sql: str | None, dataset: str | None, table: str | None, project: str) -> str:
    if sql and sql.strip():
        return W.check_select(sql)
    if not (dataset and table):
        raise W.WarehouseError("give a SELECT query, or a dataset and table")
    return f"SELECT * FROM `{project}.{_bq_ident(dataset, 'dataset')}.{_bq_ident(table, 'table')}`"


def bigquery_preview(sql: str | None = None, dataset: str | None = None, table: str | None = None,
                     limit: int = 50, pr: dict | None = None) -> dict:
    client, project, acting = _bq_client(pr)
    q = _bq_sql(sql, dataset, table, project)
    try:
        job = client.query(q)
        rows = job.result(max_results=limit, timeout=config.QUERY_TIMEOUT_S)
        cols = [{"name": f.name, "type": f.field_type} for f in rows.schema]
        data = [[tabular.jsonable(v) for v in r.values()] for r in rows]
        return {"columns": cols, "rows": data, "truncated": (rows.total_rows or 0) > len(data),
                "total_rows": rows.total_rows, "bytes_processed": job.total_bytes_processed, "acting": acting}
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"BigQuery: {_first(exc)}") from exc


def bigquery_job(sql: str | None, dataset: str | None, source_table: str | None, table: str, mode: str,
                 pr: dict | None = None):
    table = _check_target(table, mode)

    def fn(job: Job) -> dict:
        job.log("==> Connecting to BigQuery")
        try:
            client, project, acting = _bq_client(pr)
        except W.WarehouseError as exc:
            raise JobError(str(exc)) from exc
        job.log(f"Acting as {acting['principal'] or acting['name'] or 'the assigned identity'} · data project "
                f"{project} · jobs in {acting['exec_project']}")
        q = _bq_sql(sql, dataset, source_table, project)
        job.log("Running query…")
        try:
            result = client.query(q).result(timeout=config.EXPORT_TIMEOUT_S, page_size=50_000)
            frames = result.to_dataframe_iterable()
        except Exception as exc:  # noqa: BLE001
            raise JobError(f"BigQuery: {_first(exc)}") from exc
        return _load_frames(job, frames, table, mode, "BigQuery")
    return fn


# Trino (federated SQL over the warehouse, BigQuery and lake files).
def _trino_connect():
    try:
        import trino
    except ImportError as exc:
        raise W.WarehouseError("the trino client library is missing") from exc
    p = config.trino_params()
    return trino.dbapi.connect(host=p["host"], port=p["port"], user=p["user"], catalog=p["catalog"],
                               http_scheme=p["http_scheme"], source=p["source"],
                               request_timeout=config.QUERY_TIMEOUT_S)


def trino_status() -> dict:
    p = config.trino_params()
    try:
        import socket
        with socket.create_connection((p["host"], p["port"]), timeout=2):
            pass
    except OSError:
        return {"configured": False, "host": f"{p['host']}:{p['port']}",
                "hint": "Start the Trino capability in Studio"}
    return {"configured": True, "host": f"{p['host']}:{p['port']}", "catalog": p["catalog"]}


def _trino_rows(sql: str, limit: int | None = None) -> tuple[list[str], list[tuple]]:
    conn = _trino_connect()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchmany(limit) if limit else cur.fetchall()
        cols = [d[0] for d in (cur.description or [])]
        return cols, rows
    finally:
        conn.close()


def trino_catalogs() -> dict:
    try:
        _, rows = _trino_rows("SHOW CATALOGS")
    except W.WarehouseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"Trino: {_first(exc)}") from exc
    return {"catalogs": sorted(r[0] for r in rows if r[0] not in ("system", "jmx"))}


_TRINO_IDENT = re.compile(r"^[A-Za-z0-9_\-]{1,255}$")


def trino_tables(catalog: str, schema: str | None = None) -> dict:
    for v in filter(None, (catalog, schema)):
        if not _TRINO_IDENT.fullmatch(v):
            raise W.WarehouseError(f"invalid Trino name: {v!r}")
    try:
        if schema:
            _, rows = _trino_rows(f'SHOW TABLES FROM "{catalog}"."{schema}"')
            return {"catalog": catalog, "schema": schema, "tables": sorted(r[0] for r in rows)}
        _, rows = _trino_rows(f'SHOW SCHEMAS FROM "{catalog}"')
        return {"catalog": catalog, "schemas": sorted(r[0] for r in rows if r[0] != "information_schema")}
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"Trino: {_first(exc)}") from exc


def trino_preview(sql: str, limit: int = 50) -> dict:
    sql = W.check_select(sql)
    try:
        cols, rows = _trino_rows(sql, limit)
    except W.WarehouseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise W.WarehouseError(f"Trino: {_first(exc)}") from exc
    return {"columns": [{"name": c, "type": ""} for c in cols],
            "rows": [[tabular.jsonable(v) for v in r] for r in rows], "truncated": len(rows) >= limit}


def trino_job(sql: str, table: str, mode: str):
    sql = W.check_select(sql)
    table = _check_target(table, mode)

    def fn(job: Job) -> dict:
        import pandas as pd
        job.log("==> Connecting to Trino")
        try:
            conn = _trino_connect()
        except W.WarehouseError as exc:
            raise JobError(str(exc)) from exc
        try:
            cur = conn.cursor()
            job.log("Running query…")
            try:
                cur.execute(sql)
                first = cur.fetchmany(50_000)
            except Exception as exc:  # noqa: BLE001
                raise JobError(f"Trino: {_first(exc)}") from exc
            cols = [d[0] for d in (cur.description or [])]

            def frames():
                batch = first
                while batch:
                    yield pd.DataFrame.from_records(batch, columns=cols)
                    batch = cur.fetchmany(50_000)
            return _load_frames(job, frames(), table, mode, "Trino")
        finally:
            conn.close()
    return fn


# ── OUT: warehouse → file ───────────────────────────────────────────────
def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat(sep=" ") if isinstance(v, dt.datetime) else v.isoformat()
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return v


def _arrow_type(pg_type: str):
    import pyarrow as pa
    return {"boolean": pa.bool_(), "bigint": pa.int64(), "integer": pa.int64(), "smallint": pa.int64(),
            "real": pa.float64(), "double": pa.float64(), "numeric": pa.float64(), "date": pa.date32(),
            "timestamp": pa.timestamp("us"), "timestamptz": pa.timestamp("us", tz="UTC")}.get(pg_type, pa.string())


def _arrow_value(v: Any, pg_type: str) -> Any:
    if v is None:
        return None
    if pg_type == "numeric" and isinstance(v, decimal.Decimal):
        return float(v)
    if pg_type in ("real", "double", "boolean", "bigint", "integer", "smallint", "date", "timestamp", "timestamptz"):
        return v
    if isinstance(v, (dict, list)):
        return json.dumps(v, default=str)
    if isinstance(v, (bytes, memoryview)):
        return bytes(v).hex()
    return str(v)


def export_name(name: str | None, fmt: str) -> str:
    ext = FORMATS_OUT[fmt]
    base = fsroots.safe_filename(name or "", default=f"export-{time.strftime('%Y%m%d-%H%M%S')}")
    if base.lower().endswith(ext):
        base = base[: -len(ext)]
    for e in FORMATS_OUT.values():
        if base.lower().endswith(e):
            base = base[: -len(e)]
    return (base or "export") + ext


def _cleanup_exports() -> None:
    try:
        for p in EXPORT_DIR.iterdir():
            if time.time() - p.stat().st_mtime > EXPORT_TTL_S:
                p.unlink(missing_ok=True)
    except OSError:
        pass


def export_job(sql: str, fmt: str, destination: str, filename: str | None, overwrite: bool = False):
    sql = W.check_select(sql)
    if fmt not in FORMATS_OUT:
        raise tabular.TabularError("format must be csv, excel or parquet")
    if destination not in ("download", "lake"):
        raise tabular.TabularError("destination must be download or lake")
    name = export_name(filename, fmt)
    lake_dir = None
    if destination == "lake":
        lake_dir = fsroots.resolve("lake", "exports", must_exist=False)
        lake_dir.mkdir(parents=True, exist_ok=True)
        if (lake_dir / name).exists() and not overwrite:
            raise fsroots.PathError(f"exports/{name} already exists", 409)

    def fn(job: Job) -> dict:
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        _cleanup_exports()
        token = uuid.uuid4().hex
        out_dir = lake_dir if lake_dir else EXPORT_DIR
        tmp = out_dir / f".export-{token}.part"
        job.log(f"==> Exporting to {name} ({fmt})")
        rows = 0
        try:
            rows = _write(sql, fmt, tmp, job)
            if lake_dir:
                final = lake_dir / name
                os.replace(tmp, final)
                os.chmod(final, 0o664)
                job.log(f"✓ {rows:,} rows saved to Shared lake › exports/{name}")
                return {"rows": rows, "root": "lake", "path": f"exports/{name}", "name": name}
            final = EXPORT_DIR / f"{token}{FORMATS_OUT[fmt]}"
            os.replace(tmp, final)
            (EXPORT_DIR / f"{token}.json").write_text(json.dumps({"name": name, "fmt": fmt}))
            job.log(f"✓ {rows:,} rows ready to download")
            return {"rows": rows, "token": token, "name": name, "bytes": final.stat().st_size}
        except W.WarehouseError as exc:
            raise JobError(str(exc)) from exc
        finally:
            tmp.unlink(missing_ok=True)
    return fn


def _write(sql: str, fmt: str, path: Path, job: Job) -> int:
    rows = 0
    cap = config.EXPORT_MAX_ROWS if fmt != "excel" else min(config.EXPORT_MAX_ROWS, EXCEL_MAX_ROWS)

    def tick():
        job.check_cancel()
        job.set_progress(rows, None, f"{rows:,} rows")

    batches = W.stream_query(sql)
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            header = False
            for cols, data in batches:
                if not header:
                    w.writerow([c["name"] for c in cols])
                    header = True
                for r in data:
                    w.writerow([_cell(v) for v in r])
                rows += len(data)
                _cap(rows, cap, fmt)
                tick()
        return rows
    if fmt == "excel":
        import xlsxwriter
        wb = xlsxwriter.Workbook(str(path), {"constant_memory": True, "strings_to_numbers": False,
                                             "strings_to_formulas": False, "strings_to_urls": False,
                                             "default_date_format": "yyyy-mm-dd"})
        ws = wb.add_worksheet("data")
        bold = wb.add_format({"bold": True})
        dtf = wb.add_format({"num_format": "yyyy-mm-dd hh:mm:ss"})
        df_ = wb.add_format({"num_format": "yyyy-mm-dd"})
        try:
            r_i = 0
            for cols, data in batches:
                if r_i == 0:
                    for j, c in enumerate(cols):
                        ws.write_string(0, j, c["name"], bold)
                    r_i = 1
                for r in data:
                    for j, v in enumerate(r):
                        if v is None:
                            continue
                        if isinstance(v, dt.datetime):
                            ws.write_datetime(r_i, j, v.replace(tzinfo=None), dtf)
                        elif isinstance(v, dt.date):
                            ws.write_datetime(r_i, j, dt.datetime(v.year, v.month, v.day), df_)
                        elif isinstance(v, bool):
                            ws.write_boolean(r_i, j, v)
                        elif isinstance(v, (int, float, decimal.Decimal)):
                            ws.write_number(r_i, j, float(v))
                        else:
                            ws.write_string(r_i, j, str(_cell(v))[:32767])
                    r_i += 1
                rows += len(data)
                _cap(rows, cap, fmt)
                tick()
        finally:
            wb.close()
        return rows
    import pyarrow as pa
    import pyarrow.parquet as pq
    writer = None
    try:
        for cols, data in batches:
            if writer is None:
                schema = pa.schema([(c["name"], _arrow_type(c["type"])) for c in cols])
                writer = pq.ParquetWriter(str(path), schema, compression="zstd")
            if data:
                arrays = [pa.array([_arrow_value(r[j], c["type"]) for r in data], type=schema.field(j).type)
                          for j, c in enumerate(cols)]
                writer.write_table(pa.Table.from_arrays(arrays, schema=schema))
            rows += len(data)
            _cap(rows, cap, fmt)
            tick()
    finally:
        if writer is not None:
            writer.close()
    return rows


def _cap(rows: int, cap: int, fmt: str) -> None:
    if rows > cap:
        hint = " — Excel sheets hold about a million rows; use CSV or Parquet" if fmt == "excel" else ""
        raise JobError(f"more than {cap:,} rows{hint}. Add a WHERE clause or LIMIT.")


def export_file(token: str) -> tuple[Path, str]:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise fsroots.PathError("not found", 404)
    try:
        meta = json.loads((EXPORT_DIR / f"{token}.json").read_text())
    except (OSError, ValueError) as exc:
        raise fsroots.PathError("this export has expired — run it again", 404) from exc
    p = EXPORT_DIR / f"{token}{FORMATS_OUT.get(meta.get('fmt'), '.csv')}"
    if not p.is_file():
        raise fsroots.PathError("this export has expired — run it again", 404)
    return p, meta.get("name") or p.name
