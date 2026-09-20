# FinAlly: single container, single port (PLAN.md section 11).
#   Stage 1 builds the Next.js frontend into static files.
#   Stage 2 runs FastAPI, which serves both /api/* and those static files on :8000.
#
# Build:  docker build -t finally .
# Run:    docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
# Railway: see deploy/RAILWAY.md (the app listens on Railway's $PORT automatically).

# ---------------------------------------------------------------------------
# Stage 1: build the frontend (static export)
# ---------------------------------------------------------------------------
FROM node:20-slim AS frontend-build
WORKDIR /frontend

# Install dependencies first so this layer is cached until package files change.
# `npm ci` installs exactly what package-lock.json pins (reproducible; fails on drift).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# `next build` runs with NODE_ENV=production, which enables `output: "export"` in
# next.config.mjs. The result is a plain static site in /frontend/out (HTML, JS, CSS and the
# contents of public/), not .next/.
COPY frontend/ ./
RUN npm run build

# ---------------------------------------------------------------------------
# Stage 2: backend + static frontend
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# The image mirrors the repo layout under /app because the backend derives its paths from
# where app/ lives (PROJECT_ROOT = /app):
#   /app/backend/app            application code
#   /app/backend/db/schema.sql  schema, applied on startup (kept OUT of the volume mount)
#   /app/static                 the built frontend, served at /
#   /app/db                     SQLite database (finally.db), the volume mount point
WORKDIR /app/backend

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/backend/.venv/bin:$PATH"

RUN pip install --no-cache-dir uv

# Dependencies first (cached until pyproject.toml / uv.lock change). --no-install-project skips
# building the app itself, which would need the source tree; the code is run from /app/backend.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Application code and schema.
COPY backend/app ./app
COPY backend/db/schema.sql ./db/schema.sql

# The built frontend (Stage 1 output).
COPY --from=frontend-build /frontend/out /app/static

# Run as a non-root user. /app/db is created and owned by that user so that a fresh named
# volume mounted there (Docker copies the ownership from the image) is writable.
RUN useradd --create-home --uid 10001 finally \
    && mkdir -p /app/db \
    && chown -R finally:finally /app/db
USER finally

# No VOLUME instruction: Railway rejects Dockerfiles that contain one. It is not needed here,
# because docker run / compose mount a named volume at /app/db explicitly, and Docker copies this
# directory's ownership into a fresh named volume. On Railway, attach a Railway volume at /app/db.
EXPOSE 8000

# Listen on $PORT when the platform provides it (Railway injects one at runtime and ignores
# EXPOSE); default to 8000 for docker run / compose. The health check follows the same port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:%s/api/health' % os.environ.get('PORT', '8000'), timeout=3)"

# Shell form (via sh -c) so ${PORT:-8000} is expanded; exec makes uvicorn PID 1 for clean shutdown.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
