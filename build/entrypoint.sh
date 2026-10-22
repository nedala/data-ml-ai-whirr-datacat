#!/usr/bin/env bash
# DataPlatform Data & AI Appliance — WORKSPACE entrypoint (compose service `appliance`).
#
# Runs, supervised, as uid 1000:
#   jupyter   :8888  /jupyter/   (token = JUPYTER_TOKEN, required)
#   workbench :8002  /pkg/ /files/ /ingress/ /dbt/  (FastAPI, /opt/workbench)
#   sshd      :2222              (only if SSH_ENABLED=1)
#
# code-server, Superset, Postgres, nginx and the egress proxy are separate
# compose services.
#
#   entrypoint.sh                      start the workspace (default; also `start`)
#   entrypoint.sh refresh-templates    copy NEW template files into ~/work
#                                      (work_templates/common + the persona folder);
#                                      changed templates go to ~/work/.templates-updates/
#   entrypoint.sh seed                 synthetic ISP data → warehouse + lake
#                                      (python -m bsp_seed), then dbt deps/build (foreground)
#   entrypoint.sh <cmd…>               exec an arbitrary command (debug)
set -euo pipefail

: "${USER_HANDLE:=analyst}"
: "${PERSIST_VOL:=/home/analyst/work}"
: "${LAKE_DIR:=/data/lake}"
export LAKE_DIR
TEMPLATES="${TEMPLATES_DIR:-/opt/templates}"
LOG_DIR="${LOG_DIR:-/data/logs}"
SEED_MARKER="${LOG_DIR}/.sample-data-seeded"

log()  { printf '%s [workspace] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '%s [workspace] WARN: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf '%s [workspace] FATAL: %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 64; }

# ── template seeding: new files only, never overwrite ────────────────────
# Manifest ~/work/.templates-manifest.json records every file we seeded (and
# its template hash) so a file the user DELETED is not re-seeded, and a
# template that changed upstream lands in .templates-updates/<path> instead
# of replacing the user's copy.
refresh_templates() {
  [[ -d "${TEMPLATES}" ]] || { warn "no templates at ${TEMPLATES}"; return 0; }
  python - "${TEMPLATES}" "${PERSIST_VOL}" "${APPLIANCE_PERSONA:-data-pro}" <<'PY'
import hashlib, json, os, shutil, sys
src_root, work, persona = sys.argv[1], sys.argv[2], sys.argv[3]
# work_templates/common + the persona's own folder at the top of ~/work, and the
# OTHER personas' notebooks in ~/work/<data|ml|ai>/ — an appliance can run any
# capability (MLOps, AI tools…), so every notebook set is always at hand.
PERSONA_DIRS = {"data-pro": "data", "ml-scientist": "ml", "ai-engineer": "ai"}
own = PERSONA_DIRS.get(persona)
SOURCES = [("common", "")] + ([(own, "")] if own else []) + [(d, d) for d in ("data", "ml", "ai") if d != own]
SKIP_DIRS = {"__pycache__", ".ipynb_checkpoints", ".git", "target", "dbt_packages", "logs", "node_modules"}
manifest_path = os.path.join(work, ".templates-manifest.json")
updates_root = os.path.join(work, ".templates-updates")
try:
    manifest = json.load(open(manifest_path))
except (OSError, ValueError):
    manifest = {}

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

# template files that land at the top of ~/work under another name
RENAME = {"WORKSPACE_README.md": "README.md", "WORKSPACE_MCP.json": ".mcp.json"}

new = upd = 0
def seed(src, rel):
    """Copy a NEW file; never overwrite. A file the user deleted is not
    re-seeded; a template that changed upstream goes to .templates-updates/."""
    global new, upd
    dst = os.path.join(work, rel)
    tsha = sha(src)
    if not os.path.exists(dst):
        if rel in manifest:          # user deleted it — respect that
            return
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        os.chmod(dst, 0o644)
        manifest[rel] = tsha
        new += 1
        return
    manifest.setdefault(rel, tsha)
    if sha(dst) == tsha or manifest.get(rel) == tsha:
        return                        # identical, or already offered
    out = os.path.join(updates_root, rel)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    shutil.copy2(src, out)
    manifest[rel] = tsha
    upd += 1

for source, prefix in SOURCES:
    base = os.path.join(src_root, source)
    if not os.path.isdir(base):
        continue
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith((".pyc", ".DS_Store")):
                continue
            src = os.path.join(dirpath, fn)
            rel = os.path.relpath(src, base)
            rel = RENAME.get(rel, rel) if not prefix else rel
            seed(src, os.path.join(prefix, rel) if prefix else rel)
tmp = manifest_path + ".tmp"
with open(tmp, "w") as f:
    json.dump(manifest, f, indent=1, sort_keys=True)
os.replace(tmp, manifest_path)
print(f"[workspace] templates ({', '.join(p or s for s, p in SOURCES)}): {new} new file(s) copied, {upd} updated template(s) "
      f"placed in {updates_root if upd else '(none)'}")
PY
}

# ── sample data → warehouse, then dbt (once; marker file) ────────────────
pg_ready() {
  python - <<'PY' >/dev/null 2>&1
import os, psycopg2
psycopg2.connect(host=os.environ.get("PG_HOST", "postgres"), port=int(os.environ.get("PG_PORT", "5432")),
                 dbname=os.environ.get("PG_DB", "bsp_edw"), user=os.environ.get("PG_USER", "analyst"),
                 password=os.environ["PG_PASS"], connect_timeout=5).close()
PY
}

auto_seed() {
  [[ -n "${PG_PASS:-}" ]] || { log "seed: PG_PASS not set — skipped"; return 0; }
  [[ -f "${SEED_MARKER}" ]] && { log "seed: already done ($(cat "${SEED_MARKER}")) — delete ${SEED_MARKER} or run 'entrypoint.sh seed' to re-run"; return 0; }
  local ok=0
  python -c "import bsp_seed" 2>/dev/null || { warn "seed: bsp_seed not importable (PYTHONPATH=${PYTHONPATH:-}) — skipped"; return 0; }
  for _ in $(seq 1 60); do                       # wait up to ~10 min for Postgres
    if pg_ready; then ok=1; break; fi
    sleep 10
  done
  [[ ${ok} -eq 1 ]] || { warn "seed: warehouse ${PG_HOST:-postgres}:${PG_PORT:-5432} not reachable — skipped"; return 0; }
  python /opt/services/pgbootstrap.py edw || warn "seed: could not ensure the bsp_edw schemas"
  log "seed: loading the synthetic ISP dataset (raw schema + lake parquet)"
  python -m bsp_seed --target postgres --target parquet || { warn "seed: bsp_seed failed (will retry on next start)"; return 0; }
  date -u +%Y-%m-%dT%H:%M:%SZ > "${SEED_MARKER}"
  local proj="${PERSIST_VOL}/dbt_project"
  if [[ -f "${proj}/dbt_project.yml" ]]; then
    log "seed: dbt deps + build (target ${DBT_TARGET:-postgres})"
    ( cd "${proj}" \
      && { [[ ! -f packages.yml && ! -f dependencies.yml ]] || dbt deps --profiles-dir . -q; } \
      && dbt build --profiles-dir . --target "${DBT_TARGET:-postgres}" -q \
      && dbt docs generate --profiles-dir . --target "${DBT_TARGET:-postgres}" -q ) \
      && log "seed: dbt project built" || warn "seed: dbt build had errors (see ${LOG_DIR}/auto-seed.log)"
  else
    log "seed: no dbt_project/dbt_project.yml yet — dbt build skipped"
  fi
}

case "${1:-start}" in
  refresh-templates) refresh_templates; exit 0 ;;
  seed) mkdir -p "${LOG_DIR}" 2>/dev/null || true; rm -f "${SEED_MARKER}"; auto_seed; exit 0 ;;
  start) ;;
  *) exec "$@" ;;
esac

# ── 1. never root ────────────────────────────────────────────────────────
[[ ${EUID} -ne 0 ]] || die "refusing to run as root (compose sets user: 1000:1000)"
log "starting as uid=$(id -u) gid=$(id -g) handle=${USER_HANDLE}"
[[ -n "${JUPYTER_TOKEN:-}" ]] || die "JUPYTER_TOKEN is not set — start the workspace from Studio so the vault injects it"

# ── 2. dirs (bind mounts may be fresh) ───────────────────────────────────
mkdir -p "${PERSIST_VOL}" "${LOG_DIR}" /data/pyuser 2>/dev/null || true
# Google ADC (vault key → RAM file) + OpenCode defaults for every tool started below
# shellcheck source=build/services/gcp-adc.sh
[[ -r /opt/services/gcp-adc.sh ]] && source /opt/services/gcp-adc.sh

mkdir -p "${LAKE_DIR}"/{landing,reference,exports,samples} 2>/dev/null \
  || warn "${LAKE_DIR} is not writable — check host ownership of ./data/lake"
for d in "${PERSIST_VOL}" /data/pyuser "${LAKE_DIR}"; do
  [[ -w "$d" ]] || warn "$d is not writable by uid $(id -u) — check host ownership of the bind mount"
done
# the shared lake, visible from JupyterLab's file browser (rooted at ~/work)
[[ -e "${PERSIST_VOL}/lake" || -L "${PERSIST_VOL}/lake" ]] || ln -s "${LAKE_DIR}" "${PERSIST_VOL}/lake" 2>/dev/null || true

# ── 3. workspace templates (first run seeds everything; later runs add new
#      files only and park changed templates in ~/work/.templates-updates/) ─
refresh_templates || warn "template refresh failed (workspace still starts)"

# ── 4. IPython defaults: `from lib import …` from any notebook ────────────
IPY_STARTUP="${HOME}/.ipython/profile_default/startup"
mkdir -p "${IPY_STARTUP}"
cat > "${IPY_STARTUP}/00-appliance.py" <<'PY'
# Written by the workspace entrypoint on every start — edit 99-*.py instead.
import os as _os, sys as _sys

for _p in (_os.path.expanduser("~/work"), "/opt/templates"):
    if _os.path.isdir(_os.path.join(_p, "lib")) and _p not in _sys.path:
        _sys.path.append(_p)
del _p
PY

# ── 5. supervisor: restart any child that dies, with back-off ────────────
PIDS=()
supervise() {
  local name="$1"; shift
  (
    trap 'kill -TERM "${child:-0}" 2>/dev/null; wait "${child:-0}" 2>/dev/null; exit 0' TERM INT
    backoff=2
    while true; do
      started=$(date +%s)
      # redact credentials some tools print in their banner URLs
      "$@" > >(sed -u -E -e "s/((access_)?token=)[^[:space:]&\"']+/\1***/g" -e "s/^/[${name}] /" \
                 | tee -a "${LOG_DIR}/${name}.log") 2>&1 &
      child=$!
      set +e; wait "${child}"; rc=$?; set -e
      ran=$(( $(date +%s) - started ))
      (( ran > 60 )) && backoff=2
      warn "${name} exited (rc=${rc}) after ${ran}s — restarting in ${backoff}s"
      sleep "${backoff}"
      backoff=$(( backoff * 2 > 60 ? 60 : backoff * 2 ))
    done
  ) &
  PIDS+=("$!")
  log "supervising ${name} (pid $!)"
}
shutdown() {
  log "shutting down children"
  kill -TERM "${PIDS[@]}" 2>/dev/null || true
  wait || true
  exit 0
}
trap shutdown TERM INT

# Appliance typography for JupyterLab (custom.css, refreshed on every start).
if [[ -f "${UI_KIT_DIR:-/opt/ui-kit}/jupyter.css" ]]; then
  mkdir -p "${HOME}/.jupyter/custom"
  cp -f "${UI_KIT_DIR:-/opt/ui-kit}/jupyter.css" "${HOME}/.jupyter/custom/custom.css"
fi
# No "Jupyter news" prompt (it polls the internet); only if the user hasn't chosen
NEWS="${HOME}/.jupyter/lab/user-settings/@jupyterlab/apputils-extension/notification.jupyterlab-settings"
if [[ ! -f "${NEWS}" ]]; then
  mkdir -p "$(dirname "${NEWS}")"
  printf '{\n  "fetchNews": "false",\n  "checkForUpdates": false\n}\n' > "${NEWS}"
fi

# Jupyter. Token via env (JUPYTER_TOKEN is read by IdentityProvider), not argv.
supervise jupyter jupyter lab \
  --no-browser --ip=0.0.0.0 --port=8888 \
  --ServerApp.base_url=/jupyter/ \
  --ServerApp.root_dir="${PERSIST_VOL}" \
  --ServerApp.trust_xheaders=True \
  --ServerApp.allow_remote_access=True \
  --LabApp.check_for_updates_class=jupyterlab.NeverCheckForUpdate \
  --LabApp.custom_css=True \
  --KernelSpecManager.ensure_native_kernel=True

# Workbench (packages, files, import & export, dbt Studio).
if [[ -f /opt/workbench/app.py ]]; then
  supervise workbench python -m uvicorn app:app --app-dir /opt/workbench \
    --host 0.0.0.0 --port 8002 --proxy-headers --forwarded-allow-ips '*'
else
  warn "/opt/workbench/app.py missing — /pkg/, /files/, /ingress/ and /dbt/ unavailable"
fi

# The Linux Desktop's DevTools at http://127.0.0.1:9222 inside this container
# too (Playwright / CDP clients in notebooks and terminals); services use
# BROWSER_CDP_URL=http://browser:9223 directly.
if [[ "${CDP_FORWARD:-1}" == "1" && -f /opt/services/cdp_forward.py ]]; then
  supervise cdp python /opt/services/cdp_forward.py
fi

# Optional seamless SSH key login
if [[ "${SSH_ENABLED:-0}" == "1" ]] && command -v ssh-setup.sh >/dev/null; then
  supervise sshd ssh-setup.sh
fi

# ── 6. first-run sample data (AUTO_SEED=0 to skip) ───────────────────────
# Loads the synthetic ISP dataset into the warehouse + lake and builds the
# bsp_edw dbt project once (marker: ${SEED_MARKER}); re-run any time with
# `entrypoint.sh seed` (or `make seed APPLIANCE=…`).
if [[ "${AUTO_SEED:-1}" == "1" ]]; then
  auto_seed >>"${LOG_DIR}/auto-seed.log" 2>&1 &
fi

log "workspace up: /jupyter/ /pkg/ /files/ /ingress/ /dbt/"
# Block on the supervisors (they never exit on their own).
while true; do wait || true; sleep 1; done
