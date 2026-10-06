# AL: start, warm up and demo shortcuts. Run `make` for the list of targets.
#
#   make start                 start the server in the background (no --reload), wait until healthy
#   make preflight             warm every arm, then run the warm-up script (do this before presenting)
#   make open-arm3             open the Arm 3 page in the browser
#
# Variables (override on the command line, e.g. `make run PORT=8001 MODE=arm2`):
#   PORT=8000   MODE=arm3   ARMS=arm1,arm2,arm3   QUESTION="..."   MOCK=1

PORT     ?= 8000
MODE     ?= arm3
ARMS     ?= arm1,arm2,arm3
QUESTION ?=
MOCK     ?=

VENV    := .venv/bin
URL     := http://localhost:$(PORT)
PIDFILE := .sparky-$(PORT).pid
LOGFILE := .sparky-$(PORT).log
comma   := ,
ARM_LIST := $(subst $(comma), ,$(ARMS))

# Export .env (if present) into the recipe's shell, like `set -a; source .env; set +a`.
# SPARKY_MODE and SPARKY_MOCK_MODE are set after it so command-line variables win over .env.
ENV = set -a; [ -f .env ] && . ./.env; set +a; export SPARKY_MODE=$(MODE); $(if $(MOCK),export SPARKY_MOCK_MODE=1;,)
SERVE = $(VENV)/uvicorn sparky.server:app --port $(PORT)

.DEFAULT_GOAL := help
.PHONY: help install run dev run-mock start stop restart status logs warm warmup preflight \
        open-arm1 open-arm2 open-arm3 test bench bench-cold clean

help: ## Show this list
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  \033[1m%-12s\033[0m %s\n", $$1, $$2}'
	@echo; echo "Variables: PORT=$(PORT) MODE=$(MODE) ARMS=$(ARMS) MOCK=$(or $(MOCK),off)"

install: ## Create .venv, install dependencies, create .env from the example
	python3 -m venv .venv
	$(VENV)/pip install -q -e '.[dev]'
	@[ -f .env ] || { cp .env.example .env; echo "Created .env: edit it (set DBT_HOST at minimum)."; }

# --- Starting -----------------------------------------------------------------------------
run: ## Run the server in the foreground (default mode MODE=arm3). Ctrl+C to stop
	@$(ENV) $(SERVE)

dev: ## Run in the foreground with auto-reload (do NOT use when presenting: reload drops warm sessions)
	@$(ENV) $(SERVE) --reload

run-mock: ## Run in the foreground in mock mode: replay recorded transcripts, no model calls
	@$(MAKE) --no-print-directory run MOCK=1

start: ## Start the server in the background (logs in .sparky-PORT.log) and wait until it is healthy
	@if [ -f $(PIDFILE) ] && kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
	  echo "Already running (pid $$(cat $(PIDFILE))) at $(URL). Use 'make restart' to apply new settings."; \
	else \
	  $(ENV) nohup $(SERVE) > $(LOGFILE) 2>&1 & echo $$! > $(PIDFILE); \
	  pid=$$(cat $(PIDFILE)); ok=0; \
	  printf "Starting on $(URL) (mode $(MODE)$(if $(MOCK), mock))..."; \
	  for i in $$(seq 1 40); do \
	    kill -0 $$pid 2>/dev/null || break; \
	    if curl -sf $(URL)/health >/dev/null; then ok=1; break; fi; sleep 0.5; \
	  done; \
	  if [ $$ok = 1 ]; then echo " healthy (pid $$pid). Logs: make logs"; \
	  else echo " FAILED. Last log lines:"; tail -5 $(LOGFILE); kill $$pid 2>/dev/null; rm -f $(PIDFILE); exit 1; fi; \
	fi

stop: ## Stop the background server
	@if [ -f $(PIDFILE) ] && kill -0 $$(cat $(PIDFILE)) 2>/dev/null; then \
	  kill $$(cat $(PIDFILE)) && echo "Stopped pid $$(cat $(PIDFILE))"; else echo "Not running"; fi; rm -f $(PIDFILE)

restart: stop start ## Stop then start the background server (use after changing .env or code)

status: ## Show whether the server is up
	@curl -sf $(URL)/health >/dev/null && echo "Up at $(URL) (pid $$(cat $(PIDFILE) 2>/dev/null || echo '?'))" \
	  || { echo "Not reachable at $(URL)"; exit 1; }

logs: ## Follow the background server's log
	@tail -f $(LOGFILE)

# --- Warming up ----------------------------------------------------------------------------
warm: ## Pre-connect a session for each arm in ARMS on the running server (fast; no model calls)
	@curl -sf $(URL)/health >/dev/null || { echo "Server not reachable at $(URL). Run 'make start' first."; exit 1; }
	@for a in $(ARM_LIST); do curl -sf -X POST "$(URL)/warm?mode=$$a" >/dev/null && echo "warming $$a"; done
	@echo "Sessions connect in the background; give them a few seconds."

warmup: ## Run each arm's demo question once via the script (QUESTION="..." to override)
	@$(ENV) $(VENV)/python scripts/warmup.py --arms $(ARMS) $(if $(QUESTION),--question "$(QUESTION)")

preflight: warm warmup ## Before presenting: warm every arm, then run the warm-up script
	@echo "Preflight done. Now ask each arm's question once in the browser, then hard-refresh."

open-arm1: ## Open the Arm 1 (Text2SQL) page
	@$(MAKE) --no-print-directory _open ARM=arm1
open-arm2: ## Open the Arm 2 (semantic layer) page
	@$(MAKE) --no-print-directory _open ARM=arm2
open-arm3: ## Open the Arm 3 (context-aware) page
	@$(MAKE) --no-print-directory _open ARM=arm3
.PHONY: _open
_open:
	@u="$(URL)/?mode=$(ARM)$(if $(MOCK),&mock=1)"; echo "$$u"; (open "$$u" 2>/dev/null || xdg-open "$$u" 2>/dev/null || true)

# --- Development ---------------------------------------------------------------------------
test: ## Run the unit tests (no network or credentials needed)
	@$(VENV)/pytest -q

bench: ## Live timing/cost benchmark over 6 questions (uses the warm pool); writes bench.json
	@$(ENV) $(VENV)/python scripts/bench.py

bench-cold: ## Same benchmark with a fresh session per question (no warm pool)
	@$(ENV) $(VENV)/python scripts/bench.py --cold

clean: ## Remove caches and local logs (keeps .venv and .env)
	@rm -f .sparky-*.pid .sparky-*.log bench*.json
	@find . -name __pycache__ -not -path './.venv/*' -prune -exec rm -rf {} + ; rm -rf .pytest_cache
