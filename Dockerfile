# Runtime image of price-alert-bot. Works with the legacy builder and BuildKit
# (no RUN --mount, COPY --chmod/--link, heredocs or "# syntax=").
# Configuration and secrets come from the environment at run time only.

# ---- build stage: resolve and install locked runtime dependencies ----
FROM python:3.14.7-slim-trixie AS builder

# Pinned uv binary, same version that maintains uv.lock; it never ships to the final image.
COPY --from=ghcr.io/astral-sh/uv:0.11.31 /uv /usr/local/bin/uv

# The venv must sit at the same absolute path in both stages and use the
# system interpreter of the base image (uv must not download its own Python).
ENV UV_PYTHON_DOWNLOADS=0 \
    UV_PYTHON_PREFERENCE=only-system \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app

# Dependency layer: depends only on the manifests, so code changes keep it cached.
# --locked (not --frozen): a stale uv.lock must fail the build.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

# ---- final stage: runtime only, no uv, no dev dependencies ----
FROM python:3.14.7-slim-trixie

# Import path is declared here and does not rely on WORKDIR.
ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers

# Unprivileged regular user (not a system one) with a fixed UID; its home is
# declared as /nonexistent and not created, nothing writes there.
RUN useradd --uid 10001 --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv

# Full Chromium (no headless shell: WB rejects its token, PROJECT.md 10.0 D6) of the
# exact version of the locked `playwright` package, installed by the package itself
# from the venv, plus system libraries and fonts (--with-deps, needs root, hence
# before USER). Its own layer after the venv: code changes do not re-download it.
# PLAYWRIGHT_BROWSERS_PATH is a fixed image directory: owned by root, readable and
# executable by everyone, writable by nobody but root.
RUN playwright install --with-deps chromium --no-shell \
    && rm -rf /var/lib/apt/lists/* \
    && chmod -R u=rwX,go=rX /opt/pw-browsers
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app

# Normalize modes so the image does not depend on the umask of the build host:
# readable by everyone, writable by root only. .venv is left as built, except
# the lock file uv leaves there with mode 666 (world-writable).
RUN chmod -R u=rwX,go=rX /app/app /app/alembic /app/alembic.ini \
    && chmod 644 /app/.venv/.lock

USER 10001

EXPOSE 8000

# One process, no --workers/--reload. Migrations are a separate run of this
# image: docker run <image> alembic upgrade head
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
