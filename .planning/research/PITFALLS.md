# Pitfalls Research

**Domain:** AI trading workstation (FastAPI + SQLite + LLM structured outputs + Next.js static export + SSE + Docker)
**Researched:** 2026-02-11
**Confidence:** HIGH (verified via Context7, official docs, and multiple community sources)

## Critical Pitfalls

### Pitfall 1: SQLite "Database is Locked" Under Concurrent Async Writes

**What goes wrong:**
FastAPI serves requests concurrently via asyncio. When multiple endpoints (trade execution, portfolio snapshots, watchlist changes, chat message logging) write to SQLite simultaneously through separate connections, SQLite returns `SQLITE_BUSY` / "database is locked" errors. The app appears to work in manual testing but breaks under any realistic concurrent usage (e.g., a trade fires while the 30-second portfolio snapshot task runs).

**Why it happens:**
SQLite allows only one writer at a time. The default journal mode (rollback) blocks readers during writes. Worse, the default `BEGIN DEFERRED` transaction mode creates "upgrade deadlocks" where two readers both try to promote to writers, and the busy_timeout is **not respected** for these upgrade deadlocks — the error fires instantly regardless of timeout.

**How to avoid:**
1. Enable WAL mode on every connection: `PRAGMA journal_mode=WAL;`
2. Set a generous busy timeout: `PRAGMA busy_timeout=5000;`
3. Use `BEGIN IMMEDIATE` for all write transactions (avoids upgrade deadlocks)
4. Use a single shared connection or a serialized connection pool (one write at a time) — not one connection per request
5. Keep write transactions as short as possible — no LLM calls or network I/O inside a transaction

**Warning signs:**
- Intermittent "database is locked" errors in logs during development
- Tests pass individually but fail when run in parallel
- Portfolio snapshot background task colliding with trade execution

**Phase to address:**
Database layer phase — this must be the first thing configured when setting up SQLite access. Every subsequent feature depends on correct concurrency handling.

---

### Pitfall 2: LiteLLM + OpenRouter Structured Output Detection Failure

**What goes wrong:**
LiteLLM's `supports_response_schema` check returns `False` for OpenRouter models because OpenRouter is not in LiteLLM's hardcoded provider list for structured output support. When this check fails, LiteLLM silently strips `response_format` from the request. The LLM then returns free-form text instead of the expected JSON schema. The application crashes trying to parse the response, or worse, silently produces garbled trade instructions.

**Why it happens:**
OpenRouter is a meta-provider routing to underlying models. LiteLLM has no programmatic way to detect per-model structured output support through OpenRouter. The `response_format` parameter gets silently dropped rather than raising an error. This was actively discussed in LiteLLM GitHub issues #10465 and #13438 as of 2025.

**How to avoid:**
1. Pass `response_format` via `extra_body` to bypass LiteLLM's schema-stripping logic:
   ```python
   response = completion(
       model="openrouter/openai/gpt-oss-120b",
       messages=messages,
       extra_body={"response_format": {"type": "json_schema", "json_schema": schema}}
   )
   ```
2. Alternatively, set `require_parameters=True` in the OpenRouter `providers` object to ensure only providers supporting `json_schema` are used
3. Always validate the response parses as the expected schema before executing actions
4. Implement a fallback: if JSON parsing fails, re-prompt once or return an error message to the user

**Warning signs:**
- LLM returns conversational text instead of JSON
- `response_format` appears in your code but the LLM ignores the schema
- Works fine when testing against OpenAI directly but breaks through OpenRouter

**Phase to address:**
LLM integration phase — validate the exact LiteLLM + OpenRouter + model combination produces structured output before building any auto-execution logic on top of it.

---

### Pitfall 3: SSE Connection Silently Buffered by Reverse Proxy or ASGI Layer

**What goes wrong:**
The SSE price stream appears to work in local development, but in Docker (or behind nginx/load balancer), price updates arrive in batches instead of real-time. The client receives 10-30 seconds of updates all at once, then nothing, then another batch. The trading terminal feels laggy and broken despite the backend working correctly.

**Why it happens:**
Multiple layers can buffer SSE responses: (a) uvicorn's response buffering, (b) Docker's internal networking, (c) any reverse proxy. Additionally, Python's default WSGI mode buffers responses. The `Content-Type: text/event-stream` header alone does not disable buffering at all layers.

**How to avoid:**
1. Use the `sse-starlette` library (EventSourceResponse) instead of raw StreamingResponse — it handles the SSE protocol correctly and sets proper headers
2. Set `X-Accel-Buffering: no` header on SSE responses (disables nginx buffering)
3. Set `Cache-Control: no-cache` and `Connection: keep-alive` headers
4. Send periodic heartbeat comments (`: heartbeat\n\n`) every 15-30 seconds to prevent proxy idle timeout disconnections
5. Ensure ASGI server (uvicorn) is used, never WSGI (gunicorn sync mode)

**Warning signs:**
- Prices update in batches during Docker testing
- SSE works on `localhost:8000` but not through Docker port mapping
- EventSource `onmessage` fires many events at once after a delay

**Phase to address:**
Market data / SSE streaming phase — the existing `stream.py` module should already handle some of this, but verify behavior inside Docker specifically.

---

### Pitfall 4: Next.js Static Export Breaks with API Routes or Dynamic Features

**What goes wrong:**
Developers add API routes, middleware, or server-side features in the Next.js app, then the `next build` with `output: 'export'` fails with cryptic errors like "API Routes cannot be used with output: export." Or the build succeeds but the app silently falls back to client-side-only rendering, losing expected functionality.

**Why it happens:**
`output: 'export'` produces a purely static HTML/JS/CSS bundle — no Node.js server runtime. Any feature requiring a server (API routes, middleware, `getServerSideProps`, dynamic route params from requests, cookies, headers) is fundamentally incompatible. The static export is designed to be served by any static file server (in this case, FastAPI).

**How to avoid:**
1. Never create files in `app/api/` or `pages/api/` in the Next.js project — all API logic lives in FastAPI
2. Use only `'use client'` components or static generation (`generateStaticParams` with hardcoded paths)
3. All data fetching happens client-side via `fetch('/api/...')` or `EventSource` after the page loads
4. Do not use `next/headers`, `next/cookies`, or any server-only imports
5. Add a pre-build lint check that verifies no API routes or server-only imports exist
6. Test the static export early — run `next build` in the very first frontend phase, not at the end

**Warning signs:**
- Build errors mentioning "export" and "API routes"
- `next build` produces warnings about unsupported features
- Pages that work in `next dev` but show blank content after static export

**Phase to address:**
Frontend scaffolding phase — configure `output: 'export'` and verify the build works before writing any components. Re-verify after each major feature addition.

---

### Pitfall 5: Chart Component Memory Leaks from Improper React Lifecycle Management

**What goes wrong:**
Each time a user clicks a ticker in the watchlist (selecting a different chart), or when SSE data triggers re-renders, the Lightweight Charts instance is recreated without destroying the previous one. After 20-30 ticker switches, the browser tab consumes 500MB+ of memory, animations stutter, and the terminal becomes unusable.

**Why it happens:**
Lightweight Charts creates a canvas-based chart attached to a DOM element. React's virtual DOM reconciliation does not automatically clean up imperative chart instances. If the chart is created in `useEffect` without a cleanup function that calls `chart.remove()`, or if the component re-renders and creates duplicate chart instances, each orphaned chart continues consuming memory and potentially running internal timers.

**How to avoid:**
1. Store chart and series references in `useRef`, not `useState` (refs don't trigger re-renders)
2. Always return a cleanup function from `useEffect` that calls `chart.remove()`
3. For real-time updates, use `series.update()` (O(1) per tick) — never `series.setData()` (O(n) full replacement)
4. Separate the chart container component (manages lifecycle) from the data-feeding logic (manages SSE subscription)
5. Use a stable `key` prop on chart wrapper components to control when React remounts vs. updates

**Warning signs:**
- Browser memory usage grows continuously as the user interacts
- Chrome DevTools shows increasing numbers of detached DOM nodes
- Chart animations become progressively slower
- Multiple chart instances visible in the same container

**Phase to address:**
Frontend chart integration phase — establish the chart component pattern (create/update/destroy lifecycle) before building any specific chart features.

---

### Pitfall 6: Docker Multi-Stage Build Produces Broken Python Virtual Environment

**What goes wrong:**
The Python virtual environment is created in the builder stage at one path (e.g., `/build/.venv`) but the application runs from a different path in the runtime stage (e.g., `/app/.venv`). Python's venv encodes absolute paths in its activation scripts, `pyvenv.cfg`, and shebang lines of installed console scripts. The app fails to start with import errors or "command not found" for entry points.

**Why it happens:**
`uv sync` creates a `.venv` with paths baked into the venv metadata. When you `COPY --from=builder /build/.venv /app/.venv` in the runtime stage, the internal paths still reference `/build/.venv`. Unlike pip, uv defaults to hardlinks (`UV_LINK_MODE`), which fail silently across Docker build stages (different filesystems).

**How to avoid:**
1. Set `ENV UV_LINK_MODE=copy` in the Dockerfile — hardlinks cannot cross Docker stage boundaries
2. Set `ENV UV_COMPILE_BYTECODE=1` for faster startup in production
3. Create the venv at the same path in both stages (e.g., always use `/app` as WORKDIR)
4. Use `--mount=type=cache,target=/root/.cache/uv` for build caching
5. Install dependencies before copying application code (layer caching optimization):
   ```dockerfile
   COPY pyproject.toml uv.lock ./
   RUN uv sync --frozen --no-dev
   COPY . .
   ```

**Warning signs:**
- Container starts but crashes with `ModuleNotFoundError`
- `uvicorn` or other CLI tools not found despite being in dependencies
- Warnings about "hardlinks not available" during build

**Phase to address:**
Docker / deployment phase — build the Dockerfile early and verify the app starts inside the container before building out features.

---

### Pitfall 7: LLM Auto-Execution Without Validation Causes Silent Data Corruption

**What goes wrong:**
The LLM returns a structured response specifying trades, but the values are nonsensical — buying 1,000,000 shares, selling a ticker the user doesn't own, or referencing a ticker that doesn't exist. Because auto-execution has no validation layer, these trades execute and corrupt the portfolio state. The user sees a negative cash balance or phantom positions.

**Why it happens:**
Even with structured output enforcement (JSON schema compliance), the LLM controls the *values* within the schema. The schema ensures correct types and field names, but cannot enforce business logic constraints (sufficient cash, valid quantity, existing position for sells). Developers trust that "structured output = correct output."

**How to avoid:**
1. Apply the same validation to LLM-initiated trades as manual trades — never bypass validation for auto-execution
2. Validate before executing: sufficient cash, valid ticker, non-negative quantity, sufficient shares for sells
3. Cap maximum trade size as a sanity check (e.g., no single trade > 50% of portfolio value)
4. Return validation errors in the chat response so the LLM can inform the user
5. Log all LLM-initiated trades with a flag marking them as AI-originated for debugging

**Warning signs:**
- Portfolio shows negative cash balance
- Positions appear for tickers not in the watchlist
- LLM executes trades the user didn't ask for

**Phase to address:**
LLM integration phase — validation must be built into the trade execution path before enabling auto-execution. The trade endpoint should be a single validated function called by both manual and LLM code paths.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Raw SQL strings instead of an ORM | Fast to write, no ORM learning curve | SQL injection risk, no type safety, hard to refactor schema | Acceptable for this project — 6 simple tables, single-user, no untrusted input reaches SQL. Use parameterized queries. |
| Single SQLite connection (no pool) | Simple, avoids concurrency bugs | Cannot handle concurrent reads efficiently | Acceptable — single-user app, WAL mode handles read concurrency. Serialized writes via a single connection avoids all locking issues. |
| No database migrations (lazy init) | No migration tool complexity | Cannot evolve schema without wiping data | Acceptable — capstone demo project. Data is ephemeral (sim prices, fake portfolio). Document schema version in the table. |
| Inline CSS animations instead of animation library | No extra dependency, simple | Harder to coordinate complex multi-element animations | Acceptable — the only animation is price flash (green/red fade), which is a single CSS transition. |
| Polling-based SSE (sleep + check cache) | Simple generator loop | Wastes CPU cycles checking unchanged data | Acceptable if interval matches data source (~500ms). Consider version-based change detection (already in PriceCache). |
| Storing chat history without summarization | Full conversation context to LLM | Token count grows, eventually exceeds context window | Acceptable for demo sessions. Add a rolling window (last 20 messages) as a safeguard. |

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| LiteLLM + OpenRouter | Passing `response_format` as a top-level param (gets silently stripped) | Use `extra_body` to bypass LiteLLM's provider-capability checks, or test that structured output actually works before shipping |
| OpenRouter + Cerebras | Assuming all models support `json_schema` structured outputs via all providers | Set `require_parameters=True` in providers config to force routing to capable providers. Enable Response Healing plugin for non-streaming requests. |
| Next.js static export + FastAPI | Using `next/image` optimization (requires a Node server) | Use standard `<img>` tags or configure a custom loader. Or skip images entirely (trading terminal has none). |
| EventSource + custom headers | Trying to pass auth headers via native EventSource API (not supported) | Not needed here (same-origin, no auth), but if needed later, use `fetch()` with ReadableStream or the `eventsource` npm polyfill package. |
| SQLite + Docker volumes | Writing the DB to a path not covered by the volume mount | Ensure the backend writes to `/app/db/finally.db` and the volume mounts to `/app/db`. Verify with `docker exec ls /app/db/` after first run. |
| FastAPI static file serving + Next.js client-side routing | FastAPI returns 404 for client-side routes (e.g., `/portfolio`) because no file exists at that path | Configure a catch-all route that serves `index.html` for any path not matching `/api/*`. This enables Next.js client-side routing. |

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| Calling `setData()` on every SSE tick | Charts visibly flicker, CPU pegged at 100%, frame drops | Use `series.update()` for single-point updates (O(1)). Reserve `setData()` for initial load only. | Immediately — 2 updates/sec with setData on 10 tickers = 20 full redraws/sec |
| Re-rendering entire watchlist on each price update | React reconciliation runs on 10+ rows every 500ms, causing jank | Memoize individual ticker rows (`React.memo`), update only changed tickers via a stable key + price comparison | Noticeable with 10+ tickers at 500ms update interval |
| Unbounded sparkline data arrays | Memory grows continuously, sparkline rendering slows as array grows | Cap sparkline arrays at a fixed window (e.g., 200 points). Shift oldest points out as new ones arrive. | After ~30 minutes (3600+ points per ticker at 500ms) |
| Portfolio snapshot writes every 30 seconds forever | SQLite file grows unboundedly, queries slow as table grows | Add a retention policy (e.g., keep 1 hour of 30s snapshots, then downsample to 5-minute intervals) | After hours of continuous running |
| SSE reconnection storm | If EventSource disconnects and reconnects rapidly, multiple concurrent SSE connections pile up on the server | Implement exponential backoff on client reconnection. Track active SSE connections server-side and limit per client. | When network is flaky or server restarts |

## Security Mistakes

| Mistake | Risk | Prevention |
|---------|------|------------|
| OpenRouter API key exposed in frontend code or Docker image | API key leaked, unauthorized usage, billing charges | Store in `.env`, pass via `--env-file` to Docker, never bake into image layers. Verify with `docker history`. |
| SQL injection via ticker symbol input | Attacker crafts ticker like `'; DROP TABLE trades; --` | Always use parameterized queries (`?` placeholders), never string interpolation for SQL. Validate ticker format (uppercase alpha, 1-5 chars). |
| Unrestricted trade quantities via API | Malicious client sends buy for 999999999 shares | Validate trade quantity bounds server-side. Cap at reasonable maximum. Validate cash sufficiency. |
| SSE endpoint without connection limits | Client opens hundreds of SSE connections, exhausting server resources | Track active connections per client. Limit to 1-2 concurrent SSE connections. Close stale connections. |

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Price flash animation fires on initial page load | Every price "flashes" green/red when the page first renders, which is confusing (no actual change occurred) | Only trigger flash animation when a price *changes* from a previously displayed value. Skip animation on first render. |
| Chat loading state without feedback | User sends a message and sees nothing for 2-5 seconds (Cerebras inference time) — they think it's broken and click again | Show a typing indicator immediately. Disable the send button while waiting. Queue duplicate messages. |
| Heatmap/treemap unreadable with 1-2 positions | Treemap with 1 rectangle is just a colored box — meaningless visualization | Show the treemap only when the user has 3+ positions. Show a simple list for fewer positions. Or always show the positions table alongside the treemap. |
| Trade confirmation happens too fast | User clicks "buy" and the trade executes instantly with no feedback — they're unsure it worked | Show a brief toast/notification confirming the trade with ticker, quantity, price, and new cash balance. Animate the positions table to highlight the changed row. |
| Sparklines empty on page load | Sparklines accumulate from SSE data since page load. For the first 30-60 seconds, they show almost nothing — the terminal looks broken | Pre-fill sparklines with a short history from the price cache (last N prices stored server-side), or clearly indicate "building..." state. |

## "Looks Done But Isn't" Checklist

- [ ] **SSE stream:** Verify it works inside Docker, not just via `uvicorn` directly — check for buffering, timeouts, proxy issues
- [ ] **Static export:** Run `next build` with `output: 'export'` and serve from FastAPI — verify client-side routing works for all pages
- [ ] **Trade execution:** Test buying, selling, selling more than owned (should fail), buying with insufficient cash (should fail), selling to zero (position should be removed)
- [ ] **LLM structured output:** Verify the *actual* OpenRouter response contains valid JSON matching the schema — not just that the code to parse it exists. Test with the real model, not mocks.
- [ ] **Price flash animation:** Verify flash only triggers on price change, not on initial render or reconnection
- [ ] **Chart cleanup:** Open DevTools Memory tab, switch tickers 20 times, verify memory is stable (no leak)
- [ ] **Docker volume:** Stop container, restart, verify portfolio and chat history persist
- [ ] **Portfolio value calculation:** Verify total value = cash + sum(position_quantity * current_price) — not stale prices, not average cost
- [ ] **Chat context window:** Send 50+ messages in a conversation, verify the LLM still responds coherently (not exceeding context limit)
- [ ] **Watchlist add/remove:** Add a ticker via chat, verify it appears in watchlist AND starts receiving SSE price updates

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| SQLite locked errors in production | LOW | Enable WAL mode + busy_timeout. Can be done without schema changes or data loss. Single PRAGMA statement. |
| LiteLLM strips structured output | LOW | Switch to `extra_body` parameter. Single line change in the LLM call. |
| Broken venv paths in Docker | MEDIUM | Fix Dockerfile WORKDIR to match between stages. Rebuild image. No code changes needed, but requires understanding the root cause. |
| Chart memory leaks | MEDIUM | Add `chart.remove()` cleanup to useEffect. Requires refactoring chart components if lifecycle was not structured correctly from the start. |
| Next.js build fails with export | LOW-MEDIUM | Remove offending server-only code. If API routes were mistakenly added, delete them and move logic to FastAPI. Straightforward but tedious if deeply integrated. |
| LLM auto-executes invalid trades | HIGH | If portfolio data is corrupted, may need to reset the database. Add validation retroactively. The fix is simple but the data corruption may be unrecoverable. |
| SSE buffering in Docker | LOW | Add appropriate headers and heartbeat. Pure configuration change. |

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| SQLite concurrency / locking | Database layer | Run 10 concurrent trade requests; verify zero lock errors |
| LiteLLM structured output stripping | LLM integration | Call OpenRouter with structured output schema; verify JSON response parses correctly |
| SSE buffering in Docker | Docker / deployment | Open SSE stream in browser via Docker port; verify sub-second price updates |
| Next.js static export incompatibility | Frontend scaffolding | Run `next build` with `output: 'export'`; verify clean build with zero warnings |
| Chart memory leaks | Frontend chart integration | Switch tickers 50 times in DevTools; verify heap size is stable |
| Docker venv path mismatch | Docker / deployment | `docker run` the image; verify `uvicorn` starts and imports succeed |
| LLM auto-execution without validation | LLM integration + trade execution | Have LLM attempt an invalid trade (buy $1M of stock with $10K cash); verify it fails gracefully |
| FastAPI catch-all for client-side routing | Frontend serving integration | Navigate directly to a client-side route URL; verify page loads (not 404) |
| Price flash on initial load | Frontend watchlist component | Refresh page; verify no flash animation on first render |
| Chat context overflow | LLM integration | Send 100 messages; verify no token limit errors |

## Sources

- [SQLite concurrent writes and "database is locked" errors](https://tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/) — MEDIUM confidence (community deep-dive, verified against SQLite official docs)
- [SQLite WAL mode official documentation](https://sqlite.org/wal.html) — HIGH confidence (official docs)
- [aiosqlite "Database is Locked" issue #251](https://github.com/omnilib/aiosqlite/issues/251) — HIGH confidence (primary source, reproducer included)
- [LiteLLM response_format for OpenRouter issue #10465](https://github.com/BerriAI/litellm/issues/10465) — HIGH confidence (primary source)
- [LiteLLM structured outputs documentation](https://docs.litellm.ai/docs/completion/json_mode) — HIGH confidence (Context7 verified)
- [OpenRouter structured outputs documentation](https://openrouter.ai/docs/guides/features/structured-outputs) — HIGH confidence (official docs)
- [Next.js static exports guide](https://nextjs.org/docs/app/guides/static-exports) — HIGH confidence (official docs)
- [Next.js API routes in static export warning](https://nextjs.org/docs/messages/api-routes-static-export) — HIGH confidence (official docs)
- [TradingView Lightweight Charts — React integration](https://tradingview.github.io/lightweight-charts/tutorials/react/advanced) — HIGH confidence (Context7 verified)
- [TradingView Lightweight Charts — series.update() for real-time data](https://tradingview.github.io/lightweight-charts/docs/5) — HIGH confidence (Context7 verified)
- [uv Docker integration guide](https://docs.astral.sh/uv/guides/integration/docker/) — HIGH confidence (official Astral docs)
- [Production-ready Python Docker containers with uv](https://hynek.me/articles/docker-uv/) — MEDIUM confidence (well-known Python community author)
- [FastAPI lifespan events documentation](https://fastapi.tiangolo.com/advanced/events) — HIGH confidence (Context7 verified)
- [sse-starlette library](https://github.com/sysid/sse-starlette) — MEDIUM confidence (community library, widely used)
- [Multi-stage builds — Python specifics](https://pythonspeed.com/articles/multi-stage-docker-python/) — MEDIUM confidence (well-known Docker/Python resource)

---
*Pitfalls research for: FinAlly AI Trading Workstation*
*Researched: 2026-02-11*
