# Market Data Backend — Summary

**Status:** Complete, tested, reviewed, all issues resolved. **Task 0 (T0.1–T0.5) complete** (milestone M0); see "Task 0 Fixes" below. The full design is in `planning/MARKET_DATA_DESIGN.md`.

## What Was Built

A complete market data subsystem in `backend/app/market/` (8 modules, ~500 lines) providing live price simulation and real market data via a unified interface.

### Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation
        └──→ Trade execution
```

### Modules

| File | Purpose |
|------|---------|
| `models.py` | `PriceUpdate` — immutable frozen dataclass (ticker, price, previous_price, timestamp, change, direction) |
| `interface.py` | `MarketDataSource` — abstract base class defining `start/stop/add_ticker/remove_ticker/get_tickers`, plus `normalize_ticker()` |
| `cache.py` | `PriceCache` — thread-safe price store with version counter for SSE change detection (bumped on update and on remove) |
| `seed_prices.py` | Realistic seed prices, per-ticker GBM params (drift/volatility), correlation groups |
| `simulator.py` | `GBMSimulator` (Geometric Brownian Motion with Cholesky-correlated moves) + `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` — REST polling client for Polygon.io via the `massive` package; `extract_price()` parses real `TickerSnapshot`s |
| `factory.py` | `create_market_data_source()` — selects simulator or Massive based on `MASSIVE_API_KEY` env var |
| `stream.py` | `create_stream_router()` — FastAPI SSE endpoint factory (fresh router per call) using version-based change detection; sends `data: {}` when empty, keep-alive comments when idle |

### Key Design Decisions

- **Strategy pattern** — both data sources implement the same ABC; downstream code is source-agnostic
- **PriceCache as single point of truth** — producers write, consumers read; no direct coupling
- **GBM with correlated moves** — Cholesky decomposition of sector-based correlation matrix; tech stocks correlate at 0.6, finance at 0.5, cross-sector at 0.3
- **Random shock events** — ~0.1% chance per tick per ticker of a 2-5% move for visual drama
- **SSE over WebSockets** — simpler, one-way push, universal browser support

## Test Suite

**104 tests, all passing.** 7 test modules in `backend/tests/market/`. Overall coverage: 97%.

| Module | Coverage |
|--------|----------|
| models.py, cache.py, interface.py, factory.py, seed_prices.py | 100% |
| simulator.py | 97% |
| massive_client.py | 95% (tests use real `massive` `TickerSnapshot` objects, not `MagicMock`) |
| stream.py | 91% (new `test_stream.py`) |

## Task 0 Fixes (milestone M0)

| # | Fix | Tests |
|---|-----|-------|
| T0.1 | Both sources normalize tickers (`normalize_ticker()`: `strip().upper()`) in `start()`, `add_ticker()`, `remove_ticker()`, and de-duplicate in `start()`. `add_ticker()` before `start()` is kept, not dropped | `test_simulator_source.py::TestSimulatorNormalization`, `test_massive.py::TestMassiveNormalizationAndRaces` |
| T0.2 | `PriceCache.remove()` bumps `version` (only when the ticker was present); `version` is read under the lock | `test_cache.py::TestPriceCacheVersioning` |
| T0.3 | SSE emits `data: {}` when the cache is empty | `test_stream.py::test_empty_cache_after_removal_emits_empty_object` |
| T0.4 | `create_stream_router()` builds a fresh `APIRouter` per call | `test_stream.py::TestCreateStreamRouter` |
| T0.5 | Massive parsing fixed: timestamp from `last_trade.sip_timestamp` (nanoseconds), not the non-existent `last_trade.timestamp`; price falls back to `day.close`, then `prev_day.close`. The worker thread gets a copy of the ticker list, and tickers removed during an in-flight poll are not written back | `test_massive.py::TestExtractPrice`, `test_removed_mid_poll_not_resurrected` |

Also: the SSE generator sends `: keep-alive` comments after 15 s idle and re-raises `CancelledError`; `GBMSimulator` copies per-ticker params so the module constants can't be mutated.

## Code Review & Fixes Applied

A comprehensive code review identified 7 issues. All were resolved:

1. **pyproject.toml build config** — added `[tool.hatch.build.targets.wheel] packages = ["app"]`
2. **Lazy imports removed** — `massive` is a core dependency; imports moved to top level
3. **SSE return type fixed** — `_generate_events` annotated as `AsyncGenerator[str, None]`
4. **Public `get_tickers()`** — added to `GBMSimulator` to avoid private attribute access
5. **Correlation constants cleaned up** — removed unused `DEFAULT_CORR`, consolidated into `CROSS_GROUP_CORR`
6. **Unused test imports removed** — `pytest`, `math`, `asyncio` cleaned from 4 test files
7. **Massive test mocks fixed** — `source._client` set in tests, patches target correct names

## Demo

A Rich terminal demo is available at `backend/market_data_demo.py`:

```bash
cd backend
uv run market_data_demo.py
```

Displays a live-updating dashboard with all 10 tickers, sparklines, color-coded direction arrows, and an event log for notable price moves. Runs 60 seconds or until Ctrl+C.

## Usage for Downstream Code

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # Reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
