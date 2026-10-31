.DEFAULT_GOAL := help
COMPOSE := docker compose
TESTS := $(COMPOSE) --profile test run --rm tests

.PHONY: help env up down clean logs ps migrate revision test cov lint typecheck frontend-test

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## /\t/'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || cp .env.example .env

up: env ## Build and start the whole stack
	$(COMPOSE) up --build

down: ## Stop the stack, keeping data
	$(COMPOSE) down

clean: ## Stop the stack and delete all data volumes
	$(COMPOSE) down --volumes

logs: ## Follow logs
	$(COMPOSE) logs -f

ps: ## Show service status
	$(COMPOSE) ps

migrate: ## Apply database migrations
	$(COMPOSE) run --rm migrate

revision: ## Autogenerate a migration, e.g. make revision m="add column"
	$(COMPOSE) --profile test run --rm -v ./backend/migrations:/app/migrations tests alembic revision --autogenerate -m "$(m)"

test: ## Run backend tests in a container, against real Postgres
	$(TESTS) pytest tests -q

cov: ## Run backend tests with a coverage summary
	$(TESTS) pytest tests -q --cov=app --cov-report=term-missing

lint: ## Run ruff
	$(TESTS) ruff check .

typecheck: ## Run mypy
	$(TESTS) mypy app

frontend-test: ## Run frontend lint, type checks and tests (needs local Node)
	cd frontend && npm ci && npm run lint && npm run typecheck && npm test
