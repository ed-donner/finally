#!/usr/bin/env bash
# Stop FinAlly (macOS / Linux). Idempotent. The data volume is kept.
set -euo pipefail

CONTAINER="finally"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not on PATH." >&2
  exit 1
fi

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Stopping and removing container '$CONTAINER'..."
  docker rm -f "$CONTAINER" >/dev/null
  echo "Stopped. Data volume 'finally-data' was kept."
else
  echo "Container '$CONTAINER' is not running."
fi
