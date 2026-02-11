# Project Research Summary

**Project:** FinAlly — AI Trading Workstation
**Domain:** Real-time financial terminal with LLM chat assistant
**Researched:** 2026-02-11
**Confidence:** HIGH

## Executive Summary

FinAlly is a Bloomberg-inspired trading terminal for a simulated portfolio, distinguished by conversational AI that can analyze holdings and auto-execute trades. Research shows this type of product requires real-time price streaming (Server-Sent Events), canvas-based financial charting, async database access for portfolio state, and LLM structured outputs for reliable action execution. The market data subsystem is already complete with a GBM simulator, SSE streaming infrastructure, and Massive API integration.

The recommended stack balances modern capabilities with operational simplicity: FastAPI serves a static Next.js export from a single Docker container, SQLite provides zero-config persistence suitable for single-user simulated portfolios, and LiteLLM bridges to OpenRouter for LLM chat. The frontend uses Lightweight Charts for streaming price data and Recharts for portfolio visualizations. This architecture enables one-command deployment while demonstrating production patterns (async Python, structured LLM outputs, real-time web updates).

The critical technical risk is SQLite concurrency under async operations. Without proper configuration (WAL mode, shared connection, immediate transactions), concurrent writes from trade execution, chat logging, and background snapshot tasks will produce "database is locked" errors. Secondary risks include LiteLLM's OpenRouter structured output detection (requires workarounds) and chart memory leaks from improper React lifecycle management. All identified pitfalls have validated prevention strategies documented in research.

## Key Findings

### Recommended Stack

The stack is optimized for single-container deployment with no authentication, serving a static frontend and streaming real-time data to a single user. The market data layer (FastAPI backend, PriceCache, SSE streaming) is already production-ready with 73 passing tests.

**Core technologies:**
- **aiosqlite >=0.22.0**: Async SQLite access via raw SQL (no ORM) — FastAPI's async runtime requires non-blocking database calls; aiosqlite bridges to stdlib sqlite3 without ORM complexity for a simple 6-table schema
- **litellm >=1.78.0**: Unified LLM API gateway — abstracts OpenRouter with OpenAI-compatible interface, handles structured output passthrough and provider routing for gpt-oss-120b via Cerebras inference
- **Next.js 16.x with static export**: React framework producing an SPA served by FastAPI — eliminates CORS, runs in single container, uses Turbopack for 5x faster builds
- **Lightweight Charts 5.1.0**: Canvas-based financial charting — purpose-built for streaming price data, handles 10,000+ points smoothly, official React integration pattern via useRef
- **Recharts 3.7.0**: React charting for treemap and line charts — declarative SVG charts for non-streaming portfolio visualizations (heatmap colored by P&L, portfolio value over time)
- **Tailwind CSS 4.x**: Utility-first styling — v4 uses CSS-first config (no JS config needed), 5x faster builds, perfect for dark terminal aesthetic

Full version matrix, alternatives analysis, and Docker build patterns documented in STACK.md.

### Expected Features

Research identifies 11 table stakes features, 7 competitive differentiators, and 9 deliberate anti-features (complexity without proportional value). Feature dependencies show SSE price streaming (complete) feeds all visualizations, database schema enables trade execution, and both systems combine to power LLM chat with auto-execution.

**Must have (table stakes):**
- Live price watchlist with flash animations — core expectation from any trading platform
- Buy/sell trade execution — instant market orders with validation (cash check, share check)
- Positions table with live P&L — ticker, quantity, avg cost, current price, unrealized P&L
- Cash balance and total portfolio value — always visible, updating live with price ticks
- Selected ticker chart — click watchlist ticker to view price chart in main area
- Dark terminal theme — Bloomberg/TradingView aesthetic, data-dense layout

**Should have (competitive advantages):**
- **AI chat assistant with auto-execution** — the headline feature; LLM can analyze portfolio and execute trades via structured output without confirmation dialogs
- **Portfolio heatmap (treemap)** — positions sized by weight, colored by P&L; visually striking and immediately communicates portfolio composition
- **P&L chart over time** — background task snapshots portfolio value every 30s; line chart shows consequence of decisions
- **Inline trade confirmations in chat** — AI-executed trades render as structured cards, making agentic behavior visible and tangible
- **AI-driven watchlist management** — natural language CRUD ("add Tesla", "track energy stocks")

**Defer to v2+:**
- Sparkline mini-charts — polish for watchlist density
- Trade history view — simple append-only log, low priority
- E2E Playwright tests — important for quality but not blocking demo
- Cloud deployment (Terraform) — stretch goal per PLAN.md

Full feature prioritization matrix, competitor analysis, and MVP definition in FEATURES.md.

### Architecture Approach

Single-container architecture with FastAPI serving both static frontend files and API/SSE endpoints. Market data subsystem (complete) writes to PriceCache, SSE router streams to browser, portfolio/watchlist/chat modules (new) handle state mutations via SQLite. All components wire through FastAPI lifespan context manager for clean startup/shutdown. Database layer uses connection-per-app-instance pattern (SQLite is single-writer; connection pooling adds no benefit for single-user).

**Major components:**
1. **Database Layer (app/db/)** — aiosqlite connection management, lazy schema initialization, seed data; all SQL isolated in this module
2. **Portfolio Service (app/portfolio/)** — trade execution with validation, P&L calculation, portfolio valuation; business logic separate from REST API
3. **Watchlist Router (app/watchlist/)** — REST CRUD for watchlist; propagates changes to MarketDataSource to start/stop ticker streaming
4. **Chat Service (app/chat/)** — constructs system prompt with portfolio context, calls LiteLLM for structured output, auto-executes trades and watchlist changes
5. **Snapshot Background Task (app/snapshots.py)** — records portfolio value every 30s for P&L chart
6. **Next.js Frontend (frontend/src/)** — Zustand stores for SSE-fed price data and REST-fetched portfolio state; components subscribe to fine-grained state slices

Key patterns: FastAPI lifespan for resource management, Zustand stores with SSE feeding, LLM structured output with auto-execution, static Next.js export served by FastAPI. Full component responsibilities, data flow diagrams, and project structure in ARCHITECTURE.md.

### Critical Pitfalls

Seven critical pitfalls identified with validated prevention strategies. Most are configuration/pattern issues, not fundamental technology problems. Recovery cost ranges from LOW (single line change) to MEDIUM (requires refactoring if not caught early).

1. **SQLite "database is locked" under concurrent async writes** — Default SQLite configuration fails with multiple concurrent writes (trade + snapshot + chat). Prevention: enable WAL mode, set busy_timeout, use single shared connection, `BEGIN IMMEDIATE` transactions. Address in database layer phase.

2. **LiteLLM + OpenRouter structured output detection failure** — LiteLLM's `supports_response_schema` returns False for OpenRouter models, silently stripping `response_format`. Prevention: pass via `extra_body` to bypass provider checks, validate actual JSON response. Address in LLM integration phase.

3. **SSE connection buffered by reverse proxy/ASGI layer** — Price updates arrive in batches instead of real-time inside Docker. Prevention: use `sse-starlette` library, set `X-Accel-Buffering: no`, send heartbeat comments. Verify in Docker deployment phase.

4. **Next.js static export breaks with API routes or server features** — Adding API routes or middleware causes `next build` to fail or silently degrade. Prevention: never create `app/api/` in Next.js, all API logic in FastAPI, test static export early. Address in frontend scaffolding phase.

5. **Chart component memory leaks from improper React lifecycle** — Lightweight Charts instances not destroyed between re-renders; memory grows continuously. Prevention: store chart ref (not state), return cleanup function with `chart.remove()`, use `series.update()` not `setData()`. Address in frontend chart integration phase.

6. **Docker multi-stage build produces broken Python venv** — Venv paths baked at build time don't match runtime paths. Prevention: `ENV UV_LINK_MODE=copy`, consistent WORKDIR across stages, dependency install before code copy. Address in Docker deployment phase.

7. **LLM auto-execution without validation corrupts portfolio** — Structured output ensures JSON shape but not business logic (valid quantities, sufficient cash). Prevention: apply same validation to AI-initiated and manual trades, cap maximum trade size, log AI-originated trades. Address in LLM integration phase.

Full pitfall details, warning signs, phase mapping, and recovery strategies in PITFALLS.md.

## Implications for Roadmap

Research reveals clear dependency ordering: database layer first (all stateful features depend on it), portfolio and watchlist second (LLM chat depends on them), LLM integration third (needs trade execution and portfolio context), frontend fifth (can overlap once API contracts exist), Docker/integration last.

### Phase 1: Database Foundation
**Rationale:** All new features require persistence (portfolio state, watchlist, chat history, snapshots). No other new component can be built or tested without database access. Pitfall research shows SQLite concurrency must be configured correctly from the start or errors propagate through every feature.

**Delivers:**
- aiosqlite connection management with WAL mode and busy timeout
- Lazy schema initialization (CREATE TABLE IF NOT EXISTS)
- Seed data (default user, 10-ticker watchlist)
- All 6 tables: users_profile, watchlist, positions, trades, portfolio_snapshots, chat_messages

**Addresses:** Table stakes requirement for stateful portfolio tracking

**Avoids:** SQLite "database is locked" pitfall (Pitfall #1)

**Research flag:** Standard pattern — async SQLite with aiosqlite is well-documented. Skip additional research.

### Phase 2: Portfolio & Trade Execution
**Rationale:** Trade execution is the core user interaction and a prerequisite for LLM chat (the LLM cannot execute trades until this exists). Portfolio valuation logic combines database positions with PriceCache prices, establishing the integration pattern all other features follow.

**Delivers:**
- Trade execution service with validation (cash/share checks, price from PriceCache)
- Portfolio valuation (total value, positions with unrealized P&L)
- REST API: GET /api/portfolio, POST /api/portfolio/trade, GET /api/portfolio/history

**Addresses:**
- Must-have: buy/sell trade execution, positions table, total portfolio value
- Differentiator: foundation for AI auto-execution

**Avoids:** LLM auto-execution without validation (Pitfall #7) — validation built into single trade execution path

**Uses:** aiosqlite (Phase 1), PriceCache (existing), FastAPI (existing)

**Research flag:** Standard pattern — REST APIs with FastAPI, SQLite CRUD. Skip research.

### Phase 3: Watchlist Management
**Rationale:** Can be built in parallel with or immediately after portfolio service (no dependency). Establishes the pattern for modifying MarketDataSource (add/remove tickers) which the LLM integration will reuse. Smaller scope than portfolio, good for validating database + REST patterns.

**Delivers:**
- REST API: GET /api/watchlist, POST /api/watchlist, DELETE /api/watchlist/{ticker}
- Dynamic ticker management (propagate changes to MarketDataSource)

**Addresses:**
- Must-have: user can manage watchlist
- Differentiator: foundation for AI watchlist management

**Uses:** aiosqlite (Phase 1), MarketDataSource (existing)

**Research flag:** Standard CRUD pattern. Skip research.

### Phase 4: App Assembly & Lifespan
**Rationale:** Portfolio and watchlist services now exist; wire them into the FastAPI application via lifespan context manager. This creates the running backend server that frontend can develop against. Load initial watchlist from database and start MarketDataSource. Start portfolio snapshot background task.

**Delivers:**
- FastAPI lifespan with resource initialization
- All API routers mounted with `/api` prefix
- app.state wiring for shared resources (db, cache, source)
- Background task for portfolio snapshots (every 30s)

**Addresses:** Must-have requirements now accessible via HTTP endpoints

**Avoids:** Global mutable state anti-pattern (Pitfall context)

**Uses:** All Phase 1-3 components

**Research flag:** Standard FastAPI pattern. Skip research.

### Phase 5: LLM Chat Integration
**Rationale:** Depends on portfolio service (needs context and trade execution) and watchlist (needs CRUD). LLM integration is complex and has known pitfalls (structured output detection, validation), so isolate it after simpler features are stable. Mock mode should be implemented first for testing without API keys.

**Delivers:**
- LLM service with system prompt construction
- LiteLLM integration with structured output via extra_body workaround
- Auto-execution of trades and watchlist changes
- Chat history persistence
- REST API: POST /api/chat
- Mock LLM mode for testing

**Addresses:**
- Differentiator: AI chat assistant with auto-execution (headline feature)
- Differentiator: inline trade confirmations, AI portfolio analysis

**Avoids:**
- LiteLLM structured output detection failure (Pitfall #2)
- LLM validation issues (Pitfall #7 — reuses Phase 2 validation)

**Uses:** Portfolio service (Phase 2), Watchlist (Phase 3), aiosqlite (Phase 1), LiteLLM (new)

**Research flag:** Needs validation — LiteLLM + OpenRouter structured output integration has known issues and workarounds. Quick verification during phase planning to confirm extra_body approach works with current versions. LOW research depth (specific integration check, not broad domain research).

### Phase 6: Frontend Foundation
**Rationale:** Can start earlier once API contracts are defined (Phase 4), but isolated here for clarity. Must configure static export and Tailwind from the start. Build core layout and dark theme before implementing features. SSE connection and Zustand stores are foundational patterns all components will use.

**Delivers:**
- Next.js 16 project with `output: 'export'` verified
- Tailwind CSS 4 dark theme
- Layout: Header, Watchlist panel, Chart area, Portfolio area, Chat panel
- useSSE hook connecting to /api/stream/prices
- Zustand stores (price, portfolio, chat)

**Addresses:**
- Must-have: dark terminal aesthetic
- Foundation for all interactive features

**Avoids:** Next.js static export breaks (Pitfall #4) — configure and test export before building features

**Uses:** Next.js 16, Tailwind 4, Zustand (new stack)

**Research flag:** Standard Next.js + React patterns. Skip research.

### Phase 7: Watchlist & Price Display
**Rationale:** Depends on frontend foundation (Phase 6). Uses SSE data flowing into Zustand store. Establishes price flash animation pattern. This is the first thing users see, so important for making the app feel "alive."

**Delivers:**
- Watchlist component (ticker grid with symbol, price, change %)
- Price flash animation (green/red fade on uptick/downtick)
- Connection status indicator (EventSource.readyState)
- Watchlist add/remove UI

**Addresses:**
- Must-have: live price watchlist, price flash animations, connection status
- Table stakes: watchlist management

**Avoids:** Price flash on initial render (UX pitfall)

**Uses:** useSSE hook, priceStore (Phase 6)

**Research flag:** Standard React patterns. Skip research.

### Phase 8: Chart Integration
**Rationale:** Depends on Watchlist (Phase 7) for ticker selection. Lightweight Charts has specific React integration patterns (useRef/useEffect). Memory leak pitfall is well-documented; establish correct lifecycle pattern now.

**Delivers:**
- Main chart component using Lightweight Charts
- Click ticker in watchlist → render chart for selected ticker
- Accumulate price data from SSE into time-series
- Chart instance lifecycle (create, update, destroy)

**Addresses:** Must-have: selected ticker chart

**Avoids:** Chart memory leaks (Pitfall #5) — cleanup function with chart.remove()

**Uses:** Lightweight Charts 5.1.0, priceStore (Phase 6)

**Research flag:** Standard pattern with known pitfall. Skip research (pitfall doc has solution).

### Phase 9: Portfolio Visualizations
**Rationale:** Depends on portfolio API (Phase 2) and frontend foundation (Phase 6). Recharts for treemap and P&L chart. Demonstrates portfolio state fetched via REST (not SSE).

**Delivers:**
- Positions table (ticker, qty, avg cost, price, P&L, % change)
- Portfolio treemap (sized by weight, colored by P&L)
- P&L chart (portfolio value over time from snapshots)
- Cash balance and total value in header

**Addresses:**
- Must-have: positions table, cash balance, total portfolio value
- Differentiator: portfolio heatmap

**Uses:** Recharts 3.7.0, portfolioStore (Phase 6), GET /api/portfolio, GET /api/portfolio/history

**Research flag:** Standard Recharts patterns. Skip research.

### Phase 10: Trade Execution UI
**Rationale:** Depends on portfolio API (Phase 2) and portfolio visualizations (Phase 9) for feedback. Simple form UI calling existing trade endpoint. Error handling validates UI logic.

**Delivers:**
- TradeBar component (ticker input, quantity input, buy/sell buttons)
- Error display for validation failures
- Re-fetch portfolio after successful trade

**Addresses:** Must-have: buy/sell trade execution

**Avoids:** Silent trade failures (responsive error handling requirement)

**Uses:** POST /api/portfolio/trade, portfolioStore

**Research flag:** Standard form handling. Skip research.

### Phase 11: AI Chat Interface
**Rationale:** Depends on chat API (Phase 5) and portfolio visualizations (Phase 9) for rendering trade confirmations. Last major feature, depends on most other systems.

**Delivers:**
- Chat component (message input, history, loading state)
- Inline action cards (trade confirmations, watchlist changes)
- Message rendering (user vs assistant)

**Addresses:**
- Differentiator: AI chat assistant, inline trade confirmations
- Differentiator: AI portfolio analysis, AI watchlist management

**Uses:** POST /api/chat, chatStore (Phase 6)

**Research flag:** Standard React chat UI. Skip research.

### Phase 12: Docker & Integration
**Rationale:** Requires completed frontend (Phase 11) and backend (Phase 5). Multi-stage build with known pitfalls. Validate SSE works through Docker networking.

**Delivers:**
- Multi-stage Dockerfile (Node → Python)
- Static Next.js export served by FastAPI
- SQLite volume mount
- Start/stop scripts (macOS/Linux + Windows)

**Addresses:** Single-container deployment requirement

**Avoids:**
- Docker venv path mismatch (Pitfall #6)
- SSE buffering in Docker (Pitfall #3)

**Uses:** All backend + frontend components

**Research flag:** Standard Docker patterns with known pitfalls. Skip research.

### Phase Ordering Rationale

- **Database first (Phase 1)** because every stateful feature depends on it; SQLite concurrency must be configured correctly before any concurrent operations exist
- **Portfolio before LLM (Phase 2 before 5)** because LLM chat needs portfolio context and trade execution capability
- **Watchlist in parallel with Portfolio (Phase 3)** because they're independent; watchlist is simpler and validates database + REST patterns
- **App assembly after services exist (Phase 4)** because it wires components together; skeleton can be started earlier but full lifespan needs all services
- **LLM after portfolio + watchlist (Phase 5)** because it calls both services; structured output integration needs verification but is isolated
- **Frontend foundation before features (Phase 6)** because static export config and dark theme must be correct from the start
- **Chart integration before portfolio viz (Phase 8 before 9)** because chart lifecycle pattern is complex and has known pitfalls; establish pattern early
- **Docker last (Phase 12)** because it needs both working frontend build and stable backend

### Research Flags

Phases needing validation during planning:
- **Phase 5 (LLM Chat Integration):** Verify LiteLLM + OpenRouter structured output with extra_body workaround works with current versions. Quick verification check, not deep research. Context7 already has LiteLLM docs; just need to test the specific integration.

Phases with standard patterns (skip research):
- **Phase 1 (Database):** aiosqlite is well-documented; pattern from architecture research is sufficient
- **Phase 2 (Portfolio):** Standard REST API + SQLite CRUD
- **Phase 3 (Watchlist):** Standard REST API + SQLite CRUD
- **Phase 4 (App Assembly):** FastAPI lifespan is standard and documented
- **Phase 6 (Frontend Foundation):** Next.js static export is standard
- **Phase 7-11 (Frontend features):** Standard React + charting library patterns
- **Phase 12 (Docker):** Multi-stage builds are standard; pitfalls are known

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | All technologies verified via Context7 (Next.js, Lightweight Charts, LiteLLM, FastAPI) or official docs (aiosqlite, Recharts). Version compatibility matrix complete. Market data subsystem already in production. |
| Features | HIGH | Feature landscape validated against competitor analysis (Bloomberg, TradingView, Robinhood, Composer). MVP definition clear. Anti-features justified (limit orders, auth, streaming tokens add complexity without demo value). |
| Architecture | HIGH | Component boundaries align with PLAN.md. Patterns verified (FastAPI lifespan, Zustand, static export). Data flow matches single-container constraint. Build order derived from clear dependency graph. |
| Pitfalls | HIGH | All seven critical pitfalls have primary sources (GitHub issues, official docs, deep-dive articles). Prevention strategies validated. Phase mapping complete. Recovery costs assessed. |

**Overall confidence:** HIGH

All major technology decisions have official documentation or Context7-verified sources. The one MEDIUM confidence area (LiteLLM + OpenRouter structured output workaround) has a documented solution path and is flagged for quick verification in Phase 5 planning.

### Gaps to Address

No significant research gaps remain. Two areas to validate during implementation:

- **LiteLLM OpenRouter integration (Phase 5):** Confirm the `extra_body` workaround for structured outputs works with LiteLLM >=1.78.0 and OpenRouter's current API. This is a tactical integration check, not a research gap. Fallback: call OpenRouter directly via httpx if LiteLLM proves problematic.

- **SSE through Docker networking (Phase 12):** Verify price stream arrives real-time (not buffered) when accessed through Docker port mapping. If buffering occurs, apply documented mitigations (X-Accel-Buffering header, heartbeat comments). Known issue with known solutions.

Both gaps are "validate during implementation" rather than "research before planning." Proceed with roadmap creation.

## Sources

### Primary (HIGH confidence)
- Context7: /vercel/next.js/v16.1.5 — static export configuration, App Router patterns
- Context7: /tradingview/lightweight-charts — React integration, series API, performance patterns
- Context7: /websites/litellm_ai — structured outputs, OpenRouter provider, response_format
- Context7: /websites/tailwindcss — v4 installation, CSS-first config, PostCSS setup
- Context7: /recharts/recharts/v3.3.0 — Treemap component, custom content rendering
- Context7: /pydantic/pydantic — model_json_schema for structured output schemas
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/) — lifespan context manager pattern
- [SQLite WAL Mode](https://sqlite.org/wal.html) — official concurrency documentation
- [aiosqlite GitHub](https://github.com/omnilib/aiosqlite) — async SQLite library
- [Next.js Static Exports](https://nextjs.org/docs/app/guides/static-exports) — official static export guide
- [uv Docker Integration](https://docs.astral.sh/uv/guides/integration/docker/) — official uv container patterns

### Secondary (MEDIUM confidence)
- [SQLite Concurrent Writes](https://tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/) — deep-dive on locking issues
- [aiosqlite Issue #251](https://github.com/omnilib/aiosqlite/issues/251) — database locked reproduction and solutions
- [LiteLLM Issue #10465](https://github.com/BerriAI/litellm/issues/10465) — OpenRouter structured output detection
- [OpenRouter Structured Outputs](https://openrouter.ai/docs/guides/features/structured-outputs) — json_schema + require_parameters
- [Production Python Docker with uv](https://hynek.me/articles/docker-uv/) — Hynek Schlawack's Docker patterns
- [sse-starlette](https://github.com/sysid/sse-starlette) — SSE library for FastAPI

All secondary sources cross-referenced with official documentation where available.

---
*Research completed: 2026-02-11*
*Ready for roadmap: yes*
