# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Market Data API

The market data subsystem lives in `app/market/`. Use these imports:

```python
from app.market import (PriceCache, PriceUpdate, MarketDataSource, normalize_ticker,
                        create_market_data_source, create_stream_router)
```

Full design: `planning/MARKET_DATA_DESIGN.md`.

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `timestamp`, `reference_price` (session start / previous close, may be None), plus properties `change`, `change_percent`, `direction` ("up"/"down"/"flat", tick-to-tick — use for flashes), `day_change`, `day_change_percent` (vs reference — use for the watchlist "daily change" column), and `to_dict()`.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None, reference_price=<carry forward>) -> PriceUpdate`
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `remove(ticker)`
  - `version` property — monotonic counter, bumped on every update and remove (SSE change detection)

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MassiveDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` / `set_tickers(iterable)` -> `stop()`. All methods normalize tickers (`normalize_ticker`: strip + upper).
  - **Track watchlist ∪ held positions:** after any watchlist change or trade, call `await source.set_tickers(watchlist | held_tickers)`.

- **`create_market_data_source(cache, initial_prices=None)`** — Factory. Returns `MassiveDataSource` if `MASSIVE_API_KEY` is set (poll interval `MASSIVE_POLL_INTERVAL`, default 15 s), otherwise `SimulatorDataSource`. `initial_prices` (e.g. `{ticker: avg_cost}` of positions) seeds the simulator so restarts don't cause fake P&L jumps.

- **Massive modes** — auto-detected: `snapshot` (paid plans, live/15-min delayed) or `eod` (free plan: end-of-day closes, 5 req/min budget). `source.mode` tells you which.

### SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # Returns FastAPI APIRouter
# Endpoint: GET /api/stream/prices (text/event-stream)
```

### Seed Data

Default tickers: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed prices and per-ticker volatility/drift params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

## Demo

```bash
uv run market_data_demo.py   # Live terminal dashboard with simulated prices
```
