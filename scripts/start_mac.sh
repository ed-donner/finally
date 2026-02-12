#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
BUILD_FLAG=""
OPEN_BROWSER="false"

for arg in "$@"; do
  case "$arg" in
    --build)
      BUILD_FLAG="--build"
      ;;
    --open)
      OPEN_BROWSER="true"
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--build] [--open]" >&2
      exit 1
      ;;
  esac
done

if [ -n "$BUILD_FLAG" ]; then
  docker compose -f "$COMPOSE_FILE" up -d "$BUILD_FLAG"
else
  docker compose -f "$COMPOSE_FILE" up -d
fi

echo "FinAlly is starting on http://localhost:8003"

docker compose -f "$COMPOSE_FILE" ps

if [ "$OPEN_BROWSER" = "true" ]; then
  if command -v open >/dev/null 2>&1; then
    open http://localhost:8003
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open http://localhost:8003 >/dev/null 2>&1 || true
  fi
fi
