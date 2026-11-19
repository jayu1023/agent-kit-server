VENV ?= .venv
PY   := $(VENV)/bin/python

.PHONY: help venv install dev test test-dart fixture mcp ask approve up down clean

help:
	@grep -E '^[a-z-]+:.*?##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t26

venv: ## create a python 3.12 venv (needs uv)
	uv venv -p 3.12 $(VENV)

install: ## install dev dependencies
	uv pip install --python $(PY) -r requirements-dev.txt

dev: ## run the api with reload on :8000
	$(VENV)/bin/uvicorn app.main:app --reload --port 8000

test: ## python suite
	$(PY) -m pytest

fixture: ## regenerate the Dart wire fixture from a real agent run
	$(PY) scripts/capture_events.py

test-dart: fixture ## prove wire compatibility against the real agent_kit package
	cd clients/flutter_smoke && flutter test

mcp: ## run the MCP server on stdio (for Claude Code / Desktop)
	$(PY) mcp_server.py

ask: ## stream a run: make ask Q="your question"
	curl -N -X POST localhost:8000/runs \
		-H 'content-type: application/json' \
		-d "$$(python3 -c 'import json,os;print(json.dumps({"question":os.environ["Q"],"run_id":"demo"}))')"

approve: ## approve the paused demo run: make approve [OK=false]
	curl -N -X POST localhost:8000/runs/demo/approve \
		-H 'content-type: application/json' \
		-d '{"approved": $(or $(OK),true)}'

up: ## docker compose up
	docker compose up --build

down:
	docker compose down -v

clean:
	rm -rf .pytest_cache data/reports clients/flutter_smoke/.dart_tool
