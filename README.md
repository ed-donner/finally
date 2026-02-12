# FinAlly — AI Trading Workstation

Root-level infrastructure for running FinAlly in a single container on port `8003`, plus containerized backend and Playwright tests.

## Quick Start

1. Create env file:

```bash
cp .env.example .env
```

2. Start app:

```bash
./scripts/start_mac.sh --build
# or: docker compose up -d --build
```

3. Open:

- `http://localhost:8003`
- Health check: `http://localhost:8003/api/health`

4. Stop app:

```bash
./scripts/stop_mac.sh
# or: docker compose down
```

## Single-Container Runtime

- `Dockerfile` builds backend runtime and (if present) frontend static assets.
- If `frontend/` is missing during build, a safe placeholder static page is served.
- Runtime listens on `PORT=8003`.
- SQLite data persists via Docker volume mount at `/app/db`.
- `APP_MODULE` (default `app.main:app`) can point to a backend ASGI app. If unavailable, fallback runtime serves `/api/health`, static files, and market stream router when available.

## Docker Compose

Start:

```bash
docker compose up -d --build
```

Stop:

```bash
docker compose down
```

## Start/Stop Scripts

- macOS/Linux:
  - `scripts/start_mac.sh [--build] [--open]`
  - `scripts/stop_mac.sh`
- Windows PowerShell:
  - `scripts/start_windows.ps1 [-Build] [-Open]`
  - `scripts/stop_windows.ps1`

## Containerized Tests

Runs backend pytest + Playwright in containers:

```bash
./scripts/test_mac.sh
# optional: ./scripts/test_mac.sh --build
# real-LLM validation: ./scripts/test_mac.sh --real-llm
```

Windows PowerShell:

```powershell
./scripts/test_windows.ps1
# real-LLM validation: ./scripts/test_windows.ps1 -RealLlm
```

Manual test compose commands:

```bash
docker compose -f test/docker-compose.test.yml build
docker compose -f test/docker-compose.test.yml run --rm backend-tests
docker compose -f test/docker-compose.test.yml up -d app
docker compose -f test/docker-compose.test.yml run --rm playwright
docker compose -f test/docker-compose.test.yml down -v
```

Real LLM E2E only:

```bash
TEST_LLM_MOCK=false REAL_LLM_E2E=true PLAYWRIGHT_TEST_ARGS="--grep @real-llm --workers=1 --retries=0" \
  docker compose -f test/docker-compose.test.yml run --rm playwright
```

## Playwright Harness

Located in `test/`:

- `test/docker-compose.test.yml`
- `test/playwright.config.ts`
- `test/specs/smoke.spec.ts`

The Playwright container targets `BASE_URL=http://app:8003` inside the test network.

## Environment Variables

See `.env.example`:

- `OPENROUTER_API_KEY` (required for real LLM chat flow)
- `MASSIVE_API_KEY` (optional)
- `LLM_MOCK` (recommended `true` in tests)
- `APP_MODULE` (backend ASGI module, default `app.main:app`)

For dedicated real-LLM Playwright validation, use script flags:

- macOS/Linux: `./scripts/test_mac.sh --real-llm`
- Windows: `./scripts/test_windows.ps1 -RealLlm`

Behavior:

- If `OPENROUTER_API_KEY` is missing, the real-LLM E2E test is skipped.
- If the key is present but invalid (or OpenRouter rejects it), the real-LLM E2E test fails with a clear auth/config error message.
