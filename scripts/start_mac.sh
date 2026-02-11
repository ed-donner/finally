#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
IMAGE_NAME="finally"
CONTAINER_NAME="finally-app"

cd "$PROJECT_DIR"

# Build if needed or if --build flag passed
if [[ "$1" == "--build" ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    echo "Building Docker image..."
    docker build -t "$IMAGE_NAME" .
fi

# Stop existing container if running
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Run
echo "Starting FinAlly..."
docker run -d \
    --name "$CONTAINER_NAME" \
    -p 8000:8000 \
    -v finally-data:/app/db \
    --env-file .env \
    "$IMAGE_NAME"

echo "FinAlly is running at http://localhost:8000"

# Open browser on macOS
if command -v open &>/dev/null; then
    sleep 2
    open http://localhost:8000
fi
