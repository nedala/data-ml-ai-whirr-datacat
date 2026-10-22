"""Entrypoint of the `mlflow` service:  python -m bsp_mlops.mlflow_server

MLflow 3.x tracking server + model registry, hosted natively under /mlflow/:
  backend store   Postgres database `mlflow` (user analyst, PG_PASS) — passed via
                  the MLFLOW_BACKEND_STORE_URI environment variable, never argv
  artifacts       /data/mlops/artifacts, proxied by the server (--serve-artifacts),
                  so clients only need HTTP (mlflow-artifacts:/ URIs)
  prefix          --static-prefix /mlflow  → UI, REST API and artifact proxy all
                  live under /mlflow (tracking URI http://mlflow:5000/mlflow)
  security        MLflow 3's host-header / CORS middleware: allowed hosts are the
                  internal service names, localhost, the public facade host and
                  the private (RFC 1918) ranges — so both http://mlflow:5000 and
                  https://<host-ip>:<FACADE_PORT> work (nginx forwards
                  Host: $http_host); CORS origins = the facade
"""

from __future__ import annotations

import os
import sys
import time
from urllib.parse import quote_plus


def _env(n: str, d: str = "") -> str:
    return os.environ.get(n, "").strip() or d


def _default_allowed_hosts(facade: str) -> str:
    """Host-header patterns MLflow should accept.

    MLflow's own defaults (loopback + RFC 1918 private ranges — the console is
    reached at ``https://<host-ip>:<FACADE_PORT>``) plus the appliance's internal
    service names. Passing ``--allowed-hosts`` REPLACES MLflow's defaults, so the
    private-IP patterns have to be repeated here or LAN access gets a 403
    "Invalid Host header".
    """
    extra = ["mlflow", "mlflow:*", "nginx", "nginx:*", facade, f"{facade}:*"]
    try:
        from mlflow.server.security_utils import get_default_allowed_hosts

        patterns = get_default_allowed_hosts() + extra
    except Exception:  # noqa: BLE001 — keep working if MLflow moves the helper
        loop = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*",
                "0.0.0.0", "0.0.0.0:*", "[::1]", "[[]::1]:*"]
        private = ["192.168.*", "10.*", *[f"172.{i}.*" for i in range(16, 32)], "fc00:*", "fd00:*"]
        patterns = loop + extra + private
    return ",".join(dict.fromkeys(p for p in patterns if p))


def ensure_database(name: str) -> None:
    import psycopg2

    kw = dict(host=_env("PG_HOST", "postgres"), port=_env("PG_PORT", "5432"), user=_env("PG_USER", "analyst"),
              password=_env("PG_PASS"))
    for attempt in range(60):
        try:
            conn = psycopg2.connect(dbname=_env("PG_DB", "bsp_edw"), **kw)
            break
        except Exception as e:
            print(f"[mlflow] waiting for postgres ({e.__class__.__name__})", flush=True)
            time.sleep(2)
    else:
        sys.exit("postgres unreachable")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("select 1 from pg_database where datname=%s", (name,))
        if not cur.fetchone():
            try:
                cur.execute(f'create database "{name}"')
                print(f"[mlflow] created database {name}", flush=True)
            except Exception as e:  # the postgres init normally creates it; report and carry on
                print(f"[mlflow] could not create database {name}: {e}", flush=True)
    conn.close()


def main() -> None:
    db = _env("MLFLOW_DB", "mlflow")
    ensure_database(db)
    art = _env("MLFLOW_ARTIFACTS_DIR", "/data/mlops/artifacts")
    os.makedirs(art, exist_ok=True)
    os.environ["MLFLOW_BACKEND_STORE_URI"] = (
        f"postgresql+psycopg2://{quote_plus(_env('PG_USER', 'analyst'))}:{quote_plus(_env('PG_PASS'))}@"
        f"{_env('PG_HOST', 'postgres')}:{_env('PG_PORT', '5432')}/{db}")
    facade = _env("PUBLIC_FACADE_HOST", "localhost")
    port = _env("FACADE_PORT", "31201")
    hosts = _env("MLFLOW_ALLOWED_HOSTS", _default_allowed_hosts(facade))
    origins = _env("MLFLOW_CORS_ORIGINS", f"https://{facade}:{port},https://localhost:{port},https://{facade},"
                                          f"http://localhost:5000")
    args = ["mlflow", "server", "--host", "0.0.0.0", "--port", _env("MLFLOW_PORT", "5000"),
            "--artifacts-destination", art, "--serve-artifacts", "--static-prefix", "/mlflow",
            "--workers", _env("MLFLOW_WORKERS", "2"), "--allowed-hosts", hosts, "--cors-allowed-origins", origins]
    print(f"[mlflow] starting: {' '.join(args)}", flush=True)  # no secrets in argv
    os.execvp(args[0], args)


if __name__ == "__main__":
    main()
