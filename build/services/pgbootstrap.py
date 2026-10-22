"""Idempotent Postgres bootstrap for the warehouse server: per-service
metadata databases (superset, mlflow) and the bsp_edw schema layout.

Runs as the Postgres superuser `analyst` (PG_PASS) so it works whether or not
the docker-entrypoint-initdb.d scripts ran (they only run on an EMPTY data
directory — an upgraded appliance keeps its old one).

    python pgbootstrap.py db --db superset [--owner analyst]
    python pgbootstrap.py edw          # vector extension + every bsp_edw schema

Retries connection errors (Postgres may still be starting), never prints a
password. Exit codes: 0 ok · 2 usage · 1 failure.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from contextlib import closing

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:  # pragma: no cover
    print("pgbootstrap: psycopg2 is not installed in this image", file=sys.stderr)
    sys.exit(1)

HOST = os.environ.get("PG_HOST", "postgres")
PORT = int(os.environ.get("PG_PORT", "5432"))
SUPERUSER = os.environ.get("PG_USER", "analyst")
SUPER_DB = os.environ.get("PG_DB", "bsp_edw")


def log(msg: str) -> None:
    print(f"[pgbootstrap] {msg}", flush=True)


def connect(dbname: str, user: str, password: str, attempts: int = 30):
    last: Exception | None = None
    for i in range(attempts):
        try:
            conn = psycopg2.connect(host=HOST, port=PORT, dbname=dbname, user=user,
                                    password=password, connect_timeout=5)
            conn.autocommit = True  # CREATE DATABASE cannot run in a txn
            return conn
        except psycopg2.OperationalError as exc:
            last = exc
            text = str(exc)
            # wrong password / missing role / missing db: retrying won't help
            if "password authentication failed" in text or "does not exist" in text:
                raise
            time.sleep(min(2 + i, 10))
    raise RuntimeError(f"cannot connect to {HOST}:{PORT}/{dbname} as {user}: {last}")


def superconn(dbname: str = SUPER_DB):
    pw = os.environ.get("PG_PASS", "")
    if not pw:
        raise SystemExit("pgbootstrap: PG_PASS is empty")
    return connect(dbname, SUPERUSER, pw)


def ensure_db(cur, db: str, owner: str) -> None:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,))
    if cur.fetchone():
        log(f"database {db} exists")
        return
    cur.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
        sql.Identifier(db), sql.Identifier(owner)))
    log(f"created database {db} (owner {owner})")


def cmd_db(args) -> int:
    with closing(superconn()) as conn, conn.cursor() as cur:
        ensure_db(cur, args.db, args.owner or SUPERUSER)
    return 0


EDW_SCHEMAS = ("raw", "landing", "staging", "intermediate", "marts", "features", "mlops", "ai", "rag",
               "disputes", "ccai")


def cmd_edw(args) -> int:
    with closing(superconn()) as conn, conn.cursor() as cur:
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg2.Error as exc:   # plain postgres image without pgvector
            log(f"vector extension unavailable: {exc.pgerror or exc}".strip())
        for schema in EDW_SCHEMAS:
            cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {} AUTHORIZATION {}").format(
                sql.Identifier(schema), sql.Identifier(SUPERUSER)))
    log(f"bsp_edw schemas ensured: {', '.join(EDW_SCHEMAS)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pgbootstrap")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("db")
    b.add_argument("--db", required=True)
    b.add_argument("--owner")
    b.set_defaults(fn=cmd_db)
    e = sub.add_parser("edw")
    e.set_defaults(fn=cmd_edw)
    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except Exception as exc:  # noqa: BLE001
        print(f"pgbootstrap: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
