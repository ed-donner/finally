# Market Data Backend — Summary

**Status:** Complete, tested, reviewed twice (v1 + v2), all issues resolved.
**Tests:** 102 passing · **Coverage:** 97% · **Lint:** ruff clean.

## What Was Built

A complete market data subsystem in `backend/app/market/` (8 modules, ~600 lines) providing live price simulation and real market data via a unified interface.

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
| `models.py` | `PriceUpdate` — immutable frozen dataclass (ticker, price, previous_price, **day_open**, timestamp). Properties: `change`/`change_percent`/`direction` (vs previous tick) and `day_change`/`day_change_percent` (vs day open — the watchlist "daily change %"). `timestamp` is required. |
| `interface.py` | `MarketDataSource` — abstract base class defining `start/stop/add_ticker/remove_ticker/get_tickers`, plus a non-abstract `health()` hook. Also exports `normalize_ticker()` (strip + uppercase), used by both sources. |
| `cache.py` | `PriceCache` — thread-safe price store with version counter for SSE change detection; `snapshot()` returns `(version, prices)` atomically. **Sole price-rounding layer** (2 dp). |
| `seed_prices.py` | Realistic seed prices, per-ticker GBM params (drift/volatility), correlation groups |
| `simulator.py` | `GBMSimulator` (Geometric Brownian Motion with Cholesky-correlated moves; **seedable RNG**) + `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` — REST polling client for Polygon.io via the `massive` package; explicit request timeouts, health tracking |
| `factory.py` | `create_market_data_source()` — selects simulator or Massive based on `MASSIVE_API_KEY` env var |
| `stream.py` | `create_stream_router()` — FastAPI SSE endpoint factory (fresh router per call) using version-based change detection, with a 15s keep-alive heartbeat |

### Key Design Decisions

- **Strategy pattern** — both data sources implement the same ABC; downstream code is source-agnostic
- **PriceCache as single point of truth** — producers write, consumers read; no direct coupling. It is also the single place prices are rounded to 2 dp.
- **`day_open` carried separately from `previous_price`** — `previous_price` drives the per-tick flash; `day_open` (simulator session-open / Massive `day.open` → `prev_day.close`) drives the watchlist's "daily change %"
- **GBM with correlated moves** — Cholesky decomposition of sector-based correlation matrix; tech stocks correlate at 0.6, finance at 0.5, cross-sector at 0.3. Falls back to uncorrelated moves if the matrix is ever non-positive-definite (rather than crashing).
- **Single seedable RNG** — one `np.random.Generator` drives both GBM normals and shock events, so tests are deterministic
- **Random shock events** — ~0.1% chance per tick per ticker of a 2-5% move for visual drama
- **SSE over WebSockets** — simpler, one-way push, universal browser support
- **Source health surfaced** — `health()` reports `last_update` and (for Massive) `consecutive_failures`, so a dead upstream is distinguishable from a quiet market

## Test Suite

**102 tests, all passing.** 7 test modules in `backend/tests/market/`.

| Module | Tests | Coverage of target |
|--------|-------|----------|
| test_models.py | 14 | models.py: 100% |
| test_cache.py | 17 | cache.py: 100% |
| test_simulator.py | 26 | simulator.py: 98% |
| test_simulator_source.py | 13 | (integration tests) |
| test_factory.py | 7 | factory.py: 100% |
| test_massive.py | 21 | massive_client.py: 96% |
| test_stream.py | 4 | stream.py: 93% |

Overall coverage: **97%**.

## Code Review & Fixes Applied

Two review passes have been completed; all actionable findings are resolved.

### Review v1 (`planning/REVIEW.md`) — must-fix contract items

- **C1** — added `day_open` + `day_change`/`day_change_percent` so the watchlist's "daily change %" is computable from the SSE payload
- **H1** — `PriceCache.snapshot()` reads version + prices atomically (no torn reads on the SSE path)
- **H2** — SSE keep-alive heartbeat (15s) so idle connections aren't reaped
- **H4** — Massive timestamp/field path corrected against the real SDK (`sip_timestamp` ns → seconds; `day.open`/`prev_day.close`)
- Plus earlier hygiene fixes (build config, top-level imports, SSE return type, public `get_tickers()`, correlation constants, test mocks)

### Review v2 (`planning/MARKET_DATA_REVIEW_v2.md`) — reliability + polish

- **H5** — Massive snapshots the ticker list before `asyncio.to_thread` (no "list changed size" race)
- **M5** — explicit `RESTClient` timeouts + bounded `stop()` so a stuck poll can't hang shutdown
- **H3** — `finally` cleanup in the SSE generator
- **N1** — `create_stream_router()` builds a fresh `APIRouter` per call (no module global)
- **N2** — `MarketDataSource.health()` (last_update / consecutive_failures)
- **N3** — test drives the real `/prices` route handler and asserts SSE headers
- **M2** — single seedable RNG drives GBM and events; tests assert shock magnitude
- **M3** — Cholesky epsilon-nudge → uncorrelated fallback instead of crashing
- **M6 / L5** — shared `normalize_ticker()` in both sources; double-`start()` raises
- **L1–L6** — dead fixture removed; `timestamp` required; round once (cache); deduped log count; `rich` → `demo` extra
- **M1** — intentionally left as-is (the simulator already moves visibly on ~80% of ticks)

## Demo

A Rich terminal demo is available at `backend/market_data_demo.py`. `rich` lives
in the optional `demo` extra (kept out of the runtime image), so install it:

```bash
cd backend
uv run --extra demo market_data_demo.py
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
update.day_change_percent           # the watchlist "daily change %"

# Dynamic watchlist (tickers are normalized: "tsla" → "TSLA")
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Feed liveness (for the connection indicator)
source.health()   # {"healthy": bool, "last_update": float | None, ...}

# Shutdown
await source.stop()
```

> `start()` may be called only once per lifecycle (raises `RuntimeError` if
> already running — call `stop()` first to restart). `SimulatorDataSource`
> accepts an optional `seed=` for deterministic price paths in tests.
