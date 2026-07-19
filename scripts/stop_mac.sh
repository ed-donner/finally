#!/usr/bin/env bash
# FinAlly — stop script (macOS / Linux).
# Stops and removes the running container. Does NOT remove the data volume,
# so the SQLite database (portfolio, trades, chat history) persists.
#
# Usage:
#   ./scripts/stop_mac.sh
set -euo pipefail

CONTAINER="finally"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not on PATH." >&2
  exit 1
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Stopping and removing container $CONTAINER ..."
  docker rm -f "$CONTAINER" >/dev/null
  echo "Stopped. Data volume 'finally-data' preserved."
else
  echo "Container $CONTAINER is not running; nothing to do."
fi
