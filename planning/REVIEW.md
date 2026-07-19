c# PLAN.md — Comprehensive Review

**Reviewer:** Coding agent (review pass)
**Scope:** `planning/PLAN.md`, checked against the one component already built (`backend/app/market/`), plus `README.md`, `.gitignore`, `backend/pyproject.toml`.
**Verdict:** Strong plan — clear, well-reasoned, and unusually well-scoped for an agent-built project. The issues below are concentrated in one place: **PLAN.md is the coordination contract between agents, and it is under-specified exactly where agents must agree.** Fixing §8 (API schemas) and the six correctness gaps is worth more than everything else combined.

---

## 1. What Works Well

These are worth preserving as the plan evolves:

- **The rationale table (§3)** is the best thing in the document. Decisions *with reasons* let a later agent know what is load-bearing and what is incidental. Most plans omit this.
- **Scope discipline.** Market-orders-only, SSE-over-WebSockets, SQLite-over-Postgres, single container. Each removes a whole class of work. The plan consistently chooses the boring option and says why.
- **Schema future-proofing.** `user_id` defaulting to `"default"` on every table is cheap now and avoids a migration later. Good call.
- **Explicit agent boundaries (§4).** "Internal structure is up to the Frontend Engineer agent" is the right altitude — it constrains interfaces, not implementations.
- **The already-built market data layer validates the design.** The ABC + PriceCache + factory pattern in `backend/app/market/` is genuinely clean: correct GBM math with Cholesky-correlated draws, a properly locked cache, 73 tests at 84% coverage. The plan's §6 translated into good code, which is evidence the spec style works.

---

## 2. Critical — Contract Gaps That Will Cause Divergent Work

### 2.1 §8 defines no request/response schemas

This is the highest-priority fix. §8 lists paths and prose descriptions, but no field names, types, status codes, or error shapes. The frontend and backend agents cannot both be right by accident. `GET /api/portfolio` says "Current positions, cash balance, total value, unrealized P&L" — that is five different valid JSON shapes.

Concretely undefined:
- Is it `{cash, positions: [...]}` or `{cash_balance, positions: {...}}`?
- Is P&L per-position, aggregate, or both? Absolute, percent, or both?
- What status code does a rejected trade return — 400, 409, 422, or 200 with `{"error": ...}`?
- Are prices in the portfolio response, or must the frontend join against the SSE stream?

**Recommendation:** Add a §8 sub-section with a concrete JSON example of every request and response, including one error response. If the backend agent defines Pydantic models, the plan should name them as the source of truth and require the frontend to mirror them. This is ~1 page of writing that removes the largest single risk in the build.

### 2.2 "Daily change %" has no data source

§10 requires the watchlist to show "daily change %". Nothing in the system can compute it.

`PriceUpdate.change_percent` (`backend/app/market/models.py:24-28`) is **tick-over-tick** — the delta since the last 500ms update, typically ~0.001%. It is not a daily change. There is no previous-close, no session-open anchor, and no daily bar anywhere in the cache or schema. The simulator starts from a seed price with no concept of "today".

**Recommendation:** Decide and write it down. Cheapest option: record each ticker's price at process start as a session anchor, expose it in the SSE payload as `session_open`, and rename the UI field to "Session %". If a true daily change is wanted, it must come from the Massive daily bar and the simulator must fake a previous close — which then also needs to survive restarts (see 2.4).

### 2.3 The main chart has no history source

§2 and §10 are careful and explicit that sparklines accumulate on the frontend from SSE since page load. The **main chart area** gets no such treatment — it is specified as "a larger chart for the currently selected ticker, with at minimum price over time", with no endpoint behind it and no persistence.

As written, the headline visual of a "Bloomberg terminal" is an empty chart on page load that slowly fills in and is wiped on every refresh. That is likely not the intent, and it is the kind of gap that surfaces only at demo time.

**Recommendation:** Either (a) add a `GET /api/history/{ticker}?since=` endpoint plus a `price_history` table or ring buffer, or (b) state explicitly that the main chart is session-accumulated like the sparklines and accept the empty-on-load behavior. Option (b) is legitimate and cheap — but it must be a decision, not an omission.

### 2.4 Prices are ephemeral; positions are durable. P&L breaks on restart.

The plan never reconciles these two facts:
- Positions, `avg_cost`, and cash persist in SQLite across restarts (§7, by design, via Docker volume).
- Prices live only in the in-memory `PriceCache`. On restart, the simulator re-seeds from `SEED_PRICES` (`backend/app/market/seed_prices.py:4-15`).

So: buy NVDA as the simulator drifts up to $850, restart the container, and NVDA snaps back to $800. The user now holds an instant unrealized loss they never traded into. The P&L chart shows a cliff. `portfolio_snapshots` faithfully records the discontinuity forever.

This is a persistence-model inconsistency, not a cosmetic one — and the volume mount actively makes it worse, because the positions survive precisely so the prices can contradict them.

**Recommendation:** Persist the last price per ticker (a `last_prices` table, or reuse the newest `portfolio_snapshots` timestamp) and seed the simulator from it on startup, falling back to `SEED_PRICES` only for genuinely new tickers. Add this to §6 and §7.

### 2.5 Unknown tickers get a random price — including the plan's own example

`backend/app/market/simulator.py:151`:

```python
self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
```

Any ticker outside the seeded ten gets a **uniformly random price between $50 and $300**. §9's own worked example is the LLM adding `PYPL` to the watchlist — which will materialize at, say, $237.41 today and $88.02 after a restart.

Compounding this, §8's `POST /api/watchlist {ticker}` specifies no validation. Any string is accepted. The LLM can add tickers, and LLMs hallucinate tickers — so a hallucinated `ASDF` becomes a tradable instrument with a random price and full portfolio weight. There is no defined rejection path.

**Recommendation:** Add a ticker validation policy to §6/§8, and state the behavior for tickers with no seed price. Options, in increasing cost: reject anything not in a known-tickers list (simplest, and honest about being a simulator); extend `SEED_PRICES` to a few hundred tickers; or in Massive mode, validate against the API and reject unknown symbols. Whatever is chosen, the random-price fallback should not survive as the silent default.

### 2.6 Removing a watched ticker while holding it orphans the position

`SimulatorDataSource.remove_ticker()` (`simulator.py:251-255`) drops the ticker from the simulator **and** calls `self._cache.remove(ticker)`. The `positions` row is untouched — watchlist and positions are independent tables.

Result: `DELETE /api/watchlist/AAPL` while holding 10 AAPL leaves a position whose price lookup returns `None`. Portfolio total value, the heatmap, the positions table, and every subsequent `portfolio_snapshots` row now depend on how one agent happened to handle a `None` price — crash, zero, or silent omission. The LLM can trigger this path unattended via `watchlist_changes`.

**Recommendation:** Specify the rule in §6. Recommended: the set of tracked tickers is the **union of the watchlist and all open positions**, so a held ticker keeps streaming even when unwatched. Alternatively, reject removal of a held ticker with a clear error. This must be decided in the plan, not per-agent.

---

## 3. Significant — Design Issues to Resolve

### 3.1 Trade execution is under-specified
"Market orders only, instant fill at current price" (§2) leaves open:
- **Price source on a cache miss** — can the user trade a ticker that isn't on the watchlist and therefore has no price? What is returned?
- **Quantity validation** — zero, negative, `NaN`, absurdly large. Fractional shares are supported (§7), so what precision?
- **Atomicity** — a trade reads cash, validates, then writes a `positions` row, a `trades` row, an updated cash balance, and a `portfolio_snapshots` row. That must be one transaction. The plan never says so, and "the backend lazily initializes" is the only DB-discipline guidance given.

### 3.2 SQLite concurrency is unaddressed
Concurrent writers exist by design: request handlers plus the 30-second snapshot background task. The plan should mandate WAL mode, a busy timeout, and a single connection/session discipline. Left unstated, `database is locked` will appear intermittently under demo load — the worst possible time and the hardest class of bug to reproduce.

### 3.3 `portfolio_snapshots` grows without bound
Every 30 seconds forever: 2,880 rows/day, ~1M rows/year, on a volume that explicitly persists. `GET /api/portfolio/history` (§8) takes no `limit`, `since`, or `resolution` parameter, so the P&L chart eventually fetches the entire table on every page load.

**Recommendation:** Add query parameters and a retention/downsampling rule (e.g., keep 1-minute resolution for 24h, then hourly). Even "we accept unbounded growth for a course project" is worth writing down.

### 3.4 The two market data sources are not as interchangeable as claimed
§6 says all downstream code is "agnostic to the source". It isn't, semantically. `PriceUpdate.change`/`direction` mean "delta over 500ms" from the simulator and "delta over 15 seconds" from Massive. The §2 price-flash animation therefore fires ~2×/second in simulator mode and once per 15s in Massive mode, with ~30× larger magnitudes. The interface is shared; the semantics are not. Worth a sentence in §6 so the frontend agent isn't surprised.

### 3.5 Massive polling math may not fit the free tier
§6: "Polls for the union of all watched tickers" at "every 15 seconds" on a 5-calls/min free tier. Whether that fits depends entirely on an unstated detail: is the union fetched in **one** grouped call, or **one call per ticker**? At ten tickers, per-ticker polling is 40 calls/min — 8× over the limit — and the default watchlist is exactly ten tickers. Specify the call shape and the rate-limit/backoff behavior.

### 3.6 Chat history is persisted but unreachable
`chat_messages` stores the full conversation (§7), and §9 loads it server-side for context — but §8 exposes no `GET /api/chat`. The frontend therefore shows an empty chat panel on refresh while the history sits in the database being fed to the LLM. The user sees the assistant reference a conversation that isn't on screen. Add `GET /api/chat` (paginated) or explain the omission.

### 3.7 LLM operational behavior is undefined
§9 covers the happy path well. Missing: timeout, retry policy, behavior on an invalid/absent `OPENROUTER_API_KEY` (does chat degrade or does the page break?), conversation-history truncation (context grows unbounded — the same issue as 3.3, but it costs tokens per message), and what the user sees when the model returns unparseable JSON. §12 asks for tests of "graceful handling of malformed responses" without §9 ever defining what graceful means.

### 3.8 "Deterministic mock responses" is not a specification
`LLM_MOCK=true` is load-bearing for all E2E tests, but its contract is undefined. The E2E scenario "send a message, receive a response, trade execution appears inline" requires the mock to return a *trade* — so the mock must be keyword-triggered or scripted. Define the mapping (e.g., a message containing "buy" returns a fixed buy trade), otherwise the test agent and the backend agent will write incompatible halves.

### 3.9 No-auth is fine locally; §11 makes it a real risk
Single-user, no-auth is well-justified for a local student app. But §11 offers cloud deployment (App Runner, Render) as a documented path, and the deployed container would expose unauthenticated trade execution *and* an unauthenticated LLM endpoint spending the operator's OpenRouter key. That is a metered-cost abuse vector, not a fake-money one.

**Recommendation:** One explicit line in §11 — this app has no authentication and must not be exposed publicly; if deployed, put it behind auth or an IP allowlist.

---

## 4. Documentation Defects (Verified Against the Repo)

These are concrete and checkable today:

| # | Issue | Location | Fix |
|---|---|---|---|
| 1 | **Wrong skill name.** §9 references the "cerebras-inference skill" twice. The actual skill is `cerebras` (`.claude/skills/cerebras/SKILL.md`). An agent following the plan literally will not find it. | §9 | Rename to `cerebras` |
| 2 | **`.env.example` does not exist.** §4 lists it as committed; `README.md` opens Quick Start with `cp .env.example .env`. The documented first step of the project fails today. | §4 / README | Create it from §5's block |
| 3 | **`finally.db` is not gitignored.** §4 states "finally.db is gitignored". `.gitignore` contains `db.sqlite3` but no pattern matching `db/finally.db`. The database will be committed on first run. | §4 / `.gitignore` | Add `db/*.db` |
| 4 | **`db/` and `db/.gitkeep` do not exist.** §4 shows them in the tree. | §4 | Create, or note as not-yet-built |
| 5 | **Volume mount contradiction.** §11's command uses a *named volume* (`-v finally-data:/app/db`), but the prose immediately below says "The `db/` directory in the project root maps to `/app/db`". Those are mutually exclusive — a named volume is invisible in the project directory. §4 separately calls top-level `db/` the "volume mount target". | §4 / §11 | Pick one. A bind mount (`-v "$(pwd)/db:/app/db"`) matches the prose and is friendlier for students who want to inspect the DB |
| 6 | **`OPENROUTER_API_KEY` required-ness contradicts itself.** §5 marks it "Required"; §9 says `LLM_MOCK=true` enables "development without an API key". README's table says "Required: Yes" flatly. | §5 / §9 / README | "Required unless `LLM_MOCK=true`" |
| 7 | **docker-compose contradiction.** §3's rationale says "no docker-compose for production"; §4's tree includes `docker-compose.yml` as an "optional convenience wrapper". | §3 / §4 | Clarify it is a dev convenience only |
| 8 | **SSE payload shape is documented wrong.** §6 says "Each SSE event contains ticker, price, previous price, timestamp, and change direction" — i.e. one event per ticker. The built implementation (`stream.py:81-83`) sends **one event containing a map of all tickers**: `data: {"AAPL": {...}, "GOOGL": {...}}`. The frontend agent will write the wrong parser from the plan. | §6 | Document the actual map shape and the `retry: 1000` directive |

Also worth noting: `CLAUDE.md` says to consult the planning docs "only when required", but it inlines PLAN.md via `@planning/PLAN.md` — so all 22KB enters context on every session regardless. If context budget matters, consider trimming PLAN.md to the contract (§4–§9) and moving vision/UX narrative to a separate doc.

---

## 5. Code-Level Notes on the Built Component

The market data layer is high quality overall; these are small and worth fixing before the pattern is copied by later agents.

1. **`stream.py` module-level router is a latent duplicate-route bug.** `router = APIRouter(...)` is declared at module scope (line 17) but `@router.get("/prices")` is registered *inside* `create_stream_router()` (line 26). Calling the factory twice — a second app instance, or any test that builds the app twice — registers `/prices` twice on the same shared global router. The docstring's claim that the factory "lets us inject the PriceCache without globals" is undercut by the router itself being a global. Move the `APIRouter(...)` construction inside the factory.

2. **`PriceCache.version` reads without the lock** (`cache.py:64-67`) while every other accessor takes it. Safe in practice under CPython for an int load, but it's an inconsistency in an otherwise disciplined lock protocol, and it's the one field a reader polls in a hot loop. Take the lock or add a comment explaining why it's intentionally unlocked.

3. **`SimulatorDataSource.add_ticker`/`remove_ticker` mutate simulator state from the request thread** while `_run_loop` steps the simulation in the event loop. Both run on the same event loop today so it's safe, but `GBMSimulator` is not itself synchronized and `_rebuild_cholesky` reallocates `_tickers`/`_cholesky` mid-flight. Worth a comment recording the single-event-loop assumption, since the plan explicitly anticipates future multi-user scenarios.

4. **`MARKET_DATA_SUMMARY.md` reports `massive_client.py` at 56% coverage** ("expected — API methods mocked"). Given §6 depends on the Massive path for real data and 3.5 above flags the polling shape as unresolved, this is the least-verified module in the system. Fine for now; note it as known risk rather than as accepted.

---

## 6. Testing Strategy Gaps (§12)

- **No CI wiring.** `.github/workflows/` contains only `claude.yml` and `claude-code-review.yml` — no test workflow. §12 defines a good test strategy with nothing running it on push.
- **No coverage threshold.** The market layer landed at 84%; without a floor in `pyproject.toml`, the next agent has no target.
- **Frontend framework is left open.** "React Testing Library or similar" — the "or similar" is a decision that affects `package.json`, the Dockerfile, and CI. Decide it in the plan.
- **The SSE resilience E2E test needs a method.** "Disconnect and verify reconnection" is genuinely awkward with `EventSource` under Playwright; it needs route interception/abort. Name the technique or the test agent will improvise or skip it.
- **Untested paths that matter most are the ones in §2 above:** trade with insufficient cash *via the LLM path*, removing a held ticker, restart-with-open-positions, and adding an unknown ticker. Each is a specified-behavior gap first and a test gap second.

---

## 7. Prioritized Recommendations

**Do before any further agent work (these unblock or misdirect everyone downstream):**
1. Add full request/response JSON schemas + error shapes to §8 (2.1).
2. Resolve daily-change-% — pick session-anchor or drop it (2.2).
3. Decide the main chart's history source (2.3).
4. Specify ticker validation and the unknown-ticker price rule (2.5).
5. Specify watchlist-vs-positions tracking union (2.6).
6. Fix the eight documentation defects in §4 — especially the skill name, `.env.example`, and the SSE payload shape.

**Do before the backend agent starts:**
7. Price persistence across restart (2.4).
8. Trade atomicity + validation rules (3.1); SQLite WAL/concurrency (3.2).
9. Snapshot retention + history pagination (3.3).
10. Define the `LLM_MOCK` contract (3.8) and LLM timeout/failure behavior (3.7).

**Do before deployment is attempted:**
11. Resolve the volume-mount contradiction (§4/§11, defect 5).
12. Add the no-auth deployment warning (3.9).

**Cleanup:**
13. `stream.py` router scoping; `PriceCache.version` locking (5.1, 5.2).
14. CI workflow + coverage floor; decide the frontend test framework (§6 above).

---

## 8. Summary

The plan's architecture is sound and its scoping is disciplined — the completed market data layer is good evidence that this spec style produces good code. The weakness is not judgment but **specificity in the places where two agents must agree**: API schemas, error behavior, and the handful of state-consistency rules (price persistence, ticker validation, watchlist-vs-positions) that no single agent owns. Those gaps are invisible while one agent works alone and become integration failures the moment two do.

The single highest-leverage change is §8: turn the endpoint table into a real contract with example payloads. Nearly half the issues above are downstream of its absence.
