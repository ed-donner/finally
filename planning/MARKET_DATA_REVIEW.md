# Market Data Backend — Code Review

**Reviewer:** Claude (Opus 4.8) · **Date:** 2026-08-08
**Scope:** `backend/market/`, `backend/config.py`, `backend/main.py`, `backend/db/`, and `backend/tests/`
**Reference specs:** `planning/PLAN.md`, `planning/MARKET_DATA_SUMMARY.md`, `planning/archive/*`

## Verdict

The market data backend is **well-designed, cleanly factored, and production-shaped for its scope.** It faithfully implements the two-source-one-interface architecture from the plan: a GBM simulator and a Massive REST client behind a common `MarketDataSource` ABC, a lock-guarded in-memory cache, a single background writer, and an SSE endpoint. The code is readable, well-commented (comments explain *why*, not *what*), and the module boundaries match the plan's contract.

**Tests: all 32 pass** (`uv run pytest`, Python 3.13.7, pytest 9.1.1, 1.83s). They are deterministic, seeded, and mock the network (`httpx.MockTransport`) — no API key or connectivity required, exactly as the summary claims.

There are **no critical/blocking defects.** I found two real robustness bugs (one confirmed to crash startup, one confirmed to silently freeze the price feed), a handful of minor hardening gaps, and some test-coverage holes. Details and severities below.

---

## Findings

### Medium

**M1 — Background feed task dies permanently if `load_active_tickers()` raises; the whole stream freezes with no recovery.**
`backend/market/feed.py:52-78`. `_tick_once()` wraps `source.get_prices()` in `try/except`, but the preceding `await asyncio.to_thread(self._get_active_tickers)` is unguarded. If the DB read raises (SQLite locked/busy, disk error, corrupted file), the exception propagates out of `_tick_once()` into `_run()`'s `while True` loop, which has no error boundary. The task terminates and is never restarted, so **live prices stop updating for the entire remaining lifetime of the process** — the only signal is asyncio's "Task exception was never retrieved" logged on GC. This directly contradicts the summary's resilience claim ("Source failures … never raise into the feed loop"): DB failures do.

Confirmed empirically — injecting a raising `get_active_tickers` kills `feed._task` (`task done? True`, `RuntimeError('db locked')`).

*Fix:* wrap the body of `_run()`'s loop in `try/except Exception: log.exception(...)` so a transient error skips one cycle instead of killing the feed, and/or move the `to_thread` call inside `_tick_once`'s existing try. (`prime()` failing at startup is acceptable fail-fast; the runtime loop dying is not.)

**M2 — `load_settings()` crashes at import if a numeric env var is present but empty.**
`backend/config.py:31,33`. `float(_clean("MASSIVE_POLL_SECONDS", "15"))` and the `SSE_PUSH_SECONDS` line use `os.getenv(name, default)`, which returns `""` (not the default) when the variable is *set but blank*. `float("")` raises `ValueError`, and because `settings = load_settings()` runs at module import, **the entire app fails to boot.** `SIM_SEED` is correctly guarded (`if _clean(...)`); the two floats are not.

Confirmed empirically — `MASSIVE_POLL_SECONDS="" uv run python -c "from config import load_settings; load_settings()"` raises `ValueError: could not convert string to float: ''`.

This is an easy trap: a user copies `.env.example`, sees `MASSIVE_POLL_SECONDS=15`, and blanks it out expecting "use the default." Instead the container won't start.

*Fix:* have `_clean` fall back to the default when the value strips to empty, or parse with a helper that treats `""` as unset (e.g. `float(_clean(...) or "15")`).

### Low

**L1 — Unbounded `days` on `GET /api/history/{ticker}`.**
`backend/market/routes.py:60-63`. `days` is an unvalidated `int`. For the simulator, `days=10_000_000` loops that many times synchronously in `SimEngine.history()`, blocking the event loop (a self-inflicted DoS). For Massive it's capped by `limit=5000`, but negative values produce odd slicing (`results[-days:]` with `days<0`). Clamp to a sane range, e.g. `days: int = Query(90, ge=1, le=365)`.

**L2 — Arbitrary tickers grow the simulator's state unboundedly.**
`backend/market/gbm.py:29-39` (`_ensure`). Every distinct ticker ever requested (via `/api/history/{ticker}` or the watchlist) is lazily seeded and retained in `self._prices`/`self._seeds` forever. In a single-user local app this is negligible, but any endpoint that accepts a free-form ticker lets memory grow without bound. Worth a cap or an allow-list check if ticker input is ever exposed more widely.

**L3 — `ticker` path segment is uppercased but not otherwise validated.**
`backend/market/routes.py:63` / `backend/market/massive.py:57`. `ticker.upper()` is interpolated into the Massive request path. FastAPI's default path param won't match `/`, so this is low-risk, but URL-encoded characters could still perturb the outbound path. A simple `^[A-Z.]{1,10}$` guard (also rejecting junk before it hits the simulator's state, see L2) would be cheap insurance.

**L4 — Spec deviation: `/api/history/{ticker}` isn't in PLAN §8's endpoint table.**
The endpoint is a reasonable and necessary addition (the frontend's "Main chart area" needs price-over-time), and it's documented in `MARKET_DATA_SUMMARY.md`. But PLAN.md §8 lists no market-history endpoint (only `/api/portfolio/history` for portfolio value). Recommend adding a row to PLAN §8 so the contract stays the single source of truth for the frontend agent, and to avoid confusion with the portfolio-value history endpoint.

### Nits / Observations (non-blocking)

- **`_initialized_paths` guard skips re-seeding a deleted DB at runtime** (`db/connection.py:44,71`). Because schema uses `IF NOT EXISTS` and seeding is `COUNT(*)==0`-gated, re-init is idempotent anyway, so the guard is a pure optimization — fine, just noting the runtime-deletion edge case won't reseed until restart.
- **`Bar.low` serializes as `"l"` only via manual mapping** (`routes.py:68`, `massive.py:74`). Any future code that does `asdict(bar)` would emit `"low"`, not the `"l"` the frontend charts expect. The manual map is correct today; a `@property l` or a shared serializer would prevent drift.
- **SSE broadcasts the full cache to every client**, not a per-client watchlist. Correct and documented for the single-user model; flagged only so the multi-user future path is a conscious change.
- **Massive `price = last or day or prev`** treats a genuine `0.0` last-trade as falsy and falls through. Harmless for equities (price is never 0), just noting the idiom.
- **Per-ticker lock acquisition in `_tick_once`** calls `cache.get()` in a loop, each taking the lock separately. Negligible at 10 tickers; a single `snapshot()` before the loop would halve lock traffic if the watchlist ever grows large.

---

## Test Coverage Assessment

**Strong** on the pure logic: GBM determinism, price positivity over 10k steps, sector correlation, synthetic-history invariants (OHLC ordering, daily spacing, last-close-matches-live), cache copy semantics, feed direction/no-op/exception-survival/lifecycle, Massive parsing + all three error paths (429/401/transport) for both prices and history, factory selection, and DB union logic.

**Gaps worth closing:**

1. **No test exercises the SSE endpoint itself** (`routes.py:_price_events`) — the dedup-by-timestamp logic, heartbeat cadence, and disconnect handling are entirely untested. A `TestClient` streaming test (or a direct async-generator drive) would cover the most user-facing, hardest-to-eyeball code in the module.
2. **No test for `config.load_settings()`** — would have caught M2. Add cases for absent, present-and-valid, and present-but-empty numeric vars.
3. **No test for feed resilience to a DB/`get_active_tickers` error** — would have caught M1. The existing `test_feed_survives_source_exception` only covers `get_prices` raising.
4. **Massive `get_history` request construction is unverified** — the date-range/padding math (`days*1.5+5`) and `results[-days:]` slicing aren't asserted against the outbound request.
5. **`history` with default `days=90`** isn't exercised (tests always pass small explicit values).

---

## What's Done Well

- Clean ABC seam (`source.py`) with a no-op `aclose()` default; both sources conform and the factory is trivial and tested.
- Defensive Massive client: every network path degrades to `{}`/`[]` and logs once, so rate limits and bad keys never crash the feed (as designed).
- Synthetic history walks GBM *backwards* from the live price so the chart's last close matches the stream — a genuinely thoughtful touch, and tested.
- `feed.prime()` eliminates the empty-cache race on first request; verified by the summary's contract and the feed tests.
- Correlated GBM (market + sector + idiosyncratic factors summing to ~unit variance) is a nice bit of realism, and the correlation is actually asserted in tests.
- Env handling is centralized in `config.py` (the one place vars are read), `.env.example` is complete and matches the code, and `.gitignore` correctly keeps `db/.gitkeep` while ignoring `db/*.db`.

## Recommended Priority

1. Fix **M1** (feed loop error boundary) and **M2** (config empty-string floats) — both are small changes that prevent silent/total failure.
2. Add the three regression tests that would have caught M1, M2, and cover the SSE generator (coverage gaps 1–3).
3. Address **L1/L3** (validate `days` and `ticker`) — cheap input hardening.
4. Reconcile the `/api/history/{ticker}` endpoint into PLAN §8 (**L4**).
