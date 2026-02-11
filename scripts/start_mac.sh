#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"

# Stop existing container if running
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Build if --build flag passed or image doesn't exist
if [[ "${1:-}" == "--build" ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    echo "Building FinAlly Docker image..."
    docker build -t "$IMAGE_NAME" .
fi

# Run container
docker run -d \
    --name "$CONTAINER_NAME" \
    -p 8000:8000 \
    -v finally-data:/app/db \
    --env-file .env \
    "$IMAGE_NAME"

echo ""
echo "FinAlly is running at http://localhost:8000"
echo "Stop with: ./scripts/stop_mac.sh"
