# External Integrations

**Analysis Date:** 2026-02-11

## APIs & External Services

**Market Data:**
- Massive (Polygon.io) - Real-time stock market data
  - SDK/Client: `massive>=1.0.0` (RESTClient)
  - Auth: `MASSIVE_API_KEY` environment variable
  - Usage: Optional; if present, backend polls REST API for real prices; if missing, built-in GBM simulator is used
  - Endpoint: `GET /v2/snapshot/locale/us/markets/stocks/tickers` for snapshot data
  - Rate limits: Free tier 5 req/min (poll every 15s), paid tiers support faster polling
  - Implementation: `app/market/massive_client.py` (MassiveDataSource class)

**LLM/AI Services:**
- OpenRouter - LLM proxy API for AI chat assistant
  - SDK/Client: LiteLLM (via OpenRouter integration)
  - Auth: `OPENROUTER_API_KEY` environment variable
  - Usage: Chat endpoint calls OpenRouter → Cerebras inference provider for fast LLM responses
  - Model: `openrouter/openai/gpt-oss-120b` (configured via CLAUDE.md references)
  - Response format: Structured JSON with `message`, `trades`, `watchlist_changes` fields
  - Not yet implemented in codebase but specified in PLAN.md and mentioned in backend CLAUDE.md

## Data Storage

**Databases:**
- SQLite 3 (embedded)
  - Connection: Local file at `/app/db/finally.db` (or `db/finally.db` on host)
  - Client: Python standard `sqlite3` module (or via abstraction layer)
  - Schema: 6 tables (`users_profile`, `watchlist`, `positions`, `trades`, `portfolio_snapshots`, `chat_messages`)
  - Initialization: Lazy - tables created automatically if missing on first request
  - User support: Single "default" user hardcoded (future-proofed for multi-user via `user_id` columns)

**File Storage:**
- None currently; future support for user uploads would require external storage or local filesystem

**Caching:**
- In-memory price cache: `PriceCache` class in `app/market/cache.py`
  - Thread-safe store for latest ticker prices
  - Used by both simulator and Massive poller to feed SSE streaming
  - No external cache service (Redis, Memcached); single-process, in-memory only

## Authentication & Identity

**Auth Provider:**
- None currently - No user login/authentication
- Single hardcoded user ID: `"default"`
- All data operations implicitly use this user
- Future: `user_id` columns in database tables allow migration to multi-user with auth layer

**Session Management:**
- Stateless REST API (no session tokens)
- SSE stream established per browser connection (no persistent session)

## Monitoring & Observability

**Error Tracking:**
- None configured - Errors logged to stdout via Python `logging` module
- Can be integrated with Sentry, DataDog, etc. via environment variable configuration

**Logging:**
- Python standard `logging` module
- Log level and handlers configurable via environment (or hardcoded in app)
- Critical components log key lifecycle events:
  - `app/market/massive_client.py` - API poll success/failure
  - `app/market/simulator.py` - Price generation events
  - `app/market/stream.py` - SSE connection events

**Metrics:**
- None configured - Could integrate Prometheus, CloudWatch, etc.

## CI/CD & Deployment

**Hosting:**
- Docker container (single image, single port 8000)
- Designed for deployment to:
  - AWS App Runner (serverless container)
  - Render (container platform)
  - Docker Hub (manual Docker runs)
  - Local Docker development

**CI Pipeline:**
- Not configured yet - GitHub Actions workflows not in place
- Future: Could add workflows for test, lint, build, push to registry

**Build Artifacts:**
- Docker image: `finally:latest` (or version tag)
- Image size: ~500MB-1GB (Python 3.12 slim + dependencies + static frontend)

## Environment Configuration

**Required env vars:**
- `OPENROUTER_API_KEY` - OpenRouter API key (string, starts with `sk-or-v1-*`)
  - Required for: Chat endpoint functionality
  - Location: `.env` file (gitignored)
  - Example: `.env.example` should be committed with placeholder

**Optional env vars:**
- `MASSIVE_API_KEY` - Polygon.io API key for real market data
  - If not set or empty: Backend uses built-in GBM simulator
  - If set: Backend uses Massive REST API polling
  - Rate tier determines poll interval (free=15s, paid=2-5s)

**Testing env vars:**
- `LLM_MOCK` - Set to `"true"` for deterministic mock LLM responses (skips OpenRouter call)
  - Used in E2E tests (docker-compose.test.yml) to avoid API costs
  - Default: `"false"` (use real API)

**Secrets location:**
- `.env` file at project root (gitignored)
- `.env.example` committed with placeholder values (for reference)
- Docker container reads via `--env-file .env` flag in start scripts

## Webhooks & Callbacks

**Incoming:**
- None currently

**Outgoing:**
- None currently
- Future: Could add trade confirmation webhooks, price alert notifications, etc.

## Frontend Integration

**API Base:**
- All frontend API calls to same origin (`/api/*` endpoints)
- No CORS configuration needed (frontend and backend in same container)

**Real-time Communication:**
- Server-Sent Events (SSE) via `EventSource` API
  - Endpoint: `GET /api/stream/prices`
  - One-way: server → client price updates only
  - Auto-reconnection: Built into browser EventSource API

**Chart/Data Libraries:**
- Lightweight Charts or Recharts (TBD in frontend implementation)
  - No external charting API; all computation done client-side

## Backend Service Dependencies

**Market Data Abstraction:**
- Abstract interface: `app/market/interface.py` (MarketDataSource)
- Two implementations:
  - `app/market/simulator.py` (SimulatorDataSource) - No external deps
  - `app/market/massive_client.py` (MassiveDataSource) - Depends on Massive API
- Factory: `app/market/factory.py` - Selects implementation based on env var

**Price Cache:**
- `app/market/cache.py` (PriceCache) - In-memory thread-safe store
- Shared across simulator/poller and SSE streaming
- No database dependency; all in RAM

**SSE Streaming:**
- `app/market/stream.py` (create_stream_router) - FastAPI router
- Reads from PriceCache every ~500ms
- No external dependencies

---

*Integration audit: 2026-02-11*
