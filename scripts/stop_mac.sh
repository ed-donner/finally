#!/bin/bash
CONTAINER_NAME="finally-app"

echo "Stopping FinAlly..."
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
echo "Stopped. Data volume preserved."
