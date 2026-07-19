# syntax=docker/dockerfile:1

# ==============================================================================
# FinAlly — multi-stage build
#   Stage 1 (node):   build the Next.js static export  -> frontend/out
#   Stage 2 (python): install backend via uv, copy the export in as static/
# Final image serves the FastAPI app (app.main:app) on port 8000 and the
# static frontend from /app/static. SQLite lives at /app/db (named volume).
# ==============================================================================

# ------------------------------------------------------------------------------
# Stage 1: Frontend build (Next.js static export -> /app/frontend/out)
# ------------------------------------------------------------------------------
FROM node:20-slim AS frontend-builder

WORKDIR /app/frontend

# Install dependencies first for better layer caching.
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci

# Build the static export. next.config with `output: 'export'` emits to ./out
COPY frontend/ ./
RUN npm run build

# ------------------------------------------------------------------------------
# Stage 2: Backend runtime (FastAPI + uv), serving the static export
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# uv: fast, reproducible Python dependency management (copied from official image)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# uv settings: use a project-local .venv, compile bytecode, copy (not link) so
# the venv is self-contained and the build works across filesystems.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    FINALLY_DB_PATH=/app/db/finally.db

WORKDIR /app

# 1) Install ONLY dependencies first (no project source) for layer caching.
#    --no-dev skips the `dev` optional group; --frozen requires uv.lock to match.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# 2) Copy backend source and install the project itself into the venv.
COPY backend/ ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# 3) Copy the built frontend export to the location FastAPI serves as static.
COPY --from=frontend-builder /app/frontend/out ./static

# Runtime volume mount point for the SQLite database (db/finally.db).
RUN mkdir -p /app/db

EXPOSE 8000

# Basic container healthcheck against the API health endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else sys.exit(1)"

# Serve FastAPI (which also mounts the static export) on all interfaces.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
