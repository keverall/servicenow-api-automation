.PHONY: help venv sync lock upgrade python-install lint format typecheck test coverage build clean local-sync docker-build

UV ?= uv
SRC := src tests

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv and install the project with uv
	$(UV) sync --frozen

sync: ## Sync the environment, refreshing the lock file when needed
	$(UV) sync

lock: ## Refresh uv.lock from pyproject.toml
	$(UV) lock

upgrade: ## Refresh uv.lock and upgrade the resolved dependencies
	$(UV) lock --upgrade

python-install: ## Install the Python version pinned in .python-version
	$(UV) python install

lint: ## Run ruff lint + format check
	$(UV) run ruff check $(SRC)
	$(UV) run ruff format --check $(SRC)

format: ## Apply ruff formatting and autofixes
	$(UV) run ruff check --fix $(SRC)
	$(UV) run ruff format $(SRC)

typecheck: ## Run mypy
	$(UV) run mypy

test: ## Run the unit test suite
	$(UV) run pytest

coverage: ## Run tests with a coverage report
	$(UV) run pytest --cov=opsramp_automation --cov-report=term-missing --cov-report=xml

build: ## Build the wheel and sdist
	$(UV) build

clean: ## Remove build and cache artefacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov
	$(UV) cache clean

local-sync: ## Run a single dry-run sync against the configured instance
	$(UV) run opsramp-gw sync --once --dry-run

docker-build: ## Build the container image
	docker build -t opsramp-automation:local .
