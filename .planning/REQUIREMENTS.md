# Requirements: FinAlly

**Defined:** 2026-02-11
**Core Value:** Users see live-updating prices, trade instantly with fake money, and chat with an AI that can analyze their portfolio and execute trades — all in a single dark, data-rich terminal aesthetic.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Database

- [x] **DB-01**: SQLite database initializes lazily on first request — creates schema and seeds data if missing
- [x] **DB-02**: Schema includes all 6 tables: users_profile, watchlist, positions, trades, portfolio_snapshots, chat_messages
- [x] **DB-03**: Default seed data: one user with $10,000 cash and 10 watchlist tickers (AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX)
- [x] **DB-04**: SQLite uses WAL mode with busy_timeout for safe concurrent access

### Portfolio

- [x] **PORT-01**: User can view current positions with ticker, quantity, avg cost, current price, unrealized P&L, and % change
- [x] **PORT-02**: User can view cash balance and total portfolio value (cash + positions at current prices)
- [x] **PORT-03**: User can buy shares at current market price (deducts cash, creates/updates position)
- [x] **PORT-04**: User can sell shares at current market price (adds cash, reduces/removes position)
- [x] **PORT-05**: Trade validation rejects buys with insufficient cash and sells with insufficient shares
- [x] **PORT-06**: Every executed trade is recorded in trade history (append-only log)
- [x] **PORT-07**: Portfolio value snapshots recorded every 30 seconds and immediately after each trade
- [x] **PORT-08**: User can view portfolio value history over time (for P&L chart)

### Watchlist

- [x] **WATCH-01**: User can view current watchlist tickers with latest prices
- [x] **WATCH-02**: User can add a ticker to the watchlist
- [x] **WATCH-03**: User can remove a ticker from the watchlist
- [x] **WATCH-04**: Watchlist changes sync with market data source (added tickers start streaming, removed tickers stop)

### Chat

- [ ] **CHAT-01**: User can send a message and receive an AI response with portfolio-aware analysis
- [ ] **CHAT-02**: AI responses include structured output (message + optional trades + optional watchlist changes)
- [ ] **CHAT-03**: Trades in AI response auto-execute through the same validation as manual trades
- [ ] **CHAT-04**: Watchlist changes in AI response auto-apply (add/remove tickers)
- [ ] **CHAT-05**: Failed trade executions from AI are reported in the chat response
- [ ] **CHAT-06**: Chat messages (user and assistant) persist in database with executed actions
- [ ] **CHAT-07**: Recent conversation history included in LLM context for continuity
- [ ] **CHAT-08**: Mock LLM mode returns deterministic responses when LLM_MOCK=true

### App Assembly

- [x] **APP-01**: FastAPI app uses lifespan for startup/shutdown of market data source, database, and background tasks
- [x] **APP-02**: All API routes mounted under /api/* with correct path prefixes
- [x] **APP-03**: Static Next.js export served by FastAPI for all non-API routes
- [x] **APP-04**: Health check endpoint at GET /api/health

### Frontend — Layout & Theme

- [ ] **UI-01**: Single-page dark terminal layout with Bloomberg-inspired aesthetic (backgrounds #0d1117/#1a1a2e, muted borders)
- [ ] **UI-02**: Header displays total portfolio value (updating live), cash balance, and connection status indicator
- [ ] **UI-03**: Layout is desktop-first, data-dense, functional on tablet
- [ ] **UI-04**: Color scheme uses accent yellow (#ecad0a), blue primary (#209dd7), purple secondary (#753991)

### Frontend — Watchlist

- [ ] **FE-WATCH-01**: Watchlist panel shows grid of tickers with symbol, current price, daily change %, and direction indicator
- [ ] **FE-WATCH-02**: Prices flash green (uptick) or red (downtick) with ~500ms CSS fade animation on each update
- [ ] **FE-WATCH-03**: Sparkline mini-charts beside each ticker, accumulated from SSE stream since page load
- [ ] **FE-WATCH-04**: Clicking a ticker selects it for the main chart area
- [ ] **FE-WATCH-05**: User can add/remove tickers from watchlist via UI controls

### Frontend — Charts

- [ ] **FE-CHART-01**: Main chart area shows price over time for the selected ticker (canvas-based, lightweight-charts)
- [ ] **FE-CHART-02**: Chart data accumulated from SSE stream since page load
- [ ] **FE-CHART-03**: Portfolio heatmap (treemap) with positions sized by weight and colored by P&L (green=profit, red=loss)
- [ ] **FE-CHART-04**: P&L line chart showing total portfolio value over time from snapshot data

### Frontend — Trading

- [ ] **FE-TRADE-01**: Trade bar with ticker field, quantity field, buy button, and sell button
- [ ] **FE-TRADE-02**: Trade execution is instant — no confirmation dialog
- [ ] **FE-TRADE-03**: Trade errors displayed inline with clear feedback
- [ ] **FE-TRADE-04**: Positions table shows all holdings with ticker, qty, avg cost, current price, unrealized P&L, % change

### Frontend — Chat

- [ ] **FE-CHAT-01**: AI chat panel as docked/collapsible sidebar
- [ ] **FE-CHAT-02**: Message input with send functionality
- [ ] **FE-CHAT-03**: Scrolling conversation history
- [ ] **FE-CHAT-04**: Loading indicator while waiting for LLM response
- [ ] **FE-CHAT-05**: Trade executions and watchlist changes shown inline as action confirmations

### Frontend — Real-time

- [ ] **FE-RT-01**: SSE connection to /api/stream/prices using native EventSource API
- [ ] **FE-RT-02**: Automatic reconnection on disconnect
- [ ] **FE-RT-03**: Connection status reflected in header indicator (green/yellow/red)

### Packaging

- [ ] **PKG-01**: Multi-stage Dockerfile (Node 20 slim builds frontend, Python 3.12 slim runs backend)
- [ ] **PKG-02**: Docker container serves everything on port 8000
- [ ] **PKG-03**: SQLite database persists via Docker named volume
- [ ] **PKG-04**: docker-compose.yml for convenience
- [ ] **PKG-05**: Start/stop scripts for macOS/Linux (bash)
- [ ] **PKG-06**: Start/stop scripts for Windows (PowerShell)

### Testing

- [ ] **TEST-01**: E2E tests with Playwright covering: fresh start, watchlist CRUD, buy/sell trades, portfolio updates, AI chat (mocked)
- [ ] **TEST-02**: E2E tests run against Docker container with LLM_MOCK=true
- [ ] **TEST-03**: docker-compose.test.yml for test infrastructure

## v2 Requirements

Deferred to future release. Tracked but not in current roadmap.

### Enhanced Visualization

- **VIZ-01**: Candlestick/OHLCV charts with multiple timeframes
- **VIZ-02**: Technical indicators (RSI, MACD, Bollinger Bands)

### Social & Discovery

- **DISC-01**: Real-time news feed integration
- **DISC-02**: Sector/industry classification for tickers

### Deployment

- **DEPLOY-01**: Terraform configuration for AWS App Runner
- **DEPLOY-02**: CI/CD pipeline with automated testing

## Out of Scope

| Feature | Reason |
|---------|--------|
| User authentication / multi-user | Single-user demo with fake money — auth adds massive complexity for zero value |
| Limit/stop orders | Requires order book, partial fills, order lifecycle — triples portfolio complexity |
| Real brokerage integration | Simulated-only by design for course capstone |
| Token-by-token chat streaming | Cerebras is fast enough; structured outputs must complete before trade auto-execution |
| Mobile-first responsive design | Data-dense terminal aesthetic requires wide screen; functional on tablet is sufficient |
| OAuth / social login | No auth at all — single hardcoded user |
| Real-time WebSocket chat | SSE is one-way push; chat is request/response, not streaming |
| Persistent chat across sessions | Recent messages loaded for context; fresh feel each session |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| DB-01 | Phase 1 | Complete |
| DB-02 | Phase 1 | Complete |
| DB-03 | Phase 1 | Complete |
| DB-04 | Phase 1 | Complete |
| PORT-01 | Phase 2 | Complete |
| PORT-02 | Phase 2 | Complete |
| PORT-03 | Phase 2 | Complete |
| PORT-04 | Phase 2 | Complete |
| PORT-05 | Phase 2 | Complete |
| PORT-06 | Phase 2 | Complete |
| PORT-07 | Phase 2 | Complete |
| PORT-08 | Phase 2 | Complete |
| WATCH-01 | Phase 3 | Complete |
| WATCH-02 | Phase 3 | Complete |
| WATCH-03 | Phase 3 | Complete |
| WATCH-04 | Phase 3 | Complete |
| CHAT-01 | Phase 5 | Pending |
| CHAT-02 | Phase 5 | Pending |
| CHAT-03 | Phase 5 | Pending |
| CHAT-04 | Phase 5 | Pending |
| CHAT-05 | Phase 5 | Pending |
| CHAT-06 | Phase 5 | Pending |
| CHAT-07 | Phase 5 | Pending |
| CHAT-08 | Phase 5 | Pending |
| APP-01 | Phase 4 | Complete |
| APP-02 | Phase 4 | Complete |
| APP-03 | Phase 4 | Complete |
| APP-04 | Phase 4 | Complete |
| UI-01 | Phase 6 | Pending |
| UI-02 | Phase 6 | Pending |
| UI-03 | Phase 6 | Pending |
| UI-04 | Phase 6 | Pending |
| FE-WATCH-01 | Phase 7 | Pending |
| FE-WATCH-02 | Phase 7 | Pending |
| FE-WATCH-03 | Phase 7 | Pending |
| FE-WATCH-04 | Phase 7 | Pending |
| FE-WATCH-05 | Phase 7 | Pending |
| FE-CHART-01 | Phase 7 | Pending |
| FE-CHART-02 | Phase 7 | Pending |
| FE-CHART-03 | Phase 8 | Pending |
| FE-CHART-04 | Phase 8 | Pending |
| FE-TRADE-01 | Phase 8 | Pending |
| FE-TRADE-02 | Phase 8 | Pending |
| FE-TRADE-03 | Phase 8 | Pending |
| FE-TRADE-04 | Phase 8 | Pending |
| FE-CHAT-01 | Phase 9 | Pending |
| FE-CHAT-02 | Phase 9 | Pending |
| FE-CHAT-03 | Phase 9 | Pending |
| FE-CHAT-04 | Phase 9 | Pending |
| FE-CHAT-05 | Phase 9 | Pending |
| FE-RT-01 | Phase 6 | Pending |
| FE-RT-02 | Phase 6 | Pending |
| FE-RT-03 | Phase 6 | Pending |
| PKG-01 | Phase 10 | Pending |
| PKG-02 | Phase 10 | Pending |
| PKG-03 | Phase 10 | Pending |
| PKG-04 | Phase 10 | Pending |
| PKG-05 | Phase 10 | Pending |
| PKG-06 | Phase 10 | Pending |
| TEST-01 | Phase 10 | Pending |
| TEST-02 | Phase 10 | Pending |
| TEST-03 | Phase 10 | Pending |

**Coverage:**
- v1 requirements: 62 total
- Mapped to phases: 62
- Unmapped: 0

---
*Requirements defined: 2026-02-11*
*Last updated: 2026-02-11 after Phase 2 & 3 completion*
