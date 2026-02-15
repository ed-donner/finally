#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${FLY_APP_NAME:-finally-ed}"
CONFIG_PATH="${FLY_CONFIG_PATH:-planning/fly.toml}"
PORT_VALUE="${FLY_PORT:-8080}"
ENV_FILE="${FLY_ENV_FILE:-.env}"
URL="https://${APP_NAME}.fly.dev"

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Error: required command not found: $cmd" >&2
    exit 1
  fi
}

FLY_BIN="${FLY_BIN:-flyctl}"
if ! command -v "$FLY_BIN" >/dev/null 2>&1; then
  if [[ -x "$HOME/.fly/bin/flyctl" ]]; then
    FLY_BIN="$HOME/.fly/bin/flyctl"
  fi
fi
require_cmd "$FLY_BIN"

if [[ ! -f "$CONFIG_PATH" ]]; then
  echo "Error: fly config not found at $CONFIG_PATH" >&2
  exit 1
fi

# Keep runtime port aligned with fly.toml internal_port for this app.
echo "Setting Fly runtime env PORT=$PORT_VALUE for app '$APP_NAME'..."
"$FLY_BIN" secrets set PORT="$PORT_VALUE" --app "$APP_NAME"

if [[ -f "$ENV_FILE" ]]; then
  echo "Importing secrets from '$ENV_FILE' into app '$APP_NAME'..."
  # Accept dotenv lines in KEY=VALUE format; ignore blanks/comments.
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" | "$FLY_BIN" secrets import --app "$APP_NAME"
else
  echo "No env file found at '$ENV_FILE'; skipping secret import."
fi

echo "Deploying app '$APP_NAME' using config '$CONFIG_PATH'..."
"$FLY_BIN" deploy \
  --app "$APP_NAME" \
  --config "$CONFIG_PATH" \
  --remote-only

echo
echo "Deployment complete."
echo "App URL: $URL"
echo "Health check: ${URL}/api/health"
