# Codebase Concerns

**Analysis Date:** 2026-02-11

## Critical Missing Components (Blocking MVP)

**Full Backend Application Not Started:**
- Issue: Backend has only the market data subsystem. Missing: main FastAPI app, database schema/initialization, portfolio management, trade execution, watchlist endpoints, chat integration, all API routes listed in PLAN.md
- Files: Entire `backend/app/` missing main app.py or equivalent FastAPI application bootstrap
- Impact: Cannot run the application at all. Backend cannot serve frontend or respond to any API requests. This blocks all subsequent work on portfolio, trading, chat, and database features
- Fix approach: Create FastAPI main application in `backend/app/main.py` that initializes the app, sets up routers (market streaming, portfolio, watchlist, chat, health), configures middleware, and manages application lifecycle

**Frontend Empty:**
- Issue: `frontend/` directory is completely empty. No Next.js project initialized
- Files: `frontend/` (no files)
- Impact: No UI exists. User cannot interact with the trading workstation at all. All frontend components (watchlist, chart, portfolio heatmap, chat panel, trade bar) must be built from scratch
- Fix approach: Initialize Next.js project with TypeScript in `frontend/`, set up project structure, configure static export build

**Database Layer Not Implemented:**
- Issue: No SQLite schema definitions, no database connection management, no initialization logic. Plan specifies schema in section 7 with 6 tables (users_profile, watchlist, positions, trades, portfolio_snapshots, chat_messages) but none exist
- Files: No `backend/db/` schema files, no migrations or initialization
- Impact: Cannot persist user data, trades, positions, chat history, or portfolio snapshots. All data is lost on restart
- Fix approach: Create `backend/db/schema.sql` with all 6 table definitions including constraints and indexes, create initialization logic in backend that runs migrations on startup

**Portfolio & Trade Endpoints Missing:**
- Issue: Plan requires POST `/api/portfolio/trade`, GET `/api/portfolio`, GET `/api/portfolio/history` but no implementation exists
- Files: No portfolio management module
- Impact: Users cannot buy/sell shares, view positions, or see P&L. Core trading functionality unavailable
- Fix approach: Create `backend/app/portfolio/` module with trade execution logic, portfolio valuation, position tracking, using database

**Watchlist Endpoints Missing:**
- Issue: Plan requires GET/POST/DELETE `/api/watchlist/*` endpoints but not implemented
- Files: No watchlist management code
- Impact: Users cannot manage watched tickers. Cannot add or remove tickers from watchlist
- Fix approach: Create `backend/app/watchlist/` module with CRUD operations for watchlist

**Chat Integration Missing:**
- Issue: Plan requires POST `/api/chat` with LLM integration via LiteLLM → OpenRouter, structured output parsing, and trade auto-execution. Zero implementation
- Files: No chat module, no LLM integration
- Impact: No AI assistant. Users cannot ask questions, get portfolio analysis, or execute trades via natural language
- Fix approach: Create `backend/app/chat/` module with LLM client, structured output schema, trade/watchlist execution logic

**Docker Configuration Missing:**
- Issue: No Dockerfile, no docker-compose.yml, no start/stop scripts. Plan specifies multi-stage Docker build and volume mount strategy
- Files: Missing `Dockerfile`, `docker-compose.yml`, `scripts/start_*.sh`, `scripts/stop_*.sh`
- Impact: Cannot containerize or deploy application. Users cannot run the app as intended
- Fix approach: Create Dockerfile with Node build stage (frontend) + Python stage (backend), add docker-compose.yml, implement start/stop scripts for macOS/Linux and Windows

---

## Test Coverage Gaps

**Massive API Client Not Tested for Real Failures:**
- What's not tested: Error scenarios — invalid API keys (401), rate limits (429), malformed responses, network timeouts, missing fields in snapshot data
- Files: `backend/tests/market/test_massive.py`
- Risk: Massive poller fails silently in production and never recovers. No visibility into which specific Massive API errors occurred. Tests mock the client; no integration tests with real API
- Priority: High
- Fix approach: Add parametrized tests for common HTTP errors, add timeout/retry mechanism testing, add field validation tests

**SSE Stream Endpoint Not Tested:**
- What's not tested: Client disconnection handling, reconnection scenarios, long-running connections, concurrent clients, error propagation on cache failures
- Files: `backend/app/market/stream.py` has no tests
- Risk: SSE stream may hang on client disconnect, may not send events on rapid price changes, may accumulate stale connections
- Priority: High
- Fix approach: Add async tests in `backend/tests/market/test_stream.py` for disconnection detection, concurrent client simulation, rapid update handling

**No Database-Layer Tests:**
- What's not tested: Schema validation, constraint enforcement, transaction handling, concurrent access, data consistency across portfolio operations
- Files: No database test module exists
- Risk: Database corruption, lost trades, inconsistent portfolio state, race conditions on concurrent trade execution
- Priority: Critical
- Fix approach: Create comprehensive SQLite tests once database schema exists

**No Integration Tests for Trade Execution:**
- What's not tested: Trade validation (sufficient cash, sufficient shares), P&L calculations, position updates, trade history recording, cash balance updates
- Files: No portfolio test module exists
- Risk: Incorrect portfolio state after trading, cash balance not decremented, positions not created, P&L miscalculated
- Priority: Critical

---

## Performance & Scaling Concerns

**Cholesky Matrix Rebuild on Every Ticker Add/Remove:**
- Problem: `GBMSimulator._rebuild_cholesky()` is O(n²) and called every time a ticker is added or removed. With large watchlists, this becomes slow
- Files: `backend/app/market/simulator.py` lines 154-172
- Cause: Rebuilding entire correlation matrix instead of updating incrementally
- Current impact: Negligible for ≤50 tickers. Becomes noticeable >100 tickers
- Improvement path: Implement incremental Cholesky rank-1 update. Research Sherman-Morrison formula for Cholesky updates to avoid full rebuild

**SSE Sends All Prices on Every Update, Regardless of Change:**
- Problem: `_generate_events()` sends complete price snapshot (all tickers) every 500ms regardless of which ticker(s) actually changed
- Files: `backend/app/market/stream.py` lines 76-83
- Cause: Version-based detection means any single ticker change triggers full snapshot send
- Current impact: Bandwidth waste and client CPU churn processing unchanged prices. With 100+ tickers, ~2KB per 500ms = 32KB/min per client
- Improvement path: Send delta updates (only changed tickers) instead of full snapshots. Requires client-side change detection refactoring

**Single Massive API Call for All Tickers:**
- Problem: Polling fetches snapshots for ALL tickers in a single API call. If watchlist grows large (100+ tickers), timeout risk increases
- Files: `backend/app/market/massive_client.py` line 125
- Cause: `get_snapshot_all()` loads all tickers at once
- Current impact: With 10 tickers and 15s poll interval (free tier), acceptable. With 500+ tickers, timeouts likely
- Improvement path: Batch ticker requests into groups, parallel polling, or implement pagination if Massive API supports it

**Price Cache Lock Contention:**
- Problem: `PriceCache` uses a single `Lock()` protecting all operations. Under high update frequency (500ms interval × 100 tickers = 200 updates/sec), lock contention may occur
- Files: `backend/app/market/cache.py` lines 6, 29, 46, 52, 61, 62, 74
- Cause: Coarse-grained locking on entire price dict
- Current impact: Negligible with simulator (single thread writing). May become issue with Massive API polling + multiple concurrent SSE readers + future multi-user portfolio reads
- Improvement path: Use fine-grained locks per ticker, or switch to RwLock if library available. Or use async-aware locking instead of threading.Lock

**No Database Query Optimization:**
- Problem: When database layer is built, portfolio calculations (total value, P&L) will likely query trades and positions without indexes
- Files: TBD (not yet implemented)
- Risk: O(n) scans on every portfolio request. With 1000+ trades/positions, slow response times
- Improvement path: Add database indexes on (user_id, ticker), materialized portfolio snapshots, or denormalized position cache

---

## Known Bugs & Edge Cases

**Simulator Prices Can Become Extremely Stale if Not Stepped:**
- Symptoms: If simulator loop fails or lags, SSE clients receive stale prices. No freshness check or heartbeat
- Files: `backend/app/market/simulator.py` lines 260-270
- Trigger: Unhandled exception in `_run_loop()` breaks the task; exception is logged but loop exits silently
- Current impact: If exception occurs (e.g., numpy crash), simulator stops but client continues reading stale cache
- Workaround: Task cancellation detection in `stop()` is present, but task exception is not monitored
- Fix: Wrap simulator task in monitoring; detect task failure and auto-restart or notify frontend

**Massive API Timestamps Require Millisecond → Second Conversion, Not Validated:**
- Symptoms: If Massive API changes response format or provides missing `last_trade.timestamp`, code crashes with AttributeError that's caught but logged as a skip
- Files: `backend/app/market/massive_client.py` lines 100-115
- Trigger: Malformed Massive snapshot response
- Current impact: Skipped ticker on that poll cycle; cache not updated. Not fatal but silent data loss
- Workaround: Try/except catches and logs; user unaware price is stale
- Fix: Validate timestamp format, provide fallback current timestamp, log as warning not debug

**Price Change Percent Calculation Divides by Zero Without Guard:**
- Symptoms: If previous_price is 0.0, change_percent returns 0.0 silently
- Files: `backend/app/market/models.py` lines 24-28
- Trigger: Manual price update with previous_price=0 (or first update with price=0)
- Current impact: Misleading change_percent value (should be infinity or undefined). Low risk as prices start from seeds
- Workaround: Guard is present (lines 26-27)
- Fix: Already has guard; no action needed

**SSE Client Disconnect Detection Uses `request.is_disconnected()`, May Lag:**
- Symptoms: Disconnected clients may not be detected immediately; SSE generator continues sending events for up to 500ms after actual disconnect
- Files: `backend/app/market/stream.py` line 71
- Trigger: Hard network disconnect (no FIN/RST)
- Current impact: Wasted CPU and network bandwidth. Browser's reconnect may fail if too much buffered data. Not critical for small client count
- Improvement path: More aggressive disconnect polling (check every 100ms instead of waiting for next event), or use socket options

---

## Security Considerations

**No Authentication or Authorization:**
- Risk: Any HTTP client can execute trades, view portfolio, manage watchlist. No multi-user isolation
- Files: All API endpoints (TBD - not implemented yet)
- Current mitigation: Single-user hardcoded default user. PLAN.md explicitly states this is intentional for MVP
- Recommendations: For production, implement JWT auth, per-user session tokens, RBAC. For now, acceptable for demo

**Environment Variables Not Validated:**
- Risk: Invalid `MASSIVE_API_KEY` or missing `OPENROUTER_API_KEY` silently fails. No startup validation
- Files: `backend/app/market/factory.py` line 24 (no validation), future `backend/app/chat/` module
- Current mitigation: Errors logged when API calls fail
- Recommendations: Add startup checks: `if os.environ.get("OPENROUTER_API_KEY").strip() == "": raise RuntimeError("OPENROUTER_API_KEY required")`

**LLM Trade Auto-Execution Without Confirmation:**
- Risk: LLM can execute any trade without user approval. If LLM is tricked/jailbroken, could liquidate entire portfolio
- Files: TBD (chat module not yet implemented)
- Current mitigation: Intentional design choice (Plan section 9 explains reasoning: demo environment, zero financial stakes, shows agentic capabilities)
- Recommendations: In production, require user confirmation modal or implement trade limits (max % of portfolio per request)

**No Rate Limiting on API Endpoints:**
- Risk: Malicious client can spam requests. No protection against brute-force or DoS
- Files: All endpoints (TBD - not yet implemented)
- Current mitigation: None
- Recommendations: Add rate limiting middleware (e.g., slowapi) with per-IP limits: 100 req/min for public endpoints, 10 req/min for trades/chat

**Secrets in .env May Be Committed Accidentally:**
- Risk: `.env` contains `OPENROUTER_API_KEY` and `MASSIVE_API_KEY`. If developer accidentally commits, keys are exposed
- Files: `.env` (gitignored, but check .gitignore)
- Current mitigation: `.env` is in `.gitignore`
- Recommendations: Verify `.env` never committed; use pre-commit hook to prevent; document `.env.example` with placeholder values

---

## Fragile Areas

**GBM Simulator Correlation Matrix:**
- Files: `backend/app/market/simulator.py` lines 154-197
- Why fragile: Cholesky decomposition can fail if correlation matrix is not positive semi-definite. Manual correlation assignments (hardcoded constants in `_pairwise_correlation()`) must be kept consistent with correlation definition rules
- Safe modification: Any change to correlation constants (INTRA_TECH_CORR, INTRA_FINANCE_CORR, etc.) must maintain mathematical validity. Always test with stress tests: add 50+ random tickers, run 10k steps, verify prices stay positive
- Test coverage: Existing tests check correlation math but not edge cases (e.g., what if all correlations = 1.0? What if ticker added to multiple groups?)
- Risk if modified: Cholesky decomposition fails → simulator crashes with linalg error

**Tick/Price Rounding:**
- Files: `backend/app/market/simulator.py` line 116, `backend/app/market/cache.py` line 36
- Why fragile: Prices are rounded to 2 decimals in two places independently. If rounding logic diverges, front-end displays price different from what trade uses
- Safe modification: Use a centralized rounding function in `backend/app/market/models.py` (e.g., `def round_price(p: float) -> float: return round(p, 2)`). Import everywhere
- Test coverage: Tests verify 2-decimal rounding but not consistency across multiple round trips
- Risk: User sees "AAPL $190.50" but trade fills at $190.51

**Simulator Event Probability Tuning:**
- Files: `backend/app/market/simulator.py` lines 54, 105-108, 151-152
- Why fragile: Event probability (default 0.001) produces dramatic moves (2-5% shocks). If changed without care, can produce unrealistic price jumps or freeze market
- Safe modification: Any change to event_probability or shock_magnitude needs re-calibration. Test with 1000+ steps and verify realized volatility matches expectations
- Test coverage: One test explicitly sets probability to 1.0 to force events, but no statistical validation
- Risk: Changing probability to 0.1 would produce 100× more events → market unrecognizable

---

## Missing Critical Features (Blocking User Experience)

**No Health Check Endpoint:**
- Problem: Plan specifies GET `/api/health` for Docker/deployment, not implemented
- Files: TBD (missing)
- Impact: Docker containers cannot be health-checked. Kubernetes liveness probes fail
- Fix: Add simple GET endpoint that returns `{"status": "ok"}`

**No Graceful Shutdown:**
- Problem: FastAPI app doesn't call `await market_data_source.stop()` on shutdown. Background tasks may not clean up properly
- Files: TBD (not yet implemented)
- Impact: Lingering async tasks on container stop. Potential data corruption if database transaction isn't flushed
- Fix: Register FastAPI lifespan context manager that calls stop on exit

**No Logging Configuration:**
- Problem: Logging uses module-level `logger = logging.getLogger(__name__)` but root logging not configured. No central log level, format, or output
- Files: Multiple files in `backend/app/market/`
- Impact: Developers cannot control log verbosity at runtime (e.g., for debugging)
- Fix: Add logging.config in main app with JSON/structured logging configuration

**No Error Response Standardization:**
- Problem: API hasn't been built yet, but Plan doesn't specify error response format. Responses will be inconsistent without upfront design
- Files: TBD (all endpoints)
- Impact: Frontend error handling code cannot assume response structure
- Fix: Define shared error schema: `{"error": "description", "code": "ERROR_CODE", "details": {}}`

---

## Dependencies at Risk

**numpy Dependency for Cholesky:**
- Risk: Simulator heavily depends on numpy for matrix operations and random numbers. If numpy is removed or incompatible, simulator breaks
- Impact: Cannot generate correlated price updates
- Migration plan: Could reimplement Cholesky in pure Python (tedious but possible). Or switch to scipy (heavier). For now, numpy is essential
- Priority: Low (numpy is stable, widely used)

**massive Library Version Lock:**
- Risk: `massive>=1.0.0` is not pinned. uv.lock file locks exact version, but if lock is deleted/regenerated, may pull incompatible version
- Impact: Massive API client may fail with version mismatch
- Migration plan: Keep uv.lock in git; pin to exact version in pyproject.toml if Massive API evolves rapidly
- Priority: Medium (should monitor releases)

**litellm Dependency Not Yet Added:**
- Risk: Plan requires LiteLLM for LLM integration. Not in pyproject.toml yet. When added, check version stability
- Impact: Chat module cannot be built without LiteLLM
- Priority: High (blocking chat feature)

---

## Technical Debt Summary

| Area | Severity | Impact | Effort to Fix |
|------|----------|--------|---------------|
| No FastAPI main app | Critical | App cannot run | 4-6 hours |
| Frontend empty | Critical | No UI | 40+ hours |
| Database not implemented | Critical | No persistence | 8-10 hours |
| Missing portfolio/trade endpoints | Critical | Cannot trade | 6-8 hours |
| Missing chat integration | High | No LLM assistant | 8-10 hours |
| No Docker setup | High | Cannot deploy | 4-6 hours |
| SSE stream not tested | High | Reliability risk | 3-4 hours |
| Massive client error handling | Medium | Silent failures | 2-3 hours |
| Price cache lock contention | Low | Scaling limit | 2 hours (if needed) |
| No health check | Low | Docker blind spot | 30 min |

---

*Concerns audit: 2026-02-11*
