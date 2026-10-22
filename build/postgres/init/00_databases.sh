#!/usr/bin/env bash
# First-boot database setup (runs only on an EMPTY data directory; services
# that need their own database also converge it on every start via
# build/services/pgbootstrap.py, so upgraded data directories are covered).
#
#   bsp_edw   the warehouse (POSTGRES_DB) — schemas below, owned by analyst,
#             with the pgvector extension
#   superset  Superset metadata
#   mlflow    MLflow tracking metadata
#
# No password is ever hardcoded here.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" <<'SQL'
SELECT format('CREATE DATABASE superset OWNER %I', current_user)
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'superset')\gexec
SELECT format('CREATE DATABASE mlflow OWNER %I', current_user)
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'mlflow')\gexec
SQL

psql -v ON_ERROR_STOP=1 --username "${POSTGRES_USER}" --dbname "${POSTGRES_DB}" -f /docker-entrypoint-initdb.d/sql/10_bsp_edw.sql
