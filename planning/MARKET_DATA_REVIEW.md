# Market Data Backend — Code Review

**Date:** 2026-08-18
**Scope:** `backend/app/market/` (9 source files) and `backend/tests/market/` (9 test files)
**Reviewer environment:** `uv` installed fresh, `uv sync --extra dev` pulled real `massive==2.2.0`, `numpy==2.4.2`, `fastapi==0.128.7`, etc. (not the versions implied by `planning/MARKET_DATA_DESIGN.md`, which matters — see §3.1).

This supersedes `planning/archive/MARKET_DATA_REVIEW.md` (2026-02-10). All 7 issues from that review have been fixed (build config, `get_tickers()` encapsulation, SSE return type, module-level router, unused imports, Massive test fragility). This pass re-reviewed the code fresh and ran everything against real dependencies rather than trusting the prior writeup's conclusions.

---

## 1. Test Results

**99 tests collected, 99 passed, 0 failed.** (Up from 73 in the prior review — new `test_interface.py`, `test_seed_prices.py`, and `test_stream.py` modules have been added.)

```
uv run --extra dev pytest -v --cov=app --cov-report=term-missing
...
99 passed in 4.33s
```

**Coverage: 97% overall** (up from 84%):

| Module | Coverage | Missing |
|---|---|---|
| models.py | 100% | |
| cache.py | 100% | |
| interface.py | 100% | |
| seed_prices.py | 100% | |
| factory.py | 100% | |
| simulator.py | 98% | `_add_ticker_internal` duplicate-guard, `_run_loop` exception branch (see §3.3) |
| massive_client.py | 94% | `_poll_loop`'s own sleep-loop wrapper, `_fetch_snapshots` body (mocked in every test) |
| stream.py | 92% | `StreamingResponse(...)` construction line, `CancelledError` branch |

**Lint:** `ruff check app/ tests/` — clean, no violations.

**Format:** `ruff format --check` — 5 test files would be reformatted (`test_models.py`, `test_seed_prices.py`, `test_simulator.py`, `test_simulator_source.py`, `test_stream.py`). Trivial, not caught by `ruff check` since these are whitespace-only diffs, not lint rule violations.

---

## 2. Architecture Assessment

The strategy-pattern design holds up well:

```
MarketDataSource (ABC)
├── SimulatorDataSource  (GBM simulator)
└── MassiveDataSource    (Polygon.io REST poller)
        │
        ▼
   PriceCache (shared, thread-safe)
        │
        ▼
   SSE stream → Frontend
```

Confirmed by direct testing, not just inspection:
- GBM prices stay strictly positive over 10,000 steps (existing test, verified).
- Cholesky decomposition succeeds for the **full realistic 10-ticker default watchlist** (7 tech + 2 finance + TSLA) — verified manually since no test exercises this combination (see §3.2).
- Cholesky also succeeds under stress with 110 tickers (10 seeded + 100 dynamically-added unknowns) — no `LinAlgError`, correlation structure is safely PSD across the sizes this app will ever see.
- All previously-fixed issues stayed fixed: `pyproject.toml` has `[tool.hatch.build.targets.wheel] packages = ["app"]`, `massive_client.py` imports `RESTClient`/`SnapshotMarketType` at module level, `GBMSimulator.get_tickers()` is public, `stream.py`'s `_generate_events` is correctly typed `AsyncGenerator[str, None]`, and `create_stream_router()` builds a fresh `APIRouter` per call instead of reusing a module-level singleton.

---

## 3. Issues Found

### 3.1 Massive API integration is broken against the real SDK (Severity: **High**)

`massive_client.py:103`:

```python
price = snap.last_trade.price
timestamp = snap.last_trade.timestamp / 1000.0
```

The installed `massive` package (`v2.2.0`, resolved from the `>=1.0.0` constraint in `pyproject.toml`) has **no `timestamp` attribute on `LastTrade`**. The actual dataclass (`massive/rest/models/trades.py`) exposes `sip_timestamp`, `participant_timestamp`, and `trf_timestamp` — never a bare `timestamp` — and those fields are **Unix nanoseconds**, not milliseconds.

Reproduced end-to-end against the real SDK model (not a mock):

```python
from massive.rest.models.snapshot import TickerSnapshot
snap = TickerSnapshot.from_dict({"ticker": "AAPL", "lastTrade": {"p": 190.50, "t": 1707580800123456789}})
# ... source._poll_once() with this snapshot ...
```
```
Skipping snapshot for AAPL: 'LastTrade' object has no attribute 'timestamp'
cache price for AAPL: None
```

**Impact:** with a valid `MASSIVE_API_KEY` and a successful API call, `_poll_once` hits `AttributeError` on *every* snapshot, every poll cycle, forever. The existing `except (AttributeError, TypeError)` handler swallows it and logs a per-ticker warning, so the app never crashes — it just silently never populates the cache with real prices. The SSE connection stays "connected" in the UI (per §13.3 of the design doc, this is meant to describe an *invalid key* scenario) but with an empty/stale feed even when the key is perfectly valid and the network call succeeds. This is worse than the documented failure mode because there's no operational signal beyond a repeating log warning — a user pointing FinAlly at real data would see a blank watchlist and have no obvious reason why.

**Why the test suite didn't catch this:** every test in `test_massive.py` builds its fake snapshot with `MagicMock()` (`snap.last_trade.timestamp = timestamp_ms`), and `MagicMock` auto-vivifies any attribute you assign or access — so the mock happily has a `.timestamp` attribute that the real `LastTrade` dataclass does not. The tests validate the *code's own logic* (skip-on-error, timestamp division, cache writes) but never validate that the attribute names asserted against actually exist on the real SDK response shape. This is a textbook mock/reality divergence.

**Fix:** use `snap.last_trade.sip_timestamp` (or `participant_timestamp`, whichever semantic is preferred) and divide by `1_000_000_000.0` (nanoseconds → seconds), not `1000.0`. Also worth adding one test that constructs a real `TickerSnapshot`/`LastTrade` via `from_dict()` (as done for this repro) rather than a bare `MagicMock`, so a future SDK-shape mismatch fails a test instead of failing silently in production.

### 3.2 No test exercises the full default 10-ticker watchlist (Severity: Medium)

`test_simulator.py` only ever constructs `GBMSimulator` with 1–2 tickers. The production correlation structure (tech intra=0.6, finance intra=0.5, TSLA=0.3, cross=0.3) is a non-trivial block matrix, not simple equicorrelation — it's the kind of structure where Cholesky decomposition can fail to be positive-semidefinite for the wrong parameter combination. It happens to work (verified manually in this review), but nothing in the test suite would catch it if a future correlation constant change broke that invariant. This was already flagged in the prior archived review (§4.2) and is still open.

**Fix:** add a test that builds `GBMSimulator(tickers=list(SEED_PRICES.keys()))` and asserts `step()` succeeds without raising, covering the real production shape.

### 3.3 `_run_loop` exception-handling branch is untested (Severity: Low)

`simulator.py:271-272` (the `except Exception: logger.exception(...)` in `SimulatorDataSource._run_loop`) is never exercised — confirmed by the coverage report and by reading `test_simulator_source.py::test_exception_resilience`, which despite its name never injects a failure. It just asserts the background task is still running after a normal sleep. The resilience the design doc claims for this loop (a bad tick can't kill the feed) is real code but not verified by any test.

**Fix:** patch `GBMSimulator.step` (or the cache's `update`) to raise once, then assert the task survives and continues writing to the cache on the next tick.

### 3.4 `MassiveDataSource` ticker-case inconsistency between write paths (Severity: Low)

`start()`, `add_ticker()`, and `remove_ticker()` all normalize tickers via `.upper().strip()` before touching `self._tickers` or the cache. `_poll_once()` does not: it writes `self._cache.update(ticker=snap.ticker, ...)` using whatever casing the API response returns, unnormalized. In practice Polygon/Massive symbols are already uppercase, so this is low real-world risk, but it's an inconsistency in the codebase's own normalization discipline — if the API ever returned a differently-cased ticker, it would create a second cache entry rather than updating the existing one, silently splitting a ticker's price history in two.

### 3.5 Pre-start asymmetry between the two `MarketDataSource` implementations (Severity: Trivial)

`SimulatorDataSource.add_ticker()`/`remove_ticker()` are silent no-ops if called before `start()` (`self._sim` is `None`, guarded by `if self._sim:` with no else/log). `MassiveDataSource.add_ticker()` has no such guard — it will happily append to `self._tickers` even pre-`start()`. Neither behavior is wrong per the ABC's documented contract (`start()` must be called first), but the two implementations diverge in what happens if a caller violates that contract, which could produce different debugging experiences depending on which source is active. Worth a one-line note in `interface.py` or aligning the guard, not urgent.

### 3.6 `PriceCache.version` still reads outside the lock (Severity: Trivial, carried over)

Unchanged from the prior review (§3.4 there): `version` is a plain property read without `self._lock`. Harmless under CPython's GIL for a single `int` read; the design doc (§13.4) explicitly accepts this tradeoff at the project's target scale. Not a regression, just noting it's still the case and still fine.

### 3.7 No concurrent-writer test for `PriceCache` (Severity: Trivial, carried over)

Also unchanged from the prior review (§4.2 there). The lock usage reads correctly by inspection and there's no evidence of an actual bug, but a multi-thread stress test would give empirical confidence rather than relying on code review alone — relevant because the Massive path's synchronous SDK calls run via `asyncio.to_thread`, i.e. a real OS thread, while the simulator and SSE reader operate on the event loop.

---

## 4. What's Solid

- **Strategy pattern is clean** — `PriceCache` genuinely decouples both producers from all consumers; nothing downstream branches on which source is active.
- **GBM math is correct and numerically stable** — verified prices stay positive over 10k steps, per-tick moves are appropriately small given the `dt` scale, and the exponential formulation can't underflow to zero or go negative.
- **Correlated moves work in practice**, not just in theory — manually confirmed against both the real 10-ticker production set and a 110-ticker stress case.
- **All 7 issues from the prior (archived) review are genuinely fixed**, not just marked fixed — verified each one directly in the current source rather than trusting the summary doc.
- **Defensive error handling is real** — a malformed snapshot for one ticker doesn't take down the batch; an API failure doesn't crash the poll loop; `stop()` is idempotent on both implementations.
- **97% coverage, 99/99 tests green, lint clean.** The test suite is broad and mostly well-targeted — it just has one dangerous blind spot (§3.1) common to any test suite that mocks a third-party SDK's response shape instead of constructing it.

---

## 5. Verdict

The market data subsystem is well-architected and the simulator path (the default, no-API-key mode most users and all E2E tests will exercise) is solid, correct, and thoroughly tested. **The Massive/real-data path is currently non-functional** due to §3.1 — this is the one finding that should block calling the Massive integration "done," since it means the `MASSIVE_API_KEY` feature described in `PLAN.md` §5–§6 doesn't actually work today despite 100% of its tests passing.

**Must fix before considering Massive integration complete:**
1. §3.1 — `snap.last_trade.timestamp` → `snap.last_trade.sip_timestamp`, and the unit conversion from nanoseconds (`/ 1_000_000_000.0`) instead of milliseconds (`/ 1000.0`). Add at least one test built from a real `TickerSnapshot.from_dict()` payload, not a `MagicMock`, to guard against this class of regression.

**Should fix:**
2. §3.2 — add a test covering the full default 10-ticker `SEED_PRICES` set through `GBMSimulator`.
3. §3.3 — make `test_exception_resilience` actually inject a failure.
4. §3.4 — normalize ticker casing in `_poll_once()` to match the other write paths.

**Nice to have:**
5. §3.5 — align pre-start behavior between `SimulatorDataSource` and `MassiveDataSource`, or document the difference.
6. Run `ruff format` on the 5 flagged test files.
7. §3.7 — a concurrent-writer stress test for `PriceCache`, for empirical (not just inspection-based) confidence.

Simulator-only deployments (the default path with no `MASSIVE_API_KEY`) can proceed without blocking on this review. Anything depending on real market data via Massive should not be considered ready until §3.1 is fixed.
