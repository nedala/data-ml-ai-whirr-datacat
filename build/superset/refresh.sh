#!/usr/bin/env bash
# Re-run the idempotent Superset bootstrap (connections, datasets, the ten
# dashboards). Use after `dbt build`, or after the ML Scientist / AI Engineer
# services have created mlops.* / ccai.* / disputes.* — the "AI & ML
# Operations" dashboard then gains its missing sections.
#
#   build/superset/refresh.sh            # from the repo root (make superset-refresh)
#   build/superset/refresh.sh --verify   # … and run every chart query
# Inside the container:  /opt/venvs/superset/bin/python /opt/superset-assets/bootstrap_dashboards.py
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec docker compose exec -T superset /opt/venvs/superset/bin/python /opt/superset-assets/bootstrap_dashboards.py "$@"
