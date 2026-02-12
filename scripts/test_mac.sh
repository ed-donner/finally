#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_COMPOSE_FILE="${ROOT_DIR}/test/docker-compose.test.yml"

BUILD_FLAG=""
REAL_LLM_MODE="false"
for arg in "$@"; do
  case "$arg" in
    --build)
      BUILD_FLAG="--no-cache"
      ;;
    --real-llm)
      REAL_LLM_MODE="true"
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--build] [--real-llm]" >&2
      exit 1
      ;;
  esac
done

if [ "$REAL_LLM_MODE" = "true" ]; then
  export TEST_LLM_MOCK="false"
  export REAL_LLM_E2E="true"
  export PLAYWRIGHT_TEST_ARGS="--grep @real-llm --workers=1 --retries=0"
else
  export TEST_LLM_MOCK="true"
  export REAL_LLM_E2E="false"
  export PLAYWRIGHT_TEST_ARGS="--grep-invert @real-llm"
fi

if [ -n "$BUILD_FLAG" ]; then
  docker compose -f "$TEST_COMPOSE_FILE" build $BUILD_FLAG
else
  docker compose -f "$TEST_COMPOSE_FILE" build
fi

docker compose -f "$TEST_COMPOSE_FILE" run --rm backend-tests

docker compose -f "$TEST_COMPOSE_FILE" up -d app
trap 'docker compose -f "$TEST_COMPOSE_FILE" down -v' EXIT

docker compose -f "$TEST_COMPOSE_FILE" run --rm playwright

echo "Containerized backend + Playwright tests completed."
