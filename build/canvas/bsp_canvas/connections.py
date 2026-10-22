"""Reusable Connections: name + type + non-secret params. Secrets are only
referenced by vault key name (`vault:NAME`, set in Studio → Credentials) and
resolved from the process environment at use time — never stored here.

Stored in ${CANVAS_DIR}/connections.yaml (git-friendly). The built-in
`warehouse` connection (local Postgres bsp_edw) is always present.

Databases: postgres · oracle (python-oracledb thin) · mssql (pyodbc + ODBC
Driver 18, falling back to pymssql) · mysql · bigquery.
Object storage: gcs (Studio → Cloud identities; GCP_GCS_JSON / GCP_SA_JSON) · s3 (AWS / MinIO / Ceph) · azure_blob (ADLS Gen2).

Google principal switch (BigQuery and GCS connections, param `identity`):
"" = the identity assigned in Studio (BigQuery · read / Cloud Storage). A named
identity is resolved by Studio for a USER, who must be on its access list:
  * browse / test / preview / a run someone starts → the signed-in user of that
    request (gate header + Studio session cookie, see app.py);
  * scheduled runs → the user who saved the connection, through the grant
    Studio signed when they saved it (identity_grants.json, never served).
Studio returns a short-lived token; no key ever reaches this service.
"""

from __future__ import annotations

import contextlib
import fnmatch
import gzip
import io
import json
import re
import socket
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .config import canvas_dir, env, interpolate, secret

DB_TYPES = ("postgres", "oracle", "mssql", "mysql", "bigquery")
STORAGE_TYPES = ("gcs", "s3", "azure_blob")
_lock = threading.Lock()

F = dict


def identity_names() -> list[str]:
    """Studio → Cloud identities names (GCP_IDENTITIES keys) — never the keys themselves.
    The browser narrows this to the identities the signed-in user may use."""
    try:
        from bsp_common import gcp
        return gcp.identity_names()
    except Exception:  # noqa: BLE001 — bsp_common missing / bad env: no choices
        return []


def _identity_field(default: str) -> dict:
    return F(key="identity", label="Acting as (Google identity)", type="select", default="", identity=True,
             options=[["", f"Assigned default — {default} (Studio → Cloud identities)"]] + [[n, n] for n in identity_names()],
             hint="Assigned default, or a named identity shared with you in Studio → Cloud identities. Runs you start act "
                  "as you; scheduled runs act as the user who saved this connection.")


TYPES: dict[str, dict[str, Any]] = {
    "postgres": {"label": "PostgreSQL", "kind": "database", "icon": "database", "fields": [
        F(key="host", label="Host", type="text", required=True), F(key="port", label="Port", type="number", default=5432),
        F(key="database", label="Database", type="text", required=True),
        F(key="username", label="User", type="text", required=True),
        F(key="password_ref", label="Password (vault reference)", type="secret", placeholder="vault:CRM_PG_PASSWORD"),
        F(key="sslmode", label="SSL mode", type="select", options=["prefer", "require", "verify-full", "disable"],
          default="prefer")]},
    "oracle": {"label": "Oracle", "kind": "database", "icon": "database", "fields": [
        F(key="host", label="Host", type="text", required=True), F(key="port", label="Port", type="number", default=1521),
        F(key="service_name", label="Service name", type="text", placeholder="FREEPDB1", hint="…or a SID below"),
        F(key="sid", label="SID", type="text"),
        F(key="username", label="User", type="text", required=True),
        F(key="password_ref", label="Password (vault reference)", type="secret", placeholder="vault:BILLING_ORA_PASSWORD")]},
    "mssql": {"label": "SQL Server / Azure SQL", "kind": "database", "icon": "database", "fields": [
        F(key="host", label="Host", type="text", required=True), F(key="port", label="Port", type="number", default=1433),
        F(key="database", label="Database", type="text", required=True),
        F(key="auth", label="Authentication", type="select", options=["sql", "entra_service_principal"], default="sql"),
        F(key="username", label="User / client id", type="text", required=True),
        F(key="password_ref", label="Password / client secret (vault reference)", type="secret",
          placeholder="vault:CARE_MSSQL_PASSWORD"),
        F(key="encrypt", label="Encrypt", type="bool", default=True),
        F(key="trust_server_certificate", label="Trust server certificate", type="bool", default=False),
        F(key="driver", label="Driver", type="select", options=["auto", "pyodbc", "pymssql"], default="auto",
          hint="auto = ODBC Driver 18 when installed, else pymssql")]},
    "mysql": {"label": "MySQL / MariaDB", "kind": "database", "icon": "database", "fields": [
        F(key="host", label="Host", type="text", required=True), F(key="port", label="Port", type="number", default=3306),
        F(key="database", label="Database", type="text", required=True),
        F(key="username", label="User", type="text", required=True),
        F(key="password_ref", label="Password (vault reference)", type="secret", placeholder="vault:MYSQL_PASSWORD")]},
    "bigquery": {"label": "BigQuery", "kind": "database", "icon": "cloud", "fields": [
        F(key="project", label="Project", type="text", placeholder="${GCP_PROJECT_ID}",
          hint="Mock = local DuckDB when no Google credential is configured"),
        _identity_field("BigQuery · read")]},
    "gcs": {"label": "Google Cloud Storage", "kind": "storage", "icon": "cloud", "fields": [
        F(key="project", label="Project", type="text", placeholder="${GCP_PROJECT_ID}"),
        _identity_field("Cloud Storage"),
        F(key="credentials_ref", label="Service-account key (vault reference)", type="secret", default="vault:GCP_SA_JSON",
          hint="Only when no Google identity is chosen: vault:NAME of a key; vault:GCP_SA_JSON = the assigned one"),
        F(key="anonymous", label="Public bucket (anonymous)", type="bool", default=False)]},
    "s3": {"label": "Amazon S3 / S3-compatible", "kind": "storage", "icon": "cloud", "fields": [
        F(key="endpoint_url", label="Endpoint URL", type="text", placeholder="blank = AWS · http://minio:9000"),
        F(key="region", label="Region", type="text", default="us-east-1"),
        F(key="access_key_ref", label="Access key id (vault reference)", type="secret", placeholder="vault:S3_ACCESS_KEY"),
        F(key="secret_key_ref", label="Secret access key (vault reference)", type="secret", placeholder="vault:S3_SECRET_KEY")]},
    "azure_blob": {"label": "Azure Blob / ADLS Gen2", "kind": "storage", "icon": "cloud", "fields": [
        F(key="account_name", label="Storage account", type="text", required=True),
        F(key="auth", label="Authentication", type="select", options=["key", "sas", "service_principal"], default="key"),
        F(key="key_ref", label="Account key / SAS token (vault reference)", type="secret", placeholder="vault:AZ_STORAGE_KEY"),
        F(key="tenant_id", label="Tenant id", type="text", show_if={"auth": "service_principal"}),
        F(key="client_id", label="Client id", type="text", show_if={"auth": "service_principal"}),
        F(key="client_secret_ref", label="Client secret (vault reference)", type="secret",
          show_if={"auth": "service_principal"})]},
}

WAREHOUSE = {"name": "warehouse", "type": "postgres", "builtin": True,
             "description": "Local warehouse (bsp_edw) — PG_HOST / PG_PASS from the appliance",
             "params": {}}


# ── store ────────────────────────────────────────────────────────────────
def _path() -> Path:
    return canvas_dir() / "connections.yaml"


def _load() -> dict[str, dict]:
    try:
        data = yaml.safe_load(_path().read_text()) or {}
    except OSError:
        data = {}
    return {c["name"]: c for c in data.get("connections", []) if isinstance(c, dict) and c.get("name")}


def list_connections() -> list[dict]:
    return [WAREHOUSE] + sorted(_load().values(), key=lambda c: c["name"])


def get(name: str) -> dict:
    if name in ("", None, "warehouse"):
        return WAREHOUSE
    c = _load().get(name)
    if not c:
        raise KeyError(f"connection {name!r} not found")
    return c


def _check_no_secrets(params: dict) -> None:
    for k, v in params.items():
        if k.endswith("_ref"):
            if v not in (None, "") and not str(v).startswith("vault:"):
                raise ValueError(f"{k}: secrets must be vault references (vault:NAME), not values")
        elif re.search(r"(password|secret|token|key)$", k) and v:
            raise ValueError(f"{k}: store secrets in Studio → Credentials and reference them as {k}_ref: vault:NAME")


def save(conn: dict) -> dict:
    name = (conn.get("name") or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_\-]{0,62}", name) or name == "warehouse":
        raise ValueError("name: lowercase letters, digits, - and _ (not 'warehouse')")
    if conn.get("type") not in TYPES:
        raise ValueError(f"type must be one of {', '.join(TYPES)}")
    params = {k: v for k, v in (conn.get("params") or {}).items() if v not in (None, "")}
    _check_no_secrets(params)
    rec = {"name": name, "type": conn["type"], "description": conn.get("description", ""), "params": params}
    if params.get("identity") and conn["type"] in ("bigquery", "gcs"):
        old = _load().get(name) or {}
        g = _grants().get(name) or {}
        from bsp_common import gcp
        if not (g.get("identity") == params["identity"] and (old.get("params") or {}).get("identity") == params["identity"]
                and g.get("user", "").lower() == gcp.acting_user().lower()):
            grant = gcp.studio_grant(params["identity"])     # 403 when this user may not act as it
            _set_grant(name, {"identity": params["identity"], "user": grant["user"], "grant": grant["grant"],
                              "principal": grant.get("principal_email", ""), "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        rec["identity_user"] = (_grants().get(name) or {}).get("user", "")
    else:
        _set_grant(name, None)
    with _lock:
        allc = _load()
        allc[name] = rec
        _path().write_text(yaml.safe_dump({"connections": sorted(allc.values(), key=lambda c: c["name"])},
                                          sort_keys=False, allow_unicode=True))
    return rec


def delete(name: str) -> None:
    with _lock:
        allc = _load()
        allc.pop(name, None)
        _path().write_text(yaml.safe_dump({"connections": list(allc.values())}, sort_keys=False))
    _set_grant(name, None)


# ── principal switch: grants for scheduled runs (never served by the API) ──
def _grants_path() -> Path:
    return canvas_dir() / "identity_grants.json"


def _grants() -> dict[str, dict]:
    try:
        data = json.loads(_grants_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _set_grant(name: str, rec: dict | None) -> None:
    with _lock:
        data = _grants()
        if rec is None and name not in data:
            return
        if rec is None:
            data.pop(name, None)
        else:
            data[name] = rec
        p = _grants_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.chmod(0o600)
        tmp.replace(p)


def acting_for(conn: dict):
    """Context for Google calls on a connection with a named identity: the
    request's / run's signed-in user when there is one, else (scheduler) the
    user who saved the connection, through their grant. No identity → as is."""
    ident = (conn.get("params") or {}).get("identity") or ""
    if not ident or conn.get("type") not in ("bigquery", "gcs"):
        return contextlib.nullcontext()
    from bsp_common import gcp
    if gcp.acting_user():
        return contextlib.nullcontext()
    g = _grants().get(conn.get("name", "")) or {}
    if g.get("identity") != ident or not g.get("grant"):
        raise PermissionError(f"connection {conn.get('name')!r} acts as {ident!r} but has no saved grant — "
                              "open the connection and save it again")
    return gcp.acting(g["user"], grant=g["grant"])


def acting_label(conn: dict) -> str:
    """'data.analyst@… on da-tsa-dv' for messages (named identities only)."""
    p = conn.get("params") or {}
    if not p.get("identity"):
        return ""
    from bsp_common import gcp
    try:
        with acting_for(conn):
            d = gcp.studio_token(p["identity"], consumer="bigquery_read" if conn["type"] == "bigquery" else "gcs")
    except Exception:  # noqa: BLE001 — label only
        return p["identity"]
    proj = interpolate(p.get("project") or "") or d.get("default_project", "")
    return f"{d.get('principal_email') or p['identity']}{' on ' + proj if proj else ''}"


def _sec(p: dict, key: str) -> str:
    ref = p.get(key)
    return secret(ref) if ref else ""


# ── databases ────────────────────────────────────────────────────────────
def _odbc_driver() -> str | None:
    try:
        import pyodbc
        drivers = pyodbc.drivers()
    except Exception:
        return None
    for d in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
        if d in drivers:
            return d
    return None


def sqlalchemy_url(conn: dict):
    from sqlalchemy.engine import URL, make_url
    t, p = conn["type"], conn.get("params") or {}
    if conn.get("builtin"):
        from bsp_common import settings
        return make_url(settings.get().pg_url)
    pw = _sec(p, "password_ref") or None
    if t == "postgres":
        return URL.create("postgresql+psycopg2", p.get("username"), pw, p["host"], int(p.get("port") or 5432),
                          p.get("database"), query={"sslmode": p.get("sslmode", "prefer"), "connect_timeout": "10"})
    if t == "mysql":
        return URL.create("mysql+pymysql", p.get("username"), pw, p["host"], int(p.get("port") or 3306), p.get("database"))
    if t == "oracle":
        q = {"service_name": p["service_name"]} if p.get("service_name") else {}
        return URL.create("oracle+oracledb", p.get("username"), pw, p["host"], int(p.get("port") or 1521),
                          p.get("sid") if not p.get("service_name") else None, query=q)
    if t == "mssql":
        drv = p.get("driver", "auto")
        odbc = _odbc_driver() if drv in ("auto", "pyodbc") else None
        if drv == "pyodbc" and not odbc:
            raise RuntimeError("ODBC Driver 18 for SQL Server is not installed in this image — use driver pymssql")
        if odbc:
            q = {"driver": odbc, "Encrypt": "yes" if p.get("encrypt", True) else "no",
                 "TrustServerCertificate": "yes" if p.get("trust_server_certificate") else "no"}
            if p.get("auth") == "entra_service_principal":
                q["Authentication"] = "ActiveDirectoryServicePrincipal"
            return URL.create("mssql+pyodbc", p.get("username"), pw, p["host"], int(p.get("port") or 1433),
                              p.get("database"), query=q)
        if p.get("auth") == "entra_service_principal":
            raise RuntimeError("Entra service-principal auth needs ODBC Driver 18 (pyodbc)")
        return URL.create("mssql+pymssql", p.get("username"), pw, p["host"], int(p.get("port") or 1433),
                          p.get("database"))
    raise ValueError(f"{t} is not a SQLAlchemy database connection")


def engine(conn: dict):
    from sqlalchemy import create_engine
    url = sqlalchemy_url(conn)
    kw: dict[str, Any] = {"pool_pre_ping": True}
    if url.get_backend_name() == "oracle":
        kw["thick_mode"] = False
    return create_engine(url, **kw)


def namespace(conn: dict) -> str:
    """OpenLineage dataset namespace for a connection."""
    t, p = conn["type"], conn.get("params") or {}
    if conn.get("builtin"):
        return f"postgres://{env('PG_HOST', 'postgres')}:{env('PG_PORT', '5432')}"
    if t in DB_TYPES and t != "bigquery":
        scheme = {"postgres": "postgres", "mysql": "mysql", "oracle": "oracle", "mssql": "sqlserver"}[t]
        return f"{scheme}://{p.get('host')}:{p.get('port') or ''}".rstrip(":")
    if t == "bigquery":
        return "bigquery"
    if t == "gcs":
        return "gs://"
    if t == "s3":
        return f"s3://{p.get('endpoint_url') or 'aws'}"
    return f"wasbs://{p.get('account_name')}"


@contextlib.contextmanager
def _bq_identity(conn: dict):
    """BigQuery calls as the connection's named Google identity, for the acting
    user (see acting_for); '' = BigQuery · read, exactly as before."""
    from bsp_connectors import bigquery
    p = conn.get("params") or {}
    ident = p.get("identity") or ""
    if not ident:
        yield
        return
    if not hasattr(bigquery, "using_principal"):
        raise RuntimeError("this image's BigQuery connector cannot switch identity")
    with acting_for(conn), bigquery.using_principal(ident, interpolate(p.get("project") or "") or None):
        yield


DEFAULT_SCHEMA = {"postgres": "public", "mssql": "dbo", "mysql": None, "oracle": None}


def db_schemas(conn: dict) -> list[str]:
    if conn["type"] == "bigquery":
        from bsp_connectors import bigquery
        with _bq_identity(conn):
            return [d["dataset_id"] for d in bigquery.list_datasets()]
    from sqlalchemy import inspect
    eng = engine(conn)
    try:
        names = inspect(eng).get_schema_names()
    finally:
        eng.dispose()
    hide = {"information_schema", "pg_catalog", "pg_toast", "sys", "guest", "INFORMATION_SCHEMA", "db_owner",
            "db_accessadmin", "db_securityadmin", "db_ddladmin", "db_backupoperator", "db_datareader",
            "db_datawriter", "db_denydatareader", "db_denydatawriter", "mysql", "performance_schema"}
    out = [n for n in names if n not in hide and not n.startswith("pg_temp") and not n.startswith("pg_toast")]
    if conn["type"] == "oracle":  # skip Oracle-maintained schemas
        sysuser = {"SYS", "SYSTEM", "XDB", "OUTLN", "DBSNMP", "APPQOSSYS", "AUDSYS", "CTXSYS", "DVSYS", "GSMADMIN_INTERNAL",
                   "LBACSYS", "MDSYS", "OJVMSYS", "OLAPSYS", "ORDSYS", "WMSYS", "DBSFWUSER", "GGSYS", "REMOTE_SCHEDULER_AGENT",
                   "ANONYMOUS", "DVF", "GSMCATUSER", "GSMUSER", "SYSBACKUP", "SYSDG", "SYSKM", "SYSRAC", "SYS$UMF",
                   "DIP", "MDDATA", "ORDDATA", "ORDPLUGINS", "SI_INFORMTN_SCHEMA", "XS$NULL", "GSMROOTUSER", "PDBADMIN",
                   "OPS$ORACLE", "VECSYS", "BAASSYS", "DGPDB_INT", "GSMCATUSER"}
        out = [n for n in out if n.upper() not in sysuser and not n.upper().startswith("APEX")]
    return out


def db_tables(conn: dict, schema: str | None, search: str = "") -> list[dict]:
    if conn["type"] == "bigquery":
        from bsp_connectors import bigquery
        with _bq_identity(conn):
            rows = [{"schema": schema, "name": t["table_id"], "kind": t.get("type", "TABLE").lower()}
                    for t in bigquery.list_tables(schema)]
    else:
        from sqlalchemy import inspect
        eng = engine(conn)
        try:
            ins = inspect(eng)
            rows = [{"schema": schema, "name": n, "kind": "table"} for n in ins.get_table_names(schema=schema or None)]
            rows += [{"schema": schema, "name": n, "kind": "view"} for n in ins.get_view_names(schema=schema or None)]
        finally:
            eng.dispose()
    s = search.lower()
    return sorted([r for r in rows if s in r["name"].lower()], key=lambda r: r["name"])


def db_columns(conn: dict, schema: str | None, table: str) -> list[dict]:
    if conn["type"] == "bigquery":
        from bsp_connectors import bigquery
        with _bq_identity(conn):
            return [{"name": f["name"], "type": f["type"], "nullable": f.get("mode") != "REQUIRED"}
                    for f in bigquery.table_schema(schema, table)]
    from sqlalchemy import inspect
    eng = engine(conn)
    try:
        ins = inspect(eng)
        pk = set((ins.get_pk_constraint(table, schema=schema or None) or {}).get("constrained_columns") or [])
        cols = ins.get_columns(table, schema=schema or None)
    finally:
        eng.dispose()
    return [{"name": c["name"], "type": str(c["type"]), "nullable": c.get("nullable", True),
             "primary_key": c["name"] in pk, "load_type": load_type(str(c["type"]))} for c in cols]


def load_type(src_type: str) -> str:
    """Source column type → the type it lands as (type-mapping preview)."""
    t = src_type.upper()
    if any(x in t for x in ("INT", "SERIAL")) and "INTERVAL" not in t:
        return "bigint"
    if any(x in t for x in ("NUMERIC", "DECIMAL", "NUMBER", "MONEY")):
        return "bigint" if re.search(r"NUMBER\(\d+(,\s*0)?\)$", t) else "numeric / double"
    if any(x in t for x in ("FLOAT", "DOUBLE", "REAL", "BINARY_DOUBLE")):
        return "double precision"
    if "BOOL" in t or t == "BIT":
        return "boolean"
    if "TIMESTAMP" in t or "DATETIME" in t:
        return "timestamp with time zone"
    if t.startswith("DATE"):
        return "date (Oracle DATE → timestamp)" if t == "DATE" else "date"
    if "TIME" in t:
        return "time"
    if any(x in t for x in ("BLOB", "BINARY", "BYTEA", "RAW", "IMAGE", "ROWVERSION")):
        return "binary"
    if "JSON" in t:
        return "json"
    return "text"


def row_estimate(conn: dict, schema: str | None, table: str) -> dict:
    from sqlalchemy import text
    t = conn["type"]
    if t == "bigquery":
        from bsp_connectors import bigquery
        with _bq_identity(conn):
            ds = schema if "." in (schema or "") else f"{bigquery.project()}.{schema}"   # extra data projects: "proj.ds"
            n = bigquery.query(f"select count(*) as n from `{ds}.{table}`")[0]["n"]
        return {"rows": n, "method": "count"}
    sqls = {
        "postgres": ("select greatest(c.reltuples, 0)::bigint from pg_class c join pg_namespace n on n.oid = c.relnamespace "
                     "where n.nspname = :s and c.relname = :t", "statistics"),
        "mssql": ("select sum(p.rows) from sys.partitions p join sys.tables t on t.object_id = p.object_id "
                  "join sys.schemas s on s.schema_id = t.schema_id where s.name = :s and t.name = :t and p.index_id in (0,1)",
                  "statistics"),
        "oracle": ("select num_rows from all_tables where owner = upper(:s) and table_name = upper(:t)", "statistics"),
        "mysql": ("select table_rows from information_schema.tables where table_schema = :s and table_name = :t",
                  "statistics"),
    }
    eng = engine(conn)
    try:
        with eng.connect() as c:
            est = None
            if t in sqls:
                sch = schema or DEFAULT_SCHEMA.get(t) or (conn.get("params") or {}).get("username", "") \
                    or (conn.get("params") or {}).get("database", "")
                try:
                    est = c.execute(text(sqls[t][0]), {"s": sch, "t": table}).scalar()
                except Exception:
                    est = None
            if est:
                return {"rows": int(est), "method": "statistics"}
            q = quote_table(conn, schema, table)
            return {"rows": int(c.execute(text(f"select count(*) from {q}")).scalar() or 0), "method": "count"}
    finally:
        eng.dispose()


def quote_table(conn: dict, schema: str | None, table: str) -> str:
    from sqlalchemy.engine import make_url
    dialect = engine(conn).dialect if not conn.get("builtin") else None
    if dialect is None:
        from sqlalchemy.dialects import postgresql
        dialect = postgresql.dialect()
    prep = dialect.identifier_preparer
    return (prep.quote_schema(schema) + "." if schema else "") + prep.quote(table)


# ── reachability + hard timeouts (a Test must never spin forever) ──────
TEST_TIMEOUT = 15          # seconds for a whole connection Test
BROWSE_TIMEOUT = 30        # schema / table / bucket browsing
DEMO_HOSTS = {"minio": "MinIO", "mssql": "SQL Server", "oracle": "Oracle"}
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306, "oracle": 1521, "mssql": 1433}


def endpoint(conn: dict) -> tuple[str, int, bool] | None:
    """(host, port, direct) a connection dials; direct=False when an egress proxy carries it."""
    t, p = conn["type"], conn.get("params") or {}
    if conn.get("builtin"):
        return None
    if t in DEFAULT_PORTS and p.get("host"):
        return str(p["host"]), int(p.get("port") or DEFAULT_PORTS[t]), True
    if t == "s3" and p.get("endpoint_url"):
        from urllib.parse import urlsplit
        u = urlsplit(str(p["endpoint_url"]))
        if not u.hostname:
            return None
        direct = not urllib.request.getproxies() or bool(urllib.request.proxy_bypass(u.hostname))
        return u.hostname, u.port or (443 if u.scheme == "https" else 80), direct
    return None


def _demo_hint(host: str) -> str:
    return (f" — {DEMO_HOSTS[host]} is a demo source: add the 'Demo sources' capability in Studio"
            if host in DEMO_HOSTS else "")


def preflight(conn: dict, timeout: float = 5.0) -> None:
    """Fail in seconds (not minutes) when the host is unknown or the port is closed."""
    ep = endpoint(conn)
    if not ep:
        return
    host, port, direct = ep
    try:   # Docker's DNS can take ~8 s to give up on an unknown name: cap it
        with_timeout(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM, timeout=3, what="dns")
    except (socket.gaierror, TimeoutError):
        if direct or host in DEMO_HOSTS:
            raise ConnectionError(f"host {host} not reachable (no such host on the appliance network){_demo_hint(host)}")
        return            # resolved by the egress proxy
    if not direct:
        return
    try:
        with socket.create_connection((host, port), timeout=timeout):
            pass
    except OSError as e:
        raise ConnectionError(f"host {host}:{port} not reachable ({e.strerror or type(e).__name__}){_demo_hint(host)}")


def with_timeout(fn, *args, timeout: float, what: str):
    """Run fn(*args) in a daemon thread; raise TimeoutError if it does not answer in time."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except BaseException as e:  # noqa: BLE001 — re-raised in the caller
            box["error"] = e

    import contextvars
    th = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True, name=f"canvas-{what}")
    th.start()
    th.join(timeout)
    if th.is_alive():
        raise TimeoutError(f"{what}: no answer within {int(timeout)} s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _timeout_hint(conn: dict) -> str:
    t = conn["type"]
    if t in ("gcs", "bigquery"):
        return ("check the Google identity (Studio → Cloud identities → Test) and that Google APIs are allowed "
                "through the egress proxy")
    if t == "s3" and not (conn.get("params") or {}).get("endpoint_url"):
        return "check the access keys and that AWS S3 is allowed through the egress proxy"
    return "check host, port, firewall and credentials"


def test(conn: dict) -> dict:
    """Connection Test with a fast reachability check and a hard {TEST_TIMEOUT}s limit."""
    t, p = conn["type"], conn.get("params") or {}
    if t == "gcs" and not p.get("anonymous") and not p.get("identity") and not _gcp_info(p):
        raise ValueError("no Google credential — add / assign one in Studio → Cloud identities (or reference a key in "
                         "credentials_ref), or tick 'Public bucket (anonymous)'")
    preflight(conn)
    try:
        return with_timeout(_test, conn, timeout=TEST_TIMEOUT, what=f"{conn.get('name') or t} test")
    except TimeoutError as e:
        raise TimeoutError(f"{e} — {_timeout_hint(conn)}") from None


def _test_gcs(conn: dict) -> dict:
    """One direct call with Google's client: gcsfs retries 403s until the test times
    out, which hides the real cause (usually a missing IAM role on the key)."""
    from google.api_core import exceptions as gexc
    from google.cloud import storage

    from bsp_common import gcp

    p = conn.get("params") or {}
    if p.get("identity"):
        with acting_for(conn):
            creds = gcp.credentials("gcs", identity=p["identity"])
        project = interpolate(p.get("project") or "") or creds.bsp_default_project or env("GCP_PROJECT_ID")
        who = f"{creds.bsp_principal or p['identity']} (identity {p['identity']!r})"
    else:
        info = _gcp_info(p)
        creds = gcp.from_info(info, scopes=["https://www.googleapis.com/auth/devstorage.read_only"])
        project = interpolate(p.get("project") or "") or info.get("project_id") or env("GCP_PROJECT_ID")
        who = info.get("client_email") or "the signed-in Google account"
    client = storage.Client(project=project, credentials=creds)
    bucket = (p.get("bucket") or "").strip()
    try:
        if bucket:
            n = sum(1 for _ in client.list_blobs(bucket, max_results=5, timeout=10))
            return {"ok": True, "detail": f"connected as {who} — bucket gs://{bucket} readable ({n} object(s) sampled)"}
        names = [b.name for b in client.list_buckets(max_results=50, timeout=10)]
        return {"ok": True, "detail": f"connected as {who} — {len(names)} bucket(s) visible in {project}"}
    except gexc.Forbidden:
        need = (f"roles/storage.objectViewer on gs://{bucket}" if bucket
                else f"storage.buckets.list on project {project} (or set a bucket on this connection)")
        raise PermissionError(f"Google Cloud Storage refused access (403) for {who}: it needs {need}. "
                              "Ask a GCP project admin to grant it.") from None
    except gexc.NotFound:
        raise ValueError(f"bucket gs://{bucket} does not exist (or is not visible to {who})") from None


def _test(conn: dict) -> dict:
    t = conn["type"]
    if t == "bigquery":
        from bsp_connectors import bigquery
        with _bq_identity(conn):
            ds = bigquery.list_datasets()
            proj = bigquery.project()
        who = acting_label(conn) or f"assigned identity on {proj}"
        return {"ok": True, "detail": f"{len(ds)} dataset(s) ({bigquery.mode()}) — acting as {who}"}
    if t == "gcs" and not (conn.get("params") or {}).get("anonymous"):
        return _test_gcs(conn)
    if t in STORAGE_TYPES:
        fs = filesystem(conn)
        buckets = list_buckets(conn, fs)
        return {"ok": True, "detail": f"connected — {len(buckets)} bucket(s)/container(s) visible"}
    from sqlalchemy import text
    eng = engine(conn)
    try:
        with eng.connect() as c:
            probe = {"oracle": "select banner from v$version where rownum = 1", "mssql": "select @@version"}.get(
                t, "select version()")
            try:
                v = str(c.execute(text(probe)).scalar())
            except Exception:
                c.execute(text("select 1 from dual" if t == "oracle" else "select 1"))
                v = "ok"
    finally:
        eng.dispose()
    return {"ok": True, "detail": v.splitlines()[0][:200], "driver": str(sqlalchemy_url(conn).drivername)}


# ── object storage ───────────────────────────────────────────────────────
def _gcp_info(p: dict) -> dict | None:
    """Credential JSON (service_account or authorized_user) for a GCS connection:
    a vault:NAME key, else the Cloud Storage identity assigned in Studio
    (GCP_GCS_JSON), else GCP_SA_JSON. A named identity has no key here: see
    _gcs_token()."""
    if p.get("identity"):
        return None
    ref = p.get("credentials_ref")
    raw = secret(ref) if ref and ref != "vault:GCP_SA_JSON" else (env("GCP_GCS_JSON") or env("GCP_SA_JSON"))
    if not raw:
        return None
    if not raw.lstrip().startswith("{"):
        import base64
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def filesystem(conn: dict):
    t, p = conn["type"], conn.get("params") or {}
    if t == "gcs":
        import gcsfs
        if p.get("anonymous"):
            return gcsfs.GCSFileSystem(token="anon", skip_instance_cache=True)
        if p.get("identity"):          # principal switch: Studio-issued token, refreshed through Studio
            from bsp_common import gcp
            with acting_for(conn):
                creds = gcp.credentials("gcs", identity=p["identity"])
            return gcsfs.GCSFileSystem(project=interpolate(p.get("project") or "") or creds.bsp_default_project or None,
                                       token=creds, skip_instance_cache=True)
        info = _gcp_info(p)
        return gcsfs.GCSFileSystem(project=interpolate(p.get("project") or "") or env("GCP_PROJECT_ID") or None,
                                   token=info if info else "google_default", skip_instance_cache=True)
    if t == "s3":
        import s3fs
        ck = {"region_name": p.get("region") or "us-east-1"}
        if p.get("endpoint_url"):
            ck["endpoint_url"] = p["endpoint_url"]
        return s3fs.S3FileSystem(key=_sec(p, "access_key_ref") or None, secret=_sec(p, "secret_key_ref") or None,
                                 anon=not p.get("access_key_ref"), client_kwargs=ck, skip_instance_cache=True,
                                 config_kwargs={"s3": {"addressing_style": "path"}} if p.get("endpoint_url") else None)
    if t == "azure_blob":
        import adlfs
        auth = p.get("auth", "key")
        if auth == "service_principal":
            return adlfs.AzureBlobFileSystem(account_name=p["account_name"], tenant_id=p.get("tenant_id"),
                                             client_id=p.get("client_id"), client_secret=_sec(p, "client_secret_ref"),
                                             skip_instance_cache=True)
        if auth == "sas":
            return adlfs.AzureBlobFileSystem(account_name=p["account_name"], sas_token=_sec(p, "key_ref"),
                                             skip_instance_cache=True)
        return adlfs.AzureBlobFileSystem(account_name=p["account_name"], account_key=_sec(p, "key_ref"),
                                         skip_instance_cache=True)
    raise ValueError(f"{t} is not an object-storage connection")


def scheme(conn: dict) -> str:
    return {"gcs": "gs", "s3": "s3", "azure_blob": "az"}[conn["type"]]


def dlt_credentials(conn: dict) -> Any:
    """Credentials for dlt's filesystem destination."""
    t, p = conn["type"], conn.get("params") or {}
    if t == "gcs":
        if p.get("identity"):
            raise ValueError("writing to Cloud Storage as a switched identity is not supported by the loader yet — "
                             "use the assigned Cloud Storage identity (Acting as: assigned default) for destinations")
        return _gcp_info(p)
    if t == "s3":
        c = {"aws_access_key_id": _sec(p, "access_key_ref"), "aws_secret_access_key": _sec(p, "secret_key_ref"),
             "region_name": p.get("region") or "us-east-1"}
        if p.get("endpoint_url"):
            c["endpoint_url"] = p["endpoint_url"]
        return c
    if t == "azure_blob":
        if p.get("auth") == "service_principal":
            return {"azure_storage_account_name": p["account_name"], "azure_tenant_id": p.get("tenant_id"),
                    "azure_client_id": p.get("client_id"), "azure_client_secret": _sec(p, "client_secret_ref")}
        key = "azure_storage_sas_token" if p.get("auth") == "sas" else "azure_storage_account_key"
        return {"azure_storage_account_name": p["account_name"], key: _sec(p, "key_ref")}
    return None


def list_buckets(conn: dict, fs=None) -> list[str]:
    fs = fs or filesystem(conn)
    try:
        return sorted(str(x).rstrip("/").split("/")[-1] for x in fs.ls(""))
    except Exception:
        return []


def browse(conn: dict, path: str = "") -> list[dict]:
    """Folders and files directly under bucket/prefix ('' = buckets)."""
    fs = filesystem(conn)
    path = path.strip("/")
    if not path:
        return [{"name": b, "path": b, "type": "bucket"} for b in list_buckets(conn, fs)]
    out = []
    for e in fs.ls(path, detail=True):
        name = e["name"].rstrip("/")
        out.append({"name": name.split("/")[-1], "path": name, "type": "folder" if e.get("type") == "directory" else "file",
                    "size": e.get("size"), "modified": _mtime(e)})
    return sorted(out, key=lambda x: (x["type"] != "folder", x["name"]))


def _mtime(e: dict) -> str | None:
    for k in ("LastModified", "mtime", "updated", "last_modified", "modification_time"):
        v = e.get(k)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            v = datetime.fromtimestamp(v, tz=timezone.utc)
        if isinstance(v, str):
            try:
                v = datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                return v
        if isinstance(v, datetime):
            return (v if v.tzinfo else v.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat()
    return None


def glob_files(conn: dict, bucket: str, pattern: str, fs=None) -> list[dict]:
    fs = fs or filesystem(conn)
    base = bucket.strip("/")
    pat = (pattern or "**").lstrip("/")
    if "*" not in pat and "?" not in pat and "[" not in pat:
        full = f"{base}/{pat}"
        if fs.isdir(full):
            pat = pat.rstrip("/") + "/**"
    entries = fs.glob(f"{base}/{pat}", detail=True)
    files = []
    for name, e in (entries.items() if isinstance(entries, dict) else ((x, fs.info(x)) for x in entries)):
        if e.get("type") == "directory" or name.endswith("/"):
            continue
        files.append({"path": name, "name": name.split("/")[-1], "size": e.get("size"), "modified": _mtime(e)})
    return sorted(files, key=lambda f: f["path"])


def read_object(fs, path: str, fmt: str = "auto", sheet: Any = None, delimiter: str = "") -> pd.DataFrame:
    from .nodes import read_table_bytes
    with fs.open(path, "rb") as fh:
        data = fh.read()
    name = path
    if path.endswith(".gz"):
        data = gzip.decompress(data)
        name = path[:-3]
    return read_table_bytes(data, name, fmt, sheet, delimiter=delimiter)
