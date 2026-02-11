---
phase: 10-packaging-testing
plan: 01
subsystem: infra
tags: [docker, multi-stage-build, node, python, uv, uvicorn, scripts]

# Dependency graph
requires:
  - phase: 06-frontend-shell
    provides: Next.js static export (frontend/out)
  - phase: 01-backend-foundation
    provides: FastAPI app with uvicorn entry point
provides:
  - Multi-stage Dockerfile building frontend + backend into single image
  - docker-compose.yml with named volume and healthcheck
  - Start/stop scripts for macOS/Linux and Windows
  - .env.example documenting all environment variables
affects: [10-02-testing, deployment]

# Tech tracking
tech-stack:
  added: [docker, docker-compose]
  patterns: [multi-stage-build, uv-cache-mount, named-volume-persistence]

key-files:
  created:
    - Dockerfile
    - docker-compose.yml
    - .dockerignore
    - .env.example
    - scripts/start_mac.sh
    - scripts/stop_mac.sh
    - scripts/start_windows.ps1
    - scripts/stop_windows.ps1

key-decisions:
  - "uv cache mount (--mount=type=cache) for faster rebuilds"
  - "curl installed in image for docker healthcheck"
  - "Single CMD using .venv/bin/uvicorn directly (no activation needed)"

patterns-established:
  - "Multi-stage build: Node 20 slim stage 1 for frontend, Python 3.12 slim stage 2 for runtime"
  - "Copy dependency files first, then source for Docker layer caching"
  - "Idempotent scripts: safe to run multiple times without side effects"

# Metrics
duration: 2min
completed: 2026-02-11
---

# Phase 10 Plan 01: Docker Packaging Summary

**Multi-stage Dockerfile packaging Next.js static export + FastAPI backend into single container on port 8000 with start/stop scripts**

## Performance

- **Duration:** 2 min
- **Started:** 2026-02-11T20:35:05Z
- **Completed:** 2026-02-11T20:37:14Z
- **Tasks:** 2
- **Files modified:** 8

## Accomplishments
- Multi-stage Dockerfile builds frontend (Node 20) and packages with Python 3.12 backend
- Container serves frontend at / and API at /api/health on port 8000 (verified with curl)
- docker-compose.yml provides convenience wrapper with named volume "finally-data" and healthcheck
- Start/stop scripts for both macOS/Linux (bash) and Windows (PowerShell)

## Task Commits

Each task was committed atomically:

1. **Task 1: Dockerfile, .dockerignore, docker-compose.yml, and .env.example** - `ff8abde` (feat)
2. **Task 2: Start/stop scripts for macOS/Linux and Windows** - `57fffd8` (feat)

## Files Created/Modified
- `Dockerfile` - Multi-stage build: Node 20 slim frontend builder, Python 3.12 slim runtime
- `.dockerignore` - Excludes .git, node_modules, .venv, .planning, db files from build context
- `docker-compose.yml` - App service with port 8000, named volume, and curl healthcheck
- `.env.example` - Documents OPENROUTER_API_KEY, MASSIVE_API_KEY, LLM_MOCK
- `scripts/start_mac.sh` - Builds image if needed, runs container with volume and env file
- `scripts/stop_mac.sh` - Stops and removes container, preserves data volume
- `scripts/start_windows.ps1` - PowerShell equivalent of start_mac.sh
- `scripts/stop_windows.ps1` - PowerShell equivalent of stop_mac.sh

## Decisions Made
- Used `uv sync --locked` with `--mount=type=cache,target=/root/.cache/uv` for efficient rebuilds
- Installed curl in the image specifically for the Docker healthcheck command
- Used `/app/.venv/bin/uvicorn` as CMD directly (no shell activation needed with UV_LINK_MODE=copy)

## Deviations from Plan

None - plan executed exactly as written.

## Issues Encountered

None

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Docker packaging complete, ready for E2E testing (Plan 10-02)
- SSE streaming through Docker networking verified (health check passes, frontend loads)
- Container can be started with `./scripts/start_mac.sh` or `docker-compose up`

## Self-Check: PASSED

All 8 created files verified present. Both task commits (ff8abde, 57fffd8) verified in git log.

---
*Phase: 10-packaging-testing*
*Completed: 2026-02-11*
