#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"

# Use docker if available, otherwise fall back to podman
if command -v docker &>/dev/null; then
    ENGINE="docker"
elif command -v podman &>/dev/null; then
    ENGINE="podman"
else
    echo "Error: neither docker nor podman found on PATH." >&2
    exit 1
fi

# Stop existing container if running
"$ENGINE" rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Build if --build flag passed or image doesn't exist
if [[ "${1:-}" == "--build" ]] || ! "$ENGINE" image inspect "$IMAGE_NAME" &>/dev/null; then
    echo "Building FinAlly image with $ENGINE..."
    "$ENGINE" build -t "$IMAGE_NAME" .
fi

# Run container
"$ENGINE" run -d \
    --name "$CONTAINER_NAME" \
    -p 8000:8000 \
    -v finally-data:/app/db \
    --env-file .env \
    "$IMAGE_NAME"

echo ""
echo "FinAlly is running at http://localhost:8000"
echo "Stop with: ./scripts/stop_mac.sh"
