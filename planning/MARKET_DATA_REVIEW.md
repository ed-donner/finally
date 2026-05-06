# Market Data Subsystem — Code Review

**Date:** 2026-05-05  
**Reviewer:** Claude Sonnet 4.6  
**Scope:** `backend/app/market/` (8 modules) + `backend/tests/market/` (6 test modules)

---

## Test Results

**73/73 tests passing. Lint: clean (ruff, zero violations).**

```
platform linux — Python 3.13.7
73 passed in 1.75s
```

All tests run fast (no slow I/O; async tests use real event loops with short sleeps).

---

## Coverage

| Module | Stmts | Miss | Cover | Uncovered Lines |
|--------|-------|------|-------|-----------------|
| `models.py` | 26 | 0 | **100%** | — |
| `cache.py` | 39 | 0 | **100%** | — |
| `factory.py` | 15 | 0 | **100%** | — |
| `interface.py` | 13 | 0 | **100%** | — |
| `seed_prices.py` | 8 | 0 | **100%** | — |
| `massive_client.py` | 67 | 4 | 94% | 85–87 (`_poll_loop` body), 125 (`_fetch_snapshots` body) |
| `simulator.py` | 139 | 3 | 98% | 149 (duplicate guard in `_add_ticker_internal`), 268–269 (exception handler in `_run_loop`) |
| `stream.py` | 36 | 24 | **33%** | 26–48 (endpoint), 62–87 (`_generate_events` generator) |
| **TOTAL** | **349** | **31** | **91%** | |

The overall 91% is strong. The `stream.py` 33% is the only material gap.

---

## Findings

### Critical

*None. The subsystem is correct and safe for its current scope.*

---

### Significant

#### 1. `stream.py` has no tests (33% coverage)

The SSE streaming endpoint — the most user-visible output of this entire subsystem — has zero test coverage. Lines 26–48 (the FastAPI route and `StreamingResponse` construction) and 62–87 (the `_generate_events` async generator) are entirely untested.

This matters because:
- The generator's disconnect detection (`request.is_disconnected()`) and version-diffing logic have edge cases that are hard to reason about without tests.
- The `retry: 1000\n\n` preamble, JSON serialisation of `to_dict()`, and event framing (`data: ...\n\n`) are all produced here and consumed directly by the frontend.

**Recommendation:** Add at least three tests using `httpx.AsyncClient` + `starlette.testclient`:
1. A connected client receives a well-formed SSE event when the cache has data.
2. Events are only emitted when the version changes (no-change polling is silent).
3. The generator exits cleanly when the client disconnects.

#### 2. `create_stream_router()` mutates a module-level router

In `stream.py`:

```python
# Line 17 — module-level singleton
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/prices")          # ← decorates the module-level router
    async def stream_prices(...):
        ...
    return router
```

Each call to `create_stream_router()` registers a new handler on the *same* `router` instance. If called twice (e.g., in test setup), duplicate routes are silently added. FastAPI will serve only the first registered handler, which will have a stale `price_cache` closure — a silent correctness bug.

**Fix:** Move `router` inside the factory function so each call produces an independent router:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    @router.get("/prices")
    async def stream_prices(...):
        ...
    return router
```

---

### Minor

#### 3. `version` property reads outside the lock

`PriceCache.version` (line 65) reads `self._version` without acquiring `self._lock`. The `update()` method increments `_version` inside the lock. Under CPython's GIL, integer reads are effectively atomic, so this won't corrupt data. However, it is inconsistent with the rest of the class and would be a race on a free-threaded Python build (PEP 703, available in 3.13+).

**Fix:** Either read `_version` inside the lock, or document the deliberate omission.

#### 4. `SimulatorDataSource.add_ticker` does not normalise case

`MassiveDataSource.add_ticker` upper-cases and strips the ticker (lines 67–68). `SimulatorDataSource.add_ticker` does not. If the app ever switches data sources at runtime or if watchlist entries arrive with mixed case, the two implementations will behave differently.

**Fix:** Add `ticker = ticker.upper().strip()` at the top of `SimulatorDataSource.add_ticker`.

#### 5. `start()` can be called twice without protection

The `MarketDataSource` interface documents "calling `start()` twice is undefined behavior." Neither implementation enforces this. `SimulatorDataSource.start()` would silently leak the first `asyncio.Task`; `MassiveDataSource.start()` would leak both the task and the `RESTClient`.

**Fix:** Add a guard at the top of each `start()`:
```python
if self._task is not None:
    raise RuntimeError("start() called more than once")
```

#### 6. Uncovered exception path in `simulator.py` `_run_loop` (lines 268–269)

The `except Exception: logger.exception(...)` block is never triggered in tests. The resilience test (`test_exception_resilience`) only checks that the task is still running after normal operation — it does not inject a failure into `GBMSimulator.step()`.

**Recommendation:** Patch `self._sim.step` to raise an exception in one test iteration, then verify the loop continues and logs the error.

---

## Architecture Assessment

The design is solid and well-suited for this project:

- **Strategy pattern** (`MarketDataSource` ABC) cleanly separates the two data sources. Downstream code is fully source-agnostic. The factory's env-var logic is simple and testable — all three factory edge cases (no key, empty key, whitespace key) are covered.
- **PriceCache as single source of truth** is the right call. A single background producer, multiple readers, no direct coupling between the simulator and the SSE layer. Thread-safety via a single `Lock` is appropriate given the low contention.
- **GBM with Cholesky-correlated moves** is mathematically correct. The formula `S(t+dt) = S(t) * exp((mu - σ²/2)·dt + σ·√dt·Z)` is standard Itô calculus. The `dt = 0.5 / 5,896,800 ≈ 8.48e-8` is correctly derived from 252 trading days × 6.5 hours × 3600 seconds. GBM guarantees prices remain positive (exp is always positive), which the test `test_prices_are_positive` with 10,000 iterations verifies convincingly.
- **Cholesky decomposition** is rebuilt on every `add_ticker`/`remove_ticker`. O(n²) build cost is negligible for n < 50. The correlation values (0.6 tech, 0.5 finance, 0.3 cross-sector, 0.3 TSLA) form a valid positive-definite matrix — `np.linalg.cholesky` would raise `LinAlgError` otherwise, and the tests indirectly verify this.
- **SSE over WebSockets** is the right call. One-way push, universal browser support, native `EventSource` retry built in. The version-based change detection in `_generate_events` avoids sending duplicate events when no prices have changed.
- **`asyncio.to_thread` for the synchronous Massive REST call** correctly avoids blocking the event loop. This is easy to miss and was done right.

---

## Summary

The market data subsystem is production-quality code. Tests are thorough, fast, and well-structured. The math is correct. The architecture is clean and extensible.

The two items that need attention before the subsystem is integrated with the rest of the app:

1. **Fix `create_stream_router` to not mutate a module-level router** (significant, easy fix).
2. **Add SSE streaming tests** (significant gap — the frontend depends entirely on this output).

The remaining minor issues (case normalisation asymmetry, `start()` double-call guard, version lock consistency) are low risk but worth addressing before the project ships.
