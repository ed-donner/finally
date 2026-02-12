# FinAlly Status

## Current State
- Full-stack implementation is in place on this branch:
  - Backend API + DB + SSE + chat auto-execution
  - Frontend terminal UI
  - Docker single-container runtime on port `8003`
  - Unit tests and Playwright E2E scaffold
- Docker has been installed in this Sprite instance and is working.
- Docker daemon is running as Sprite service `docker-daemon`.
- App container is running and healthy at `http://localhost:8003`.

## Verified Test Results
- Backend unit/integration tests: **pass** (`80 passed`)
- Frontend unit tests: **pass** (`5 passed`)
- Containerized backend tests (`scripts/test_mac.sh` first stage): **pass**
- Containerized Playwright tests: infrastructure now runs in Docker, but some specs still need selector/interaction hardening (strict locator + click interception issues).

## OpenRouter Validation
- Environment propagation has been verified end-to-end:
  - `.env` contains `OPENROUTER_API_KEY`
  - `docker compose` passes it into the app container
  - app container sees `OPENROUTER_API_KEY` and `LLM_MOCK=False`
- Direct OpenRouter call from inside the running app container returns:
  - HTTP `401`
  - `{"error":{"message":"User not found.","code":401}}`
- Conclusion: this is currently an API key/account-side issue, not an env-loading issue in app/runtime.

## What Was Fixed Recently
- Docker installation and daemon startup in Sprite.
- Dockerfile build/runtime fixes (multi-stage build and runtime command).
- Test docker-compose fixes:
  - removed host port bind conflict for test app
  - aligned Playwright image version to package version
  - switched internal base URL alias to avoid HSTS `.app` hostname behavior
- Backend LLM error handling now degrades gracefully instead of returning 500s on upstream auth/network errors.

## Checkpoints
- Sprite checkpoint `v5` created after Docker + runtime integration fixes.

## Next Steps After Key Update
1. Update `.env` with a valid OpenRouter key.
2. Re-run full suite including:
   - backend + frontend unit tests
   - containerized Playwright suite (`LLM_MOCK=true`)
   - real OpenRouter E2E flow (`LLM_MOCK=false`) validating LLM-driven watchlist add/remove and trade execution.
3. Patch remaining Playwright flakiness/selectors until fully green.
