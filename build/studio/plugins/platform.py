"""Studio plugin: base platform services (no page).

Hooks (contract: build/studio/core.py docstring):

* ``ROTATE`` — apply a new credential to the RUNNING service before Studio
  recreates the affected containers.
* ``health()`` — warehouse / Superset / VS Code / workspace / browser.
  (per-user / shared bind-mount dirs are created by Studio's orchestrator)

ROTATE call contract (defensive — works with either ordering):
  Studio calls ``ROTATE[ENV](new_value)`` *before* it writes ``new_value`` to
  the vault, so ``vault.get`` still returns the OLD value. Each handler tries
  the old credential first and then the new one (in case the vault was already
  updated or a previous attempt half-succeeded); success with the new one is
  treated as "already applied". A handler raises RuntimeError only when it
  could not apply the change AND the service will not converge on recreate.
"""

from __future__ import annotations

import logging
import socket
from typing import Callable

import core  # Studio core — the only Studio module plugins may import

log = logging.getLogger("studio.plugins.platform")

PG_HOST, PG_USER, PG_DB = "postgres", "analyst", "bsp_edw"


# ── small helpers ──────────────────────────────────────────────────────────
def _vault(path: str, default=None):
    try:
        return core.vault.get(path, default)
    except Exception:  # VaultLocked or missing
        return default


def _secret(service: str, env: str) -> str:
    return _vault(f"services.{service}.{env}", "") or ""


def _audit(event: str, detail: str = "") -> None:
    try:
        core.audit(event, detail)
    except Exception:  # pragma: no cover
        log.info("%s %s", event, detail)


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── ROTATE handlers ────────────────────────────────────────────────────────
def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _pg_alter(role: str, new: str, login_candidates: list[str]) -> None:
    """ALTER ROLE <role> PASSWORD <new>, authenticating as the analyst
    superuser with each candidate password, then via the local-socket trust
    path inside the postgres container as a last resort."""
    stmt = f'ALTER ROLE "{role}" WITH LOGIN PASSWORD {_sql_literal(new)}'
    errors = []
    try:
        import psycopg2
        for cand in dict.fromkeys(c for c in login_candidates if c):
            try:
                conn = psycopg2.connect(host=PG_HOST, port=5432, dbname=PG_DB, user=PG_USER,
                                        password=cand, connect_timeout=5)
            except psycopg2.OperationalError as exc:
                errors.append(str(exc).strip().splitlines()[0])
                continue
            try:
                conn.autocommit = True
                with conn.cursor() as cur:
                    cur.execute(stmt)
                return
            finally:
                conn.close()
    except ImportError:
        errors.append("psycopg2 unavailable")
    # local socket (pg_hba: local all all trust in the official image)
    rc, out = core.exec_in("postgres", ["psql", "-v", "ON_ERROR_STOP=1", "-U", PG_USER,
                                        "-d", PG_DB, "-c", stmt], user="postgres", timeout=60)
    if rc != 0:
        raise RuntimeError(f"could not change the Postgres password for {role}: "
                           f"{'; '.join(errors)}; exec fallback rc={rc}: {out[-300:]}")


def rotate_pg_pass(new: str) -> None:
    _pg_alter(PG_USER, new, [_secret("postgres", "PG_PASS"), new])
    _audit("rotate.PG_PASS", "ALTER ROLE analyst applied; recreate every service using PG_PASS")


def rotate_superset_admin(new: str) -> None:
    # superset.sh syncs the admin password from env on every start, so a
    # failure here is not fatal: the recreate that follows converges.
    try:
        rc, out = core.exec_in("superset", ["superset", "fab", "reset-password",
                                            "--username", "admin", "--password", new],
                               timeout=120)
        if rc != 0:
            log.warning("superset reset-password rc=%s: %s", rc, out[-300:])
    except Exception as exc:
        log.warning("superset not running (%s); password applies on next start", exc)
    _audit("rotate.SUPERSET_ADMIN_PASS", "applied (or deferred to restart)")


def _noop(env: str) -> Callable[[str], None]:
    def apply(_new: str) -> None:
        _audit(f"rotate.{env}", "applied by container recreate")
    return apply


ROTATE: dict[str, Callable[[str], None]] = {
    "PG_PASS": rotate_pg_pass,
    "SUPERSET_ADMIN_PASS": rotate_superset_admin,
    # read from env at every start → recreate suffices
    "CODE_PASSWORD": _noop("CODE_PASSWORD"),
    "JUPYTER_TOKEN": _noop("JUPYTER_TOKEN"),
    "SUPERSET_SECRET_KEY": _noop("SUPERSET_SECRET_KEY"),  # signs out sessions
}


# ── health ─────────────────────────────────────────────────────────────────
def _http_ok(url: str, expect: str | None = None) -> tuple[bool, str]:
    try:
        r = core.http_internal("GET", url, timeout=4)
        if r.status_code >= 400:
            return False, f"HTTP {r.status_code}"
        if expect and expect not in r.text:
            return False, "starting"
        return True, "ok"
    except Exception as exc:
        return False, exc.__class__.__name__


def health() -> list[dict]:
    try:
        status = core.compose.status()
    except Exception:
        return []
    items: list[dict] = []

    def running(svc: str) -> bool:
        return (status.get(svc) or {}).get("state") in ("running", "starting", "unhealthy")

    if running("postgres"):
        ok = _port_open(PG_HOST, 5432)
        items.append({"label": "Warehouse (Postgres)", "state": "ok" if ok else "error",
                      "detail": f"postgres:5432/{PG_DB} " + ("reachable" if ok else "not answering")})
    if running("appliance"):
        ok, why = _http_ok("http://appliance:8002/pkg/api/health")
        items.append({"label": "Workspace", "state": "ok" if ok else "warning",
                      "detail": "Jupyter + Workbench ready" if ok else f"workbench: {why}"})
    if running("superset"):
        ok, why = _http_ok("http://superset:8088/superset/health")
        items.append({"label": "Superset", "state": "ok" if ok else "warning",
                      "detail": "healthy" if ok else f"/superset/health: {why}"})
    if running("code"):
        ok, why = _http_ok("http://code:8080/healthz")
        items.append({"label": "VS Code", "state": "ok" if ok else "warning",
                      "detail": "healthy" if ok else f"/healthz: {why}"})
    if running("mcp"):
        ok, why = _http_ok("http://mcp:8095/healthz")
        items.append({"label": "AI Tools (MCP)", "state": "ok" if ok else "warning",
                      "detail": "tools ready" if ok else f"/healthz: {why}"})
    if running("browser"):
        ok = _port_open("browser", 6080)
        items.append({"label": "Linux Desktop", "state": "ok" if ok else "warning",
                      "detail": "noVNC ready" if ok else "starting"})
    return items
