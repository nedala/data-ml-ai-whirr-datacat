#!/usr/bin/env bash
# DataHub side stack wrapper — resolves the appliance, data root, network and
# port, creates the data dirs + secrets on first run, derives SSO settings,
# then runs `docker compose` for stacks/datahub. Used by stacks/datahub/Makefile.
#
#   scripts/dh.sh <compose args…>          e.g. scripts/dh.sh up -d
#   scripts/dh.sh --print-env              show the resolved (non-secret) settings
#
# Env knobs: APPLIANCE=data|ml|ai, ATTACH_NETWORK, DATA_ROOT, DATAHUB_PORT,
# DATAHUB_PROJECT, DATAHUB_PUBLIC_HOST, DBT_TARGET_DIR, OIDC_* / DATAHUB_OIDC_*.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"

# optional local overrides (.env next to compose.yml); the caller's
# environment wins over the file
if [ -f "$HERE/.env" ]; then
  while IFS='=' read -r k v; do
    case "$k" in ''|\#*) continue ;; esac
    [[ "$k" =~ ^[A-Z_][A-Z0-9_]*$ ]] || continue
    [ -z "${!k+x}" ] && export "$k=$v"
  done < "$HERE/.env"
fi

APPLIANCE="${APPLIANCE:-data}"
case "$APPLIANCE" in
  data) d_net=bsp-data-net; d_root=data/data; d_port=38081; d_proj=bsp_datahub ;;
  ml)   d_net=bsp-ml-net;   d_root=data/ml;   d_port=38082; d_proj=bsp_datahub_ml ;;
  ai)   d_net=bsp-ai-net;   d_root=data/ai;   d_port=38083; d_proj=bsp_datahub_ai ;;
  *) echo "APPLIANCE must be data, ml or ai (got '$APPLIANCE')" >&2; exit 2 ;;
esac

export ATTACH_NETWORK="${ATTACH_NETWORK:-$d_net}"
export DATAHUB_PORT="${DATAHUB_PORT:-$d_port}"
PROJECT="${DATAHUB_PROJECT:-$d_proj}"

# DATA_ROOT: appliance env files use repo-relative paths (./data/data)
DATA_ROOT="${DATA_ROOT:-$d_root}"
case "$DATA_ROOT" in /*) ;; *) DATA_ROOT="$REPO/${DATA_ROOT#./}" ;; esac
export DATA_ROOT
DH="$DATA_ROOT/datahub"
SECRETS="$DH/secrets/datahub.env"

export DATAHUB_PUBLIC_URL="${DATAHUB_PUBLIC_URL:-https://${DATAHUB_PUBLIC_HOST:-localhost}:${DATAHUB_PORT}}"
export DATAHUB_UID="${DATAHUB_UID:-$(id -u)}" DATAHUB_GID="${DATAHUB_GID:-$(id -g)}"

# dbt artifacts for the dbt recipe; never let docker create a root-owned dir
DBT_TARGET_DIR="${DBT_TARGET_DIR:-$REPO/dbt_project/target}"
[ -d "$DBT_TARGET_DIR" ] || DBT_TARGET_DIR="$DH/dbt-target-empty"   # created in init_secrets
export DBT_TARGET_DIR

init_secrets() {
  # data dirs owned by the invoking user (bind mounts, no named volumes)
  mkdir -p "$DH"/{mysql,opensearch,kafka,secrets} "$DBT_TARGET_DIR"
  chmod 700 "$DH/secrets"
  if [ ! -s "$SECRETS" ]; then
    rnd() { od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'; }
    ( umask 077
      {
        echo "# DataHub side-stack secrets — generated $(date -u +%FT%TZ). NEVER commit."
        echo "DATAHUB_SYSTEM_CLIENT_SECRET=$(rnd 24)"
        echo "DATAHUB_FRONTEND_SECRET=$(rnd 32)"
        echo "DATAHUB_TOKEN_SERVICE_SIGNING_KEY=$(rnd 32)"
        echo "DATAHUB_TOKEN_SERVICE_SALT=$(rnd 16)"
        echo "DATAHUB_DB_PASSWORD=$(rnd 18)"
        echo "DATAHUB_ADMIN_PASSWORD=$(rnd 12)"
      } > "$SECRETS" )
    echo "datahub: generated secrets in $SECRETS (0600)" >&2
  fi
  chmod 600 "$SECRETS"
  # native sign-in for the root `datahub` user (replaces the image's datahub:datahub)
  local pw; pw="$(sed -n 's/^DATAHUB_ADMIN_PASSWORD=//p' "$SECRETS")"
  ( umask 077; printf 'datahub:%s\n' "$pw" > "$DH/secrets/user.props" )
  # The frontend runs as its image user (uid 100), so the file itself must be
  # readable; it sits inside the 0700 secrets dir, so on the host only the
  # owner can reach it.
  chmod 644 "$DH/secrets/user.props"
}

oidc_env() {
  # Explicit DATAHUB_OIDC_* win; else reuse Studio's Entra app settings.
  local cid="${DATAHUB_OIDC_CLIENT_ID:-${OIDC_CLIENT_ID:-}}"
  local sec="${DATAHUB_OIDC_CLIENT_SECRET:-${OIDC_CLIENT_SECRET:-}}"
  local disc="${DATAHUB_OIDC_DISCOVERY_URI:-}"
  if [ -z "$disc" ] && [ -n "${OIDC_TENANT_ID:-}" ]; then
    disc="https://login.microsoftonline.com/${OIDC_TENANT_ID}/v2.0/.well-known/openid-configuration"
  fi
  if [ -n "$cid" ] && [ -n "$sec" ] && [ -n "$disc" ]; then
    export DATAHUB_OIDC_ENABLED=true DATAHUB_OIDC_CLIENT_ID="$cid" \
           DATAHUB_OIDC_CLIENT_SECRET="$sec" DATAHUB_OIDC_DISCOVERY_URI="$disc"
  else
    export DATAHUB_OIDC_ENABLED=false
  fi
}

oidc_env

if [ "${1:-}" = "--print-env" ]; then
  cat <<EOF
APPLIANCE=$APPLIANCE
project=$PROJECT
ATTACH_NETWORK=$ATTACH_NETWORK
DATA_ROOT=$DATA_ROOT
DATAHUB_PORT=$DATAHUB_PORT
DATAHUB_PUBLIC_URL=$DATAHUB_PUBLIC_URL
DBT_TARGET_DIR=$DBT_TARGET_DIR
sso=$( [ "$DATAHUB_OIDC_ENABLED" = true ] && echo "Entra ID OIDC" || echo "native (user datahub, password in $SECRETS)")
secrets=$SECRETS
EOF
  exit 0
fi

init_secrets

# fail early (with a clear message) when the appliance network is missing
if printf ' %s ' "$@" | grep -qE ' (up|run) '; then
  if ! docker network inspect "$ATTACH_NETWORK" >/dev/null 2>&1; then
    echo "datahub: network '$ATTACH_NETWORK' not found — start the appliance first (make up APPLIANCE=$APPLIANCE)," >&2
    echo "         or set ATTACH_NETWORK to an existing network." >&2
    exit 1
  fi
fi

# .env (if any) is already in the environment (above); --env-file also stops
# compose from re-reading it with different precedence
exec docker compose -p "$PROJECT" --project-directory "$HERE" -f "$HERE/compose.yml" \
  --env-file "$SECRETS" "$@"
