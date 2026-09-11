# PLAN.md — Comprehensive Review

_Reviewer pass, 2026-09-08. Builds on Section 13 (Design Decisions Log); does not re-litigate
decisions already recorded there. Focus: readiness of the plan for the **next** implementing
agents (backend platform, LLM, frontend, Docker/E2E), and consistency between the plan and the
**already-built** market data subsystem in `backend/app/market/`._

Severity legend: **[BLOCKER]** stops an agent cold · **[RISK]** likely to cause rework or a
production defect · **[GAP]** missing spec, should be added · **[NIT]** editorial / low stakes.

---

## 1. Plan vs. what was actually built (`backend/app/market/`)

The plan already flags the day-change anchor and unknown-ticker fallback as "follow-up
additions, not yet built." Reading the code, the divergence is wider than those two items and
should be spelled out so the platform agent knows the market layer is **not** a finished
dependency it can just import.

### 1.1 [BLOCKER] Day-change anchor does not exist anywhere in the built code
Decision #1 and §6 require the price cache to hold a per-ticker **anchor** and for it to ship in
the SSE payload, `/api/watchlist`, and `/api/bootstrap`. Reality:
- `PriceUpdate` (`models.py`) is a `frozen=True, slots=True` dataclass with fields
  `ticker, price, previous_price, timestamp` only. No anchor, and it cannot be added at runtime.
- `PriceCache.update()` (`cache.py`) takes `(ticker, price, timestamp)` — no anchor concept, no
  place to store one.
- `stream.py` `to_dict()` emits `change`, `change_percent`, `direction` — no anchor.
- `MassiveDataSource` never reads a previous-close field; `_poll_once` only pulls
  `last_trade.price`.

This is real rework on "done, tested, reviewed" code (`PriceUpdate` +/- field, `PriceCache`
anchor map + first-write capture, `to_dict`, Massive previous-close plumbing, plus 73 tests to
update). The plan should (a) acknowledge the anchor is a modification of the shipped subsystem,
not a greenfield add, (b) name the owning agent, and (c) specify where the anchor is captured in
Massive mode (Polygon snapshot `prevDay.c` / `todaysChange` — confirm the `massive` package
surfaces it; if not, the "previous close" anchor is not achievable and simulator-style
first-observed is the only option in both modes).

### 1.2 [BLOCKER] SSE payload shape in §6 does not match `stream.py`
§6: "Each SSE event contains ticker, price, previous price, day-change anchor, timestamp, and
change direction" — i.e. one event per ticker.
Built: a **single** `data:` line per tick carrying a JSON **object keyed by every ticker**
(`{"AAPL": {...}, "GOOGL": {...}}`), pushed only when `PriceCache.version` changed, plus a
one-time `retry: 1000`. Extra field `change_percent` is present; there is no `event:` type.
The frontend agent will build the `EventSource` handler straight from §6 and get it wrong.
Fix §6 to document the actual envelope (whole-snapshot object, version-gated, ~500ms poll) and
list the actual per-ticker keys, then add `anchor` to that list.

### 1.3 [RISK] SSE `: keepalive` (Decision #14 / §6) is not implemented
`_generate_events` sleeps and only yields when `version` changes. In Massive mode between polls
(15s free tier) or outside market hours ("prices ... simply stop changing"), the stream is
silent and the client cannot distinguish idle from dead — exactly the case Decision #14 exists
to solve. Needs a `yield ": keepalive\n\n"` on a ~15s timer inside the loop. Cheap, but
currently absent.

### 1.4 [RISK] Timestamp format contradiction — Decision #19 vs. the price stream
Decision #19 / §7: "UTC ISO-8601 with `Z` **everywhere**." The market layer uses **Unix epoch
floats** end to end (`PriceUpdate.timestamp = time.time()`, SSE ships the float, Massive divides
ms by 1000). Either carve an explicit exception into Decision #19 ("DB timestamps are ISO-8601
Z; the price stream uses epoch seconds") or convert at the SSE boundary. The frontend needs to
know which it is getting for the sparkline/detail-chart x-axis.

### 1.5 [NIT] Unknown-ticker synthesis is already partly built
§6 / Decision #3 describe this as not-yet-built, but `simulator.py`
(`SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))` + `DEFAULT_PARAMS`) already synthesizes
a price and GBM params for unseen tickers. What is **missing** is the ticker-format validation
("1–5 uppercase letters") — it exists nowhere in the market layer, and `MassiveDataSource`
silently `.upper().strip()`s input. Reassign this as "add validation at the API boundary"
rather than "build simulator fallback."

### 1.6 [RISK] `remove_ticker` unconditionally evicts from the cache
Both sources' `remove_ticker` call `self._cache.remove(ticker)`. The §6/§8/Decision #4
invariant "a position is never left unpriced" is therefore **entirely the caller's
responsibility**: `DELETE /api/watchlist/{ticker}` must NOT call `source.remove_ticker` when an
open position exists, and something must re-add a ticker when a trade opens a position for a
symbol that is not on the watchlist. The plan states the invariant but never says who maintains
the tracked set. See §3.1 below.

### 1.7 [RISK] Simulator ticker mutation is not concurrency-safe with the step loop
`GBMSimulator.add_ticker/remove_ticker` mutate `self._tickers` / `self._params` and rebuild the
Cholesky factor with no lock; `step()` iterates those same structures every 500ms. This is only
safe if every caller runs on the event-loop thread. Therefore the watchlist mutation routes
**must be `async def`** (not sync routes dispatched to the threadpool). Worth an explicit note
for the backend agent, since FastAPI makes the sync path easy to reach by accident.

---

## 2. Internal consistency & contradictions

### 2.1 [RISK] "Lazy init on first request" vs. startup background tasks
§7: DB is initialized "on startup (or first request)." But §3/§7 also describe a market-data
task and a 30-second `portfolio_snapshots` writer that need the schema and the watchlist
**before** any HTTP request arrives. The snapshot writer will crash on an empty DB. Resolve to:
initialize + seed on startup (lifespan), unconditionally, before starting background tasks.
Drop "or first request."

### 2.2 [RISK] Startup ordering is unspecified
Several things must happen in order and the plan never sequences them:
1. open DB, create schema if missing, seed defaults;
2. read `watchlist ∪ open positions` from DB;
3. `create_market_data_source(cache)` → `await source.start(tracked)`;
4. start the snapshot-writer task;
5. mount routers.
Add a short "Application lifespan" subsection. Without it, three agents will invent three
different orderings.

### 2.3 [RISK] Realized P&L — two descriptions of the source of truth
§2: "cumulative realized gain/loss ... computed from the trade log." §7: stored in
`positions.realized_pnl`, accumulated in the trade transaction. These are reconcilable (§7 says
the cache tables are replayable from `trades`), but §2's wording will send an agent to compute
realized P&L by scanning `trades` at request time while another reads the column. State once:
**`positions.realized_pnl` is the read path; `trades` is the rebuild path.**

### 2.4 [GAP] `/api/bootstrap` payload is undefined
It is described only prose-wise ("everything needed for first paint"). The frontend agent needs
the concrete JSON: which keys, nesting, whether it reuses the exact shapes of `/api/portfolio` +
`/api/watchlist` + `/api/portfolio/history` + `/api/chat/history`, and what limits apply to the
embedded history and chat arrays (500 / 50 to match the standalone endpoints?). Specify it as
"the four responses under four keys" or write the schema out.

### 2.5 [GAP] Error contract vs. FastAPI defaults
Decision #16 / §8 mandate `{"error": "..."}` with 400 / 404 / 502. FastAPI emits
`{"detail": ...}` and — critically — **422** for request-body validation, not 400. The backend
agent must install a custom exception handler and override `RequestValidationError`. Say so, or
the contract silently won't hold for "bad quantity / malformed ticker."

### 2.6 [GAP] `502 — upstream failure (LLM/market data provider)` — which endpoint?
Chat clearly returns 502 when `OPENROUTER_API_KEY` is missing/failing. But no endpoint surfaces
a market-data upstream failure: the source is chosen once at startup, Massive poll failures are
swallowed (`massive_client.py` logs and retries), and SSE just serves a stale cache. Either drop
"market data provider" from the 502 line or define the behavior (e.g. `/api/health` degrades, or
SSE emits an `event: error`).

### 2.7 [NIT] `chat_messages` write — "Stores the message" (singular)
§9 step 7 should say it stores **both** the user row and the assistant row (the user row has
`actions = null` per §7). As written it reads like only one row is persisted.

### 2.8 [NIT] Two paths to restore chat on load
§10 says the chat panel restores from `/api/chat/history`; §8/Decision #10 says `/api/bootstrap`
already returns "recent chat history." Pick one for first paint (bootstrap) and reserve
`/api/chat/history` for pagination, to avoid a double fetch and a flash of two states.

### 2.9 [NIT] Start scripts `--build`, E2E runner does not
§11 scripts run `docker compose up -d --build`; §12 E2E runner runs `docker compose up -d`
(no `--build`), so tests can run against a stale image after a code change. Align them.

---

## 3. Ambiguities that will block or mislead an implementing agent

### 3.1 [BLOCKER] Ownership of the "tracked set = watchlist ∪ open positions" invariant
No component is assigned to keep the market data source's ticker set in sync. Concretely
unspecified:
- On `POST /api/watchlist`: persist row **and** `await source.add_ticker()` — in which order,
  and what if `add_ticker` is a no-op because a position already tracks it?
- On `DELETE /api/watchlist/{ticker}`: skip `source.remove_ticker()` iff `positions.quantity > 0`.
- On a buy that opens a brand-new ticker not on the watchlist: who calls `add_ticker`?
- On a sell that takes `quantity` to 0 for a ticker that was removed from the watchlist earlier:
  who calls `remove_ticker` now that nothing references it?
- On startup: seed the source from the union, not just the watchlist.
This is a single helper ("reconcile_tracked_tickers") but three agents touch it. Spec it.

### 3.2 [BLOCKER] Database path / configuration
§4 shows `db/finally.db` (repo root) and `/app/db` (container volume). No env var in §5 controls
it. Tests need a throwaway DB. Add `DATABASE_PATH` (or similar) to §5 with its default and note
that E2E / unit tests point it at a temp file.

### 3.3 [RISK] SQLite concurrency model is unspecified
Concurrent writers in one process: the trade endpoint, the 30s snapshot task, lazy init, and
"immediately after each trade" snapshot. FastAPI async + `sqlite3` needs a deliberate choice:
WAL mode, `check_same_thread`, one connection vs. connection-per-request, and a write lock or
`BEGIN IMMEDIATE` so a snapshot write doesn't collide with a trade transaction. `SQLITE_BUSY`
under the E2E suite is a real risk. Add a "Database access" paragraph.

### 3.4 [BLOCKER] Uvicorn must run a single worker — not stated anywhere
Everything hinges on one in-process `PriceCache` and one simulator/poller. Multiple uvicorn
workers → N independent simulators producing divergent prices, N snapshot writers, N lazy inits
racing. §11's `CMD` must pin `--workers 1` and the plan should say why. This is easy to miss and
catastrophic for the demo.

### 3.5 [GAP] `chat_messages.actions` JSON schema
§7 says "JSON — trades executed, watchlist changes made"; §10 says the chat panel renders these
"inline as confirmations, including failures." The shape is never defined. Propose:
```json
{
  "trades":  [{"ticker":"AAPL","side":"buy","quantity":10,"status":"ok","price":190.12,"error":null}],
  "watchlist_changes": [{"ticker":"PYPL","action":"add","status":"error","error":"..."}]
}
```
Both the backend (writer) and frontend (renderer) need this frozen before either starts.

### 3.6 [GAP] Valuing a position whose price is not yet in the cache
A just-added ticker has no cache entry for a tick or two (`get_price` → `None`). How is total
value / unrealized P&L computed then — skip the leg, use `avg_cost`, use last trade price?
Affects `/api/portfolio`, `/api/bootstrap`, snapshots, and the client-side header recompute.
Pick a rule.

### 3.7 [GAP] "Day change %" is a misnomer in simulator mode
The anchor is "first observed price after tracking started" and never re-anchors except on
process restart (Decision #11). A container up for days shows an ever-growing "day change." Note
that the label means "since session/tracking start" in simulator mode, and decide whether the
UI should hedge the wording.

### 3.8 [GAP] `LLM_MOCK=true` override mechanism for E2E
§12: the runner starts compose "overriding `LLM_MOCK=true` via env." Compose reads `.env` via
`env_file`; `docker compose up` has no `-e`. Overriding requires either `environment: [LLM_MOCK]`
in `docker-compose.yml` (passthrough from the host shell) or the runner writing `.env`. Decision
#22 forbids a test compose file, so the passthrough must be baked into the main compose file —
say so explicitly.

### 3.9 [GAP] Mock LLM response contract
§9 says mock mode returns "deterministic mock responses" but never defines them. E2E scenarios
require specific behavior: a message that triggers a successful trade, one that triggers a
failed trade, one plain chat. Define the mock's input→output mapping (e.g. keyed on substrings
in the user message) so the E2E agent and the LLM agent agree.

### 3.10 [GAP] Concurrent chat / trade requests
Single user, but the frontend can fire a manual trade while a chat turn is mid-execution, or a
user double-sends. No guidance on serialization. At minimum note that trades are serialized by
the DB transaction and that a second concurrent chat request is allowed to queue / 409 / proceed.

---

## 4. Architectural risks & questionable choices

### 4.1 [RISK] `request.is_disconnected()` as the SSE liveness check
`stream.py` polls `await request.is_disconnected()` every 500ms. Under uvicorn this is known to
be unreliable behind some proxies and can miss disconnects, leaking generator tasks that keep
touching the cache. Consider also breaking on write failure and capping stream lifetime. Low
priority for a local demo, real for the "Optional Cloud Deployment."

### 4.2 [RISK] Whole-snapshot SSE payload on every version change
The simulator bumps `version` once per ticker per tick, so `version` jumps by ~N each 500ms and
the stream re-serializes **all** tickers every time. Fine at 10–20 tickers; if the AI or user
piles on symbols it grows O(N) per 500ms per client. Acceptable for scope — just acknowledge the
ceiling (say, soft-cap the watchlist at ~30).

### 4.3 [RISK] Unbounded client-side price history
§2/§10: sparklines and the detail chart accumulate from SSE "since page load" with no cap. A
tab left open for hours grows arrays without bound (10–20 tickers × 2 Hz). Specify a ring buffer
/ max points per series (e.g. last 1,800 = 15 min) — this is a frontend spec item, not just an
optimization.

### 4.4 [RISK] Recharts for the detail chart at 2 Hz
Decision #20 mandates Recharts for all four visuals, with a canvas escape hatch only for the
detail chart. Recharts (SVG) re-rendering a growing line at 2 Hz will get janky within minutes.
Recommend pre-emptively: throttle chart updates to ~1 Hz and downsample to a fixed width,
regardless of library. Flag so the frontend agent budgets for it rather than discovering it late.

### 4.5 [RISK] No schema migration path with a persisted volume
Decision explicitly defers migrations, and lazy "create if missing" does not `ALTER` for new
columns. Since the volume persists across rebuilds (§11), the first schema change after anyone
has run the app will hit a stale DB with no upgrade path. At least document "delete the volume
to reset" and consider a `schema_version` row now so a future migration hook has a hook point.

### 4.6 [RISK] Auto-executed trades from unschema'd LLM output
§9 notes Cerebras may not enforce the JSON schema (Decision #8 retry-once fallback). Combined
with auto-execution and no confirmation, a hallucinated `{"ticker":"APPL","quantity":9999}` goes
straight to validation. Validation catches insufficient cash, but not a wrong-but-affordable
ticker/qty. Consider a sanity clamp (e.g. reject trades whose notional > current total value, or
> some multiple of cash) as defense in depth. Cheap, and a good demo-safety story.

### 4.7 [NIT] `.env` "read by the backend" wording
§5: "The backend reads `.env` from the project root." In the container, compose's `env_file`
injects real environment variables — the backend should read `os.environ` only. `python-dotenv`
(if used) is a local-dev convenience. Clarify to avoid an agent shipping a container that tries
to open a non-existent `.env`.

---

## 5. Gaps — things the plan should cover but doesn't

| # | Gap | Why it matters |
|---|-----|----------------|
| 5.1 | **Agent roles & build order** (already "still open" in §13) | Four agents, shared files, hard ordering deps (§2.2, §3.1). This is now the single biggest blocker to starting. Write `planning/ROLES.md`. |
| 5.2 | **Application lifespan / startup sequence** | §2.1, §2.2. |
| 5.3 | **`DATABASE_PATH` env var + test DB strategy** | §3.2. |
| 5.4 | **`--workers 1` requirement** | §3.4. |
| 5.5 | **SQLite concurrency (WAL, connection strategy, write serialization)** | §3.3. |
| 5.6 | **`/api/bootstrap` concrete schema** | §2.4. |
| 5.7 | **`chat_messages.actions` schema** | §3.5. |
| 5.8 | **Custom exception handler for the `{"error"}` contract + 422→400** | §2.5. |
| 5.9 | **Mock LLM response contract** | §3.9. |
| 5.10 | **`LLM_MOCK` compose passthrough** | §3.8. |
| 5.11 | **Missing-price valuation rule** | §3.6. |
| 5.12 | **Client-side history cap** | §4.3. |
| 5.13 | **Ticker validation location + regex** (`^[A-Z]{1,5}$`) applied at every write path: manual watchlist add, manual trade, LLM trade, LLM watchlist change | §1.5. |
| 5.14 | **`GET /api/trades` (blotter)** — §2 promises the user "sees realized P&L ... from the trade log" and E2E checks avg-cost behavior, but no endpoint exposes trades and bootstrap omits them. Either add the endpoint or state explicitly that trades are not surfaced in v1. | UI completeness / testability. |
| 5.15 | **SSE through `next dev` rewrites** — Next.js rewrites proxying `text/event-stream` can buffer and break flush semantics in dev. Note that devs should hit `:8000` directly for the stream, or test SSE only against the built export. | §10 dev-proxy usability. |
| 5.16 | **Number/currency formatting & rounding at the display layer** — §8 fixes storage rounding (6dp shares, cents cash) but not display (price decimals, P&L %, treemap thresholds). Minor, but "still open" already lists the treemap scale. | Consistency across panels. |
| 5.17 | **Health check semantics for the market task** — `/api/health` checks process + DB. It does not report whether prices are actually streaming. Consider a `/api/health` field `market_data: "ok"|"stale"` (last cache update age). | Ops / E2E "prices are streaming" assertion. |
| 5.18 | **Graceful shutdown** — lifespan must `await source.stop()` and cancel the snapshot task; SSE generators must exit. Not mentioned. | Clean `docker compose down`, no orphaned tasks in tests. |

---

## 6. Opportunities to simplify

- **6.1 Collapse `/api/portfolio` and the portfolio slice of `/api/bootstrap` into one serializer.**
  Same for watchlist and history. State in §8 that bootstrap composes the other responses
  verbatim — removes any chance of drift between the two shapes.
- **6.2 Drop `change` / `change_percent` from the SSE `to_dict()`.** The UI shows day-change %
  (vs. anchor) and a flash driven by `direction`; per-tick absolute change and per-tick percent
  are unused. (Decision "not doing: trimming change/direction" — but `change_percent` isn't even
  mentioned in that decision and is pure noise. Re-open just that sub-item.)
- **6.3 One background task, not two.** The 30s `portfolio_snapshots` writer and any future
  cache-maintenance work can share a single "housekeeping" loop rather than separate tasks —
  fewer things to order, start, and stop in the lifespan.
- **6.4 Skip the standalone `GET /api/watchlist` / `GET /api/portfolio/history` for v1?** After
  bootstrap, the watchlist only changes via calls the client itself makes (it can update state
  locally from the mutation response), and history is only appended by the server (the client
  already recomputes total value live from SSE; the P&L chart can extend from SSE ticks too).
  If that holds, bootstrap + the mutation endpoints + SSE cover the whole app and two GETs go
  away. Worth a hard look before building them.
- **6.5 `previous_price` in the SSE payload is redundant with client state.** The client sees
  every tick, so it already knows the prior price for the flash. Keeping it is harmless (1
  number) but if trimming, this is a candidate.
- **6.6 Seed prices live in code twice conceptually.** `seed_prices.py` has the 10 tickers;
  §7 seed data has the same 10 in the `watchlist` table. Make the DB seed import the list from
  the market module (single source) rather than hardcoding it in schema SQL.

---

## 7. Top recommendations (do these before the next agent starts)

1. **Write `planning/ROLES.md`** — agent ownership + build order. Nominate: (a) Backend Platform
   (DB, lifespan, portfolio/watchlist/trade APIs, error contract), (b) LLM (chat endpoint, mock,
   structured output), (c) Frontend, (d) Docker/E2E. Sequence: A → (B ∥ C) → D.
2. **Add an "Application lifespan & configuration" subsection to §3 or §7**: startup ordering
   (§2.2), `--workers 1` (§3.4), `DATABASE_PATH` (§3.2), SQLite WAL + connection strategy
   (§3.3), graceful shutdown (§5.18).
3. **Reconcile §6 with the shipped market code**: real SSE envelope (§1.2), keepalive as a
   to-do with an owner (§1.3), timestamp format exception (§1.4), and an explicit note that the
   **anchor is a modification of already-tested code** with a named owner and a Massive
   previous-close feasibility check (§1.1).
4. **Specify the tracked-ticker reconciliation helper** (§3.1) and require async watchlist
   routes (§1.7).
5. **Freeze two schemas**: `/api/bootstrap` response (§2.4) and `chat_messages.actions` (§3.5).
6. **Nail the error contract**: custom handler, 422→400 mapping, and which endpoints can 502
   (§2.5, §2.6).
7. **Define the mock-LLM contract and the `LLM_MOCK` compose passthrough** (§3.8, §3.9) so E2E
   isn't blocked on the LLM agent.
8. **Add ticker validation** (`^[A-Z]{1,5}$`) at all four write paths (§5.13).

---

## 8. Smaller notes

- §4 directory tree lists `test/` for Playwright and says unit tests live in `frontend/` and
  `backend/`. `backend/tests/` already exists; confirm the frontend testing library choice
  (§12 says "React Testing Library or similar" — pick one so lockfiles are deterministic).
- §7 `portfolio_snapshots` "trims to 7 days on each write" while `/api/portfolio/history`
  defaults to `limit=500`. At one snapshot / 30s while active, 7 days ≈ 20k rows but realistic
  active use is far less; 500 is ~4 hours. Fine, just confirm the chart is meant to show
  "recent" not "all 7 days" by default.
- §9 "last 20 messages" — clarify whether that is 20 rows (10 turns) or 20 turns. §8 history
  default is 50.
- §10 header "Total value updates live ... recomputed client-side" — this diverges from the
  server's snapshot valuation whenever the client is missing a tick or a price (§3.6). Note the
  client value is an estimate that reconciles to the server on the next bootstrap/portfolio
  fetch.
- Decision #24 says skill name is `cerebras`; the repo has `.claude/skills/cerebras/` — matches.
- §11 Dockerfile "Node 20" vs. §11 stage-1 "Node 20 slim" vs. common Next.js 14+ needing Node
  18.17+/20 — fine, just pin the exact tag in the Dockerfile.
- No mention of `favicon`, page `<title>`, or basic branding for the served SPA — trivial, but
  add a line so it isn't forgotten in the "visually stunning" product.
