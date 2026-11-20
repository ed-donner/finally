# FinAlly — Spec Review (2026-11-18, second pass)

Scope: `planning/PLAN.md`, checked against `planning/API.md` and `planning/AGENTS.md`. This pass follows the Section 13 resolutions. Most of the earlier review items are well resolved. What remains are cross-document contradictions, a few contract details that will break in the browser or at the external services, and some test oracles that cannot pass as written.

Severity levels:

- **Critical**: the feature will misbehave as specified, or an external service will reject the design. Fix before coding starts.
- **High**: likely to cause a defect, a failing test, or a security or cost exposure. Fix before the affected module is built.
- **Medium**: an ambiguity that two agents could reasonably implement differently. Decide before integration.
- **Low**: polish, wording, or example hygiene.

Each item gives its location, the problem, and a concrete suggestion.

---

## Critical

### C1. SSE keepalive comments are invisible to the browser, so the connection dot will go yellow on a healthy stream
- **Where:** PLAN §6 "Keepalive", §10 "Connection Status"; API.md "Comment: keepalive", "Event: prices" ("Omitted entirely if nothing changed").
- **Problem:** `EventSource` never surfaces comment lines (`: keepalive`) to JavaScript. No event fires, and `onmessage` is not called. The frontend therefore cannot see "a keepalive in the last 20 s". Since `prices` events are omitted when nothing changes, any quiet period over 20 s turns the dot yellow while the connection is healthy. Quiet periods include Massive mode between polls, outside market hours, and on weekends. The rule is fine in simulator mode only because prices change every tick.
- **Suggestion:** Replace the comment with a named event, `event: heartbeat` and `data: {"ts": "...", "source_status": "ok"}`, sent every 5–10 s whether or not prices changed. The 20 s freshness rule then works. Update API.md, PLAN §6 and §10, and the frontend indicator test.

### C2. Massive upstream failure has no channel to reach the connection indicator
- **Where:** PLAN §6 Massive "On repeated failure, the connection indicator (Section 10) shows yellow"; §10 table; API.md SSE section.
- **Problem:** The indicator is defined only from `EventSource` state and message freshness. While Massive is returning 429 or 5xx, the SSE connection stays healthy, and with C1 fixed the heartbeats keep arriving. Nothing in the wire contract tells the frontend that the upstream is degraded, so this requirement cannot be implemented.
- **Suggestion:** Add `source_status: "ok" | "degraded"` to the heartbeat event from C1, and optionally to `snapshot`. Define "repeated failure", for example 3 consecutive failed polls. Add a row to the §10 table: open stream with `source_status = degraded` shows yellow. Alternatively, drop the requirement from §6.

### C3. The Massive free-tier call budget is exceeded at startup, and the snapshot endpoint may not be on the free tier
- **Where:** PLAN §5 (`MASSIVE_POLL_INTERVAL_SECONDS`, "Free tier allows 5 calls/min"), §6 Massive "Baseline call", §8 "Ticker existence"; §13.5 S1.
- **Problem:**
  1. At startup the client makes one `/v2/aggs/ticker/{symbol}/prev` call per cached ticker. With the default 10 tickers that is 10 calls, plus a snapshot poll every 15 s. The 5 calls/min limit is exceeded on the first boot. Each `POST /api/watchlist` adds another prev-close call, and the LLM can add 10 tickers in one turn.
  2. As far as I know, Polygon's free "Basic" plan did not include the snapshot endpoints, and free data was end-of-day. If Massive kept those plan rules, the default free-tier path returns not-authorized on every poll. I have not verified this against current Massive docs.
  3. As far as I know, Polygon's `/prev` returned `200` with zero results for unknown symbols, not `404`. The rule "not-found → `UnknownTicker`" may never fire. This is also unverified.
- **Suggestion:** Extend spike S1 to cover (a) which plan tiers include `/v2/snapshot/.../tickers`, (b) the `/prev` response for an unknown symbol, and (c) the current base URL (`api.massive.com` vs `api.polygon.io`). Put a client-side token-bucket rate limiter in the Massive adapter, sized from a `MASSIVE_CALLS_PER_MINUTE` constant, and queue prev-close lookups through it. If the snapshot payload includes `prevDay.c` (S1 already asks this), take `prev_close` from it and drop the per-ticker call. That leaves one call shape, which is consistent with §13.4 #5. Have `add_ticker` validate "unknown" from the snapshot result, or from `/v3/reference/tickers/{t}`, under the same budget. If the free tier cannot serve snapshots, say so in `.env.example` and §5.

---

## High

### H1. Random-jump rate swamps GBM volatility, and the GBM and correlation oracles fail unless jumps are disabled
- **Where:** PLAN §6 "Random events" (p = 1/120 per tick, 2–5%), "Time scale"; §12 GBM oracle and correlation test.
- **Problem:** With jump size J ~ U[2%, 5%], E[J²] ≈ 1.3e-3, so the jump variance per tick is ≈ 1.08e-5. The GBM variance per tick is σ²Δt ≈ 0.0625 × 5.09e-6 ≈ 3.2e-7. Jumps contribute about 34 times more variance than the diffusion, and the effective volatility is about 150% annualized instead of the stated 25%. In wall-clock terms, each ticker drifts about ±19% per real hour from jumps alone. With 10 tickers, some ticker jumps 2–5% roughly every 6 s. The §12 oracles ("variance within 2% of σ²Δt", "correlation within 0.02 of β_iβ_j") cannot pass with jumps enabled.
- **Suggestion:** (a) Lower the rate to about 1/1200 per tick (≈ once per 10 min per ticker, or once a minute somewhere on a 10-ticker screen), or narrow the jump to 1–3%. (b) Make jumps a separate, injectable component (`jump_prob` parameter, default from code). State that the GBM and correlation tests run with `jump_prob = 0`, and that the event test runs with diffusion off (σ = 0) so jumps can be identified exactly. (c) Define the jump as multiplicative, `S' = S·(1 ± J)`, applied after the diffusion step.

### H2. The GBM drift test cannot detect a wrong drift
- **Where:** PLAN §12 "GBM oracle".
- **Problem:** With μ = 5%, σ = 25%, and Δt ≈ 5.09e-6, the expected mean log-return is (μ − σ²/2)·Δt ≈ 9.5e-8. Over 100,000 steps the standard error is σ√Δt / √n ≈ 1.8e-6, about 19 times the expected mean. The test passes for μ = 0, μ = −50%, or a missing `−σ²/2` term.
- **Suggestion:** Test drift and diffusion separately. Drift: σ = 0, so the log-return is exactly (μ)·Δt and can be compared to 1e-12. Diffusion: keep the variance test. Itô term: run with a large Δt (for example 1/252) and large σ, so the `−σ²/2` term exceeds the SE. Alternatively, test the step function directly with a fixed `z`.

### H3. Lightweight Charts uses whole-second timestamps, but the stream produces two points per second
- **Where:** PLAN §10 "Charts" (Lightweight Charts for the main chart), §6 history buffer; API.md `history` in epoch milliseconds, 500 ms cadence.
- **Problem:** Lightweight Charts' `UTCTimestamp` is in seconds, and series data must be strictly ascending with no duplicate times. If ms is converted to seconds and floored, two ticks share a second and `setData`/`update` throws. Fractional seconds are not a documented feature. This will break the main chart on first load.
- **Suggestion:** Choose one approach and write it in §10: (a) the main chart buckets to 1 point per second (last price wins) before calling the library, or (b) the chart uses a synthetic increasing index as time with a custom tick formatter. Add a frontend unit test that feeds 600 points at 500 ms spacing. The same applies to the P&L chart if live points are appended faster than 1 per second.

### H4. The app is reachable from the LAN with no auth: anyone nearby can trade, reset, and spend the OpenRouter key
- **Where:** PLAN §11 `docker run -p 8000:8000`; §2 Non-Goals (no auth); AGENTS.md "Secrets never reach the browser".
- **Problem:** `-p 8000:8000` publishes on all interfaces. Anyone on the same network can call `POST /api/chat` (which costs money on the user's key), `POST /api/portfolio/trade`, and `POST /api/portfolio/reset`. A malicious web page can also reach `localhost:8000` through DNS rebinding.
- **Suggestion:** Publish on loopback only, `-p 127.0.0.1:8000:8000`, in the scripts and in the §11 example. Add a `TrustedHostMiddleware` allowing `localhost` and `127.0.0.1`, which defeats DNS rebinding. Make FastAPI reject non-`application/json` POST bodies (FastAPI does this by default; add a test). Add a simple in-process rate limit on `/api/chat`, for example 10 per minute, and a `429 rate_limited` error code in API.md. Write one sentence in §2 or §11: "Not designed for exposure beyond localhost."

### H5. The Docker `HEALTHCHECK` uses `curl`, which `python:3.12-slim` does not ship
- **Where:** PLAN §11 Dockerfile, Stage 2.
- **Problem:** The healthcheck command fails, the container is reported `unhealthy`, and anything that waits on `service_healthy` (for example `docker-compose.test.yml`) never starts.
- **Suggestion:** Use `HEALTHCHECK CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health').status==200 else 1)"`, or `apt-get install -y --no-install-recommends curl` in Stage 2. Add `--start-period=10s`.

### H6. The cash invariant does not match the per-trade rounding rule
- **Where:** PLAN §7 Invariant 2, §8 "Buy" (cost rounded to cents), "Rounding".
- **Problem:** Invariant 2 compares cash to `starting_cash − Σ(qty × price) + Σ(...)`. Cash is rounded to cents after every trade, so the sum of rounded costs drifts from the rounded sum of raw costs, by up to half a cent per trade. The randomized-sequence invariant test in §12 will fail intermittently. Cash stored as REAL also accumulates binary error.
- **Suggestion:** Restate the invariant as `cash = 10000.00 − Σ round(qty·price, 2) [buys] + Σ round(qty·price, 2) [sells]`, compared exactly in cents. Better still, store cash as INTEGER cents (and quantities as INTEGER ten-thousandths, since quantities are limited to 4 dp). Convert at the API boundary. This also removes the need for the 1e-6 tolerance.

### H7. The sell-side tolerance is defined for the remaining quantity but not for the sufficiency check
- **Where:** PLAN §8 "Sell", "Rounding".
- **Problem:** After buying 0.1 and 0.2, the stored quantity may be 0.30000000000000004 or 0.29999999999999998. A sell of 0.3 is rejected with `insufficient_shares` in the second case unless the check uses the same tolerance. The `0.1 + 0.2 − 0.3` test covers only the deletion path.
- **Suggestion:** Write the rule: a sell is allowed if `qty ≤ held + 1e-6`. If `held − qty ≤ 1e-6`, the position is closed. Add a test for the 0.29999999999999998 case. If H6's integer storage is adopted, this item disappears.

### H8. Held positions can have no price after a restart, and the spec does not say how valuation behaves
- **Where:** PLAN §6 cache, §7 snapshots, §8 "Snapshots"; API.md `GET /api/portfolio` (`current_price` is non-nullable in the example).
- **Problem:**
  1. In Massive mode, after a restart, the cache is empty until the first poll, which can be 15 s away or longer under rate limiting (C3). The 30 s snapshot task and `GET /api/portfolio` then have no price for held tickers. A snapshot computed with a missing price records a false crash on the P&L chart.
  2. In simulator mode, every restart resets prices to seed values, while `avg_cost` reflects prices from earlier runs. Unrealized P&L and the P&L chart jump discontinuously on each restart.
- **Suggestion:** (a) Make `current_price`, `market_value`, `unrealized_pnl`, and `weight` nullable in API.md when no price is cached, and let the frontend show "—". (b) The snapshot task skips the write if any held ticker has no price. (c) For the simulator, start each held ticker at its last trade price, or persist the last price per ticker in a small `last_prices` table. Pick one and document it in §6.

### H9. Node 20 is past end-of-life
- **Where:** PLAN §11 Stage 1 `node:20-slim`.
- **Problem:** Node 20 reached end-of-life in April 2026 and no longer receives security fixes.
- **Suggestion:** Use `node:22-slim`, or the current active LTS, and pin the minor version.

---

## Medium

### M1. Contradictions between PLAN.md and API.md that agents will implement differently
| # | PLAN.md | API.md | Suggested resolution |
|---|---|---|---|
| a | §6: `change_pct = (price − prev_close) / prev_close` (a fraction) | `× 100`, 2 dp (a percent) | Use the percent definition and fix PLAN §6. |
| b | §9: after a second failure, the fallback has `actions: null` | `POST /api/chat` returns empty `trades`/`watchlist_changes` arrays | The API returns empty arrays; the stored row has `actions = null`. State both explicitly. |
| c | §7: init runs in lifespan, before serving requests | Health: "If initialization has not finished, returns 503 `starting`" | That state is unreachable. Either drop it, or run init as a background task and gate `/api/*` on readiness. |
| d | §7: `actions` is "the same object the chat API returns, minus `message`"; §13.4 #7 says "stored verbatim" | The chat response also contains `portfolio` | Define `actions = {trades, watchlist_changes}` only. Fix §13.4 #7. |
| e | §4: "the contract wins"; AGENTS: "stop and reconcile" | API.md line 3: "fix this file first, then the code" | Use one precedence sentence, copied verbatim into all three files. |
| f | §13.1 #3: "Confirmed by spike S1" | §13.5: S1 still open | Change to "Pending spike S1". |
| g | §7: "All tables carry `user_id`" | `users_profile` uses `id`, not `user_id` | Say "all tables except `users_profile`, whose `id` is the user id". |
| h | §6 links `API.md#sse-price-stream` | The heading generates a different anchor | Add an explicit anchor, or link to the section by name. |

### M2. `PriceSource.tick()` does not fit a network-polling source, and the driver loop is unspecified
- **Where:** PLAN §6 interface.
- **Problem:** `tick()` is synchronous and returns "updates since the last call". The spec does not say who calls it or how often. For Massive, a poll is an async HTTP call. Agents will invent incompatible drivers.
- **Suggestion:** Specify a push model. `start(on_updates: Callable[[list[PriceUpdate]], None])`, where each source runs its own loop (500 ms for the simulator, the poll interval for Massive) and calls the cache. Alternatively, make `tick()` async and specify that one driver loop awaits it at the source's own cadence. Also say who maintains cache membership (watchlist ∪ positions): add/remove on watchlist changes, on closing a position in an unwatched ticker, on reset, and at startup.

### M3. API error shape vs FastAPI defaults
- **Where:** API.md "Every non-2xx response... uses this shape".
- **Problem:** FastAPI's defaults return `422` with `{"detail": [...]}` for body validation, and `{"detail": "Not Found"}` for unknown routes and 405s. All of these violate the contract unless they are overridden.
- **Suggestion:** Add a line to API.md, and a test: "Custom handlers map `RequestValidationError` → `400 invalid_request`, `HTTPException` 404/405 → the error shape, and unhandled exceptions → `500 internal_error`. Unknown `/api/*` paths return JSON `404 not_found`, never `index.html`."

### M4. LLM upstream failures have no mapped status
- **Where:** PLAN §9 "Validation and Failure Handling"; API.md chat errors.
- **Problem:** Only a missing key (`503`) and malformed output are defined. Timeouts, `401` (bad key), `402` (no credits), and `429` or `5xx` from OpenRouter are not. There is no timeout either.
- **Suggestion:** Set an LLM call timeout (for example 30 s). Add `502 llm_upstream_error`, with `details.upstream_status`, for any transport or provider error. State whether the user message is stored when the LLM call fails; I recommend storing it before the call so the history matches what the user saw. Make sure the trade lock is never held during the LLM call.

### M5. Strict `json_schema` and "optional" fields
- **Where:** PLAN §9 "Structured Output Schema" (`trades` and `watchlist_changes` optional, max 10, message 1–2000).
- **Problem:** Strict structured-output modes (OpenAI-style, and Cerebras' implementation) generally require every property to be listed as required, with `additionalProperties: false`. They also support only some validation keywords; `maxItems` and `minLength`/`maxLength` may be unsupported or ignored. A schema with optional properties and those bounds may be rejected outright.
- **Suggestion:** In the JSON schema sent to the model, make all three fields required (arrays may be empty), set `additionalProperties: false`, and omit length and count bounds. Enforce the bounds in Pydantic only. Add these points to spike S2. Also note that LiteLLM passes the OpenRouter provider pin through `extra_body={"provider": {...}}`, and that `gpt-oss-120b` is a reasoning model: set low reasoning effort for latency.

### M6. Mock LLM grammar is underspecified and cannot exercise sells
- **Where:** PLAN §9 "LLM Mock Mode"; §12 E2E chat scenarios.
- **Problem:** The pattern `"buy <TICKER> <N>"` is not anchored, and its case-sensitivity and handling of multiple commands are not stated. There is no `sell` command. The API.md example ("Buy 5 NVDA") uses the opposite word order, so a tester copying it gets no trade.
- **Suggestion:** Give exact regexes, matched case-insensitively and all applied in order: `\b(buy|sell)\s+([A-Za-z.]{1,7})\s+(\d+(\.\d{1,4})?)\b` and `\b(add|remove)\s+([A-Za-z.]{1,7})\b`. Give the exact mock reply text so E2E can assert on it.

### M7. E2E determinism and isolation
- **Where:** PLAN §12 E2E; §5 ("simulator parameters are constants, not env vars"); §6 `SIM_SEED`.
- **Problem:** `SIM_SEED` is "overridable in tests", but inside the container it is a code constant, so E2E cannot set it. Even with a seed, wall-clock timing makes price paths nondeterministic, so "heatmap renders with correct colors" depends on luck. A shared DB across tests leaks state, and the "Fresh start: $10,000" scenario needs a clean DB.
- **Suggestion:** (a) Allow `SIM_SEED` and `SIM_JUMP_PROB` as test-only env vars, documented in §5 under a "Testing" heading. (b) Assert heatmap colors relative to each position's displayed P&L sign, not absolute values. (c) In `docker-compose.test.yml`, use a `tmpfs` for `/app/db` (no bind mount), and call `POST /api/portfolio/reset` in a `beforeEach`. (d) Make the connection-indicator thresholds (20 s / 30 s) constants the frontend can lower in tests. (e) State how "drop the connection" is done: restart the app container, or `context.setOffline(true)`.

### M8. `EventSource` in the `CLOSED` state does not retry
- **Where:** PLAN §10 "Connection Status"; API.md "Reconnection is automatic".
- **Problem:** `EventSource` stops retrying permanently if a reconnect gets a non-200 response or the wrong content type. During a container restart, for example, a `503` or a static `index.html` fallback could reach it. The dot then stays red forever.
- **Suggestion:** Add a client rule: on `readyState === CLOSED`, recreate the `EventSource` with backoff (1 s, 2 s, 5 s, capped at 10 s). Add a row to the indicator table for `CLOSED`.

### M9. Start-script `--build` is ignored when a stopped container exists
- **Where:** PLAN §11 Start/Stop steps 1–3.
- **Problem:** Step 2, `docker start finally`, reuses the old image and the old `--env-file` values, even if `--build` was passed or `.env` changed.
- **Suggestion:** Write the order: if `--build` is passed, or the image ID differs from the container's image, remove the container and recreate it. Note that `.env` changes require a stop and start, since stop removes the container. Also wait for `/api/health` to return 200 (with a timeout) before opening the browser.

### M10. Local development paths and the dev server are undefined
- **Where:** PLAN §5 (`load_dotenv` for local dev), §7 (`db/finally.db`), §10 (same-origin API).
- **Problem:** A relative `db/finally.db` resolves against the working directory, so `cd backend && uv run ...` creates `backend/db/finally.db`. `next dev` on port 3000 cannot reach `/api` on the same origin, and Next.js rewrites are not supported with `output: 'export'`.
- **Suggestion:** Resolve the DB path from a `FINALLY_DB_PATH` env var, defaulting to `<project_root>/db/finally.db`, computed from the package location rather than the cwd. For frontend development, choose one: (a) develop against the built export served by FastAPI, or (b) set `NEXT_PUBLIC_API_BASE` in dev only (not a secret), with CORS for `localhost:3000` enabled only when an explicit dev flag is set. Also specify the container layout: `WORKDIR /app`, backend contents in `/app`, `CMD ["uv","run","uvicorn",...]` or the venv on `PATH`.

### M11. Blocking SQLite calls on the single event loop
- **Where:** PLAN §3 (one worker), §8 Concurrency.
- **Problem:** With one worker, synchronous `sqlite3` calls in `async def` handlers block the loop and stall every SSE stream. The 30 s snapshot task also reads cash and positions outside the trade lock, so it can read a half-applied state.
- **Suggestion:** Use `aiosqlite`, or run DB work through `run_in_threadpool` behind a single connection. Have the snapshot task take the trade lock, or read inside one transaction.

### M12. No schema-versioning path for a persistent bind-mounted DB
- **Where:** PLAN §7 "Creates any missing tables".
- **Problem:** The DB persists across rebuilds. When agents change a column during development, `CREATE TABLE IF NOT EXISTS` silently keeps the old shape.
- **Suggestion:** Store `PRAGMA user_version`. On mismatch, during development, fail fast with a clear message ("delete `db/finally.db` or use Reset"). Run the seed in one transaction so a crash mid-seed cannot leave a partial seed.

### M13. History buffer semantics are undefined
- **Where:** PLAN §6 (600-point history); API.md `history`.
- **Problem:** Is a point appended every tick, or only on change? In Massive mode, 600 points cover about 2.5 hours; in simulator mode, they cover 5 minutes. The main chart's time span therefore differs by mode, and the "price over time" chart covers only 5 minutes in the default mode.
- **Suggestion:** Append on every source update (each simulator tick, each Massive poll), and say so. State the resulting windows (5 min and 2.5 h) so the frontend picks sensible axis formats. If 5 minutes is too short for a demo, downsample older points (for example, keep 1 per 5 s beyond the first 2 minutes) rather than enlarging the snapshot payload.

### M14. Root-level files have no owning agent
- **Where:** AGENTS.md "Each agent owns one directory".
- **Problem:** `Dockerfile`, `.dockerignore`, `.env.example`, `docker-compose.yml`, `scripts/`, and `db/.gitkeep` are not owned by anyone. "Backend and market-data agents own `backend/`" also puts two agents in one tree.
- **Suggestion:** Assign root files and `scripts/` to a named integration/devops role. Split `backend/` ownership by module: market-data owns `app/market/`, and backend owns everything else.

### M15. Clients are not told when membership changes
- **Where:** API.md SSE "Client rules"; PLAN §8 Reset.
- **Problem:** `snapshot` is sent only on connect, and `prices` carries only changes. After a reset, a chat-driven watchlist change, or closing an unwatched position, connected clients keep stale tickers. They have no signal to refetch.
- **Suggestion:** Broadcast a fresh `snapshot` event to all clients whenever cache membership changes. The rule that clients replace their state on every snapshot is already in place. Also state that after any `POST /api/chat` response, the frontend refetches `/api/portfolio` and `/api/watchlist`.

---

## Low

1. **Example consistency (API.md).** Agents copy examples into fixtures, so the examples should agree with each other. The snapshot `change_pct` of `0.7` should be `0.69` (191.42 vs 190.10). `total_value: 10124.55` appears in unrelated responses. In the chat example, cash after a 655.10 buy is 9345.90, which implies cash above $10,000 beforehand. Make every example derivable from one story starting at the seed state. Replace `"...":{}` with a comment outside the JSON.
2. **"Fractional-share *prices*"** in §2 Non-Goals is confusing next to fractional quantities. Reword to "sub-cent pricing rules or tick-size enforcement", or delete it.
3. **Positions table "% change"** (§2, §10) is ambiguous. Map it explicitly to `unrealized_pnl_pct` (vs average cost), not the daily change.
4. **`weight` excludes nothing.** It is `market_value / total_value` including cash, so treemap weights do not sum to 1. State that the treemap normalizes by the sum of position values.
5. **Green and red reserved for P&L** (§2) conflicts with using Gain green for the "connected" dot and red for "disconnected". Either allow it explicitly for status, or use other tokens.
6. **Accessibility.** Flashes and the heatmap rely on color alone. Add ▲/▼ glyphs or a sign on change values for color-blind users.
7. **Watchlist size cap.** There is no maximum, and the LLM can add 10 tickers per turn. Add a cap (for example 50) with `409 watchlist_full`. This also bounds snapshot payload size and Massive call budgets.
8. **`GET /api/portfolio/history` volume.** Seven days at 30 s is about 20k points, roughly 1 MB of JSON. Add `?max_points=` with server-side downsampling, or default `since` to 24 h.
9. **Chat history in the prompt.** Say how stored `actions` are rendered into the 20-message history (for example, a compact "Executed: BUY 5 NVDA @131.02; REJECTED: ...") so rejection feedback reaches the model, as §9 intends.
10. **LLM message length.** A model reply over 2000 characters currently counts as malformed and triggers a retry. Truncating with an ellipsis is cheaper and friendlier.
11. **`prev_close` "once per day".** Name the day boundary (US/Eastern midnight, or the next market open).
12. **Price flash re-trigger.** At a 500 ms tick and 500 ms fade, consecutive same-direction ticks will not visibly re-flash unless the class is removed and re-added, for example with a key or a forced reflow. Note the technique in §10.
13. **Linux bind-mount ownership.** If the container runs as root, `db/finally.db` is root-owned on Linux hosts. If it runs as non-root, the host directory may not be writable. Decide which, and document it in §11.
14. **`.dockerignore`.** Also exclude `frontend/.next`, `frontend/out`, `**/__pycache__`, `.venv`, and `test/`.
15. **`trades.source` column.** Optional: record `manual` or `chat`. It costs nothing, helps debugging, and lets the UI label AI trades.
16. **Reset during an in-flight chat.** A chat that started before a reset can execute its trades after the reset. Acceptable for a demo; one sentence in §8 saying so prevents a bug report.
17. **AGENTS.md skill references.** Confirm that the `cerebras-inference` and `dataviz` skills exist in this environment. If they do not, say what agents should do instead.
18. **Section 13 longevity.** The resolution tables duplicate normative text and have already drifted (M1 f, M1 d). Consider marking Section 13 as historical and non-normative, so the body sections are the only source of truth.

---

## Suggested order of work

1. Fix C1 and C2 (the heartbeat event with `source_status`) in API.md, then PLAN §6 and §10.
2. Expand spike S1 (C3) and adopt the rate limiter and single-call design before writing the Massive adapter.
3. Re-tune jumps and split the oracles (H1, H2) before writing simulator tests.
4. Resolve the M1 contradiction table in one spec-only commit.
5. Apply the H4 and H5 Docker and script fixes along with M9 and M10.
6. Decide the money and quantity representation (H6, H7) before the portfolio module is written.
