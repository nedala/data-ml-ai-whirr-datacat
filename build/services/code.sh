#!/usr/bin/env bash
# code-server (browser VS Code) — compose service `code` (:8080).
#
# nginx serves it at /code/ and STRIPS the prefix; code-server emits only
# relative URLs, so no base-path flag is needed for the app itself.
# --abs-proxy-base-path /code makes its port-forward links (/absproxy/<port>)
# come out as /code/absproxy/<port>.
#
# Auth: password (argon2id hash in config.yaml — the clear password is never
# written to disk or argv). The facade session is the outer gate.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
svc_init code
# Google ADC (vault key → RAM file) + OpenCode defaults for every tool started below
# shellcheck source=build/services/gcp-adc.sh
[[ -r /opt/services/gcp-adc.sh ]] && source /opt/services/gcp-adc.sh

require_secret CODE_PASSWORD
command -v code-server >/dev/null || die 70 "code-server is not installed in this image"

HOME="${HOME:-/home/analyst}"
WORK="${CODE_WORKSPACE:-${HOME}/work}"
CFG_DIR="${HOME}/.config/code-server"
DATA_DIR="${XDG_DATA_HOME:-${HOME}/.local/share}/code-server"
USER_SETTINGS="${DATA_DIR}/User/settings.json"
PORT="${CODE_PORT:-8080}"

mkdir -p "${WORK}" "${DATA_DIR}/User" "${DATA_DIR}/extensions"
private_dir "${CFG_DIR}"

# ── password → argon2id (code-server 4.x verifies argon2 encoded hashes) ──
# The hash is cached (0600, persisted volume) and reused while it still
# verifies, so browser sessions (cookie = hash) survive container restarts
# and are invalidated exactly when the password is rotated.
export CODE_HASH_CACHE="${DATA_DIR}/.password-hash"
HASH="$(python - <<'PY'
import os, sys
try:
    from argon2 import PasswordHasher
    from argon2.exceptions import Argon2Error
except ImportError:
    sys.exit("argon2-cffi missing from the image")
ph, pw, cache = PasswordHasher(), os.environ["CODE_PASSWORD"], os.environ["CODE_HASH_CACHE"]
try:
    old = open(cache).read().strip()
    ph.verify(old, pw)
    print(old); sys.exit(0)
except (OSError, Argon2Error, ValueError):
    pass
h = ph.hash(pw)
fd = os.open(cache, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(h)
print(h)
PY
)" || die 70 "could not hash CODE_PASSWORD (argon2-cffi missing?)"

# trusted origins: the facade URL(s) the browser actually uses
TRUSTED=()
for o in ${PUBLIC_FACADE_URL:-} ${CODE_TRUSTED_ORIGINS:-}; do
  o="${o#https://}"; o="${o#http://}"; o="${o%%/*}"
  [[ -n "$o" ]] && TRUSTED+=("$o")
done

umask 077
{
  echo "bind-addr: 0.0.0.0:${PORT}"
  echo "auth: password"
  printf 'hashed-password: "%s"\n' "${HASH}"
  echo "cert: false"
  echo "disable-telemetry: true"
  echo "disable-update-check: true"
  echo "disable-getting-started-override: true"
  echo "disable-workspace-trust: true"
  echo "abs-proxy-base-path: /code"
  echo "app-name: DataPlatform VS Code"
  echo "user-data-dir: ${DATA_DIR}"
  echo "extensions-dir: ${DATA_DIR}/extensions"
  if (( ${#TRUSTED[@]} )); then
    echo "trusted-origins:"
    for o in "${TRUSTED[@]}"; do printf '  - "%s"\n' "$o"; done
  fi
} > "${CFG_DIR}/config.yaml"
umask 027
unset HASH

# ── default user settings (written once; the user owns them afterwards) ──
if [[ ! -f "${USER_SETTINGS}" ]]; then
  cat > "${USER_SETTINGS}" <<EOF
{
  "telemetry.telemetryLevel": "off",
  "update.mode": "none",
  "extensions.autoUpdate": false,
  "extensions.autoCheckUpdates": false,
  "workbench.startupEditor": "readme",
  "security.workspace.trust.enabled": false,
  "python.defaultInterpreterPath": "/usr/local/bin/python3",
  "python.venvPath": "/opt/kernels",
  "python.terminal.activateEnvironment": false,
  "jupyter.askForKernelRestart": false,
  "jupyter.kernels.excludePythonEnvironments": [],
  "terminal.integrated.cwd": "${WORK}",
  "terminal.integrated.defaultProfile.linux": "bash",
  "files.exclude": {"**/.ipynb_checkpoints": true, "**/__pycache__": true},
  "dbt.queryLimit": 500
}
EOF
  log "wrote default VS Code settings"
elif [[ -d /opt/kernels ]]; then
  # Existing settings: add the extra-kernel venv folder once (plain JSON only;
  # a file with comments is left untouched).
  python - "${USER_SETTINGS}" <<'PY' || warn "could not add python.venvPath to ${USER_SETTINGS}"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    sys.exit(0)
changed = False
if isinstance(data, dict) and "python.venvPath" not in data:
    data["python.venvPath"] = "/opt/kernels"; changed = True
# earlier default "none" → show ~/work/README.md (the workspace guide) on open
if isinstance(data, dict) and data.get("workbench.startupEditor") == "none":
    data["workbench.startupEditor"] = "readme"; changed = True
if changed:
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
PY
fi

# Extensions baked into the image live in /opt/code-server/extensions; seed
# them into the per-user dir (copy-if-missing, so user updates survive).
if [[ -d /opt/code-server/extensions ]]; then
  seeded=0
  for ext in /opt/code-server/extensions/*/; do
    [[ -d "${ext}" ]] || continue
    name="$(basename "${ext}")"
    if [[ ! -e "${DATA_DIR}/extensions/${name}" ]]; then
      cp -a "${ext}" "${DATA_DIR}/extensions/${name}"; seeded=$(( seeded + 1 ))
    fi
  done
  # VS Code rebuilds its extensions.json manifest by scanning the folder
  # when the manifest is absent — only force that when we added something.
  if (( seeded )); then
    rm -f "${DATA_DIR}/extensions/extensions.json"
    log "seeded ${seeded} baked-in extension(s)"
  fi
fi

# The appliance browser's DevTools at http://127.0.0.1:9222 for code and
# terminals in VS Code (same address as on the laptop).
if [[ "${CDP_FORWARD:-1}" == "1" ]]; then
  python "$(dirname "${BASH_SOURCE[0]}")/cdp_forward.py" &
fi

log "exec code-server on 0.0.0.0:${PORT} (folder ${WORK}, trusted origins: ${TRUSTED[*]:-none})"
exec code-server --config "${CFG_DIR}/config.yaml" "${WORK}"
