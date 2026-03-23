.PHONY: help install update run test test-v test-cov test-watch clean fmt type check docker-build docker-up docker-down docker-logs migrate migrate-create db-upgrade db-downgrade pre-commit

RED    := \033[0;31m
GREEN  := \033[0;32m
YELLOW := \033[0;33m
BLUE   := \033[0;34m
NC     := \033[0m

.DEFAULT_GOAL := help

help:
	@echo "$(BLUE)WB Alert Bot$(NC)"
	@echo ""
	@echo "$(GREEN)Dev:$(NC)"
	@echo "  install        — Install/sync dependencies (uv)"
	@echo "  update         — Update all dependencies"
	@echo "  run            — Run dev server with auto-reload"
	@echo ""
	@echo "$(GREEN)Quality:$(NC)"
	@echo "  fmt            — Format + fix code (ruff)"
	@echo "  type           — Type check (ty)"
	@echo "  check          — fmt + type"
	@echo "  pre-commit     — Run pre-commit hooks"
	@echo ""
	@echo "$(GREEN)Test:$(NC)"
	@echo "  test           — Run tests"
	@echo "  test-v         — Verbose tests"
	@echo "  test-cov       — Tests with coverage"
	@echo "  test-watch     — Tests in watch mode"
	@echo ""
	@echo "$(GREEN)DB:$(NC)"
	@echo "  migrate        — Create + apply migration"
	@echo "  migrate-create — Create migration only"
	@echo "  db-upgrade     — Apply pending migrations"
	@echo "  db-downgrade   — Rollback last migration"
	@echo ""
	@echo "$(GREEN)Docker:$(NC)"
	@echo "  docker-build   — Build image"
	@echo "  docker-up      — Start containers"
	@echo "  docker-down    — Stop containers"
	@echo "  docker-logs    — View logs"
	@echo "  docker-restart — Restart containers"
	@echo ""
	@echo "$(GREEN)Cleanup:$(NC)"
	@echo "  clean          — Remove caches"
	@echo "  clean-all      — Remove caches + venv + volumes"

install:
	@echo "$(GREEN)==> Installing dependencies...$(NC)"
	uv sync

update:
	@echo "$(YELLOW)==> Updating dependencies...$(NC)"
	uv lock --upgrade
	uv sync

run:
	@if [ -d "alembic" ] || [ -f "alembic.ini" ]; then \
		echo "$(BLUE)==> Applying migrations...$(NC)"; \
		uv run python -m alembic upgrade head || echo "$(YELLOW)Migration skipped$(NC)"; \
	fi
	uv run uvicorn app.main:app --reload

fmt:
	@echo "$(GREEN)==> Formatting...$(NC)"
	uv run ruff format .
	uv run ruff check --fix .

type:
	@echo "$(GREEN)==> Type checking...$(NC)"
	@uv run ty check || echo "$(YELLOW)⚠ Type issues found (non-critical)$(NC)"

check: fmt type
	@echo "$(GREEN)==> All checks passed!$(NC)"

pre-commit:
	uv run pre-commit run --all-files

test:
	@if [ ! -d "tests" ]; then \
		echo "$(RED)Error: tests/ not found$(NC)"; exit 1; \
	fi
	uv run pytest -v --tb=short

test-v:
	uv run pytest -vv

test-cov:
	uv run pytest --cov=app --cov-report=term-missing

test-watch:
	uv run ptw -- -v --tb=short

migrate:
	@read -p "Migration message: " msg; \
	if [ -z "$$msg" ]; then echo "$(RED)Empty message$(NC)"; exit 1; fi; \
	uv run python -m alembic revision --autogenerate -m "$$msg"; \
	uv run python -m alembic upgrade head

migrate-create:
	@read -p "Migration message: " msg; \
	if [ -z "$$msg" ]; then echo "$(RED)Empty message$(NC)"; exit 1; fi; \
	uv run python -m alembic revision --autogenerate -m "$$msg"

db-upgrade:
	uv run python -m alembic upgrade head

db-downgrade:
	uv run python -m alembic downgrade -1

docker-build:
	docker build -t wb-alert-bot .

docker-up:
	docker compose up -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f

docker-restart: docker-down docker-up

clean:
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .ty_cache -o -name htmlcov -o -name "*.egg-info" \) -exec rm -rf {} + 2>/dev/null || true
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name .coverage \) -delete 2>/dev/null || true
	@echo "$(GREEN)==> Clean!$(NC)"

clean-all: clean
	rm -rf .venv
	docker compose down -v 2>/dev/null || true
