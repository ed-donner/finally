# Market Data Backend — Code Review

**Date:** 2026-02-10
**Scope:** `backend/app/market/` (8 source files) and `backend/tests/market/` (6 test files)

---

## 1. Test Results Summary

**73 tests collected, 68 passed, 5 failed.**

All failures are in `test_massive.py` and stem from the same root cause: the `massive` package is not installed in the test environment, so `patch("app.market.massive_client.RESTClient")` fails with `AttributeError` because the module-level name `RESTClient` was never imported (it is lazy-imported inside methods). This is an environment issue, not a logic bug — the tests are correctly structured but require the `massive` package to be available (or `create=True` on the patch) so that the mock target exists.

Failing tests:
- `test_poll_updates_cache` — `asyncio.to_thread` fails because `_fetch_snapshots` is not properly mocked when `massive` is absent
- `test_malformed_snapshot_skipped` — same cause
- `test_timestamp_conversion` — same cause
- `test_stop_cancels_task` — `patch("app.market.massive_client.RESTClient")` fails because the name doesn't exist at module level
- `test_start_immediate_poll` — same as above

The underlying `_poll_once()` logic itself is correct. The 3 tests that mock `source._fetch_snapshots` directly fail because `asyncio.to_thread(self._fetch_snapshots)` calls the real method which tries to import `massive`. The 2 tests that use `patch("app.market.massive_client.RESTClient")` fail because the name doesn't exist in the module's namespace (lazy import). Both issues resolve when the `massive` package is installed.

**Lint (ruff):** Source code passes clean. Tests have 5 unused-import warnings (`pytest`, `math`, `asyncio` imported but not used in some test files).

**Coverage:** 84% overall.
| Module | Coverage | Notes |
|---|---|---|
| models.py | 100% | |
| cache.py | 100% | |
| interface.py | 100% | |
| seed_prices.py | 100% | |
| factory.py | 100% | |
| simulator.py | 98% | Uncovered: `_add_ticker_internal` duplicate guard (L145), exception log in `_run_loop` (L264-265) |
| massive_client.py | 56% | Expected — real API methods can't run without the massive package |
| stream.py | 31% | Expected — SSE generator requires a running ASGI server to test |

---

## 2. Architecture Assessment

The market data subsystem is well-designed. It follows a clean strategy pattern:

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

**Strengths:**
- Clear separation of concerns across 8 focused modules
- Factory pattern with lazy imports — the `massive` package is only needed when `MASSIVE_API_KEY` is set
- PriceCache as the single point of truth decouples producers from consumers
- Immutable `PriceUpdate` dataclass with `frozen=True, slots=True` is correct and efficient
- The GBM math is proper: log-normal price paths via `exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)`
- Correlated moves via Cholesky decomposition are a nice touch for realism
- All background tasks are properly cancellable and idempotent on stop()

---

## 3. Issues Found

### 3.1 Build Configuration Bug (Severity: High)

`pyproject.toml` is missing the hatchling package discovery configuration. Running `uv sync` fails:

```
ValueError: Unable to determine which files to ship inside the wheel
```

**Fix:** Add to `pyproject.toml`:
```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

This will block Docker builds and any fresh `uv sync` until fixed.

### 3.2 Massive Test Fragility (Severity: Medium)

Five tests in `test_massive.py` fail when the `massive` package is not installed. The root cause is twofold:

1. **`_poll_once` uses `asyncio.to_thread(self._fetch_snapshots)`** — even when `_fetch_snapshots` is patched on the instance, `to_thread` runs it in a thread executor. Three tests mock `_fetch_snapshots` as a `MagicMock` (synchronous), but `asyncio.to_thread` wraps it in `loop.run_in_executor`, which works... except that when `_fetch_snapshots` is NOT patched, the real method tries `from massive.rest.models import SnapshotMarketType` and fails.

2. **`patch("app.market.massive_client.RESTClient")`** targets a name that doesn't exist at module level because `massive_client.py` uses a lazy import inside `start()`. The patch needs `create=True` or the import needs to be at module level behind a `TYPE_CHECKING` guard.

These tests pass when `massive>=1.0.0` is installed (as `pyproject.toml` declares it as a core dependency), so this is technically a test-environment issue, not a code bug. However, since the whole point of lazy imports is to make `massive` optional for simulator-only use, the tests should also work without it.

### 3.3 `_generate_events` Return Type Annotation (Severity: Low)

`stream.py:54` declares the return type as `-> None` but the function is an async generator (it uses `yield`). The correct annotation would be `-> AsyncGenerator[str, None]` or simply removing the annotation. This doesn't cause runtime issues but is misleading for type checkers and developers.

### 3.4 `version` Property Not Under Lock (Severity: Low)

`PriceCache.version` reads `self._version` without acquiring `self._lock`:

```python
@property
def version(self) -> int:
    return self._version
```

On CPython with the GIL, reading a single `int` is atomic, so this won't cause corruption. However, it's inconsistent with the rest of the class, and if the project ever runs on a no-GIL Python build (PEP 703, Python 3.13t+), this could become a race. A minor concern given the current context.

### 3.5 `SimulatorDataSource.get_tickers` Accesses Private State (Severity: Low)

`simulator.py:254`:
```python
def get_tickers(self) -> list[str]:
    return list(self._sim._tickers) if self._sim else []
```

This reaches into `GBMSimulator._tickers` (private attribute). `GBMSimulator` should expose a `get_tickers()` method or a `tickers` property to keep the boundary clean.

### 3.6 Module-Level Router Instance (Severity: Low)

`stream.py:16` creates a module-level `router` object, and `create_stream_router()` registers a route on it via closure. If `create_stream_router` were called twice (e.g., in tests), the `/prices` route would be registered twice on the same router. In practice this won't happen because the function is called once during app startup, but it's a latent footgun for testing.

### 3.7 Unused Imports in Tests (Severity: Trivial)

Five lint warnings from `ruff`:
- `test_cache.py`: unused `pytest`
- `test_factory.py`: unused `pytest`
- `test_massive.py`: unused `asyncio`
- `test_simulator.py`: unused `math`, unused `pytest`

---

## 4. Design Observations

### 4.1 Things Done Well

- **GBM parameter tuning is thoughtful.** TSLA at sigma=0.50 vs V at 0.17 reflects real-world volatility differences. The shock event system (~0.1% per tick, producing visible moves every ~50s) adds visual drama without destabilizing prices.
- **Cholesky decomposition for correlated moves** is the mathematically correct approach. The sector-based correlation structure (tech 0.6, finance 0.5, cross 0.3) is reasonable.
- **Defensive error handling in both data sources.** Both `_run_loop` (simulator) and `_poll_once`/`_poll_loop` (massive) catch exceptions and continue, which is essential for a long-running background service.
- **SSE implementation is clean.** The version-based change detection avoids sending redundant payloads. The `retry: 1000\n\n` directive ensures browser auto-reconnect. Nginx buffering is proactively disabled.
- **Seed prices in the cache at start** means the frontend gets data on the first SSE poll, with no visible delay.
- **Thread-safe cache with Lock** is the right choice since the Massive client runs API calls via `asyncio.to_thread`.

### 4.2 Missing Tests

- **SSE streaming (`stream.py`)** at 31% coverage has no dedicated tests. Testing SSE requires an ASGI test client (e.g., `httpx.AsyncClient` with `app`). Given that this is the primary consumer of PriceCache, even a basic integration test would add confidence.
- **No concurrent/thread-safety test for PriceCache.** The lock usage looks correct from inspection, but a test with multiple threads writing simultaneously would verify it empirically.
- **No test for `GBMSimulator` with all 10 default tickers.** Tests use 1-2 tickers. A test confirming the Cholesky decomposition succeeds for the full 10-ticker default set would catch correlation matrix issues.

### 4.3 Potential Future Considerations

- The `PriceCache` doesn't cap history; it only stores the latest price per ticker, so memory is bounded at O(tickers). Good.
- The `DEFAULT_CORR` constant (0.3, `seed_prices.py:48`) is defined but never referenced in `_pairwise_correlation`. The static method returns `CROSS_GROUP_CORR` (also 0.3) as the fallback. This is semantically confusing — `DEFAULT_CORR` seems intended for tickers not in any group, but the code returns `CROSS_GROUP_CORR` for all non-matched pairs. Both happen to be 0.3, so behavior is correct, but the naming is misleading.

---

## 5. Verdict

The market data backend is solid and well-structured. The GBM simulator, price cache, abstract interface, factory pattern, and SSE streaming all work correctly and follow good practices. The architecture will integrate cleanly with the rest of the application.

**Must fix before proceeding:**
1. Add `[tool.hatch.build.targets.wheel] packages = ["app"]` to `pyproject.toml` — without this, `uv sync` and Docker builds fail.

**Should fix:**
2. Make the Massive tests resilient to the `massive` package being absent (use `create=True` on patches, or restructure mocks).
3. Fix the `_generate_events` return type annotation.
4. Remove unused imports in test files.

**Nice to have:**
5. Add a `get_tickers()` public method to `GBMSimulator`.
6. Add at least one SSE integration test.
7. Clarify `DEFAULT_CORR` vs `CROSS_GROUP_CORR` naming.
