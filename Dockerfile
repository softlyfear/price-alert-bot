# Compatible with both the legacy builder and BuildKit.
FROM python:3.14.7-slim-trixie AS builder

# Match the uv version that maintains uv.lock; exclude it from the runtime image.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

# The venv needs the same path and system interpreter in both stages.
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Cache dependencies independently of code; reject a stale lockfile.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

FROM python:3.14.7-slim-trixie

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

RUN useradd --uid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data && chown 10001:10001 /data && chmod 700 /data

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# WB rejects headless-shell tokens; install the locked Playwright's full Chromium.
# Keep it cached across code changes and writable only by root.
RUN playwright install --with-deps chromium --no-shell \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R u=rwX,go=rX /opt/pw-browsers
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
COPY scripts/sqlite_backup.py ./scripts/sqlite_backup.py

# Override the build host's umask and uv's world-writable lock file.
RUN chmod -R u=rwX,go=rX /app/app /app/alembic /app/alembic.ini /app/scripts \
    && chmod 644 /app/.venv/.lock

USER 10001

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
