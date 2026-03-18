#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"
VOLUME_NAME="finally-data"
PORT=8000
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$PROJECT_DIR/.env"

# Check for .env file
if [ ! -f "$ENV_FILE" ]; then
    echo "Warning: .env file not found at $ENV_FILE"
    echo "Copy .env.example to .env and fill in your API keys."
    echo "Continuing without environment variables..."
    ENV_FLAG=""
else
    ENV_FLAG="--env-file $ENV_FILE"
fi

# Stop existing container if running
if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping existing $CONTAINER_NAME container..."
    docker stop "$CONTAINER_NAME" > /dev/null 2>&1
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1
elif docker ps -aq -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Removing stopped $CONTAINER_NAME container..."
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1
fi

# Build image if it doesn't exist or --build flag is passed
if [ "${1:-}" = "--build" ] || ! docker image inspect "$IMAGE_NAME" > /dev/null 2>&1; then
    echo "Building Docker image..."
    docker build -t "$IMAGE_NAME" "$PROJECT_DIR"
fi

# Run the container
echo "Starting $CONTAINER_NAME..."
docker run -d \
    --name "$CONTAINER_NAME" \
    -p "$PORT:8000" \
    -v "$VOLUME_NAME:/app/db" \
    $ENV_FLAG \
    "$IMAGE_NAME"

echo ""
echo "FinAlly is running at http://localhost:$PORT"
echo "To stop: ./scripts/stop_mac.sh"

# Open browser (macOS)
if command -v open > /dev/null 2>&1; then
    open "http://localhost:$PORT"
fi
