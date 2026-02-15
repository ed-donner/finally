#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally_agents"
IMAGE_NAME="finally_agents"
VOLUME_NAME="finally-data-agents"
PORT=8000

cd "$(dirname "$0")/.."

# Build if image doesn't exist or --build flag passed
if [[ "${1:-}" == "--build" ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    echo "Building Docker image..."
    docker build -t "$IMAGE_NAME" .
fi

# Stop existing container if running
if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping existing container..."
    docker stop "$CONTAINER_NAME" >/dev/null
    docker rm "$CONTAINER_NAME" >/dev/null
fi

# Remove stopped container with same name
if docker ps -aq -f name="$CONTAINER_NAME" | grep -q .; then
    docker rm "$CONTAINER_NAME" >/dev/null
fi

# Check for .env file
ENV_FILE_ARG=""
if [[ -f .env ]]; then
    ENV_FILE_ARG="--env-file .env"
fi

echo "Starting FinAlly..."
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "$PORT:8000" \
    -v "$VOLUME_NAME:/app/db" \
    $ENV_FILE_ARG \
    "$IMAGE_NAME"

echo ""
echo "FinAlly is running at http://localhost:$PORT"
echo ""

# Open browser if on macOS
if command -v open &>/dev/null; then
    open "http://localhost:$PORT"
fi
