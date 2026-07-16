.PHONY: demo web docker mock check models health clean help
.DEFAULT_GOAL := help

PY      ?= python3
VENV    := .venv
BIN     := $(VENV)/bin

# Everything the demo varies is a flag, and every flag has a default.
MOCK         ?= 0
FAIL_MODE    ?= 429
FAIL_AFTER   ?= 1
# Must stay >= config.FAIL_DURATION_S's reasoning: longer than the single lane's
# worst case (MAX_ATTEMPTS x REQUEST_TIMEOUT_S + backoff). The proxy asserts it.
FAIL_DURATION?= 60

ifeq ($(MOCK),1)
  UPSTREAM := http://127.0.0.1:8901/v1
else
  UPSTREAM := https://inference.do-ai.run/v1
endif

PORT ?= 8080

help:
	@echo "  make web               # the dashboard -> http://localhost:$(PORT)"
	@echo "  make docker            # same thing in the container App Platform runs"
	@echo ""
	@echo "  make demo              # terminal version (needs the key)"
	@echo "  make demo MOCK=1       # offline, no key, no network"
	@echo "  make demo FAIL_MODE=timeout|5xx|429"
	@echo "  make demo FAIL_AFTER=2 FAIL_DURATION=90"
	@echo ""
	@echo "  make health            # do the pinned models actually answer? RUN BEFORE DEMO"
	@echo "  make models            # do the pinned model ids still exist?"
	@echo "  make clean"

# The dashboard. Injector + mock upstream run in-process (see web/server.py),
# so unlike `make demo` there is nothing to start or tear down.
web: $(VENV)/.stamp
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	echo "  dashboard -> http://localhost:$(PORT)"; \
	$(BIN)/uvicorn web.server:app --host 127.0.0.1 --port $(PORT) --workers 1

# Proves the image App Platform will build actually runs, before you push it.
docker:
	@docker build -q -t agent-resilience-demo . && \
	echo "  dashboard -> http://localhost:$(PORT)" && \
	docker run --rm -p $(PORT):8080 \
	  -e DIGITALOCEAN_INFERENCE_KEY \
	  --env-file .env \
	  agent-resilience-demo

$(VENV)/.stamp: requirements.lock
	@$(PY) -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' \
	  || { echo "ERROR: need Python >= 3.11 (found: $$($(PY) -V))"; exit 1; }
	@$(PY) -m venv $(VENV)
	@$(BIN)/pip install -q --require-hashes -r requirements.lock
	@touch $@

check: $(VENV)/.stamp
	@if [ "$(MOCK)" != "1" ] && [ -z "$$DIGITALOCEAN_INFERENCE_KEY" ]; then \
	  if [ -f .env ]; then echo "note: loading .env"; else \
	    echo ""; \
	    echo "  ERROR: DIGITALOCEAN_INFERENCE_KEY is not set."; \
	    echo ""; \
	    echo "    cp .env.example .env   # then add your key"; \
	    echo "    make demo              # or run offline:  make demo MOCK=1"; \
	    echo ""; exit 1; fi; fi

waitport = until $(BIN)/python -c 'import socket;socket.create_connection(("127.0.0.1",$(1)),0.2)' 2>/dev/null; do sleep 0.1; done

# One command. Starts what's needed, runs both lanes, tears everything down.
demo: check
	@set -e; \
	if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	trap 'kill $$MOCK_PID $$PROXY_PID 2>/dev/null || true' EXIT INT TERM; \
	if [ "$(MOCK)" = "1" ]; then \
	  $(BIN)/python -m mock >/dev/null & MOCK_PID=$$!; \
	  $(call waitport,8901); \
	fi; \
	$(BIN)/python -m proxy \
	  --upstream-primary $(UPSTREAM) --upstream-alt $(UPSTREAM) \
	  --fail-after $(FAIL_AFTER) --fail-duration $(FAIL_DURATION) \
	  --fail-mode $(FAIL_MODE) >/dev/null & PROXY_PID=$$!; \
	sleep 0.6; \
	if ! kill -0 $$PROXY_PID 2>/dev/null; then \
	  echo ""; \
	  echo "  ERROR: the fault injector exited instead of starting."; \
	  echo "  Two usual causes:"; \
	  echo "    1. a determinism check rejected your flags (see the message above)"; \
	  echo "    2. port 8900 is already taken -- is 'make web' still running?"; \
	  echo ""; \
	  exit 1; fi; \
	$(call waitport,8900); \
	MOCK=$(MOCK) FAIL_MODE=$(FAIL_MODE) FAIL_AFTER=$(FAIL_AFTER) \
	  $(BIN)/python runner.py

# Guards against the model pins in config.py drifting out from under the demo.
models: $(VENV)/.stamp
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; \
	$(BIN)/python -c 'import httpx,os,config; \
	  r=httpx.get(config.DO_INFERENCE_BASE_URL+"/models", headers={"Authorization":"Bearer "+os.environ["DIGITALOCEAN_INFERENCE_KEY"]}, timeout=15); \
	  ids=[m["id"] for m in r.json()["data"]]; \
	  [print(("  OK  " if m in ids else "  MISSING  ")+m) for m in (config.PRIMARY_MODEL, config.ALT_MODEL)]'

# `models` proves the ID exists. `health` proves it will actually answer you.
# Run this one before you present -- it is the check that would have caught
# gpt-oss-120b being overloaded.
health: $(VENV)/.stamp
	@if [ -f .env ]; then set -a; . ./.env; set +a; fi; $(BIN)/python health.py

clean:
	@rm -rf $(VENV) out/run-*.jsonl __pycache__ */__pycache__
