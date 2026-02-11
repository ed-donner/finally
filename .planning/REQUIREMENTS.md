# Requirements: FinAlly

**Defined:** 2026-02-11
**Core Value:** Users see live-updating prices, trade instantly with fake money, and chat with an AI that can analyze their portfolio and execute trades — all in a single dark, data-rich terminal aesthetic.

## v1 Requirements

Requirements for initial release. Each maps to roadmap phases.

### Database

- [ ] **DB-01**: SQLite database initializes lazily on first request — creates schema and seeds data if missing
- [ ] **DB-02**: Schema includes all 6 tables: users_profile, watchlist, positions, trades, portfolio_snapshots, chat_messages
- [ ] **DB-03**: Default seed data: one user with $10,000 cash and 10 watchlist tickers (AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX)
- [ ] **DB-04**: SQLite uses WAL mode with busy_timeout for safe concurrent access

### Portfolio

- [ ] **PORT-01**: User can view current positions with ticker, quantity, avg cost, current price, unrealized P&L, and % change
- [ ] **PORT-02**: User can view cash balance and total portfolio value (cash + positions at current prices)
- [ ] **PORT-03**: User can buy shares at current market price (deducts cash, creates/updates position)
- [ ] **PORT-04**: User can sell shares at current market price (adds cash, reduces/removes position)
- [ ] **PORT-05**: Trade validation rejects buys with insufficient cash and sells with insufficient shares
- [ ] **PORT-06**: Every executed trade is recorded in trade history (append-only log)
- [ ] **PORT-07**: Portfolio value snapshots recorded every 30 seconds and immediately after each trade
- [ ] **PORT-08**: User can view portfolio value history over time (for P&L chart)

### Watchlist

- [ ] **WATCH-01**: User can view current watchlist tickers with latest prices
- [ ] **WATCH-02**: User can add a ticker to the watchlist
- [ ] **WATCH-03**: User can remove a ticker from the watchlist
- [ ] **WATCH-04**: Watchlist changes sync with market data source (added tickers start streaming, removed tickers stop)

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

- [ ] **APP-01**: FastAPI app uses lifespan for startup/shutdown of market data source, database, and background tasks
- [ ] **APP-02**: All API routes mounted under /api/* with correct path prefixes
- [ ] **APP-03**: Static Next.js export served by FastAPI for all non-API routes
- [ ] **APP-04**: Health check endpoint at GET /api/health

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
| DB-01 | — | Pending |
| DB-02 | — | Pending |
| DB-03 | — | Pending |
| DB-04 | — | Pending |
| PORT-01 | — | Pending |
| PORT-02 | — | Pending |
| PORT-03 | — | Pending |
| PORT-04 | — | Pending |
| PORT-05 | — | Pending |
| PORT-06 | — | Pending |
| PORT-07 | — | Pending |
| PORT-08 | — | Pending |
| WATCH-01 | — | Pending |
| WATCH-02 | — | Pending |
| WATCH-03 | — | Pending |
| WATCH-04 | — | Pending |
| CHAT-01 | — | Pending |
| CHAT-02 | — | Pending |
| CHAT-03 | — | Pending |
| CHAT-04 | — | Pending |
| CHAT-05 | — | Pending |
| CHAT-06 | — | Pending |
| CHAT-07 | — | Pending |
| CHAT-08 | — | Pending |
| APP-01 | — | Pending |
| APP-02 | — | Pending |
| APP-03 | — | Pending |
| APP-04 | — | Pending |
| UI-01 | — | Pending |
| UI-02 | — | Pending |
| UI-03 | — | Pending |
| UI-04 | — | Pending |
| FE-WATCH-01 | — | Pending |
| FE-WATCH-02 | — | Pending |
| FE-WATCH-03 | — | Pending |
| FE-WATCH-04 | — | Pending |
| FE-WATCH-05 | — | Pending |
| FE-CHART-01 | — | Pending |
| FE-CHART-02 | — | Pending |
| FE-CHART-03 | — | Pending |
| FE-CHART-04 | — | Pending |
| FE-TRADE-01 | — | Pending |
| FE-TRADE-02 | — | Pending |
| FE-TRADE-03 | — | Pending |
| FE-TRADE-04 | — | Pending |
| FE-CHAT-01 | — | Pending |
| FE-CHAT-02 | — | Pending |
| FE-CHAT-03 | — | Pending |
| FE-CHAT-04 | — | Pending |
| FE-CHAT-05 | — | Pending |
| FE-RT-01 | — | Pending |
| FE-RT-02 | — | Pending |
| FE-RT-03 | — | Pending |
| PKG-01 | — | Pending |
| PKG-02 | — | Pending |
| PKG-03 | — | Pending |
| PKG-04 | — | Pending |
| PKG-05 | — | Pending |
| PKG-06 | — | Pending |
| TEST-01 | — | Pending |
| TEST-02 | — | Pending |
| TEST-03 | — | Pending |

**Coverage:**
- v1 requirements: 56 total
- Mapped to phases: 0
- Unmapped: 56

---
*Requirements defined: 2026-02-11*
*Last updated: 2026-02-11 after initial definition*
