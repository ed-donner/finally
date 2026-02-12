# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend-build
WORKDIR /work/frontend

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS backend-build
ENV UV_LINK_MODE=copy
WORKDIR /build/backend

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY backend/ /build/backend/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PYTHONPATH=/app/backend

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir uv

COPY --from=backend-build /build/backend /app/backend
COPY --from=frontend-build /work/frontend/out /app/static
COPY scripts/container_app.py /app/backend/container_app.py

RUN mkdir -p /app/db

EXPOSE 8003

ENV PORT=8003
ENV APP_MODULE=app.main:app

CMD ["sh", "-c", "cd /app/backend && .venv/bin/python -m uvicorn container_app:app --host 0.0.0.0 --port ${PORT:-8003}"]
