.PHONY: install run test lint typecheck format e2e docker-build demo-up demo-down migrate check

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

install:  ## create a virtualenv and install KubiDoor with dev tools
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

run:  ## run KubiDoor locally (uses your kubeconfig; recovery stays disabled unless configured)
	$(BIN)/kubidoor

test:  ## unit + integration tests (no cluster required)
	$(BIN)/pytest

lint:  ## ruff lint + format check
	$(BIN)/ruff check app tests migrations
	$(BIN)/ruff format --check app tests

format:
	$(BIN)/ruff check --fix app tests migrations
	$(BIN)/ruff format app tests

typecheck:
	$(BIN)/mypy

check: lint typecheck test  ## everything CI runs

migrate:
	KUBIDOOR_DATABASE_URL=$${KUBIDOOR_DATABASE_URL:-sqlite:///./kubidoor.db} $(BIN)/alembic upgrade head

e2e:  ## optional end-to-end test against the kind demo cluster (run `make demo-up` first)
	KUBIDOOR_E2E=1 $(BIN)/pytest -m e2e tests/e2e -o addopts="-ra"

docker-build:
	docker build -t kubidoor:latest .

demo-up:  ## kind cluster + demo scenarios + KubiDoor/Prometheus/Grafana (needs docker, kind, kubectl)
	./scripts/demo-up.sh

demo-down:  ## remove demo workloads and stop the stack (add ARGS=--delete-cluster to drop the kind cluster)
	./scripts/demo-down.sh $(ARGS)
