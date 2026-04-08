# Market Data Backend — Code Review

**Date:** 2026-04-08
**Reviewer:** Claude (Sonnet 4.6)
**Scope:** `backend/app/market/` (8 source modules) and `backend/tests/market/` (6 test modules)
**Environment:** Python 3.13.7, pytest-9.0.3, pytest-asyncio-1.3.0, pytest-cov-7.1.0

---

## 1. Test Results

**73 tests collected, 73 passed. All green.**

```
tests/market/test_cache.py             13 passed
tests/market/test_factory.py            7 passed
tests/market/test_massive.py           13 passed
tests/market/test_models.py            11 passed
tests/market/test_simulator.py         17 passed
tests/market/test_simulator_source.py  10 passed
```

This is an improvement over the earlier archived review (which recorded 5 failures in `test_massive.py`). Those failures have been resolved — tests now use `patch.object(source, "_fetch_snapshots", ...)` rather than patching the `RESTClient` class at module level, making them robust regardless of whether the `massive` package is installed.

**Lint (ruff):** All checks passed. No warnings in `app/` or `tests/`.

---

## 2. Coverage

```
Name                           Stmts   Miss  Cover   Missing lines
------------------------------------------------------------
app/__init__.py                    0      0   100%
app/market/__init__.py             6      0   100%
app/market/cache.py               39      0   100%
app/market/factory.py             15      0   100%
app/market/interface.py           13      0   100%
app/market/massive_client.py      67      4    94%   85-87, 125
app/market/models.py              26      0   100%
app/market/seed_prices.py          8      0   100%
app/market/simulator.py          139      3    98%   149, 268-269
app/market/stream.py              36     24    33%   26-48, 62-87
------------------------------------------------------------
TOTAL                            349     31    91%
```

**Overall: 91%.** The uncovered lines are all explainable:

- `massive_client.py:85-87` — the `_poll_loop` while-True body. Not covered because tests call `_poll_once` directly rather than letting the loop run. Acceptable: the loop logic is trivial (sleep + call `_poll_once`).
- `massive_client.py:125` — the actual `self._client.get_snapshot_all(...)` call inside `_fetch_snapshots`. Never reached because every test mocks `_fetch_snapshots` itself. Expected — this requires a real Polygon.io API key.
- `simulator.py:149` — the `if ticker in self._prices: return` guard inside `_add_ticker_internal`. Dead code in practice: `add_ticker` (line 122) already checks `if ticker in self._prices: return` before calling `_add_ticker_internal`. The inner guard can never be reached.
- `simulator.py:268-269` — the `logger.exception(...)` line inside the `except Exception` handler in `_run_loop`. Not covered because no test triggers an exception inside the simulation loop. Acceptable for a defensive handler.
- `stream.py:26-48, 62-87` — the entire SSE endpoint and generator. At 33%, this is the largest gap. No integration test exists for the SSE streaming path (requires a running ASGI server).

---

## 3. Architecture Assessment

The market data subsystem is clean and well-structured. It follows the strategy pattern correctly:

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, single point of truth)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation
        └──→ Trade execution
```

All eight modules have focused, single-responsibility designs with clean boundaries. The public API documented in `__init__.py` and `CLAUDE.md` is accurate and sufficient for downstream code.

---

## 4. Issues Found

### 4.1 Dead Code in `_add_ticker_internal` (Severity: Low)

`simulator.py:146-149`:

```python
def _add_ticker_internal(self, ticker: str) -> None:
    if ticker in self._prices:
        return  # <- dead code: outer add_ticker() already guards this
    ...
```

`add_ticker()` at line 122 already calls `if ticker in self._prices: return` before calling `_add_ticker_internal`. The inner guard is unreachable via the public API. It would only be hit if `_add_ticker_internal` were called directly with a duplicate (which the `__init__` loop does not do — each ticker in the initial list is distinct by assumption). This is harmless but adds noise and suppresses a coverage warning for the wrong reason.

**Fix:** Remove the guard from `_add_ticker_internal` and add a comment that this is an internal method assuming no duplicates, or keep it for defensive correctness and add `# pragma: no cover`.

### 4.2 `version` Property Not Under Lock (Severity: Low)

`cache.py:64-67`:

```python
@property
def version(self) -> int:
    return self._version  # No lock acquired
```

All other reads in `PriceCache` acquire `self._lock`. Reading `_version` without a lock is safe on CPython (the GIL makes single-object reads atomic), but Python 3.13 introduced free-threaded mode (PEP 703, `python3.13t`). On a no-GIL build, a concurrent `update()` could produce a torn read of `_version`. Since `PriceCache` is explicitly designed for concurrent use (the docstring calls it thread-safe), this is inconsistent.

**Fix:** Acquire the lock in the `version` property, or document the GIL assumption explicitly.

### 4.3 No SSE Integration Test (Severity: Medium)

`stream.py` is at 33% coverage. The SSE endpoint is the primary consumer of `PriceCache` and the real-time data delivery mechanism for the entire frontend. Yet it has no automated test. Testing it requires an ASGI test client (e.g., `httpx.AsyncClient` with the FastAPI app). Even a basic smoke test — connect, receive one event, verify JSON structure — would add meaningful confidence.

**Fix:** Add at least one test in a new `tests/market/test_stream.py` using `httpx.AsyncClient(app=app, base_url="http://test")` to verify:
- Response content-type is `text/event-stream`
- Events contain the expected JSON keys
- Generator stops on client disconnect

### 4.4 Module-Level Router Instance (Severity: Low)

`stream.py:17`:

```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])
```

`create_stream_router()` registers the `/prices` route on this module-level `router`. If the function were called twice (e.g., in multiple tests), the route would be registered twice on the same router object, producing duplicate route warnings or unexpected behavior.

In production this won't happen (called once at startup), but it is a latent test footgun. A cleaner design would create a fresh `APIRouter` inside `create_stream_router()` and return it.

### 4.5 `SimulatorDataSource` Not Started Before `add_ticker` (Severity: Low)

`simulator.py:242-249`:

```python
async def add_ticker(self, ticker: str) -> None:
    if self._sim:  # guarded
        self._sim.add_ticker(ticker)
        ...
```

If `add_ticker` is called before `start()`, the call is silently dropped (`self._sim` is `None`). The interface contract says nothing about this ordering requirement. A future caller could reasonably call `add_ticker` early and expect it to take effect once `start` is called. The same applies to `remove_ticker`.

This is low severity for now (the PLAN.md lifecycle is clear: call `start` first), but documenting this constraint in the method docstring would prevent future confusion.

### 4.6 Missing Test Scenarios (Severity: Low)

- **All 10 default tickers together**: Tests use 1-2 tickers. There is no test confirming that initializing `GBMSimulator` with all 10 default tickers produces a valid Cholesky decomposition and correct `step()` output. A malformed correlation matrix would raise a `LinAlgError` at runtime.
- **No thread-safety stress test for `PriceCache`**: The locking logic looks correct from inspection, but a test with concurrent writes from multiple threads would verify it empirically. This is especially relevant given the free-threaded Python concern in 4.2.
- **No test for `_poll_loop`**: The polling loop in `MassiveDataSource` is not exercised. Even a short integration test (start, wait one interval, stop) would cover lines 85-87.

---

## 5. Design Observations

### 5.1 Things Done Well

- **GBM math is correct.** `S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)` is the standard log-normal GBM discretization. Using `math.exp` (not approximation) is correct.
- **Cholesky-correlated moves** via `np.linalg.cholesky` is the right approach. The sector-based structure (tech 0.6, finance 0.5, cross-sector 0.3) is realistic and well-parameterized.
- **GBM parameters are thoughtful.** TSLA at sigma=0.50 vs V at 0.17 reflects real-world volatility differences. NVDA's higher mu (0.08) adds appropriate drift.
- **Immutable `PriceUpdate`** with `frozen=True, slots=True` is the correct design — it's safe to pass around without defensive copies and slightly more memory-efficient.
- **Seed cache on `start()`** ensures the SSE client gets data on the very first poll, avoiding a blank screen on fresh page load.
- **Both data sources handle errors defensively** — exceptions in the simulator loop and the Massive poll loop are caught and logged, keeping the background task alive through transient failures.
- **SSE implementation uses version-based change detection** to avoid redundant payloads. The `retry: 1000\n\n` directive enables browser auto-reconnect. The `X-Accel-Buffering: no` header is a nice operational touch for nginx proxying.
- **`asyncio.to_thread`** is the correct way to run the synchronous Massive `RESTClient` without blocking the event loop.
- **Factory pattern** keeps the source selection logic in one place and downstream code fully source-agnostic.

### 5.2 Minor Naming Observation

`TSLA_CORR = 0.3` and `CROSS_GROUP_CORR = 0.3` in `seed_prices.py` are both 0.3 and semantically distinct but numerically identical. Code is correct — `_pairwise_correlation` uses `TSLA_CORR` for TSLA pairs and `CROSS_GROUP_CORR` as the default fallback. The names accurately express the intent. No change needed, but worth noting that these happen to share the same value.

---

## 6. Verdict

The market data backend is production-quality for the course scope. The code is well-structured, mathematically sound, correctly concurrent, and fully lint-clean. All 73 tests pass.

**Must address before moving on:**
- Nothing is blocking. All previously reported critical issues (pyproject.toml build config, lazy imports, SSE return type, unused test imports, massive mock patches) have been resolved.

**Should address:**
1. Add at least one SSE integration test (`test_stream.py`) to bring `stream.py` out of 33% coverage.
2. Add a test for `GBMSimulator` initialized with all 10 default tickers (Cholesky with full set).

**Nice to have:**
3. Remove or `pragma: no cover` the dead guard in `_add_ticker_internal` (line 149).
4. Move `router = APIRouter(...)` inside `create_stream_router()` to eliminate the double-registration footgun.
5. Add a lock to the `version` property for free-threaded Python correctness.
6. Document the pre-condition that `start()` must be called before `add_ticker`/`remove_ticker`.

**The subsystem is ready for integration** with the rest of the backend (portfolio API, trade execution, watchlist management, LLM chat).
