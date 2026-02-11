#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally_agents"

if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping FinAlly..."
    docker stop "$CONTAINER_NAME" >/dev/null
    docker rm "$CONTAINER_NAME" >/dev/null
    echo "FinAlly stopped."
elif docker ps -aq -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Removing stopped container..."
    docker rm "$CONTAINER_NAME" >/dev/null
    echo "Done."
else
    echo "FinAlly is not running."
fi
