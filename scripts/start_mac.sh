#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"
PORT=8000
VOLUME="finally-data"

cd "$(dirname "$0")/.."

# Parse flags
BUILD=false
for arg in "$@"; do
  case $arg in
    --build|-b) BUILD=true ;;
  esac
done

# Create .env if it doesn't exist
if [ ! -f .env ]; then
  if [ -f .env.example ]; then
    cp .env.example .env
    echo "Created .env from .env.example — please edit it and add your OPENROUTER_API_KEY"
  fi
fi

# Check if container is already running
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "FinAlly is already running at http://localhost:${PORT}"
  exit 0
fi

# Build image if needed
if $BUILD || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
  echo "Building FinAlly Docker image…"
  docker build -t "$IMAGE_NAME" .
fi

# Remove stopped container if exists
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Run container
echo "Starting FinAlly…"
docker run -d \
  --name "$CONTAINER_NAME" \
  -p "${PORT}:8000" \
  -v "${VOLUME}:/app/db" \
  --env-file .env \
  --restart unless-stopped \
  "$IMAGE_NAME"

echo ""
echo "✓ FinAlly is running at http://localhost:${PORT}"
echo "  Logs: docker logs -f ${CONTAINER_NAME}"
echo "  Stop: ./scripts/stop_mac.sh"
echo ""

# Open browser (macOS)
if command -v open &>/dev/null; then
  sleep 1
  open "http://localhost:${PORT}" 2>/dev/null || true
fi
