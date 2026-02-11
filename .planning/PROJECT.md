# FinAlly — AI Trading Workstation

## What This Is

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot. Built as the capstone project for an agentic AI coding course, demonstrating how orchestrated AI agents produce a production-quality full-stack application.

## Core Value

Users see live-updating prices, trade instantly with fake money, and chat with an AI that can analyze their portfolio and execute trades — all in a single dark, data-rich terminal aesthetic served from one Docker container.

## Requirements

### Validated

- Market data simulator (GBM with correlated moves, random shocks) — existing
- Market data Massive/Polygon.io REST poller — existing
- MarketDataSource abstract interface (strategy pattern) — existing
- Thread-safe PriceCache with version-based change detection — existing
- SSE streaming endpoint (`/api/stream/prices`) — existing
- Dynamic watchlist support in data sources (add/remove tickers) — existing
- Factory pattern for data source selection via environment variable — existing
- 73 passing tests covering market data subsystem — existing

### Active

- [ ] SQLite database with lazy initialization and schema seeding
- [ ] Portfolio management (positions, cash balance, P&L calculation)
- [ ] Trade execution (market orders, instant fill, validation)
- [ ] Portfolio snapshots (periodic + post-trade)
- [ ] Watchlist CRUD API endpoints
- [ ] Portfolio API endpoints (positions, history, trade)
- [ ] LLM chat integration (LiteLLM via OpenRouter/Cerebras, structured outputs)
- [ ] Chat auto-execution of trades and watchlist changes
- [ ] Chat message persistence
- [ ] Health check endpoint
- [ ] FastAPI app assembly (lifespan, static serving, route mounting)
- [ ] Next.js frontend with static export
- [ ] Watchlist panel with live prices, sparklines, flash animations
- [ ] Main chart area (selected ticker)
- [ ] Portfolio heatmap (treemap by weight, colored by P&L)
- [ ] P&L chart (portfolio value over time)
- [ ] Positions table
- [ ] Trade bar (ticker, quantity, buy/sell)
- [ ] AI chat panel (messages, loading state, inline action confirmations)
- [ ] Header (total value, cash, connection status)
- [ ] Dark terminal aesthetic (Bloomberg-inspired)
- [ ] Multi-stage Dockerfile (Node build + Python runtime)
- [ ] docker-compose.yml
- [ ] Start/stop scripts (macOS/Linux + Windows)
- [ ] E2E tests with Playwright

### Out of Scope

- User authentication / multi-user — single hardcoded "default" user
- Real money / real brokerage integration — simulated only
- Limit orders / order book — market orders only
- Mobile-native app — web only, desktop-first
- Real-time chat streaming — Cerebras is fast enough for request/response
- Cloud deployment Terraform — optional stretch goal, not core

## Context

- **Existing code:** Complete market data subsystem in `backend/app/market/` (~500 lines, 8 modules, 73 tests). All downstream code should use `PriceCache` and `create_market_data_source()`.
- **Course context:** Capstone for agentic AI coding course. The app itself is the demo — it must look impressive and work fluidly.
- **Single container:** Everything runs on port 8000 from one Docker image. Frontend is a Next.js static export served by FastAPI.
- **No CORS needed:** Same origin for frontend and API.

## Constraints

- **Tech stack:** FastAPI (Python/uv) backend, Next.js (TypeScript) frontend, SQLite database, Docker deployment
- **Single container:** One Docker image, one port (8000), no external services except OpenRouter API
- **Package manager:** `uv` for Python, `npm` for Node
- **LLM provider:** LiteLLM via OpenRouter with Cerebras inference (`openrouter/openai/gpt-oss-120b`)
- **Market orders only:** No limit orders, no order book, no partial fills
- **Single user:** Hardcoded `user_id="default"`, but schema includes user_id for future multi-user
- **Color scheme:** Accent Yellow `#ecad0a`, Blue Primary `#209dd7`, Purple Secondary `#753991`, dark backgrounds `#0d1117`/`#1a1a2e`

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| SSE over WebSockets | One-way push, simpler, universal browser support | -- Pending |
| Static Next.js export | Single origin, no CORS, one container | -- Pending |
| SQLite over Postgres | No multi-user, zero config, self-contained | -- Pending |
| Market orders only | Eliminates order book complexity | -- Pending |
| LLM auto-executes trades | Zero stakes (fake money), impressive demo, agentic AI theme | -- Pending |
| No token streaming for chat | Cerebras inference is fast enough, simpler implementation | -- Pending |
| Quality model profile | Production-quality output, course capstone deserves best results | -- Pending |

---
*Last updated: 2026-02-11 after initialization*
