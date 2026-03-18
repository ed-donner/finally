#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"

if docker ps -q -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Stopping $CONTAINER_NAME..."
    docker stop "$CONTAINER_NAME" > /dev/null 2>&1
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1
    echo "Stopped."
elif docker ps -aq -f name="$CONTAINER_NAME" | grep -q .; then
    echo "Removing stopped $CONTAINER_NAME container..."
    docker rm "$CONTAINER_NAME" > /dev/null 2>&1
    echo "Removed."
else
    echo "No $CONTAINER_NAME container found."
fi
