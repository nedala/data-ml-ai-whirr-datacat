#!/usr/bin/env bash
# Entry point of the one-shot `ingest` service (acryldata/datahub-ingestion).
#   RECIPE=all (default)  → domains, glossary, postgres, dbt, trino, bigquery
#   RECIPE="postgres dbt" → just those
# Sources that are not available are SKIPPED under RECIPE=all (dbt without a
# manifest, trino not reachable, bigquery without GCP_SA_JSON) but FAIL when
# named explicitly. Secrets arrive only through the environment.
set -uo pipefail

R=/opt/bsp-datahub/recipes
export DATAHUB_TELEMETRY_ENABLED=false
export DATAHUB_ENV="${DATAHUB_ENV:-PROD}"
export DBT_TARGET=/opt/bsp-datahub/dbt-target

want="${RECIPE:-all}"
explicit=1
if [ "$want" = all ]; then want="domains glossary postgres dbt trino bigquery"; explicit=0; fi

reach() { python3 - "$1" "$2" <<'PY'
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=4).close()
except OSError:
    sys.exit(1)
PY
}

# Trino catalogs: scopes from recipes/trino_scopes.yml, live list from Trino
scope_catalogs() { python3 -c 'import yaml,sys; print(" ".join(yaml.safe_load(open(sys.argv[1]))["catalogs"]))' "$R/trino_scopes.yml"; }
# bulk: true (default for bq_* catalogs) → scripts/trino_bulk.py instead of the per-table trino source
scope_bulk() { python3 -c 'import yaml,sys; c=sys.argv[2]; s=(yaml.safe_load(open(sys.argv[1]))["catalogs"].get(c) or {}); print(1 if s.get("bulk", c.startswith("bq_")) else 0)' "$R/trino_scopes.yml" "$1"; }
trino_catalogs() { python3 - <<'PY'
import json, os, urllib.request
base = f"http://{os.environ.get('TRINO_HOST', 'trino')}:{os.environ.get('TRINO_PORT', '8080')}"
req = urllib.request.Request(base + "/v1/statement", data=b"SHOW CATALOGS", headers={"X-Trino-User": "datahub"})
out, r = [], json.load(urllib.request.urlopen(req, timeout=30))
while True:
    out += [row[0] for row in r.get("data") or []]
    if not r.get("nextUri"):
        break
    r = json.load(urllib.request.urlopen(r["nextUri"], timeout=60))
print(" ".join(out))
PY
}
# trino.yml with the catalog's schema allow / deny and lineage settings applied
scoped_recipe() { python3 - "$1" "$R/trino.yml" "$R/trino_scopes.yml" <<'PY'
import sys, yaml
cat, recipe, scopes = sys.argv[1], yaml.safe_load(open(sys.argv[2])), yaml.safe_load(open(sys.argv[3]))["catalogs"]
sc = scopes.get(cat) or {}
cfg = recipe["source"]["config"]
cfg["schema_pattern"] = {"allow": sc.get("allow") or [".*"], "deny": ["^information_schema$"] + (sc.get("deny") or [])}
if sc.get("lineage"):
    cfg["catalog_to_connector_details"] = {cat: sc["lineage"]}
else:
    cfg["ingest_lineage_to_connectors"] = False
    cfg.pop("catalog_to_connector_details", None)
recipe["pipeline_name"] = f"bsp_trino_{cat}"
print(yaml.safe_dump(recipe, sort_keys=False))
PY
}

ok=(); skipped=(); failed=()
skip() { # name reason
  if [ "$explicit" = 1 ]; then echo "!! $1: $2" >&2; failed+=("$1"); else echo "-- skip $1: $2"; skipped+=("$1"); fi
}
run() { # name recipe-file
  echo "== ingest $1 ($(basename "$2"))"
  if datahub ingest run -c "$2" --report-to "/tmp/report-$1.json"; then ok+=("$1"); else failed+=("$1"); fi
}

reach datahub-gms 8080 || { echo "!! DataHub GMS (datahub-gms:8080) not reachable — is the stack up?" >&2; exit 1; }

gcp_ready=0
if [ -n "${GCP_SA_JSON:-}" ]; then
  # held on the container's tmpfs only, never written to the data root
  umask 077; mkdir -p /tmp/gcp
  case "$(printf %s "$GCP_SA_JSON" | head -c 1)" in
    "{") printf %s "$GCP_SA_JSON" > /tmp/gcp/sa.json ;;
    *)   printf %s "$GCP_SA_JSON" | base64 -d > /tmp/gcp/sa.json ;;
  esac
  export GOOGLE_APPLICATION_CREDENTIALS=/tmp/gcp/sa.json
  gcp_ready=1
fi

for r in $want; do
  case "$r" in
    domains)  run domains  "$R/domains.yml" ;;
    glossary) run glossary "$R/glossary.yml" ;;
    postgres)
      if reach "${PG_HOST:-postgres}" "${PG_PORT:-5432}"; then run postgres "$R/postgres.yml"
      else skip postgres "${PG_HOST:-postgres}:${PG_PORT:-5432} not reachable on the appliance network"; fi ;;
    dbt)
      if [ -f "$DBT_TARGET/manifest.json" ]; then
        # catalog.json (dbt docs generate) and run_results.json are optional
        # catalog.json (dbt docs generate) and run_results.json (dbt build /
        # test) are optional: drop their lines from a tmp copy when absent
        cp "$R/dbt.yml" /tmp/dbt.yml
        if [ ! -f "$DBT_TARGET/catalog.json" ]; then
          echo "   (no catalog.json — run 'dbt docs generate' for column types)"; sed -i '/catalog_path:/d' /tmp/dbt.yml; fi
        if [ ! -f "$DBT_TARGET/run_results.json" ]; then
          echo "   (no run_results.json — test results will be missing)"; sed -i '/run_results_paths:/d' /tmp/dbt.yml; fi
        run dbt /tmp/dbt.yml
      else skip dbt "no manifest.json in DBT_TARGET_DIR (run dbt build / dbt docs generate first)"; fi ;;
    trino)
      if reach "${TRINO_HOST:-trino}" "${TRINO_PORT:-8080}"; then
        live="$(trino_catalogs)"
        for cat in ${TRINO_CATALOGS:-$(scope_catalogs)}; do
          case " $live " in *" $cat "*) ;; *) echo "-- skip trino/$cat: catalog not configured in Trino"; continue ;; esac
          export TRINO_CATALOG="$cat"
          if [ "$(scope_bulk "$cat")" = 1 ]; then
            echo "== ingest trino-$cat (bulk metadata reader, scripts/trino_bulk.py)"
            if python3 /opt/bsp-datahub/scripts/trino_bulk.py "$cat" "$R/trino_scopes.yml"; then ok+=("trino-$cat"); else failed+=("trino-$cat"); fi
          else
            scoped_recipe "$cat" > "/tmp/trino-$cat.yml" && run "trino-$cat" "/tmp/trino-$cat.yml"
          fi
        done
      else skip trino "${TRINO_HOST:-trino}:${TRINO_PORT:-8080} not reachable (start the trino capability)"; fi ;;
    bigquery)
      if [ "$gcp_ready" = 1 ]; then run bigquery "$R/bigquery.yml"
      else skip bigquery "GCP_SA_JSON not set (BigQuery not connected)"; fi ;;
    *) echo "!! unknown recipe '$r' (domains glossary postgres dbt trino bigquery)" >&2; failed+=("$r") ;;
  esac
done

echo
echo "ingest summary: ok=[${ok[*]:-}] skipped=[${skipped[*]:-}] failed=[${failed[*]:-}]"
[ "${#failed[@]}" -eq 0 ]
