# FinAlly — Specification Review

**Date:** 2026-08-08
**Reviewed:** `planning/PLAN.md` (also read: `README.md`, `.gitignore`, `.claude/skills/cerebras/SKILL.md`)
**Reviewer:** ng-reviewer agent

## Scope note

This is a **specification review, not a code review.** At the time of review the repository contains only
`CLAUDE.md`, `README.md`, `planning/PLAN.md`, `.gitignore`, `LICENSE`, and `.claude/` config — 8 tracked files.
`backend/`, `frontend/`, and `db/` exist but are empty; `Dockerfile`, `docker-compose.yml`, `scripts/`, and
`test/` do not exist.

Findings about runtime behavior (DB concurrency, SSE fan-out, P&L math) are therefore raised as
**spec defects that will produce those bugs** once agents implement them. All line references are to
`planning/PLAN.md` as of this date.

## Overall assessment

This is a strong, unusually well-considered spec. The rationale table (§3), the explicit "accepted
simplification" notes (line 25), and the per-entry independent action execution rule (line 308) are exactly
the kind of decisions that prevent agent drift.

The defects below cluster in three places:

1. **Inter-agent contract gaps** — exact JSON shapes are described in prose, never specified.
2. **Money/float correctness** — REAL columns plus fractional shares with no rounding or epsilon rules.
3. **Massive-mode behaviors** — several rules were written for the simulator and never re-checked against
   real 15-second polling.

---

## CRITICAL

### C1. The frontend/backend contract is prose, not schemas

**PLAN.md:259-289.** Endpoints are specified with English descriptions. Only `POST /api/portfolio/trade`
(line 270) names its response fields. `GET /api/portfolio`, `GET /api/portfolio/history`,
`GET /api/watchlist`, `GET /api/chat`, `POST /api/chat`, and `GET /api/health` have no field lists at all.

The Frontend Engineer and Backend Engineer are explicitly separate agents (lines 111-112) whose only shared
contract is this file. They will invent divergent shapes (`total_value` vs `totalValue`, `pnl` vs
`unrealized_pnl`, bare array vs `{items: [...]}`) and integration will fail late.

**Fix:** add a normative appendix to §8 with a literal JSON example for every request and response —
field names, types, nullability, and the error body. State that responses MUST match exactly.

### C2. SSE event format is ambiguous in a way that changes the frontend architecture

**PLAN.md:188** — "One SSE `event` per ticker per update — not a batched array." This can mean:

- (a) `event: price` with the ticker inside `data`, or
- (b) `event: AAPL`.

These are not interchangeable. (b) requires `EventSource.addEventListener` per symbol, re-registration
whenever the watchlist changes, and a re-subscribe path after every reconnect. (a) works with a single
`onmessage`. The field names inside `data` are also undefined, as is the timestamp encoding (epoch ms vs
ISO-8601) and the enumeration for "change direction."

**Fix:** mandate one fixed event name and give the exact payload:

```
event: price
data: {"ticker":"AAPL","price":190.12,"prev_price":190.05,"ts":1754640000000,"dir":"up"}
```

State that `ts` is epoch milliseconds UTC and `dir` ∈ `{"up","down"}`.

### C3. New and reconnecting SSE clients receive nothing until the next price *change*

**PLAN.md:189** — events are emitted only when a ticker's price changed since last emission. Combined with
line 190 (EventSource auto-reconnect) and line 163 (Massive free tier polls every 15s), a client that
connects or reconnects sees an empty grid for up to 15 seconds — and in Massive mode after hours, when real
prices never change, potentially forever.

The change-only rationale is sound; the missing piece is the initial snapshot.

**Fix:** specify that on connection open the server immediately emits one event per cached ticker as a
snapshot (optionally `event: snapshot`, so the client suppresses the flash animation for it), then switches
to change-only deltas.

### C4. Money stored as REAL floats with no epsilon rules → phantom positions and false "insufficient shares"

**PLAN.md:210, 224-225, 234-235, 241** — `cash_balance`, `quantity`, `avg_cost`, `price`, and `total_value`
are all REAL. With fractional shares (line 224) and repeated buys, "sell everything I own" will frequently
fail, or leave a residual position of ~1e-15 shares that renders in the positions table and as a zero-width
rectangle in the heatmap. Cash will drift to values like `9999.999999999998`, display as `$10,000.00`, and
still fail a `cash >= cost` check.

**Fix:** add a "Numeric precision" subsection to §7 with normative rules:

1. Compare with epsilon — a sell is valid if `qty <= held + 1e-9`, and is clamped to `held`.
2. Delete the position row when `quantity < 1e-9` after a sell.
3. Round cash and `avg_cost` to 8 decimals on write; format to 2 decimals for display.
4. Never compare floats with `==`.

### C5. Ticker case/whitespace normalization is unspecified, and `UNIQUE(user_id, ticker)` is case-sensitive

**PLAN.md:218** defines `UNIQUE (user_id, ticker)` on `watchlist`; **line 227** the same on `positions`.
SQLite's default TEXT collation is BINARY, so `aapl`, `AAPL`, and `AAPL ` are three distinct rows.

The trade bar has a free-text ticker field (line 371), the LLM emits tickers as free text (line 320), and
`DELETE /api/watchlist/{ticker}` (line 278) takes a path parameter. Users will end up with duplicate
positions in the same stock, splitting cost basis and corrupting P&L.

**Fix:** mandate normalization at the API boundary — `ticker.strip().upper()` — plus a validation regex
(`^[A-Z][A-Z0-9.\-]{0,9}$`) applied identically to manual input, LLM input, and path params. Optionally add
`COLLATE NOCASE` to the schema as defense in depth.

---

## HIGH

### H1. Positions in tickers that aren't on the watchlist have no price source

**PLAN.md:162** — the Massive poller polls "the union of all watched tickers"; line 176 drives the simulator
the same way. But nothing prevents `DELETE /api/watchlist/{ticker}` (line 278) while a position is open, and
nothing requires a traded ticker to be on the watchlist.

Result: `GET /api/portfolio` (line 269) computes total value and unrealized P&L from a price that has stopped
updating — or is absent entirely — and `portfolio_snapshots` silently records a wrong `total_value` every 30
seconds thereafter.

**Fix:** define the priced-ticker set as `watchlist ∪ {tickers with non-zero positions}` for both the
simulator and the Massive poller, and state that this union — not the watchlist — drives the background task.
Also document the DELETE-with-open-position behavior (recommended: allowed; ticker stays priced via the union).

### H2. `/api/health` will flap to 503 in Massive mode

**PLAN.md:289** — 200 requires "the market-data background task has ticked within the last few seconds." On
the free tier the poller ticks every 15 seconds (line 163), so the endpoint returns 503 most of the time. If
`docker-compose.yml` wires this as a container healthcheck (§11), the orchestrator restarts a healthy app in
a loop.

**Fix:** make the staleness threshold a function of the configured tick interval (e.g. `3 × interval + 5s`),
and split liveness (process + DB reachable) from market-data freshness — return 200 with a
`{"market_data": "stale"}` field rather than 503.

### H3. "Daily change %" has no data behind it

**PLAN.md:366** requires the watchlist to show "daily change %"; line 29 implies a "% change" column. But the
price cache holds only "latest price, previous price, and timestamp" (line 170), there is no price-history
table (line 25), and nothing stores a daily open or previous close. In simulator mode the concept doesn't
exist at all — the process may have started at 3pm.

**Fix:** either

- (a) add `session_open_price` per ticker to the price cache — first observed price after process start in
  simulator mode, Polygon's previous close in Massive mode — and define "daily change %" against it; or
- (b) redefine the column as "change since page load" and say so explicitly, in the style of line 25.

### H4. Conversation history sent to the LLM is unbounded

**PLAN.md:304** — "Loads recent conversation history from the `chat_messages` table" — with no limit.
**Line 283** — `GET /api/chat` returns "recent conversation history" with no limit or pagination either.
`chat_messages` is append-only and never pruned.

Every chat turn grows the prompt; cost and latency grow quadratically over a long demo session until the
context window is exceeded and the endpoint hard-fails.

**Fix:** specify a concrete bound in both places — e.g. "the last 20 messages, oldest-first" for the prompt,
and `?limit=` (default 50) for the endpoint. Note that the portfolio context block (line 303) is regenerated
fresh each turn rather than accumulated in history.

### H5. No guardrails on LLM-initiated actions, despite auto-execution with no confirmation

**PLAN.md:332-339** commits to auto-executing trades with no confirmation dialog. The prompt includes
user-supplied free text (line 305), so a user can trivially instruct the model to liquidate everything, and a
hallucinated response can emit `quantity: 1e9` or a hundred trade entries.

The stakes really are zero (fake money), so this is not a classic security hole — but it *is* an availability
and data-integrity hole: an unbounded `trades` array means unbounded DB writes and `portfolio_snapshots` rows
per request, and `watchlist_changes` can be used to churn the 20-ticker cap.

**Fix:** add a "Guardrails" subsection to §9:

- Validate every LLM-emitted entry against the same schema as manual input — ticker regex, `quantity` finite
  and `> 0`, `side ∈ {buy,sell}`, `action ∈ {add,remove}`.
- Cap actions per response (e.g. ≤10 trades, ≤10 watchlist changes); reject the excess with
  `failed: too many actions`.
- State explicitly that user text is untrusted data, not instructions, and that the system prompt takes
  precedence.

### H6. Public cloud deployment of an unauthenticated app that spends the owner's LLM credits

**PLAN.md:433-435** offers App Runner / Render deployment as a stretch goal. The app has "No login, no signup"
(line 15), no rate limiting anywhere in the spec, and `POST /api/chat` (line 284) makes a paid OpenRouter call
per request. A publicly reachable instance is a free API-credit faucet, and `POST /api/portfolio/trade` plus
the SSE endpoint are trivially DoS-able.

**Fix:** state in §11 that cloud deployment requires at minimum a shared-secret header or HTTP basic auth plus
per-IP rate limiting on `/api/chat`, and that the default local build is explicitly localhost-only.

### H7. Massive polling design can blow the rate limit by 20×

**PLAN.md:161-165** says "REST API polling," "polls for the union of all watched tickers," "free tier
(5 calls/min): poll every 15 seconds." Implemented as one quote request per ticker, 20 tickers (line 182) at
4 polls/min is **80 calls/min against a 5/min budget**.

Separately, line 177 requires a validation call on every `POST /api/watchlist`, competing for the same budget
and able to starve the poller.

**Fix:** mandate a single grouped-snapshot request per poll (Polygon's grouped/snapshot endpoint), not
per-ticker calls, and name the exact endpoint. Add an in-memory cache of validated symbols so repeated
add/remove cycles don't re-spend calls. Specify 429 backoff behavior.

### H8. Frontend re-render and memory cost is unbounded and unspecified

**PLAN.md:182** justifies the 20-ticker cap partly by "sparkline rendering cost," but §10 never says how to
bound it. 20 tickers × ~2 updates/sec ≈ 40 state updates/sec; a naive `useState` per event re-renders the
whole watchlist 40×/sec. Each sparkline array grows without limit "since page load" (line 25) — an hour-long
demo accumulates ~144,000 points across 20 tickers. The main chart (line 367) has the same issue.

**Fix:** specify a ring buffer of N points per sparkline (e.g. 120) and a cap for the main chart series.
Require SSE events to be batched and flushed once per animation frame into a single store update. Require
memoized row components keyed by ticker so only the changed row re-renders.

### H9. The E2E mock LLM has no defined contract

**PLAN.md:351-356** introduces `LLM_MOCK=true` "deterministic mock responses" but never says what they are.
Yet **line 468** requires an E2E test asserting "trade execution appears inline" — only possible if the test
agent knows exactly which input produces a mock response containing a trade. Backend and test agents will
guess differently.

**Fix:** specify the mock's rules in §9, e.g.:

- input containing `buy N TICKER` →
  `{"message":"Bought N TICKER.","trades":[{"ticker":"TICKER","side":"buy","quantity":N}]}`
- input containing `watch TICKER` → a `watchlist_changes` add
- anything else → a fixed canned message with empty arrays

---

## MEDIUM

### M1. SQLite concurrency is never addressed

§7 never mentions WAL mode, `busy_timeout`, `check_same_thread=False`, or connection lifecycle. At least four
concurrent writers/readers are implied: request handlers, the 30-second snapshot task (line 238), the
post-trade snapshot write (line 238), and LLM auto-execution (line 308). With the default journal mode the
snapshot task will intermittently throw `database is locked` during a trade.

Worse: if agents use the synchronous `sqlite3` module inside `async def` handlers, **every DB call blocks the
event loop** — which also stalls every open SSE generator.

**Fix:** add a "Concurrency" subsection — `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` at init;
all DB access in a threadpool (`def` handlers, `asyncio.to_thread`, or `aiosqlite`); writes serialized behind
a single connection or lock.

### M2. Trade execution is a read-modify-write race on `cash_balance`

A trade reads `users_profile.cash_balance`, validates, updates it, upserts `positions`, and inserts into
`trades` — three or four statements. The spec never says these are one atomic transaction. A manual trade
concurrent with an LLM-executed trade can both pass the "sufficient cash" check and drive the balance
negative.

**Fix:** state that trade execution runs inside a single `BEGIN IMMEDIATE` transaction covering the balance
check, balance update, position upsert, and trade insert, and that failed validation rolls back with no
partial writes.

### M3. `docker run` vs `docker compose`, and named volume vs bind mount, are self-contradictory

- **Line 79** — "Single Docker container | … no docker-compose for production" contradicts **line 418**, which
  makes `docker-compose.yml` the single source of truth for port/volume/env with the scripts as thin wrappers.
- **Line 411** shows `-v finally-data:/app/db` (a **named volume**), but **line 414** says "The `db/` directory
  in the project root maps to `/app/db`" (a **bind mount**). Mutually exclusive — and this determines whether
  `db/.gitkeep` (line 102) serves any purpose.
- `README.md:34-35` propagates the `docker run` form, diverging from §11's compose-first directive.

**Fix:** pick one — the **named volume** is recommended, since it avoids Windows/OneDrive bind-mount file
locking problems with SQLite (directly relevant: this repo lives under `OneDrive\Desktop`). Then correct
lines 79, 411, 414, and the README together.

### M4. The `trades` table is written but never read

Defined as an append-only log (lines 229-236) and written on every execution, but §8 exposes no endpoint to
read it and §10 has no trade-history/blotter component. It is write-only dead data — and the LLM cannot reason
about realized performance without it.

**Fix:** either add `GET /api/portfolio/trades` (with `?limit=`) plus a blotter panel, or state explicitly
that the table exists for audit/future use only.

### M5. Realized P&L is not modeled anywhere

Only *unrealized* P&L appears (lines 29, 269, 370). Once a position closes, the gain becomes invisible — it
folds silently into cash. The heatmap, positions table, and LLM context all lose the information. This may be
an intentional simplification, but unlike the sparkline note on line 25 it isn't flagged, so agents will each
resolve it differently.

**Fix:** either add a `realized_pnl` column to `users_profile` (updated on each sell), or add an explicit
"accepted simplification" note in the style of line 25.

### M6. Two different error body shapes

`POST /api/portfolio/trade` returns `400` with `{success: false, error}` (line 270), while the watchlist
endpoints just say "returns a 400 error" (lines 182, 277) — and FastAPI's default `HTTPException` produces
`{"detail": …}`. The frontend needs two error-parsing paths and will silently render "undefined" for one.

**Fix:** define one error envelope for all endpoints, stated once in §8.

### M7. `actions` JSON envelope shape is described but never shown

**PLAN.md:249** says `actions` holds "the LLM's `trades` and `watchlist_changes` arrays … with an added
`status` field per entry." Two arrays in one column implies an envelope object whose literal shape is never
given — and the frontend must render it inline as trade confirmations (line 372).

**Fix:** show the literal stored JSON:

```json
{"trades":[{"ticker":"AAPL","side":"buy","quantity":10,"status":"executed","fill_price":190.12}],
 "watchlist_changes":[{"ticker":"PYPL","action":"add","status":"failed: watchlist limit of 20 reached"}]}
```

### M8. Trade response omits the fill price and trade id

`{success, position, cash_balance}` (line 270) gives the frontend no way to show "Filled 10 AAPL @ $190.12" —
yet line 27 promises "instant fill at current price," which is the single most important number to confirm
back to the user. Same gap in the chat action annotations.

**Fix:** add `fill_price`, `trade_id`, and `executed_at` to the trade response and to each executed entry in
`actions`.

### M9. Missing indexes on the three time-series queries

`GET /api/portfolio/history` scans `portfolio_snapshots`, `GET /api/chat` scans `chat_messages`, and any trade
lookup scans `trades` — all filtered by `user_id` and ordered by a timestamp, with no index declared in §7.

**Fix:** specify indexes on `(user_id, recorded_at)`, `(user_id, created_at)`, and `(user_id, executed_at)`.

### M10. `portfolio_snapshots` grows without bound and `/history` has no limit

Every 30 seconds (line 238) = 2,880 rows/day. `GET /api/portfolio/history` (line 271) has no range or limit
parameter, so the P&L chart eventually downloads and renders a million-point series.

**Fix:** add `?limit=` / `?since=`, cap the default response (e.g. 500 points, downsampled), and specify
retention (e.g. 30s resolution for 24h, then thin to 5m).

### M11. Startup snapshots can record a garbage portfolio value

The 30s snapshot task computes `total_value` from the in-memory price cache, but at process start the cache is
empty. The first snapshot — and any snapshot taken while a newly added ticker is unpriced — treats missing
prices as 0 (or NaN), permanently poisoning the P&L chart with a spike to zero.

**Fix:** skip snapshots until every position ticker has a cached price; fall back to `avg_cost` rather than 0
for a missing price.

### M12. Lazy DB init is described inconsistently and can race

**Line 113** says "lazily initializes the database on first request"; **line 198** says "on startup (or first
request)." Two concurrent first requests — very likely, since the SPA fires several `/api/*` calls on mount —
can both attempt schema creation and seeding, producing duplicate seed rows or a `table already exists` 500.

**Fix:** settle on FastAPI lifespan startup, guarded by `CREATE TABLE IF NOT EXISTS` plus an idempotent seed
(`INSERT OR IGNORE`), and remove the "first request" wording.

### M13. GBM parameters and time-scaling are unspecified

**Line 152** says GBM "with configurable drift and volatility per ticker" and **line 154** ~500ms updates, but
never states the `dt` scaling. An agent that applies an annual σ of 0.3 per 500ms tick produces prices in the
millions within a minute. There is no price floor, so a ticker can go to zero or negative. "Correlated moves
across tickers (e.g. tech stocks move together)" (line 154) gives no sector map, and line 176's
hash-derived-price rule for unknown tickers assigns no sector.

**Fix:** state the `dt` scaling explicitly (e.g. `dt = interval / (252 × 6.5 × 3600)` trading-seconds per
year), give concrete per-ticker σ/μ defaults, mandate `price = max(price, 0.01)`, and either define a
sector→beta map or drop the correlation requirement.

### M14. Massive mode goes visually dead outside market hours

With change-only emission (line 189) and real quotes, evenings and weekends produce zero events. The
connection dot stays green, prices never flash, and the app looks broken. This is the most likely state in
which a student first runs it with a real key.

**Fix:** require a "market closed / stale data" badge driven by the last-tick timestamp, and note it in §2's
connection-status description (line 37).

### M15. Timestamps have no timezone rule

All timestamps are "TEXT (ISO timestamp)" (lines 211, 217, 226, 236, 242, 250) with no statement about UTC or
a `Z` suffix. Mixed naive-local and UTC strings sort incorrectly as TEXT and render at the wrong time in the
P&L chart.

**Fix:** mandate UTC ISO-8601 with `Z`, produced by a single shared helper.

### M16. SPA routing / static-serving details omitted

§11 says FastAPI serves the Next.js export, but never specifies route precedence (`/api/*` must be registered
before the catch-all mount), SPA fallback to `index.html` for unknown paths, or Next `output: 'export'`
trailing-slash behavior. This is a classic first-integration failure and will manifest as `/api/health`
returning HTML.

**Fix:** state that API routers mount first, the static mount is last and falls back to `index.html`, and
unknown `/api/*` paths return JSON 404 — never HTML.

### M17. SSE lifecycle: no heartbeat, no disconnect cleanup, no shutdown handling

Nothing specifies keep-alive comments, a `retry:` hint, detection of client disconnect (without which every
page refresh leaks a generator and its subscription), or draining SSE generators on shutdown (which will hang
`docker compose down` for the full stop timeout).

**Fix:** require a `: ping` comment every 15s, `retry: 3000` on connect, `await request.is_disconnected()`
cleanup, and cancellation of background tasks + SSE generators in the lifespan shutdown handler.

### M18. Post-action cache invalidation across panels is unspecified

When the LLM executes a trade or watchlist change via `POST /api/chat`, the header cash/total (line 373),
positions table, heatmap, P&L chart, and watchlist panel all go stale. §10 never says who re-fetches what.
Same for a manual trade from the trade bar.

**Fix:** state that a successful trade or watchlist mutation — from either path — triggers re-fetch of
`/api/portfolio`, `/api/portfolio/history`, and `/api/watchlist`, ideally through a shared store action so
both paths use one code path.

### M19. Structured-output "optional" fields may be rejected in strict JSON-schema mode

**Lines 328-330** mark `trades` and `watchlist_changes` optional. Strict structured-output modes — which the
cerebras skill's `response_format=MyBaseModelSubclass` path uses (`.claude/skills/cerebras/SKILL.md:40`) —
generally require every property to be listed as required. Modeling them as `Optional[…] = None` will either
error at schema-generation time or produce `None` where the executor expects a list.

**Fix:** define them as required arrays that may be empty (`list[Trade] = Field(default_factory=list)`), and
state that the executor treats absent and empty identically.

### M20. Massive-mode ticker validation isn't required of the LLM path in practice

**Line 178** says validation "applies equally whether the ticker is added manually or via the LLM's
`watchlist_changes`" — good — but §9 step 6 (line 308) describes execution without restating that it routes
through the same validation code path. Given these are written by different agents, the LLM path will likely
insert directly into the DB.

**Fix:** state that LLM actions MUST call the same internal service functions as the REST handlers, never the
DB directly.

### M21. Secrets handling is only implicit

`.env` is correctly gitignored (`.gitignore:138`) and a real `.env` exists in the working tree — good. But the
spec never forbids logging the key, never says errors must not echo LiteLLM request/response bodies, and never
mentions that LiteLLM's verbose mode prints headers including `Authorization`. `/api/health` (line 289) also
has no "must not leak configuration" constraint.

**Fix:** add one line to §5 — secrets are read from the environment only, never logged, never returned in any
API response or error body; `litellm.set_verbose` must remain off.

---

## LOW

- **L1.** `.env.example` doesn't exist but is referenced by `README.md:30` and implied by `PLAN.md:105`. The
  documented quick start fails at step one.
- **L2.** `users_profile` contradicts its own preamble. Line 206 asserts "All tables include a `user_id`
  column," but `users_profile` (lines 208-211) has `id`, not `user_id`. Harmless in practice, but agents
  implement preambles literally — reword to "all *per-user data* tables."
- **L3.** `DELETE /api/watchlist/{ticker}` semantics for a missing ticker are undefined (404 vs idempotent
  204). Pick one; idempotent 204 is friendlier for the LLM path.
- **L4.** `GET /api/watchlist` "with latest prices" doesn't say what a just-added, not-yet-priced ticker
  returns — `null`, `0`, or omitted. `0` renders as a $0.00 stock with a −100% change.
- **L5.** The connection-status tri-state isn't derivable from `EventSource` as described. Line 37 defines
  green/yellow/red, but `EventSource` only surfaces `onopen`/`onerror` + `readyState`; it retries forever and
  never reports permanent failure. Define red as "N consecutive failed reconnects" or "no event and no ping
  for T seconds."
- **L6.** The main chart has the same empty-on-load problem as sparklines, but only sparklines got the explicit
  note (line 25). Line 367's "price over time" chart is a single point at page load. Extend the note.
- **L7.** `portfolio_snapshots` stores only `total_value` — no cash/holdings split, so the P&L chart can never
  show composition. Consider adding `cash_balance` and `positions_value`.
- **L8.** Fractional-share input precision is unspecified — the trade bar (line 371) needs a min/step/decimal
  rule, otherwise users can submit `0.0000001` shares.
- **L9.** Test coverage gaps in §12: no scenario covers the 20-ticker cap rejection, invalid-ticker rejection,
  the LLM retry/canned-fallback path (line 306), `/api/health`, or the insufficient-funds error surface. No CI
  configuration or coverage target is mentioned anywhere.
- **L10.** Duplicate top-level heading — `## Project Specification` (line 3) immediately followed by
  `## 1. Vision` (line 5).
- **L11.** No observability spec — no structured logging, request IDs, or LLM latency/token metrics, which
  makes the "production-quality" claim (line 9) hard to substantiate.
- **L12.** No number/locale formatting rule — currency and percent formatting will differ per component
  (`$1,234.5` vs `$1,234.50`) across a data-dense terminal UI where alignment matters.

---

## Recommended spec edits, in priority order

1. Add a normative JSON appendix to §8 covering every request/response body, the error envelope, and the SSE
   event format — **C1, C2, M6, M7, M8**.
2. Add a "Numeric precision & rounding" subsection to §7 with epsilon comparison, position-deletion, and
   rounding rules — **C4**.
3. Add ticker normalization + a validation regex as a global rule applying to manual, path-param, and LLM
   inputs — **C5**.
4. Define the priced-ticker set as `watchlist ∪ open positions` throughout §6 — **H1**.
5. Specify the SSE connection snapshot, heartbeat, and disconnect/shutdown lifecycle — **C3, M17**.
6. Add a "Concurrency" subsection — WAL, `busy_timeout`, threadpool, `BEGIN IMMEDIATE` for trades —
   **M1, M2**.
7. Add an LLM "Guardrails" subsection and bound the conversation history — **H4, H5**.
8. Resolve the health-check threshold, the Massive grouped-poll requirement, and after-hours staleness —
   **H2, H7, M14**.
9. Resolve the compose-vs-run and named-volume-vs-bind-mount contradictions across `PLAN.md:79/411/414` and
   `README.md:34` — **M3**.
10. Specify the `LLM_MOCK` response contract so the test agent can work independently — **H9**.
