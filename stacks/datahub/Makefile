# DataHub side stack (optional; plan/architecture.md §8).
#
#   make up      APPLIANCE=data|ml|ai   start DataHub next to that appliance
#   make down                           stop (data kept)
#   make ps | logs [SVC=datahub-gms] | status
#   make ingest  [RECIPE="postgres dbt"]  one-shot ingestion (default: all)
#   make annotate [CATALOGS="bq_tsa_dv"] [MAX=300] [FORCE=1] [DRY=1] [PII=1]
#                                       metadata-only AI annotation (runs in the harness)
#   make reset                          delete THIS stack's data dir (asks first)
#   make env                            show resolved settings (no secrets)
#
# APPLIANCE picks ATTACH_NETWORK (bsp-data-net / bsp-ml-net / bsp-ai-net),
# DATA_ROOT (data/data, data/ml, data/ai) and DATAHUB_PORT (38081/2/3);
# each can also be overridden directly. Secrets for ingestion (PG_PASS,
# GCP_SA_JSON) are read from the calling environment only.

APPLIANCE ?= data
RECIPE    ?= all
SVC       ?=
export APPLIANCE RECIPE
# BigQuery egress for the ingest job goes through the appliance's px-proxy
INGEST_HTTPS_PROXY ?= http://px-proxy:3128
export INGEST_HTTPS_PROXY

DH := $(CURDIR)/scripts/dh.sh

.PHONY: help up down ps logs status ingest annotate reset env

help:
	@sed -n '2,15p' Makefile | sed 's/^# \{0,1\}//'

env:
	@$(DH) --print-env

up:
	@$(DH) up -d --wait --wait-timeout 1800
	@$(DH) --print-env | sed -n 's/^DATAHUB_PUBLIC_URL=/DataHub: /p'
	@echo "Sign in through the appliance (Studio gate), then as 'datahub' (password: DATAHUB_ADMIN_PASSWORD in the secrets file — see 'make env') or with Entra ID when SSO is configured."

down:
	@$(DH) --profile ingest down --remove-orphans

ps:
	@$(DH) ps -a

logs:
	@$(DH) logs -f --tail 200 $(SVC)

ingest:
	@$(DH) --profile ingest run --rm ingest

# the annotator needs bsp_common, DATAHUB_TOKEN and the LLM gateway: all in the harness container
HARNESS ?= bsp_$(APPLIANCE)-harness-1
CATALOGS ?=
MAX ?= 300
annotate:
	@docker exec -i $(HARNESS) python -u - $(foreach c,$(CATALOGS),--catalog $(c)) --max $(MAX) \
	  $(if $(FORCE),--force) $(if $(DRY),--dry-run) $(if $(PII),--pii-only) < scripts/annotate.py

status:
	@$(DH) --print-env
	@echo "--- containers"
	@$(DH) ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
	@echo "--- GMS"
	@$(DH) exec -T datahub-gms sh -c '\
	  curl -sf -o /dev/null http://localhost:8080/health && echo "health: OK" || { echo "health: DOWN"; exit 0; }; \
	  curl -s -X POST http://localhost:8080/api/graphql \
	    -H "Authorization: Basic __datahub_system:$$DATAHUB_SYSTEM_CLIENT_SECRET" -H "Content-Type: application/json" \
	    -d "{\"query\":\"{ ds: searchAcrossEntities(input:{types:[DATASET], query:\\\"*\\\", start:0, count:0}){ total } gt: searchAcrossEntities(input:{types:[GLOSSARY_TERM], query:\\\"*\\\", start:0, count:0}){ total } dm: searchAcrossEntities(input:{types:[DOMAIN], query:\\\"*\\\", start:0, count:0}){ total } }\"}" \
	  | sed "s/^/entities: /"; echo' 2>/dev/null || echo "health: stack not running (make up)"

reset:
	@root="$$($(DH) --print-env | sed -n 's/^DATA_ROOT=//p')/datahub"; \
	echo "This stops DataHub ($(APPLIANCE)) and DELETES $$root (catalog DB, search index, Kafka, secrets)."; \
	echo "Nothing else under the data root is touched."; \
	printf 'Type "delete" to continue: '; read ans; [ "$$ans" = delete ] || { echo aborted; exit 1; }; \
	$(DH) --profile ingest down --remove-orphans; \
	case "$$root" in */datahub) rm -rf -- "$$root" && echo "removed $$root";; *) echo "refusing: unexpected path $$root"; exit 1;; esac
