#!/usr/bin/env bash
# Trino entrypoint for the DataPlatform appliances (official trinodb/trino
# image, uid 1000). Renders catalogs into a writable dir, then execs Trino.
#   * static catalogs: /etc/trino-bsp/catalog/*.properties (warehouse, tpch, lake)
#   * bigquery + bq_<project>: from GCP_TRINO_JSON (the "Trino · BigQuery
#     catalog" cloud identity; Studio resolves its fallback to BigQuery · read)
#     or, failing that, GCP_SA_JSON. One bq_<slug> catalog per
#     GCP_TRINO_DATA_PROJECTS entry (da-tsa-dv → bq_tsa_dv); only when that
#     list is empty, a single generic `bigquery` catalog for GCP_TRINO_PROJECT.
#     Jobs / billing in GCP_TRINO_EXEC_PROJECT.
#       - service-account key → bigquery.credentials-key = base64(JSON)
#       - personal (authorized_user) sign-in → Application Default Credentials:
#         GOOGLE_APPLICATION_CREDENTIALS=/tmp/trino-gcp/adc.json (mode 600)
#     Credentials stay container-local in /tmp, never in the repo or data root.
#   * lake: skipped when /data/lake is not writable / mounted.
set -euo pipefail
SRC=/etc/trino-bsp/catalog
DST=/tmp/trino-catalog
umask 077
rm -rf "$DST"; mkdir -p "$DST"

export PG_HOST="${PG_HOST:-postgres}" PG_PORT="${PG_PORT:-5432}" PG_DB="${PG_DB:-bsp_edw}" PG_USER="${PG_USER:-analyst}"
export TRINO_UI_USER="${TRINO_UI_USER:-${USER_HANDLE:-analyst}}"
export TRINO_QUERY_MAX_MEMORY="${TRINO_QUERY_MAX_MEMORY:-2GB}"
export TRINO_QUERY_MAX_MEMORY_PER_NODE="${TRINO_QUERY_MAX_MEMORY_PER_NODE:-1GB}"
export TRINO_QUERY_MAX_EXECUTION_TIME="${TRINO_QUERY_MAX_EXECUTION_TIME:-15m}"

for f in "$SRC"/*.properties; do
  name="$(basename "$f")"
  case "$name" in
    warehouse.properties)
      if [[ -z "${PG_PASS:-}" ]]; then echo "[trino] PG_PASS not set — skipping catalog 'warehouse'"; continue; fi ;;
    billing_oracle.properties)
      if [[ -z "${BILLING_ORA_PASSWORD:-}" ]]; then echo "[trino] BILLING_ORA_PASSWORD not set — skipping catalog 'billing_oracle'"; continue; fi
      export DEMO_ORACLE_HOST="${DEMO_ORACLE_HOST:-oracle}" ;;
    care_mssql.properties)
      if [[ -z "${CARE_MSSQL_PASSWORD:-}" ]]; then echo "[trino] CARE_MSSQL_PASSWORD not set — skipping catalog 'care_mssql'"; continue; fi
      export DEMO_MSSQL_HOST="${DEMO_MSSQL_HOST:-mssql}" ;;
    lake.properties)
      if ! (umask 002; mkdir -p /data/lake/.trino-metastore /data/lake/{landing,reference,exports,samples}) 2>/dev/null \
         || [[ ! -w /data/lake ]]; then
        echo "[trino] /data/lake not writable — skipping catalog 'lake'"; continue
      fi ;;
  esac
  cp "$f" "$DST/$name"
done

GCP_KEY_SRC=GCP_SA_JSON
GCP_KEY="${GCP_SA_JSON:-}"
if [[ -n "${GCP_BQ_JSON:-}" ]]; then GCP_KEY_SRC=GCP_BQ_JSON; GCP_KEY="$GCP_BQ_JSON"; fi
if [[ -n "${GCP_TRINO_JSON:-}" ]]; then GCP_KEY_SRC=GCP_TRINO_JSON; GCP_KEY="$GCP_TRINO_JSON"; fi
data_project="${GCP_TRINO_PROJECT:-${GCP_PROJECT_ID:-to-be-named-soon}}"
exec_project="${GCP_TRINO_EXEC_PROJECT:-$data_project}"
# data projects → one bq_<slug> catalog each (da-tsa-dv → bq_tsa_dv)
IFS=',' read -r -a data_projects <<< "${GCP_TRINO_DATA_PROJECTS:-}"
bq_slug() { local s="${1,,}"; s="${s#${TRINO_BQ_STRIP_PREFIX-da-}}"; s="${s//[^a-z0-9]/_}"; printf 'bq_%s' "$s"; }
# Parent (job + Storage Read API session) project: the execution project by
# default. Table reads need bigquery.readsessions.create there; when that is
# not granted, TRINO_BQ_READ_PARENT=data uses each catalog's own data project
# (or name one project) — note that moves read / job billing to that project.
parent_for() {
  case "${TRINO_BQ_READ_PARENT:-}" in
    ""|exec) printf '%s' "$exec_project" ;;
    data) printf '%s' "$1" ;;
    *) printf '%s' "$TRINO_BQ_READ_PARENT" ;;
  esac
}
# render_bq <catalog name> <data project> <credentials line or empty>
render_bq() {
  sed -e "s|@GCP_PROJECT_ID@|$2|g" -e "s|@GCP_EXEC_PROJECT@|$(parent_for "$2")|g" -e "s|@CATALOG@|$1|g" \
      -e "s|@TRINO_BQ_METADATA_TTL@|${TRINO_BQ_METADATA_TTL:-10m}|g" \
      -e "s|^bigquery.credentials-key=@CREDENTIALS_KEY@$|$3|" \
      "$SRC/bigquery.properties.tmpl" > "$DST/$1.properties"
}
render_all_bq() {   # $1 = credentials line ("" = Application Default Credentials)
  local p n=0
  for p in "${data_projects[@]}"; do [[ -n "$(printf '%s' "$p" | tr -d '[:space:]')" ]] && n=$((n + 1)); done
  if (( n == 0 )); then
    # no data-projects list (single project, e.g. a plain service-account key): the generic catalog
    render_bq bigquery "$data_project" "$1"
    echo "[trino] catalog 'bigquery' → data project ${data_project}, execution project $(parent_for "$data_project") ($GCP_KEY_SRC, $2)"
    return
  fi
  # per-project catalogs only — no duplicate generic `bigquery` for the first project
  echo "[trino] BigQuery: ${n} data project(s) ($GCP_KEY_SRC, $2)"
  for p in "${data_projects[@]}"; do
    p="$(printf '%s' "$p" | tr -d '[:space:]')"; [[ -n "$p" ]] || continue
    render_bq "$(bq_slug "$p")" "$p" "$1"
    echo "[trino] catalog '$(bq_slug "$p")' → data project $p, execution project $(parent_for "$p")"
  done
}

if [[ "$(printf '%s' "$GCP_KEY" | tr -d '[:space:]')" == *'"type":"authorized_user"'* ]]; then
  # Personal (authorized_user) sign-in: bigquery.credentials-key takes a
  # service-account key only, so hand it to the connector as Application
  # Default Credentials (GOOGLE_APPLICATION_CREDENTIALS, container /tmp, 0600).
  # Every catalog then runs — and bills — as that one person, for every
  # appliance user (docs/platform/cloud-identities.md).
  mkdir -p /tmp/trino-gcp
  printf '%s' "$GCP_KEY" | tr -d '\r' > /tmp/trino-gcp/adc.json
  chmod 600 /tmp/trino-gcp/adc.json
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/trino-gcp/adc.json
  render_all_bq "" "personal sign-in as Application Default Credentials"
  GCP_KEY=""; GCP_KEY_ADC=1
fi
if [[ -n "$GCP_KEY" ]]; then
  raw="$(printf '%s' "$GCP_KEY" | tr -d '\r')"
  if [[ "$(printf '%s' "$raw" | tr -d '[:space:]' | cut -c1)" == "{" ]]; then
    key="$(printf '%s' "$raw" | base64 -w0)"
  else
    key="$(printf '%s' "$raw" | tr -d '[:space:]')"
    printf '%s' "$key" | base64 -d >/dev/null 2>&1 || { echo "[trino] $GCP_KEY_SRC is neither JSON nor base64 — skipping 'bigquery'"; key=""; }
  fi
  # Trino's BigQuery connector refuses (and the server then crash-loops) unless
  # the key carries every one of these fields — check them all up front.
  if [[ -n "$key" ]]; then
    decoded="$(printf '%s' "$key" | base64 -d 2>/dev/null)"
    for field in type client_id client_email private_key private_key_id; do
      if ! printf '%s' "$decoded" | grep -q "\"$field\""; then
        echo "[trino] $GCP_KEY_SRC is not a complete service-account key (no $field) — skipping 'bigquery'"; key=""; break
      fi
    done
    unset decoded
  fi
  if [[ -n "$key" ]]; then
    render_all_bq "bigquery.credentials-key=${key}" "service-account key"
  fi
  unset raw key GCP_KEY
elif [[ -z "${GCP_KEY_ADC:-}" ]]; then
  echo "[trino] GCP_TRINO_JSON / GCP_SA_JSON not set — catalog 'bigquery' not configured (BigQuery shows as not connected)"
fi

# ── egress for the bigquery catalog: JVM proxy + corporate / egress CA ──────
# (Java ignores HTTP(S)_PROXY env vars and the PEM bundles the Python
# services use, so translate them into JVM system properties.)
jopts=()
if [[ -n "${HTTPS_PROXY:-}" ]]; then
  hp="${HTTPS_PROXY#*://}"; hp="${hp%%/*}"; hp="${hp##*@}"
  jopts+=("-Dhttps.proxyHost=${hp%%:*}" "-Dhttps.proxyPort=${hp##*:}" "-Dhttp.proxyHost=${hp%%:*}" "-Dhttp.proxyPort=${hp##*:}"
          "-Dhttp.nonProxyHosts=localhost|127.0.0.1|postgres|trino|*.internal")
fi
cas=()
for f in /trust/egress-ca.pem /trust/corporate-ca.pem; do [[ -s "$f" ]] && cas+=("$f"); done
if (( ${#cas[@]} )); then
  cp "${JAVA_HOME:-/usr/lib/jvm/default}/lib/security/cacerts" /tmp/trino-cacerts 2>/dev/null \
    || cp "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")/lib/security/cacerts" /tmp/trino-cacerts
  i=0; for f in "${cas[@]}"; do
    keytool -importcert -noprompt -alias "bsp-trust-$((i++))" -file "$f" -keystore /tmp/trino-cacerts -storepass changeit >/dev/null 2>&1 \
      || echo "[trino] could not import $f into the JVM trust store"
  done
  jopts+=("-Djavax.net.ssl.trustStore=/tmp/trino-cacerts" "-Djavax.net.ssl.trustStorePassword=changeit")
fi
if (( ${#jopts[@]} )); then export JAVA_TOOL_OPTIONS="${JAVA_TOOL_OPTIONS:-} ${jopts[*]}"; fi
# The BigQuery client's Apache HTTP transport and the Storage Read API's gRPC
# channel ignore the JVM proxy properties: route both through the connector's
# own rpc-proxy settings (and trust the egress CA for the TLS it re-signs).
if [[ -n "${HTTPS_PROXY:-}" ]]; then
  for f in "$DST"/*.properties; do
    grep -q '^connector.name=bigquery$' "$f" || continue
    { echo "bigquery.rpc-proxy.enabled=true"
      echo "bigquery.rpc-proxy.uri=http://${hp}"
      if [[ -s /tmp/trino-cacerts ]]; then
        echo "bigquery.rpc-proxy.truststore-path=/tmp/trino-cacerts"
        echo "bigquery.rpc-proxy.truststore-password=changeit"
      fi; } >> "$f"
  done
fi

echo "[trino] catalogs: $(cd "$DST" && ls *.properties 2>/dev/null | sed 's/\.properties//' | tr '\n' ' ')"
umask 002   # lake files written by Trino stay readable by the workspace (uid 1000)
exec /usr/lib/trino/bin/run-trino "$@"
