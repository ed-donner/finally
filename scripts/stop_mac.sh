#!/usr/bin/env bash
# Stop FinAlly (macOS / Linux).
#
# Stops and removes the container. The database volume "finally-data" is NOT touched, so your
# portfolio, trades and chat history are still there next time you start.
# Safe to run repeatedly: if nothing is running it says so and exits successfully.

set -euo pipefail

CONTAINER="finally"
VOLUME="finally-data"

# Git Bash on Windows rewrites arguments like `-v name:/app/db` into Windows paths; turn that off.
export MSYS_NO_PATHCONV=1

command -v docker >/dev/null 2>&1 || { echo "Error: Docker is not installed." >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "Error: Docker is not running, so there is nothing to stop." >&2; exit 1; }

if docker container inspect "$CONTAINER" >/dev/null 2>&1; then
  echo "Stopping FinAlly..."
  docker stop "$CONTAINER" >/dev/null # graceful: lets the app finish writing to the database
  docker rm "$CONTAINER" >/dev/null
  echo "Stopped. Your data is kept in the \"$VOLUME\" volume."
else
  echo "FinAlly is not running. Nothing to do."
fi

echo "Start again with ./scripts/start_mac.sh"
