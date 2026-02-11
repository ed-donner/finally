# Roadmap: FinAlly

## Overview

FinAlly builds outward from its existing market data subsystem: first a database foundation, then backend services (portfolio, watchlist, chat), then a complete frontend, and finally Docker packaging with E2E tests. Each phase delivers a coherent, verifiable capability. The backend phases (1-5) produce a fully functional API server; the frontend phases (6-9) produce the Bloomberg-inspired terminal UI; the final phase packages everything into a single Docker container.

## Phases

- [x] **Phase 1: Database Foundation** - SQLite with lazy initialization, schema, and seed data ✓
- [x] **Phase 2: Portfolio & Trade Execution** - Positions, cash management, trade validation, snapshots, REST API ✓
- [x] **Phase 3: Watchlist API** - CRUD endpoints with market data source synchronization ✓
- [x] **Phase 4: App Assembly** - FastAPI lifespan, route mounting, static serving, health check ✓
- [x] **Phase 5: LLM Chat Integration** - AI assistant with structured outputs, auto-execution, mock mode ✓
- [x] **Phase 6: Frontend Foundation** - Next.js static export, dark theme, layout shell, SSE connection ✓
- [x] **Phase 7: Watchlist & Price Display** - Live watchlist with flash animations, sparklines, main chart ✓
- [ ] **Phase 8: Portfolio Visualizations & Trading** - Heatmap, P&L chart, positions table, trade bar
- [ ] **Phase 9: Chat Interface** - AI chat panel with message history and inline action confirmations
- [ ] **Phase 10: Packaging & Testing** - Dockerfile, docker-compose, scripts, Playwright E2E tests

## Phase Details

### Phase 1: Database Foundation
**Goal**: All backend services can persist and retrieve state through a properly configured async SQLite layer
**Depends on**: Nothing (first phase)
**Requirements**: DB-01, DB-02, DB-03, DB-04
**Success Criteria** (what must be TRUE):
  1. Backend starts cleanly with no pre-existing database file and creates all 6 tables automatically
  2. Default user exists with $10,000 cash and 10 watchlist tickers after first initialization
  3. Multiple concurrent async operations (simulated reads and writes) complete without "database is locked" errors
  4. Restarting the backend with an existing database preserves all data without re-seeding
**Plans:** 1 plan

Plans:
- [x] 01-01-PLAN.md -- Async SQLite module with schema, seed data, and comprehensive tests ✓

### Phase 2: Portfolio & Trade Execution
**Goal**: Users can trade shares at market prices and see accurate portfolio state through REST endpoints
**Depends on**: Phase 1
**Requirements**: PORT-01, PORT-02, PORT-03, PORT-04, PORT-05, PORT-06, PORT-07, PORT-08
**Success Criteria** (what must be TRUE):
  1. GET /api/portfolio returns positions with current prices from PriceCache, unrealized P&L, cash balance, and total portfolio value
  2. POST /api/portfolio/trade with a buy order deducts cash and creates or updates the position; a sell order adds cash and reduces or removes the position
  3. Attempting to buy with insufficient cash or sell more shares than owned returns a clear validation error
  4. GET /api/portfolio/history returns timestamped snapshots of portfolio value, with new snapshots appearing every 30 seconds and immediately after trades
  5. Every executed trade appears in an append-only trade history
**Plans:** 2 plans

Plans:
- [x] 02-01-PLAN.md -- Portfolio service layer: Pydantic models, trade execution, portfolio queries, and service tests ✓
- [x] 02-02-PLAN.md -- Route factory, snapshot background task, HTTP endpoint tests ✓

### Phase 3: Watchlist API
**Goal**: Users can manage which tickers they watch, and changes propagate to the live price stream
**Depends on**: Phase 1
**Requirements**: WATCH-01, WATCH-02, WATCH-03, WATCH-04
**Success Criteria** (what must be TRUE):
  1. GET /api/watchlist returns the current watchlist tickers with latest prices from PriceCache
  2. POST /api/watchlist with a new ticker adds it to the database and the market data source starts streaming prices for it
  3. DELETE /api/watchlist/{ticker} removes it from the database and the market data source stops streaming prices for it
**Plans:** 1 plan

Plans:
- [x] 03-01-PLAN.md -- Watchlist models, service layer, and router factory with endpoint tests ✓

### Phase 4: App Assembly
**Goal**: A single FastAPI application starts up, initializes all resources, and serves all API routes on one port
**Depends on**: Phase 1, Phase 2, Phase 3
**Requirements**: APP-01, APP-02, APP-03, APP-04
**Success Criteria** (what must be TRUE):
  1. Running the FastAPI app initializes the database, loads the watchlist into the market data source, starts price streaming, and begins portfolio snapshot recording -- all via the lifespan context manager
  2. All API endpoints are accessible under /api/* with correct prefixes (portfolio, watchlist, chat, stream, health)
  3. GET /api/health returns a success response confirming the app is running
  4. Non-API routes serve static files (placeholder index.html until frontend is built)
**Plans:** 1 plan

Plans:
- [x] 04-01-PLAN.md -- FastAPI app with lifespan wiring, health check, SPA static serving, and integration tests ✓

### Phase 5: LLM Chat Integration
**Goal**: Users can converse with an AI assistant that understands their portfolio and can execute trades and manage the watchlist through natural language
**Depends on**: Phase 2, Phase 3, Phase 4
**Requirements**: CHAT-01, CHAT-02, CHAT-03, CHAT-04, CHAT-05, CHAT-06, CHAT-07, CHAT-08
**Success Criteria** (what must be TRUE):
  1. POST /api/chat with a user message returns an AI response that references the user's actual portfolio state (positions, cash, P&L)
  2. When the AI response includes trades, they execute automatically through the same validation as manual trades, and results appear in the response
  3. When the AI response includes watchlist changes, tickers are added or removed and the market data source updates accordingly
  4. Failed AI-initiated trades (insufficient cash/shares) are reported in the chat response rather than silently dropped
  5. With LLM_MOCK=true, the endpoint returns deterministic responses without calling OpenRouter
**Plans:** 2 plans

Plans:
- [x] 05-01-PLAN.md -- LLM service layer: Pydantic models, prompt builder, mock mode, chat processing, and unit tests ✓
- [x] 05-02-PLAN.md -- Chat router factory, main.py wiring, and HTTP endpoint tests ✓

### Phase 6: Frontend Foundation
**Goal**: A dark, terminal-aesthetic single-page app shell renders in the browser with live SSE price data flowing into application state
**Depends on**: Phase 4
**Requirements**: UI-01, UI-02, UI-03, UI-04, FE-RT-01, FE-RT-02, FE-RT-03
**Success Criteria** (what must be TRUE):
  1. The page renders a dark terminal layout with backgrounds (#0d1117/#1a1a2e), muted borders, and the project color scheme (yellow accent, blue primary, purple secondary)
  2. The header shows portfolio total value, cash balance, and a connection status dot (green when connected, yellow when reconnecting, red when disconnected)
  3. SSE connection to /api/stream/prices is established on page load and automatically reconnects after disconnection
  4. Layout contains placeholder regions for watchlist, chart area, portfolio area, and chat panel -- all visible on a desktop-width screen
**Plans:** 1 plan

Plans:
- [x] 06-01-PLAN.md -- Next.js static export with Tailwind v4 dark theme, CSS Grid terminal layout, Zustand stores, SSE price stream, and Header with live portfolio data ✓

### Phase 7: Watchlist & Price Display
**Goal**: Users see a live-updating watchlist with price flash animations and can click tickers to view price charts
**Depends on**: Phase 6
**Requirements**: FE-WATCH-01, FE-WATCH-02, FE-WATCH-03, FE-WATCH-04, FE-WATCH-05, FE-CHART-01, FE-CHART-02
**Success Criteria** (what must be TRUE):
  1. Watchlist panel displays all watched tickers with symbol, current price, daily change %, and direction indicator
  2. When a price updates, the ticker row briefly flashes green (uptick) or red (downtick) with a ~500ms CSS fade
  3. Sparkline mini-charts beside each ticker show price history accumulated from the SSE stream since page load
  4. Clicking a ticker in the watchlist shows a larger price-over-time chart in the main chart area using canvas-based rendering
  5. User can add and remove tickers from the watchlist through UI controls
**Plans:** 2 plans

Plans:
- [x] 07-01-PLAN.md -- Watchlist panel with price flash animations, sparklines, ticker selection, and add/remove controls ✓
- [x] 07-02-PLAN.md -- Main chart panel with lightweight-charts v5 canvas rendering and real-time data ✓

### Phase 8: Portfolio Visualizations & Trading
**Goal**: Users can see their portfolio composition visually, track P&L over time, view all positions, and execute trades
**Depends on**: Phase 6, Phase 7
**Requirements**: FE-CHART-03, FE-CHART-04, FE-TRADE-01, FE-TRADE-02, FE-TRADE-03, FE-TRADE-04
**Success Criteria** (what must be TRUE):
  1. Portfolio heatmap renders as a treemap with positions sized by portfolio weight and colored green (profit) to red (loss)
  2. P&L line chart shows total portfolio value over time using snapshot data from the API
  3. Positions table displays all holdings with ticker, quantity, avg cost, current price, unrealized P&L, and % change
  4. Trade bar allows entering a ticker and quantity, clicking buy or sell, and seeing the portfolio update immediately without a confirmation dialog
  5. Trade validation errors display inline with clear feedback
**Plans**: TBD

Plans:
- [ ] 08-01: TBD

### Phase 9: Chat Interface
**Goal**: Users can chat with the AI assistant and see trade executions and watchlist changes rendered inline as action confirmations
**Depends on**: Phase 5, Phase 6
**Requirements**: FE-CHAT-01, FE-CHAT-02, FE-CHAT-03, FE-CHAT-04, FE-CHAT-05
**Success Criteria** (what must be TRUE):
  1. AI chat panel is visible as a docked or collapsible sidebar
  2. User can type a message and send it, with a loading indicator displayed while waiting for the AI response
  3. Conversation history scrolls and displays user and assistant messages distinctly
  4. AI-executed trades and watchlist changes appear inline as structured action confirmations (not just text)
**Plans**: TBD

Plans:
- [ ] 09-01: TBD

### Phase 10: Packaging & Testing
**Goal**: The entire application runs from a single Docker container and passes end-to-end tests
**Depends on**: Phase 5, Phase 9
**Requirements**: PKG-01, PKG-02, PKG-03, PKG-04, PKG-05, PKG-06, TEST-01, TEST-02, TEST-03
**Success Criteria** (what must be TRUE):
  1. A multi-stage Dockerfile builds the Next.js frontend and packages it with the Python backend into a single image
  2. Running the Docker container on port 8000 serves the full application -- static frontend, all API endpoints, SSE streaming
  3. SQLite data persists across container restarts via a Docker named volume
  4. Start/stop scripts work on macOS/Linux (bash) and Windows (PowerShell)
  5. Playwright E2E tests pass against the Docker container with LLM_MOCK=true, covering fresh start, watchlist CRUD, buy/sell trades, portfolio updates, and AI chat

**Plans**: TBD

Plans:
- [ ] 10-01: TBD

## Progress

**Execution Order:**
Phases execute in numeric order: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7 -> 8 -> 9 -> 10
(Phases 2 and 3 can execute in parallel; Phases 7, 8, and 9 can overlap once Phase 6 completes)

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Database Foundation | 1/1 | ✓ Complete | 2026-02-11 |
| 2. Portfolio & Trade Execution | 2/2 | ✓ Complete | 2026-02-11 |
| 3. Watchlist API | 1/1 | ✓ Complete | 2026-02-11 |
| 4. App Assembly | 1/1 | ✓ Complete | 2026-02-11 |
| 5. LLM Chat Integration | 2/2 | ✓ Complete | 2026-02-11 |
| 6. Frontend Foundation | 1/1 | ✓ Complete | 2026-02-11 |
| 7. Watchlist & Price Display | 2/2 | ✓ Complete | 2026-02-11 |
| 8. Portfolio Visualizations & Trading | 0/TBD | Not started | - |
| 9. Chat Interface | 0/TBD | Not started | - |
| 10. Packaging & Testing | 0/TBD | Not started | - |
