#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"

docker stop "$CONTAINER_NAME" 2>/dev/null && echo "FinAlly stopped." || echo "FinAlly is not running."
docker rm "$CONTAINER_NAME" 2>/dev/null || true
