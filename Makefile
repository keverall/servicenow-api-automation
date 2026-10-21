.PHONY: help install lint format typecheck test coverage build clean local-sync docker-build

PY ?= python3
PIP ?= $(PY) -m pip

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install the package and dev dependencies
	$(PIP) install -e ".[dev]"

lint: ## Run ruff lint + format check
	ruff check src tests
	ruff format --check src tests

format: ## Apply ruff formatting and autofixes
	ruff check --fix src tests
	ruff format src tests

typecheck: ## Run mypy
	mypy

test: ## Run the unit test suite
	pytest

coverage: ## Run tests with coverage report
	pytest --cov=opsramp_automation --cov-report=term-missing --cov-report=xml

build: ## Build the wheel and sdist
	$(PY) -m build

clean: ## Remove build and cache artefacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov

local-sync: ## Run a single dry-run sync against the configured instance
	opsramp-gw sync --once --dry-run

docker-build: ## Build the container image
	docker build -t opsramp-automation:local .
