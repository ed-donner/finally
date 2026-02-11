# Phase 10: Packaging & Testing - Research

**Researched:** 2026-02-11
**Domain:** Docker multi-stage builds, Playwright E2E testing, shell scripting
**Confidence:** HIGH

## Summary

Phase 10 packages the entire FinAlly application into a single Docker container and validates it with Playwright E2E tests. The build is a two-stage Dockerfile: Stage 1 uses Node 20 slim to build the Next.js static export, Stage 2 uses Python 3.12 slim with uv to install the backend and copies the frontend build output into the `static/` directory that FastAPI already serves.

The existing backend code already handles SSE anti-buffering headers (`X-Accel-Buffering: no`, `Cache-Control: no-cache`, `Connection: keep-alive`) in `stream.py`. Docker port mapping (`-p 8000:8000`) does NOT cause SSE buffering -- that issue is specific to reverse proxies (nginx, traefik) and Windows/IIS containers. The concern from STATE.md is resolved: SSE works through Docker port mapping without additional configuration.

**Primary recommendation:** Use `COPY --from=ghcr.io/astral-sh/uv:latest` for the uv binary in the Python stage. The Playwright tests should run from the host (or a separate container) against the Docker container, using `docker-compose.test.yml` with a healthcheck on the app service.

## Standard Stack

### Core

| Library/Tool | Version | Purpose | Why Standard |
|---|---|---|---|
| Docker multi-stage | - | Build frontend + backend in one image | PLAN.md Section 11 specifies this |
| Node 20 slim | 20-slim | Build Next.js static export | PLAN.md specifies Node 20 slim |
| Python 3.12 slim | 3.12-slim | Run FastAPI backend | PLAN.md specifies Python 3.12 slim |
| uv (in Docker) | latest (0.10.x) | Install Python dependencies | Project uses uv throughout |
| @playwright/test | ^1.58 | E2E test framework | Industry standard, official Docker support |
| docker-compose | v2 | Orchestrate containers | Specified in PLAN.md |

### Supporting

| Tool | Purpose | When to Use |
|---|---|---|
| `mcr.microsoft.com/playwright:v1.58.2-noble` | Playwright Docker image | For docker-compose.test.yml test runner |
| curl | Healthcheck in Docker | Container health monitoring |

## Architecture Patterns

### Dockerfile Multi-Stage Build

```dockerfile
# Stage 1: Build frontend
FROM node:20-slim AS frontend-builder
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# Stage 2: Python backend + frontend static files
FROM python:3.12-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install Python dependencies first (cache layer)
COPY backend/pyproject.toml backend/uv.lock ./
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --no-editable

# Copy backend source
COPY backend/ ./

# Install the project itself
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Copy frontend build output
COPY --from=frontend-builder /app/frontend/out ./static

# Create db directory for volume mount
RUN mkdir -p /app/db

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Source:** [uv Docker docs](https://docs.astral.sh/uv/guides/integration/docker/)

### Key Filesystem Mapping

| Host/Build | Container Path | Purpose |
|---|---|---|
| `frontend/out/` (from Stage 1) | `/app/static/` | Static files served by FastAPI |
| `db/` (volume mount) | `/app/db/` | SQLite database persistence |
| `backend/` | `/app/` | Python application code |
| `.env` | Passed via `--env-file` | Environment variables |

This mapping works because:
- `STATIC_DIR` defaults to `"static"` in `main.py` line 23
- `DB_PATH` defaults to `"db/finally.db"` in `main.py` line 22
- The working directory is `/app`, so relative paths resolve correctly

### docker-compose.yml

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    volumes:
      - finally-data:/app/db
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 10s
      timeout: 5s
      retries: 3
      start_period: 15s

volumes:
  finally-data:
```

### docker-compose.test.yml

```yaml
services:
  app:
    build: .
    ports:
      - "8000:8000"
    environment:
      - LLM_MOCK=true
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/api/health"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 10s

  tests:
    image: mcr.microsoft.com/playwright:v1.58.2-noble
    depends_on:
      app:
        condition: service_healthy
    working_dir: /app
    volumes:
      - ./test:/app
    environment:
      - BASE_URL=http://app:8000
    command: npx playwright test
    ipc: host
```

**Note:** The `tests` service uses `ipc: host` which is required for Chromium to avoid memory crashes.

### Playwright Test Project Structure

```
test/
  package.json
  playwright.config.ts
  tests/
    fresh-start.spec.ts
    watchlist.spec.ts
    trading.spec.ts
    portfolio.spec.ts
    chat.spec.ts
```

### Playwright Config Pattern

```typescript
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './tests',
  timeout: 30000,
  retries: 1,
  use: {
    baseURL: process.env.BASE_URL || 'http://localhost:8000',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
```

**Source:** [Playwright Configuration docs](https://playwright.dev/docs/test-configuration)

### Start/Stop Scripts Pattern

**macOS/Linux (bash):**
```bash
#!/usr/bin/env bash
set -euo pipefail

CONTAINER_NAME="finally"
IMAGE_NAME="finally"

# Stop existing container if running
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# Build if needed or --build flag passed
if [[ "${1:-}" == "--build" ]] || ! docker image inspect "$IMAGE_NAME" &>/dev/null; then
    docker build -t "$IMAGE_NAME" .
fi

# Run
docker run -d \
    --name "$CONTAINER_NAME" \
    -p 8000:8000 \
    -v finally-data:/app/db \
    --env-file .env \
    "$IMAGE_NAME"

echo "FinAlly running at http://localhost:8000"
```

**Windows (PowerShell):**
```powershell
$ContainerName = "finally"
$ImageName = "finally"

docker rm -f $ContainerName 2>$null

if ($args -contains "--build" -or -not (docker image inspect $ImageName 2>$null)) {
    docker build -t $ImageName .
}

docker run -d `
    --name $ContainerName `
    -p 8000:8000 `
    -v finally-data:/app/db `
    --env-file .env `
    $ImageName

Write-Host "FinAlly running at http://localhost:8000"
```

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---|---|---|---|
| SPA static serving | Custom file routing | SPAStaticFiles (already exists) | Already handles 404 -> index.html fallback |
| Browser test runner | Selenium/custom scripts | Playwright | Modern, fast, official Docker support, built-in assertions |
| Container orchestration | Shell scripts chaining docker commands | docker-compose | Declarative, handles volumes/networks/healthchecks |
| uv installation in Docker | curl + pip | `COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/` | Official pattern, no extra apt packages needed |
| Healthcheck logic | Custom health scripts | Docker HEALTHCHECK + `/api/health` endpoint | Already exists, standard Docker pattern |

## Common Pitfalls

### Pitfall 1: Frontend Build Output Path Mismatch
**What goes wrong:** Next.js exports to `out/` by default, but FastAPI expects `static/`
**Why it happens:** The `STATIC_DIR` env var defaults to `"static"`, and Next.js `output: "export"` builds to `out/`
**How to avoid:** In the Dockerfile, `COPY --from=frontend-builder /app/frontend/out ./static` maps `out/` to `static/`
**Warning signs:** 404 errors for all frontend routes, `/api/health` works but `/` returns nothing

### Pitfall 2: uv sync Without --no-dev in Production
**What goes wrong:** Image includes dev dependencies (pytest, ruff, httpx), bloating the image
**Why it happens:** `uv sync` installs everything by default including `[project.optional-dependencies] dev`
**How to avoid:** Use `uv sync --locked --no-dev --no-editable`
**Warning signs:** Image size much larger than expected

### Pitfall 3: uv Creates .venv, Not System Python
**What goes wrong:** CMD tries to run `python` or `uvicorn` directly but dependencies are in `.venv`
**Why it happens:** uv creates a virtual environment at `.venv/` by default
**How to avoid:** Use `/app/.venv/bin/uvicorn` as the CMD, or set `ENV PATH="/app/.venv/bin:$PATH"`
**Warning signs:** ModuleNotFoundError for fastapi/uvicorn at container startup

### Pitfall 4: npm ci vs npm install in Docker
**What goes wrong:** Non-deterministic builds or missing dependencies
**Why it happens:** `npm install` can update package-lock.json; `npm ci` uses the lockfile exactly
**How to avoid:** Always use `npm ci` in Docker builds (requires package-lock.json, which exists)
**Warning signs:** Different builds produce different results

### Pitfall 5: Docker Cache Invalidation Order
**What goes wrong:** Every code change triggers a full dependency reinstall
**Why it happens:** COPY of all source files before dependency install invalidates the cache
**How to avoid:** Copy only `pyproject.toml` + `uv.lock` first, run `uv sync --no-install-project`, then copy source
**Warning signs:** Slow builds even when only Python code changed

### Pitfall 6: Playwright Version Mismatch
**What goes wrong:** "Browser not found" errors in Playwright tests
**Why it happens:** The Playwright npm package version must match the Docker image version exactly
**How to avoid:** Pin both to the same version (e.g., `@playwright/test@1.58.2` + `mcr.microsoft.com/playwright:v1.58.2-noble`)
**Warning signs:** Tests fail immediately before any assertions run

### Pitfall 7: curl Not in Python Slim Image
**What goes wrong:** Docker healthcheck fails because `curl` is not available
**Why it happens:** `python:3.12-slim` doesn't include curl
**How to avoid:** Either install curl (`apt-get install -y --no-install-recommends curl`) or use a Python-based healthcheck (`python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')"`)
**Warning signs:** Container shows as "unhealthy" despite app running fine

### Pitfall 8: SSE Buffering Concern (RESOLVED)
**What goes wrong:** STATE.md flagged "SSE buffering through Docker networking needs verification"
**Resolution:** Docker port mapping (`-p 8000:8000`) uses iptables NAT forwarding which does NOT buffer HTTP responses. SSE buffering is caused by reverse proxies (nginx, traefik) not Docker's network layer. The existing headers in `stream.py` (`X-Accel-Buffering: no`, `Cache-Control: no-cache`) are correct and sufficient for when a reverse proxy IS used in production deployments.
**Evidence:** The moby/moby#28014 issue was Windows/IIS-specific, not a general Docker problem. Linux/macOS Docker forwards TCP packets directly.
**Source:** [moby/moby#28014](https://github.com/moby/moby/issues/28014)

## Code Examples

### E2E Test: Fresh Start (verified pattern from Playwright docs)

```typescript
import { test, expect } from '@playwright/test';

test('fresh start shows default state', async ({ page }) => {
  await page.goto('/');

  // Watchlist should show default tickers
  await expect(page.getByText('AAPL')).toBeVisible();
  await expect(page.getByText('GOOGL')).toBeVisible();
  await expect(page.getByText('MSFT')).toBeVisible();

  // Portfolio value should show $10,000
  await expect(page.getByText('$10,000')).toBeVisible();

  // Connection status should be green (connected)
  // SSE prices should start streaming
  await expect(page.locator('[data-testid="connection-status"]')).toBeVisible();
});
```

### E2E Test: Watchlist CRUD

```typescript
test('add and remove ticker from watchlist', async ({ page }) => {
  await page.goto('/');

  // Wait for initial load
  await expect(page.getByText('AAPL')).toBeVisible();

  // Add a new ticker
  await page.getByPlaceholder(/ticker/i).fill('PYPL');
  await page.getByRole('button', { name: /add/i }).click();
  await expect(page.getByText('PYPL')).toBeVisible();

  // Remove it
  // (depends on UI implementation - may be a remove button per row)
});
```

### E2E Test: Buy/Sell Trades

```typescript
test('buy shares reduces cash and creates position', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByText('AAPL')).toBeVisible();

  // Execute a buy trade
  await page.getByPlaceholder(/ticker/i).fill('AAPL');
  await page.getByPlaceholder(/quantity/i).fill('10');
  await page.getByRole('button', { name: /buy/i }).click();

  // Cash should decrease from $10,000
  // Position should appear in positions table
  await expect(page.getByText('AAPL')).toBeVisible();
});
```

### E2E Test: AI Chat (Mocked)

```typescript
test('chat with mocked AI returns response', async ({ page }) => {
  await page.goto('/');

  // Open chat panel if collapsed
  // Type a message
  await page.getByPlaceholder(/message/i).fill('What is my portfolio worth?');
  await page.getByRole('button', { name: /send/i }).click();

  // Wait for response (mocked, should be fast)
  await expect(page.locator('.assistant-message')).toBeVisible({ timeout: 10000 });
});
```

### .env.example

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|---|---|---|---|
| `pip install uv` in Docker | `COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/` | 2024+ | Faster, no pip needed, smaller image |
| `uv pip install` | `uv sync --locked` | uv 0.4+ | Uses lockfile for reproducibility |
| `python:3.12-bookworm-slim` | `python:3.12-slim` (trixie-based since Debian 13) | 2025 | Same concept, latest Debian base |
| Playwright `mcr.microsoft.com/playwright:focal` | `mcr.microsoft.com/playwright:v1.58.2-noble` | 2024 | Ubuntu 24.04 LTS base |
| Docker Compose v1 (`docker-compose`) | Docker Compose v2 (`docker compose`) | 2023 | Integrated into Docker CLI |

## Open Questions

1. **Exact Playwright version to pin**
   - What we know: Latest is v1.58.2 as of Feb 2026
   - What's unclear: Whether to pin to exact patch or minor version
   - Recommendation: Pin to exact version (1.58.2) in both package.json and Docker image tag for reproducibility

2. **Test data-testid attributes on frontend components**
   - What we know: E2E tests need selectors to find elements
   - What's unclear: Whether existing frontend components have data-testid attributes
   - Recommendation: Add data-testid attributes to key elements during test writing if missing. The E2E tests can also use text content selectors and ARIA roles as shown in examples.

3. **Trade bar selector specifics**
   - What we know: Trade bar exists with ticker input, quantity input, buy/sell buttons
   - What's unclear: Exact placeholder text and button labels used in the React components
   - Recommendation: Read the actual frontend components during planning to determine exact selectors

## Sources

### Primary (HIGH confidence)
- [uv Docker docs](https://docs.astral.sh/uv/guides/integration/docker/) - Installation patterns, multi-stage build, env vars, cache mounts
- [Playwright Docker docs](https://playwright.dev/docs/docker) - Official image names, version matching, `--ipc=host` requirement
- [Playwright Configuration docs](https://playwright.dev/docs/test-configuration) - `baseURL`, `webServer`, project config
- [Playwright Web Server docs](https://playwright.dev/docs/test-webserver) - Testing against external servers
- `/Users/ed/projects/finally/backend/app/main.py` - STATIC_DIR="static", DB_PATH="db/finally.db"
- `/Users/ed/projects/finally/backend/app/market/stream.py` - SSE headers already correct
- `/Users/ed/projects/finally/frontend/next.config.ts` - `output: "export"`, builds to `out/`

### Secondary (MEDIUM confidence)
- [moby/moby#28014](https://github.com/moby/moby/issues/28014) - Docker SSE issue was Windows/IIS-specific
- [npm @playwright/test](https://www.npmjs.com/package/@playwright/test) - Current version 1.58.2
- [Depot.dev uv Dockerfile guide](https://depot.dev/docs/container-builds/how-to-guides/optimal-dockerfiles/python-uv-dockerfile) - Multi-stage patterns with cache mounts

### Tertiary (LOW confidence)
- None -- all findings verified with primary or secondary sources

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH - uv Docker patterns are well documented by Astral; Playwright is industry standard
- Architecture: HIGH - Dockerfile pattern follows official uv docs; filesystem mapping verified against actual source code
- Pitfalls: HIGH - Each pitfall is derived from actual code inspection or verified documentation
- SSE concern: HIGH - Resolved with evidence; not a Docker port mapping issue

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable domain, 30-day validity)
