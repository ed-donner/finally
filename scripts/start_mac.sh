#!/usr/bin/env bash
# Start FinAlly in Docker (macOS / Linux). Idempotent.
#
# Usage: scripts/start_mac.sh [--build] [--no-open]
#   --build    force a rebuild of the Docker image
#   --no-open  do not open the browser
set -euo pipefail

IMAGE="finally"
CONTAINER="finally"
VOLUME="finally-data"
PORT="${FINALLY_PORT:-8000}"
URL="http://localhost:${PORT}"

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

BUILD=0
OPEN=1
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=1 ;;
    --no-open) OPEN=0 ;;
    -h|--help)
      sed -n '2,7p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed or not on PATH." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Error: cannot talk to the Docker daemon. Is Docker Desktop running?" >&2
  exit 1
fi

# --- .env ------------------------------------------------------------------
ENV_ARGS=()
if [[ -f .env ]]; then
  ENV_ARGS=(--env-file .env)
elif [[ -f .env.example ]]; then
  echo "Warning: .env not found; creating it from .env.example."
  echo "         Edit .env and set OPENROUTER_API_KEY to enable AI chat."
  cp .env.example .env
  ENV_ARGS=(--env-file .env)
else
  echo "Warning: no .env file; starting without environment file (AI chat will not work)."
fi

# --- Image -----------------------------------------------------------------
if [[ $BUILD -eq 1 ]] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building Docker image '$IMAGE'..."
  docker build -t "$IMAGE" .
fi

# --- Container -------------------------------------------------------------
if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  running="$(docker container inspect -f '{{.State.Running}}' "$CONTAINER")"
  if [[ "$running" == "true" && $BUILD -eq 0 ]]; then
    echo "Container '$CONTAINER' is already running."
  else
    echo "Removing existing container '$CONTAINER'..."
    docker rm -f "$CONTAINER" >/dev/null
  fi
fi

if ! docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Starting container '$CONTAINER'..."
  docker run -d \
    --name "$CONTAINER" \
    -v "$VOLUME":/app/db \
    -p "$PORT":8000 \
    ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
    "$IMAGE" >/dev/null
fi

# --- Wait for health ---------------------------------------------------------
echo -n "Waiting for FinAlly to become healthy"
healthy=0
for _ in $(seq 1 60); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then
    healthy=1
    break
  fi
  if [[ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ]]; then
    echo
    echo "Error: container exited. Recent logs:" >&2
    docker logs --tail 50 "$CONTAINER" >&2 || true
    exit 1
  fi
  echo -n "."
  sleep 1
done
echo

if [[ $healthy -eq 1 ]]; then
  echo "FinAlly is running at $URL"
else
  echo "Warning: health check did not pass within 60s. Check: docker logs $CONTAINER" >&2
fi

if [[ $OPEN -eq 1 && $healthy -eq 1 ]]; then
  if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  fi
fi
