# shellcheck shell=bash
# Shared helpers for the base-service entrypoints in /opt/services/*.sh.
# Sourced, never executed. Every service script runs in the FOREGROUND and
# exits non-zero on a real failure so the compose restart policy retries it.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
#   svc_init superset           # sets the log prefix, refuses root
#   require_secret PG_PASS      # exit 64 with a clear message when empty
#   wait_for postgres 5432 180  # TCP wait, exit 69 on timeout

set -euo pipefail

SERVICES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export SERVICES_DIR
_SVC_NAME="${_SVC_NAME:-service}"

_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()  { printf '%s [%s] %s\n' "$(_ts)" "${_SVC_NAME}" "$*"; }
warn() { printf '%s [%s] WARN: %s\n' "$(_ts)" "${_SVC_NAME}" "$*" >&2; }
die()  {
  local code=1
  if [[ "${1:-}" =~ ^[0-9]+$ ]]; then code="$1"; shift; fi
  printf '%s [%s] FATAL: %s\n' "$(_ts)" "${_SVC_NAME}" "$*" >&2
  exit "${code}"
}

# svc_init NAME — common preamble: log prefix, non-root guard, sane umask.
svc_init() {
  _SVC_NAME="$1"
  if [[ ${EUID} -eq 0 ]]; then
    die 77 "refusing to run as root (compose sets user: 1000:1000)"
  fi
  umask 027
  log "starting as uid=$(id -u) gid=$(id -g)"
}

# require_secret VAR [VAR…] — every named env var must be non-empty.
# Secrets reach containers only through the process env of Studio's compose
# call (plan §3); an empty value means the service was started by hand.
require_secret() {
  local missing=() v
  for v in "$@"; do
    if [[ -z "${!v:-}" ]]; then missing+=("${v}"); fi
  done
  if (( ${#missing[@]} )); then
    die 64 "required secret(s) not set: ${missing[*]}. Start this service from Studio" \
           "(Launch / Services) so the vault injects its credentials — a plain" \
           "'docker compose up' has no secrets."
  fi
}

# port_open HOST PORT — single non-blocking probe.
port_open() {
  timeout 3 bash -c "</dev/tcp/$1/$2" >/dev/null 2>&1
}

# wait_for HOST PORT [TIMEOUT_S=120] — block until HOST:PORT accepts TCP.
wait_for() {
  local host="$1" port="$2" timeout_s="${3:-120}" waited=0 step=2
  log "waiting for ${host}:${port} (timeout ${timeout_s}s)"
  until port_open "${host}" "${port}"; do
    if (( waited >= timeout_s )); then
      die 69 "${host}:${port} not reachable after ${timeout_s}s"
    fi
    sleep "${step}"; waited=$(( waited + step ))
  done
  log "${host}:${port} is up (after ${waited}s)"
}

# wait_http URL [TIMEOUT_S=120] — block until URL answers 2xx/3xx.
wait_http() {
  local url="$1" timeout_s="${2:-120}" waited=0
  until curl -fsS -o /dev/null --noproxy '*' "${url}"; do
    if (( waited >= timeout_s )); then return 1; fi
    sleep 2; waited=$(( waited + 2 ))
  done
}

# retry N DELAY_S CMD… — run CMD up to N times with linear back-off.
retry() {
  local n="$1" delay="$2" i=1; shift 2
  until "$@"; do
    if (( i >= n )); then return 1; fi
    warn "attempt ${i}/${n} failed: $* — retrying in $(( delay * i ))s"
    sleep $(( delay * i )); i=$(( i + 1 ))
  done
}

# xml_escape STRING — escape a value for an XML text node.
xml_escape() {
  local s="$1"
  # (bash 5.2 treats an unescaped & in the replacement as "the match")
  s="${s//&/\&amp;}"; s="${s//</\&lt;}"; s="${s//>/\&gt;}"
  s="${s//\"/\&quot;}"; s="${s//\'/\&apos;}"
  printf '%s' "${s}"
}

# private_dir PATH — create a 0700 directory for rendered configs that carry
# secrets (container-private, never a bind mount).
private_dir() {
  mkdir -p "$1"
  chmod 700 "$1"
}

# java_home — resolve JAVA_HOME from the java on PATH.
java_home() {
  dirname "$(dirname "$(readlink -f "$(command -v java)")")"
}
