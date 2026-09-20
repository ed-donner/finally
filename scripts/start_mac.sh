#!/usr/bin/env bash
# Start FinAlly in Docker (macOS / Linux).
#
#   ./scripts/start_mac.sh              start (builds the image the first time)
#   ./scripts/start_mac.sh --build      rebuild the image, then (re)start
#   ./scripts/start_mac.sh --no-open    don't open the browser
#
# Safe to run repeatedly: if FinAlly is already running it just prints the URL.
# The database lives in the Docker volume "finally-data" and survives stop/start.

set -euo pipefail

IMAGE="finally"
CONTAINER="finally"
VOLUME="finally-data"
URL="http://localhost:8000"

usage() {
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
}

die() {
  echo "Error: $*" >&2
  exit 1
}

BUILD=false
OPEN_BROWSER=true
for arg in "$@"; do
  case "$arg" in
    --build) BUILD=true ;;
    --no-open) OPEN_BROWSER=false ;;
    -h | --help) usage; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

# Git Bash on Windows rewrites arguments like `-v name:/app/db` into Windows paths; turn that off.
export MSYS_NO_PATHCONV=1

# Work from the project root no matter where the script is called from.
cd "$(dirname "${BASH_SOURCE[0]}")/.."

command -v docker >/dev/null 2>&1 || die "Docker is not installed. Get it from https://docs.docker.com/get-docker/"
docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker Desktop (or the Docker daemon) and try again."

container_exists() { docker container inspect "$CONTAINER" >/dev/null 2>&1; }
container_running() { [ "$(docker container inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || true)" = "true" ]; }

# NOTE: `docker inspect NAME` matches images and volumes too, so container checks must use
# `docker container inspect`, or an existing image named "finally" looks like a running container.

# --- 1. Build the image if it is missing (or if --build was passed) ---------------------------
REBUILT=false
if $BUILD || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "Building the FinAlly image (the first build takes a few minutes)..."
  docker build -t "$IMAGE" .
  REBUILT=true
fi

# --- 2. Start the container ---------------------------------------------------------------------
if container_running && ! $REBUILT; then
  echo "FinAlly is already running."
else
  # Replace any old container (stopped, or running an older image). The volume is untouched.
  if container_exists; then
    docker rm -f "$CONTAINER" >/dev/null
  fi

  ENV_ARGS=()
  if [ -f .env ]; then
    ENV_ARGS=(--env-file .env)
  else
    echo "Warning: no .env file found in $(pwd)." >&2
    echo "         The app will run, but AI chat needs OPENROUTER_API_KEY. Create .env and re-run." >&2
  fi

  echo "Starting the container..."
  docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    -p 8000:8000 \
    -v "$VOLUME":/app/db \
    ${ENV_ARGS[@]+"${ENV_ARGS[@]}"} \
    "$IMAGE" >/dev/null || die "Could not start the container. Is port 8000 already in use by another program?"
fi

# --- 3. Wait until the app answers -------------------------------------------------------------
probe() {
  if command -v curl >/dev/null 2>&1; then
    curl -fs --max-time 2 "$URL/api/health" >/dev/null
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O - --timeout=2 "$URL/api/health" >/dev/null
  else
    return 0 # no HTTP client available: skip the check
  fi
}

printf "Waiting for FinAlly to be ready"
READY=false
for _ in $(seq 1 60); do
  if probe; then READY=true; break; fi
  if ! container_running; then
    echo
    docker logs --tail 20 "$CONTAINER" >&2 || true
    die "The container stopped during startup (log above)."
  fi
  printf "."
  sleep 1
done
echo
$READY || die "FinAlly did not respond within 60 seconds. Check: docker logs $CONTAINER"

# --- 4. Open the browser, print instructions -----------------------------------------------------
open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1
  elif command -v cmd.exe >/dev/null 2>&1; then # Git Bash / WSL
    cmd.exe /c start "" "$URL" >/dev/null 2>&1
  else
    return 1
  fi
}

if $OPEN_BROWSER; then
  open_browser || echo "(Could not open a browser automatically; open the URL yourself.)"
fi

cat <<EOF

  FinAlly is running:  $URL

  View logs :  docker logs -f $CONTAINER
  Stop      :  ./scripts/stop_mac.sh      (your data is kept in the "$VOLUME" volume)
  Rebuild   :  ./scripts/start_mac.sh --build
  Changed .env?  Run stop_mac.sh, then start_mac.sh (keys are read when the container starts).
  Reset data:    ./scripts/stop_mac.sh && docker volume rm $VOLUME

EOF
