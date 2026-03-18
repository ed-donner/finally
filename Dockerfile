# Stage 1: Build frontend static export
FROM node:20-slim AS frontend-build

WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build

# Stage 2: Python backend with frontend static files
FROM python:3.12-slim

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app/backend

# Copy backend project files and install dependencies
COPY backend/pyproject.toml backend/uv.lock backend/README.md ./
RUN uv sync --frozen --no-dev

# Copy backend source code
COPY backend/ ./

# Copy frontend build output to static directory (backend serves from ./static)
COPY --from=frontend-build /app/frontend/out /app/backend/static

# Create db directory for SQLite volume mount
RUN mkdir -p /app/db

EXPOSE 8000

# Run from backend directory so app module resolves correctly
CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
