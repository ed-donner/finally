#!/usr/bin/env bash
# FinAlly — start script (macOS / Linux).
# Idempotent: builds the image if missing (or with --build), (re)creates the
# container with the persistent volume, port mapping, and .env file.
#
# Usage:
#   ./scripts/start_mac.sh            # build if needed, then run
#   ./scripts/start_mac.sh --build    # force a rebuild first
set -euo pipefail

IMAGE="finally:latest"
CONTAINER="finally"
PORT="8000"
VOLUME="finally-data"
FORCE_BUILD="false"

for arg in "$@"; do
  case "$arg" in
    --build) FORCE_BUILD="true" ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

# Resolve repo root (parent of this script's directory).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not on PATH." >&2
  exit 1
fi

# Ensure a .env exists (create from example on first run).
if [ ! -f .env ]; then
  echo "No .env found; creating one from .env.example."
  cp .env.example .env
fi

# Build the image if it doesn't exist yet, or if --build was passed.
if [ "$FORCE_BUILD" = "true" ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building image $IMAGE ..."
  docker build -t "$IMAGE" .
else
  echo "Image $IMAGE already exists (use --build to rebuild)."
fi

# Remove any existing container (idempotent restart).
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "Removing existing container $CONTAINER ..."
  docker rm -f "$CONTAINER" >/dev/null
fi

echo "Starting container $CONTAINER ..."
docker run -d \
  --name "$CONTAINER" \
  -p "${PORT}:8000" \
  -v "${VOLUME}:/app/db" \
  --env-file .env \
  "$IMAGE" >/dev/null

echo ""
echo "FinAlly is starting up."
echo "  -> http://localhost:${PORT}"
echo ""
echo "Logs:  docker logs -f ${CONTAINER}"
echo "Stop:  ./scripts/stop_mac.sh"
