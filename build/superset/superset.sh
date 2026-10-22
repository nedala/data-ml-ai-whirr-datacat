#!/usr/bin/env bash
# Apache Superset 6.x — compose service `superset` (gunicorn :8088), venv
# /opt/venvs/superset. Served natively under SUPERSET_APP_ROOT=/superset
# (nginx passes the path through unchanged). Health: /superset/healthz.
#
# 1. require SUPERSET_ADMIN_PASS, SUPERSET_SECRET_KEY, PG_PASS
# 2. wait for the warehouse server, ensure database `superset` (metadata)
# 3. `superset db upgrade` (retried), `superset init`
# 4. ensure the admin user exists and its password == SUPERSET_ADMIN_PASS
#    (in-process: the password never appears in argv / ps)
# 5. once healthy, run the idempotent bootstrap in the background:
#    connections (warehouse, Trino), datasets on marts.*, five dashboards,
#    saved queries. Re-run any time:  superset-bootstrap  (see below)
# 6. exec gunicorn in the foreground → Docker's restart policy retries
set -euo pipefail

log()  { printf '%s [superset] %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '%s [superset] WARN %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die()  { local rc=$1; shift; printf '%s [superset] FATAL %s\n' "$(date -u +%FT%TZ)" "$*" >&2; exit "$rc"; }
retry() { local n=$1 d=$2 i; shift 2; for ((i = 1; i <= n; i++)); do "$@" && return 0; warn "attempt $i/$n failed: $*"; sleep "$d"; done; return 1; }

VENV="${SUPERSET_VENV:-/opt/venvs/superset}"
[[ -x "${VENV}/bin/superset" ]] || die 70 "Superset venv not found at ${VENV} (build/requirements/superset.txt)"
export PATH="${VENV}/bin:${PATH}"
PY="${VENV}/bin/python"

for s in SUPERSET_ADMIN_PASS SUPERSET_SECRET_KEY PG_PASS; do
  [[ -n "${!s:-}" ]] || die 78 "${s} is not set — start Superset from Studio so the vault injects it"
done

ASSETS="${SUPERSET_ASSETS:-/opt/superset-assets}"
export SUPERSET_HOME="${SUPERSET_HOME:-/data/superset}"
export SUPERSET_CONFIG_PATH="${SUPERSET_CONFIG_PATH:-${ASSETS}/superset_config.py}"
export SUPERSET_APP_ROOT="${SUPERSET_APP_ROOT:-/superset}"
export SUPERSET_ADMIN_USER="${SUPERSET_ADMIN_USER:-admin}"
export PG_HOST="${PG_HOST:-postgres}" PG_PORT="${PG_PORT:-5432}" PG_USER="${PG_USER:-analyst}" PG_DB="${PG_DB:-bsp_edw}"
export SUPERSET_META_DB="${SUPERSET_META_DB:-superset}"
export FLASK_APP="superset.app:create_app()"
export SUPERSET_LOAD_EXAMPLES=no
PORT="${SUPERSET_PORT:-8088}"
WORKERS="${SUPERSET_WORKERS:-2}"
THREADS="${SUPERSET_THREADS:-8}"

[[ -f "${SUPERSET_CONFIG_PATH}" ]] || die 70 "Superset config not found at ${SUPERSET_CONFIG_PATH}"
mkdir -p "${SUPERSET_HOME}/cache" 2>/dev/null || true
[[ -w "${SUPERSET_HOME}" ]] || die 73 "${SUPERSET_HOME} is not writable by uid $(id -u) (host dir \${DATA_ROOT}/superset must be owned by 1000)"

log "waiting for ${PG_HOST}:${PG_PORT}"
"${PY}" - <<'PY' || die 75 "warehouse server ${PG_HOST}:${PG_PORT} not reachable"
import os, socket, time
end = time.time() + 300
while time.time() < end:
    try:
        socket.create_connection((os.environ["PG_HOST"], int(os.environ["PG_PORT"])), 3).close()
        raise SystemExit(0)
    except OSError:
        time.sleep(3)
raise SystemExit(1)
PY

ensure_meta_db() {
  "${PY}" - <<'PY'
import os, psycopg2
from psycopg2 import sql
kw = dict(host=os.environ["PG_HOST"], port=os.environ["PG_PORT"], user=os.environ["PG_USER"],
          password=os.environ["PG_PASS"], connect_timeout=5)
name = os.environ["SUPERSET_META_DB"]
try:
    psycopg2.connect(dbname=name, **kw).close()
    print(f"metadata database {name!r} present")
except psycopg2.OperationalError as exc:
    if "does not exist" not in str(exc):
        raise
    conn = psycopg2.connect(dbname=os.environ["PG_DB"], **kw)
    conn.autocommit = True
    conn.cursor().execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    conn.close()
    print(f"created metadata database {name!r}")
PY
}
retry 5 3 ensure_meta_db || die 75 "could not ensure the ${SUPERSET_META_DB} metadata database (needs CREATEDB or the postgres init to create it)"

log "superset db upgrade"
retry 5 5 superset db upgrade || die 76 "superset db upgrade failed"
log "superset init (roles + permissions)"
retry 3 5 superset init || die 76 "superset init failed"

log "ensuring admin user '${SUPERSET_ADMIN_USER}'"
"${PY}" - <<'PY' || die 76 "could not create/sync the Superset admin user"
import os, sys
from superset.app import create_app

app = create_app()
with app.app_context():
    from superset import security_manager as sm
    from superset.extensions import db

    name, pw = os.environ["SUPERSET_ADMIN_USER"], os.environ["SUPERSET_ADMIN_PASS"]
    role = sm.find_role("Admin")
    if role is None:
        sys.exit("Admin role missing — superset init did not complete")
    user = sm.find_user(username=name)
    if user is None:
        if not sm.add_user(name, "Superset", "Admin", f"{name}@appliance.local", role, password=pw):
            sys.exit("add_user failed")
        print(f"created admin user {name}")
    else:
        sm.reset_password(user.id, pw)
        if role not in user.roles:
            user.roles.append(role)
        user.active = True
        db.session.commit()
        print(f"admin user {name}: password synced from vault")
PY

# ── idempotent connection / dataset / dashboard bootstrap ──────────────────
BOOTSTRAP="${SUPERSET_BOOTSTRAP:-${ASSETS}/bootstrap_dashboards.py}"
if [[ -f "${BOOTSTRAP}" && "${SUPERSET_SKIP_BOOTSTRAP:-0}" != "1" ]]; then
  (
    SUPERSET_URL="http://127.0.0.1:${PORT}${SUPERSET_APP_ROOT}" \
      "${PY}" "${BOOTSTRAP}" --wait 300 2>&1 | sed 's/^/[superset-bootstrap] /' \
      || warn "dashboard bootstrap failed (Superset itself is fine; re-run: docker compose exec superset ${PY} ${BOOTSTRAP})"
  ) &
fi

log "exec gunicorn on 0.0.0.0:${PORT} (app root ${SUPERSET_APP_ROOT}, ${WORKERS}x${THREADS})"
exec gunicorn \
  --bind "0.0.0.0:${PORT}" \
  --workers "${WORKERS}" \
  --worker-class gthread \
  --threads "${THREADS}" \
  --timeout "${SUPERSET_WEBSERVER_TIMEOUT:-300}" \
  --keep-alive 5 \
  --limit-request-line 0 \
  --limit-request-field_size 0 \
  --forwarded-allow-ips '*' \
  --access-logfile - \
  --error-logfile - \
  "superset.app:create_app()"
