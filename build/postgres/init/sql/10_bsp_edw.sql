-- bsp_edw warehouse layout (plan/architecture.md §3). Idempotent.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS raw          AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS landing      AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS staging      AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS intermediate AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS marts        AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS features     AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS mlops        AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS ai           AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS rag          AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS disputes     AUTHORIZATION analyst;
CREATE SCHEMA IF NOT EXISTS ccai         AUTHORIZATION analyst;

COMMENT ON SCHEMA raw          IS 'Synthetic ISP source data (python -m bsp_seed --target postgres)';
COMMENT ON SCHEMA landing      IS 'Anything brought in: Import & Export, integration canvas';
COMMENT ON SCHEMA staging      IS 'dbt staging models (bsp_edw)';
COMMENT ON SCHEMA intermediate IS 'dbt intermediate models (bsp_edw)';
COMMENT ON SCHEMA marts        IS 'dbt marts: dimensions, facts, analytical marts';
COMMENT ON SCHEMA features     IS 'ML feature views / offline feature store';
COMMENT ON SCHEMA mlops        IS 'Model gateway, drift monitor and retrain orchestrator state';
COMMENT ON SCHEMA ai           IS 'LLM gateway telemetry and evaluation runs';
COMMENT ON SCHEMA rag          IS 'RAG documents, chunks and embeddings (pgvector)';
COMMENT ON SCHEMA disputes     IS 'Dispute inbox, cases and SAP BRIM submissions';
COMMENT ON SCHEMA ccai         IS 'CCAI call ingest, calls, tags and taxonomy';
