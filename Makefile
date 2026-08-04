UV ?= uv

.PHONY: check container-build container-up coverage format format-check install lint lock lock-check security test

install:
	$(UV) sync --all-groups

lock:
	$(UV) lock

lock-check:
	$(UV) lock --check

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

test:
	$(UV) run pytest

coverage:
	$(UV) run pytest --cov=repo_agent --cov-branch --cov-report=term-missing

security:
	$(UV) run pip-audit

check: lock-check format-check lint test coverage security

container-build:
	docker compose build

container-up:
	docker compose up -d
