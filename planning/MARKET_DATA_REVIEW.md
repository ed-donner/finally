# Market Data Backend — Code Review

**Reviewer:** Claude Opus 4.6
**Date:** 2026-03-17
**Scope:** `backend/app/market/` (8 modules, ~350 SLOC) and `backend/tests/market/` (6 test files, 73 tests)

---

## Test Results

| Metric | Value |
|---|---|
| Tests | **73 passed, 0 failed** |
| Overall coverage | **91%** |
| Linter (ruff) | **All checks passed** |

### Per-module coverage

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| models.py | 26 | 0 | 100% |
| cache.py | 39 | 0 | 100% |
| interface.py | 13 | 0 | 100% |
| seed_prices.py | 8 | 0 | 100% |
| factory.py | 15 | 0 | 100% |
| simulator.py | 139 | 3 | 98% |
| massive_client.py | 67 | 4 | 94% |
| stream.py | 36 | 24 | 33% |

The only real coverage gap is `stream.py` (33%) — the SSE generator is hard to unit-test because it requires a live `Request` object with `is_disconnected()`. This is acceptable; the SSE endpoint is a thin integration layer best verified by E2E tests.

---

## Architecture Assessment

The design is clean and well-structured:

- **Strategy pattern** — `MarketDataSource` ABC with two interchangeable implementations (`SimulatorDataSource`, `MassiveDataSource`), selected at runtime by `create_market_data_source()` based on an environment variable. All downstream code is source-agnostic.
- **Shared PriceCache** — Single point of truth, thread-safe, with a version counter that enables efficient SSE change detection (no unnecessary pushes).
- **Separation of concerns** — The GBM math engine (`GBMSimulator`) is a pure synchronous class; the async lifecycle wrapper (`SimulatorDataSource`) is separate. This makes the math testable without async machinery.
- **Factory pattern** — Environment-driven source selection with no code changes needed to switch between simulator and real data.

This is a solid, production-quality architecture for the scope of the project.

---

## Findings

### Issues (should fix)

**1. `SimulatorDataSource.add_ticker` lacks normalization (massive_client.py vs simulator.py inconsistency)**

`MassiveDataSource.add_ticker()` normalizes input with `ticker.upper().strip()` (line 67), but `SimulatorDataSource.add_ticker()` passes the ticker through as-is. If a user adds `" aapl "` via the simulator path, it would create a separate entry from `"AAPL"`. The watchlist API should enforce normalization, but defense-in-depth says both sources should normalize.

**Severity:** Low. The API layer will likely validate/normalize tickers before they reach the data source, but the asymmetry is a latent bug.

**2. `PriceCache.version` is not locked**

The `version` property (cache.py:65-67) reads `self._version` without acquiring `self._lock`:

```python
@property
def version(self) -> int:
    return self._version
```

On CPython with the GIL this is safe for simple integer reads, but it's inconsistent with the class's thread-safety contract and would be a real bug on a GIL-free Python (PEP 703 / free-threaded builds, which Python 3.13+ supports experimentally). Since the project requires Python 3.12+, this should be locked for correctness.

**Severity:** Low (benign on CPython today, but violates the class's own thread-safety guarantee).

**3. `stream.py` module-level router**

`stream.py` creates `router = APIRouter(...)` at module level (line 17) but also has `create_stream_router()` which registers a route on that shared router and returns it. If `create_stream_router()` were called twice (e.g., in tests), the route would be registered twice on the same router. This is unlikely in practice but is a design smell — the router should be created inside the factory function, not at module scope.

**Severity:** Low. Single-call in production, but could cause confusing behavior in tests.

### Observations (consider but not blocking)

**4. `PriceCache` uses `threading.Lock` in an async application**

The cache uses `threading.Lock()` rather than `asyncio.Lock()`. This is actually the *correct* choice here because:
- The cache is written from `asyncio.to_thread()` (Massive path) and from sync simulator code
- `threading.Lock` works across threads; `asyncio.Lock` does not

However, holding a `threading.Lock` on the async event loop thread blocks the loop. The lock is held for microseconds (dict operations), so this is fine in practice. Just noting for awareness.

**5. Random seed price for unknown tickers is non-deterministic**

`GBMSimulator._add_ticker_internal()` uses `random.uniform(50.0, 300.0)` for unknown tickers (simulator.py:151). This means the same ticker added in two different sessions will start at different prices. For a demo simulator this is fine, but it could confuse users if they restart and see different prices for a custom ticker. A hash-based deterministic seed (e.g., `hash(ticker) % 250 + 50`) would be more predictable.

**Severity:** Cosmetic. The simulator is inherently non-deterministic (GBM), so this is consistent with expectations.

**6. No test for SSE endpoint**

`stream.py` has 33% coverage. The `_generate_events` async generator is untested. A test using FastAPI's `TestClient` with `httpx` SSE streaming would cover the core logic (version-based dedup, JSON payload format, retry directive). This would meaningfully increase confidence in the integration layer.

**7. `_fetch_snapshots` return type is `list` (untyped)**

`massive_client.py:123` declares `def _fetch_snapshots(self) -> list:` — the return type should be more specific (e.g., `list[Any]` or the actual Massive snapshot type) to aid static analysis. Minor typing nit.

**8. No backpressure on SSE**

If a client is slow to consume SSE events, the async generator in `stream.py` will keep producing them. FastAPI/Starlette's `StreamingResponse` buffers these internally. For a single-user demo this is fine, but worth noting if the app were ever extended to multi-user.

**9. Shock events use `random` not `numpy.random`**

The simulator uses `numpy.random` for the GBM draws (correlated via Cholesky) but Python's built-in `random` for shock events (simulator.py:105-108). This is fine — shock events are independent and don't need correlation — but mixing two RNG sources means you can't seed both with a single call for full reproducibility.

---

## Strengths

- **Mathematically correct GBM**: The `(mu - 0.5*sigma^2)*dt` drift correction term is properly implemented. Many tutorials get this wrong by omitting the Ito correction. The time-scaling (`0.5s / trading_seconds_per_year`) is well thought out.
- **Cholesky decomposition for correlated moves**: Sector-based correlation is a nice touch that makes the simulation feel realistic. The implementation correctly rebuilds the matrix when tickers change.
- **Comprehensive error handling**: Both data sources catch and log exceptions without crashing the background loop. The Massive client handles malformed snapshots individually so one bad ticker doesn't block the rest.
- **Clean test suite**: Tests are well-organized by module, use appropriate mocking (Massive API), and cover edge cases (empty inputs, duplicates, idempotent stops). The `test_prices_are_positive` test running 10,000 steps is a good statistical property test.
- **Immutable data model**: `PriceUpdate` as a frozen dataclass with slots is the right call — prevents accidental mutation and is memory-efficient.
- **Good documentation**: The docstrings, CLAUDE.md, and planning docs provide clear context for future developers/agents.

---

## Recommendations Summary

| # | Finding | Severity | Action |
|---|---|---|---|
| 1 | Ticker normalization inconsistency | Low | Add `.upper().strip()` to `SimulatorDataSource.add_ticker` |
| 2 | `version` property unlocked | Low | Acquire lock in the property getter |
| 3 | Module-level router in stream.py | Low | Move `APIRouter()` creation inside factory |
| 4 | threading.Lock in async context | Info | No action needed (correct choice) |
| 5 | Non-deterministic seed prices | Info | Consider hash-based seeding |
| 6 | No SSE endpoint test | Medium | Add integration test with TestClient |
| 7 | Untyped `_fetch_snapshots` return | Info | Annotate return type |
| 8 | No SSE backpressure | Info | No action needed for demo scope |
| 9 | Mixed RNG sources | Info | No action needed |

---

## Verdict

**The market data backend is well-designed, thoroughly tested, and ready for integration with the rest of the platform.** The code is clean, the architecture is sound, and the test coverage is strong. The issues found are minor and none are blocking. The three "Low" severity items (ticker normalization, version locking, module-level router) are worth fixing as part of normal development but do not represent correctness risks in the current single-user, CPython deployment context.
