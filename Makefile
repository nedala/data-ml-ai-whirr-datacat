# DataPlatform Data & AI Appliances — make targets.
#   make build                      workspace + Linux Desktop images
#   make up APPLIANCE=data|ml|ai    start one appliance's control plane, then use Studio
#   make up-all                     all three side by side
SHELL := /bin/bash
.DEFAULT_GOAL := help

APPLIANCE ?= data
APPLIANCES := data ml ai
ENV_FILE := appliances/$(APPLIANCE).env
ANTIGRAVITY ?= 1

# Host path of the repo — Studio needs it to bind real host paths when it
# starts services (it runs compose from inside a container).
export HOST_PROJECT_ROOT ?= $(CURDIR)

-include .env
-include $(ENV_FILE)

IMAGE := $(or $(APPLIANCE_IMAGE),bsp/appliance:latest)
BROWSER := $(or $(BROWSER_IMAGE),bsp/browser:latest)
COMPOSE = docker compose --env-file .env --env-file appliances/$(1).env
FACADE = https://$(or $(PUBLIC_FACADE_HOST),localhost):$(FACADE_PORT)
BUILD_PROXY = $(if $(BUILD_HTTP_PROXY),--build-arg HTTP_PROXY=$(BUILD_HTTP_PROXY) --build-arg HTTPS_PROXY=$(BUILD_HTTP_PROXY))
OPEN := $(shell command -v xdg-open || command -v open || echo echo)

.PHONY: help build build-workspace build-browser up up-all down down-all ps logs open admin-reset tunnel seed superset-refresh lint test test-studio test-px test-workbench dirs corporate-ca datahub-up datahub-down datahub-ingest datahub-annotate datahub-status mlops-lifecycle mlops-evidence test-mlops

help: ## Show targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' Makefile | awk 'BEGIN{FS=":.*?## "};{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'
	@echo "  APPLIANCE=$(APPLIANCE) (data | ml | ai)   ANTIGRAVITY=$(ANTIGRAVITY) (0 = Linux Desktop without Antigravity)"

## ── Images ──────────────────────────────────────────────────────────────
build: build-workspace build-browser ## Build bsp/appliance + bsp/browser (ANTIGRAVITY=0 to leave Antigravity out)

build-workspace: ## Build the workspace image (bsp/appliance)
	docker build -t $(IMAGE) $(BUILD_PROXY) .

build-browser: ## Build the Linux Desktop image (bsp/browser, FROM the workspace image)
	docker build -t $(BROWSER) --build-arg BASE_IMAGE=$(IMAGE) --build-arg INSTALL_ANTIGRAVITY=$(ANTIGRAVITY) $(BUILD_PROXY) build/browser

## ── Everyday ────────────────────────────────────────────────────────────
.env:
	cp .env.example .env

dirs: .env ## Create the appliance's data root (DATA_ROOT) with the right ownership
	@test -f $(ENV_FILE) || { echo "unknown APPLIANCE=$(APPLIANCE) (use data | ml | ai)"; exit 2; }
	@mkdir -p $(DATA_ROOT)/{egress/ca,trust,tls,vault,studio,lake,warehouse,mlops} && chmod 700 $(DATA_ROOT)/vault $(DATA_ROOT)/egress/ca
	@if [ "$$(id -u)" != 1000 ]; then \
	  docker run --rm --user 0 --entrypoint chown -v "$(abspath $(DATA_ROOT))":/d $(IMAGE) -R 1000:1000 /d/egress /d/trust /d/lake /d/mlops; fi

up: dirs ## Start one appliance (APPLIANCE=data|ml|ai): nginx · studio · px-proxy · docs
	$(call COMPOSE,$(APPLIANCE)) up -d
	@echo "→ $(FACADE)/studio/   ($(APPLIANCE): sign in, then press Start)"

up-all: ## Start all three appliances side by side
	@for a in $(APPLIANCES); do $(MAKE) --no-print-directory up APPLIANCE=$$a || exit 1; done

down: ## Stop one appliance and every capability it runs (its data root is kept)
	$(call COMPOSE,$(APPLIANCE)) --profile '*' down

down-all: ## Stop all three appliances
	@for a in $(APPLIANCES); do $(MAKE) --no-print-directory down APPLIANCE=$$a; done

ps: ## Running services of one appliance
	$(call COMPOSE,$(APPLIANCE)) --profile '*' ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'

logs: ## Follow control-plane logs of one appliance
	$(call COMPOSE,$(APPLIANCE)) logs -f --tail=100 studio px-proxy nginx

open: ## Open Studio for one appliance in the browser
	$(OPEN) "$(FACADE)/studio/"

tunnel: ## Print the SSH command to run ON YOUR LAPTOP (browser elsewhere): HOST=<docker host> USER=<ssh user>
	@h="$(or $(HOST),$(shell hostname -I | awk '{print $$1}'))"; u="$(or $(SSH_USER),$(shell whoami))"; \
	echo "# On your laptop — keep it running, then browse https://localhost:$(FACADE_PORT)/studio/"; \
	echo "ssh -N -o ExitOnForwardFailure=yes \\"; \
	for p in $(FACADE_PORT) $(SSO_CALLBACK_PORT) $(NOVNC_PORT) $(OPENWEBUI_PORT) $(or $(DATAHUB_PORT),38081) $(CDP_PORT) $(MCP_PORT); do \
	  echo "  -L $$p:127.0.0.1:$$p \\"; done; \
	echo "  $$u@$$h"

admin-reset: ## Reset (or with CREATE=1 create) a local Studio admin: ADMIN=admin; prompts, or prints a generated password once
	$(call COMPOSE,$(APPLIANCE)) exec studio python /opt/studio/admin_reset.py $(or $(ADMIN),admin) $(if $(CREATE),--create) $(if $(GENERATE),--generate)

seed: ## Synthetic ISP data → warehouse + lake, then dbt build (needs the Workspace running)
	$(call COMPOSE,$(APPLIANCE)) exec appliance /usr/local/bin/entrypoint.sh seed

superset-refresh: ## Re-run the Superset bootstrap (connections, datasets, dashboards); VERIFY=1 runs every chart query
	$(call COMPOSE,$(APPLIANCE)) exec -T superset /opt/venvs/superset/bin/python /opt/superset-assets/bootstrap_dashboards.py $(if $(VERIFY),--verify)

## ── MLOps (ML Scientist) ────────────────────────────────────────────────
mlops-lifecycle: ## Full model lifecycle with release gates + four-eyes approval: REQUESTER=<you> APPROVER=<other> [FRESH=1] [ACCOUNTS=5000]
	$(call COMPOSE,$(APPLIANCE)) exec -T mlops python -m bsp_mlops.demo --requester "$(or $(REQUESTER),$(USER))" \
	  $(if $(APPROVER),--approver "$(APPROVER)",--wait-approval 15) $(if $(FRESH),--fresh-release-train) \
	  --accounts $(or $(ACCOUNTS),5000) --trials $(or $(TRIALS),6) --no-notebook

mlops-evidence: ## Print the A–G capability checklist with live evidence links (MD=1 for markdown)
	$(call COMPOSE,$(APPLIANCE)) exec -T mlops python -m bsp_mlops.evidence $(if $(MD),--markdown)

test-mlops: ## MLOps unit tests inside the running mlops container (no services touched)
	$(call COMPOSE,$(APPLIANCE)) exec -T -w /opt/mlops -e PYTHONDONTWRITEBYTECODE=1 mlops python -m pytest -q -p no:cacheprovider tests

## ── Optional stacks ─────────────────────────────────────────────────────
datahub-up: ## Optional DataHub stack for this appliance (stacks/datahub)
	$(MAKE) -C stacks/datahub up APPLIANCE=$(APPLIANCE)

datahub-down: ## Stop the optional DataHub stack
	$(MAKE) -C stacks/datahub down APPLIANCE=$(APPLIANCE)

datahub-ingest: ## Run DataHub ingestion recipes (RECIPE=all by default)
	$(MAKE) -C stacks/datahub ingest APPLIANCE=$(APPLIANCE) RECIPE="$(or $(RECIPE),all)"

datahub-annotate: ## Metadata-only AI annotation of the DataHub catalog (CATALOGS=, MAX=300, FORCE=1, DRY=1)
	$(MAKE) -C stacks/datahub annotate APPLIANCE=$(APPLIANCE) CATALOGS="$(CATALOGS)" MAX=$(or $(MAX),300) FORCE=$(FORCE) DRY=$(DRY)

datahub-status: ## DataHub stack status
	$(MAKE) -C stacks/datahub status APPLIANCE=$(APPLIANCE)

corporate-ca: ## Extract corporate root CAs (TLS-inspecting proxy) → build/certs/corporate/
	bash build/scripts/extract-corporate-ca.sh

## ── Quality ─────────────────────────────────────────────────────────────
lint: .env ## compose model for each appliance + shell syntax
	@for a in $(APPLIANCES); do $(call COMPOSE,$$a) --profile '*' config -q && echo "compose $$a ok" || exit 1; done
	@find build -name '*.sh' -exec bash -n {} \; && echo "shell ok"

test: test-studio test-px test-workbench ## Unit tests (Studio, px-proxy, Workbench) inside the image

test-studio:
	docker run --rm --entrypoint python -v $(CURDIR):/repo:ro -v $(CURDIR)/build/studio:/opt/studio:ro \
	  -e STUDIO_TEST_REPO=/repo -e PYTHONDONTWRITEBYTECODE=1 -w /opt/studio $(IMAGE) -m tests.run

test-px:
	docker run --rm --entrypoint /opt/venvs/egress/bin/python -v $(CURDIR)/build/px-proxy:/opt/px-proxy:ro \
	  -e PYTHONDONTWRITEBYTECODE=1 -w /opt/px-proxy $(IMAGE) -m unittest discover -s tests -v

test-workbench:
	docker run --rm --entrypoint python -v $(CURDIR)/build/workbench:/opt/workbench:ro -v $(CURDIR)/dbt_project:/repo/dbt_project:ro \
	  -e PYTHONDONTWRITEBYTECODE=1 -w /opt/workbench $(IMAGE) -m unittest discover -s tests -v
