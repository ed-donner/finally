#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"

# Use docker if available, otherwise fall back to podman
if command -v docker &>/dev/null; then
    ENGINE="docker"
elif command -v podman &>/dev/null; then
    ENGINE="podman"
else
    echo "Error: neither docker nor podman found on PATH." >&2
    exit 1
fi

"$ENGINE" stop "$CONTAINER_NAME" 2>/dev/null && echo "FinAlly stopped." || echo "FinAlly is not running."
"$ENGINE" rm "$CONTAINER_NAME" 2>/dev/null || true
