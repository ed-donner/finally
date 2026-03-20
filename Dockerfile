# Stage 1: Build Next.js frontend (static export)
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

COPY frontend/package*.json ./
RUN npm ci --quiet

COPY frontend/ ./
RUN npm run build


# Stage 2: Python backend
FROM python:3.12-slim AS backend

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Copy backend project files
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev

# Copy backend source
COPY backend/app/ ./app/

# Copy frontend static export into static/ directory
COPY --from=frontend-builder /frontend/out/ ./static/

# Create db directory for SQLite volume mount
RUN mkdir -p /app/db

EXPOSE 8000

ENV PYTHONPATH=/app
ENV DB_PATH=/app/db/finally.db

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
