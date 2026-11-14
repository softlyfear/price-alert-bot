.PHONY: help install update install-browser run test test-v test-cov test-watch clean fmt type check docker-build docker-up docker-down docker-logs migrate migrate-create db-upgrade db-downgrade db-backup db-restore storage-prepare storage-check db-import-volume redis-import-volume pre-commit hooks docker-restart clean-all

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
	@echo "  install-browser — Install Chromium for Playwright (version of the locked package)"
	@echo "  run            — Run dev server with auto-reload"
	@echo ""
	@echo "$(GREEN)Quality:$(NC)"
	@echo "  fmt            — Format + fix code (ruff)"
	@echo "  type           — Type check (mypy)"
	@echo "  check          — fmt + type"
	@echo "  pre-commit     — Run pre-commit hooks"
	@echo "  hooks          — Install git hooks (pre-commit, commit-msg, pre-push)"
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
	@echo "  db-backup      — Save a live SQLite backup to BACKUP"
	@echo "  db-restore     — Restore BACKUP while app is stopped (OVERWRITE=1)"
	@echo "  storage-prepare — Create Docker SQLite and Redis directories"
	@echo "  db-import-volume — Copy the stopped legacy SQLite volume"
	@echo "  redis-import-volume — Copy the stopped legacy Redis volume"
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
	@echo "  clean-all      — Remove caches + venv (docker volumes are kept)"

install:
	@echo "$(GREEN)==> Installing dependencies...$(NC)"
	uv sync

update:
	@echo "$(YELLOW)==> Updating dependencies...$(NC)"
	uv lock --upgrade
	uv sync

# WB accepts full Chromium, not the headless shell.
install-browser:
	@echo "$(GREEN)==> Installing Chromium for Playwright...$(NC)"
	uv run playwright install --no-shell chromium

run:
	@if [ -d "alembic" ] || [ -f "alembic.ini" ]; then \
		echo "$(BLUE)==> Applying migrations...$(NC)"; \
		uv run python -m alembic upgrade head; \
	fi
	uv run uvicorn app.main:app --reload

fmt:
	@echo "$(GREEN)==> Formatting...$(NC)"
	uv run ruff format .
	uv run ruff check --fix .

type:
	@echo "$(GREEN)==> Type checking...$(NC)"
	uv run mypy app tests

check: fmt type
	@echo "$(GREEN)==> All checks passed!$(NC)"

pre-commit:
	uv run pre-commit run --all-files

# The wrapper resolves pre-push from the worktree that triggers the push.
hooks:
	@echo "$(GREEN)==> Installing git hooks...$(NC)"
	uv run pre-commit install -f --hook-type pre-commit --hook-type commit-msg
	@hooks_dir="$$(git rev-parse --git-path hooks)"; \
	printf '#!/usr/bin/env bash\nexec "$$(git rev-parse --show-toplevel)/scripts/pre-push" "$$@"\n' > "$$hooks_dir/pre-push"; \
	chmod 0755 "$$hooks_dir/pre-push"
	@echo "$(GREEN)==> Installed: pre-commit, commit-msg (pre-commit framework), pre-push (wrapper -> scripts/pre-push)$(NC)"

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

# Compose uses this file for interpolation and for app and migrate.
ENV_FILE ?= .env
export ENV_FILE
BACKUP ?=
OVERWRITE ?=
LEGACY_SQLITE_VOLUME ?= price-alert-bot_sqlite_data
LEGACY_REDIS_VOLUME ?= price-alert-bot_redisdata
export BACKUP OVERWRITE LEGACY_SQLITE_VOLUME LEGACY_REDIS_VOLUME
# The stack profile: every service is skipped by compose without it.
COMPOSE := docker compose --env-file "$(ENV_FILE)" --profile price-alert-bot

# Default attestations would change the image ID across identical builds.
docker-build:
	@echo "$(GREEN)==> Building image...$(NC)"
	BUILDX_NO_DEFAULT_ATTESTATIONS=1 $(COMPOSE) build

# A bare `up -d` would re-run migrate while app is alive.
docker-up: storage-check docker-build
	$(COMPOSE) stop app
	$(COMPOSE) up -d

docker-down:
	$(COMPOSE) down

docker-logs:
	$(COMPOSE) logs -f

docker-restart: docker-down docker-up

# Only new directories are provisioned; existing directories are never modified.
storage-prepare:
	@set -eu; \
	command -v setfacl >/dev/null || { echo 'Install setfacl before preparing storage' >&2; exit 1; }; \
	mkdir -p -m 700 data; \
	for item in docker:10001 redis:999; do \
	  directory="data/$${item%:*}"; uid="$${item#*:}"; \
	  if [ -e "$$directory" ] || [ -L "$$directory" ]; then \
	    test -d "$$directory" && test ! -L "$$directory" || { echo "Invalid directory: $$directory" >&2; exit 1; }; \
	    echo "Existing $$directory was not changed"; \
	  else \
	    mkdir -m 700 "$$directory"; \
	    if ! setfacl -m "u:$$uid:rwx" "$$directory"; then rmdir "$$directory"; exit 1; fi; \
	    echo "Prepared $$directory for container UID $$uid"; \
	  fi; \
	done

# Do not switch an existing installation to a newly-created empty database.
storage-check:
	@set -eu; \
	for directory in data/docker data/redis; do \
	  test -d "$$directory" && test ! -L "$$directory" || { echo 'Prepare storage first: make storage-prepare' >&2; exit 1; }; \
	done; \
	volumes="$$(docker volume ls -q)"; \
	if printf '%s\n' "$$volumes" | grep -Fqx -- "$$LEGACY_SQLITE_VOLUME" && \
	   test ! -e data/docker/price-alert-bot.sqlite3; then \
	  echo 'Legacy SQLite volume exists; run make db-import-volume before starting' >&2; exit 1; \
	fi; \
	if printf '%s\n' "$$volumes" | grep -Fqx -- "$$LEGACY_REDIS_VOLUME" && \
	   test ! -f data/redis/.volume-imported; then \
	  echo 'Legacy Redis volume exists; run make redis-import-volume before starting' >&2; exit 1; \
	fi

# Read the stopped legacy volume without modifying it, then restore a checked copy.
db-import-volume: docker-build
	@set -eu; \
	case "$$LEGACY_SQLITE_VOLUME" in ''|*[!a-zA-Z0-9_.-]*) echo 'Invalid legacy volume name' >&2; exit 1;; esac; \
	test -d data/docker && test ! -L data/docker || { echo 'Prepare data/docker first: make storage-prepare' >&2; exit 1; }; \
	test ! -e data/docker/price-alert-bot.sqlite3 && test ! -L data/docker/price-alert-bot.sqlite3 || { echo 'Destination already exists' >&2; exit 1; }; \
	volumes="$$(docker volume ls -q)"; \
	printf '%s\n' "$$volumes" | grep -Fqx -- "$$LEGACY_SQLITE_VOLUME" || { echo 'Legacy SQLite volume does not exist' >&2; exit 1; }; \
	running="$$( $(COMPOSE) ps -q --status running app migrate )"; \
	test -z "$$running" || { echo 'Stop app and migrate before importing' >&2; exit 1; }; \
	running="$$(docker ps -q --filter "volume=$$LEGACY_SQLITE_VOLUME")"; \
	test -z "$$running" || { echo 'Legacy SQLite volume is in use' >&2; exit 1; }; \
	tmp="$$(mktemp "$${TMPDIR:-/tmp}/price-alert-import.XXXXXX")"; \
	trap 'rm -f -- "$$tmp"' EXIT HUP INT TERM; \
	docker run --rm --network none --read-only --tmpfs /tmp \
	  --mount "type=volume,src=$$LEGACY_SQLITE_VOLUME,dst=/source,readonly" \
	  price-alert-bot sh -ec 'umask 077; mkdir /tmp/source; \
	    for suffix in "" -wal -shm -journal; do \
	      path="/source/price-alert-bot.sqlite3$$suffix"; \
	      if [ -z "$$suffix" ] || [ -e "$$path" ]; then cp -- "$$path" /tmp/source/; fi; \
	    done; \
	    exec python -m scripts.sqlite_backup backup /tmp/source/price-alert-bot.sqlite3 -' > "$$tmp"; \
	$(MAKE) db-restore BACKUP="$$tmp" OVERWRITE=; \
	echo 'Legacy volume copied; the original volume was preserved'

# Copy all stopped Redis persistence files without replacing the destination.
redis-import-volume:
	@set -eu; \
	case "$$LEGACY_REDIS_VOLUME" in ''|*[!a-zA-Z0-9_.-]*) echo 'Invalid legacy volume name' >&2; exit 1;; esac; \
	test -d data/redis && test ! -L data/redis || { echo 'Prepare data/redis first: make storage-prepare' >&2; exit 1; }; \
	volumes="$$(docker volume ls -q)"; \
	printf '%s\n' "$$volumes" | grep -Fqx -- "$$LEGACY_REDIS_VOLUME" || { echo 'Legacy Redis volume does not exist' >&2; exit 1; }; \
	running="$$( $(COMPOSE) ps -q --status running app migrate redis )"; \
	test -z "$$running" || { echo 'Stop app, migrate and redis before importing' >&2; exit 1; }; \
	running="$$(docker ps -q --filter "volume=$$LEGACY_REDIS_VOLUME")"; \
	test -z "$$running" || { echo 'Legacy Redis volume is in use' >&2; exit 1; }; \
	docker run --rm --network none --user 999:1000 --read-only --tmpfs /tmp \
	  --mount "type=volume,src=$$LEGACY_REDIS_VOLUME,dst=/source,readonly" \
	  --mount "type=bind,src=$$(pwd)/data/redis,dst=/destination" \
	  --entrypoint sh redis:7.4.11-alpine -ec 'umask 077; \
	    [ -z "$$(ls -A /destination)" ] || { echo "Redis destination is not empty" >&2; exit 1; }; \
	    [ ! -e /source/.volume-imported ] || { echo "Reserved marker exists in source" >&2; exit 1; }; \
	    tar -C /source -cf /tmp/source.tar .; \
	    tar -C /destination -xokf /tmp/source.tar --strip-components 1; \
	    touch /destination/.volume-imported'; \
	echo 'Legacy Redis volume copied; the original volume was preserved'

# The container emits a checked snapshot; a hard link publishes it without ever
# replacing an existing host backup, even if another process wins the race.
db-backup:
	@set -eu; \
	test -n "$$BACKUP" || { echo 'Set BACKUP to a new file path' >&2; exit 1; }; \
	test ! -e "$$BACKUP" && test ! -L "$$BACKUP" || { echo 'Backup already exists' >&2; exit 1; }; \
	tmp="$$(mktemp "$${BACKUP}.tmp.XXXXXX")"; \
	trap 'rm -f -- "$$tmp"' EXIT HUP INT TERM; \
	$(COMPOSE) exec -T app python -m scripts.sqlite_backup backup /data/price-alert-bot.sqlite3 - > "$$tmp"; \
	ln -- "$$tmp" "$$BACKUP"; \
	echo "Backup saved to $$BACKUP"

# Refuse restore while either service can be using the volume. Do not run any
# other SQLite clients against the volume until restore finishes.
db-restore:
	@set -eu; \
	test -n "$$BACKUP" && test -f "$$BACKUP" || { echo 'Set BACKUP to an existing file' >&2; exit 1; }; \
	resolved="$$(readlink -f -- "$$BACKUP")"; \
	for suffix in -wal -shm -journal; do \
	  test ! -e "$$resolved$$suffix" && test ! -L "$$resolved$$suffix" || { echo 'Backup has SQLite sidecars; close its clients first' >&2; exit 1; }; \
	done; \
	running="$$( $(COMPOSE) ps -q --status running app migrate )"; \
	test -z "$$running" || { echo 'Stop app and migrate before restore' >&2; exit 1; }; \
	flag=''; \
	if [ "$$OVERWRITE" = 1 ]; then flag='--overwrite'; fi; \
	$(COMPOSE) run --rm --no-deps -T migrate python -m scripts.sqlite_backup restore - /data/price-alert-bot.sqlite3 $$flag < "$$BACKUP"; \
	echo 'SQLite restore completed'

clean:
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache -o -name htmlcov -o -name "*.egg-info" \) -exec rm -rf {} + 2>/dev/null || true
	@find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name .coverage \) -delete 2>/dev/null || true
	@echo "$(GREEN)==> Clean!$(NC)"

clean-all: clean
	rm -rf .venv
