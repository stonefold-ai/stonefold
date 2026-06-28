# Accounts-Payable demo (docs/05). Run from the demo/ directory.
# Prereqs: Docker + (for the real LLM) an API key in .env (copy .env.example).
COMPOSE = docker compose

.PHONY: up seed demo run ui logs ps down help

help:
	@echo "make up    - build & start gateway + postgres + redis (UI at http://localhost:8088)"
	@echo "make seed  - (re)load the fake ledger (vendors, accounts, invoices)"
	@echo "make demo  - guided CLI walkthrough (uses your LLM if a key is set, else the fake)"
	@echo "make run   - run the agent against the live gateway: process the invoice inbox"
	@echo "make logs  - tail the gateway log"
	@echo "make down  - stop everything and remove volumes"

up:
	$(COMPOSE) up -d --build
	@echo "Gateway + UI:  http://localhost:8088"

seed:
	$(COMPOSE) exec -T postgres psql -U acp -d acp < seed/ledger_seed.sql
	@echo "ledger seeded (vendors, accounts, invoices)"

demo:
	$(COMPOSE) run --rm agent python -m acp_ap_demo --provider $${LLM_PROVIDER:-auto}

run:
	$(COMPOSE) run --rm agent python -m acp_ap_demo.agent_cli --scenario inbox

ui:
	@echo "Open http://localhost:8088"

logs:
	$(COMPOSE) logs -f gateway

ps:
	$(COMPOSE) ps

down:
	$(COMPOSE) down -v
