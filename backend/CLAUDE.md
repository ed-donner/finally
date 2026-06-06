# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Market Data API

The market data subsystem lives in `app/market/`. Use these imports:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `day_open`, `timestamp` (all **required** — `PriceCache` always supplies the timestamp), plus properties:
  - `change` / `change_percent` / `direction` ("up"/"down"/"flat") — vs the **previous tick**; drives the price-flash animation.
  - `day_change` / `day_change_percent` — vs **`day_open`** (the session/day open); this is the **"daily change %"** the watchlist shows.
  - `to_dict()` — JSON serialization; emits all of the above keys plus `day_open`.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None, day_open=None) -> PriceUpdate` — `day_open`: if provided (Massive snapshot's day open / prev close), used as-is; otherwise carried forward from the prior tick; on the first tick it defaults to `price` (the simulator's session open).
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `snapshot() -> tuple[int, dict[str, PriceUpdate]]` — atomic `(version, prices-copy)` under one lock; use this (not `version` + `get_all()`) for SSE change detection to avoid torn reads.
  - `remove(ticker)`
  - `version` property — monotonic counter, increments on every update

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MassiveDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` -> `stop()`. Notes:
  - `start()` may be called only once per lifecycle (raises `RuntimeError` if a task is already running; call `stop()` first to restart).
  - Tickers are normalized via `normalize_ticker()` (strip + uppercase) in **both** sources, so `"aapl"`, `" AAPL "` and `"AAPL"` are the same instrument.
  - `health() -> {"healthy": bool, "last_update": float | None, ...}` — feed liveness for the connection indicator. The simulator is healthy while its loop runs; `MassiveDataSource` reports unhealthy after a run of failed polls (and includes `consecutive_failures`).

- **`create_market_data_source(cache)`** — Factory. Returns `MassiveDataSource` if `MASSIVE_API_KEY` is set, otherwise `SimulatorDataSource`. `SimulatorDataSource` accepts an optional `seed` for deterministic tests.

### SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # Returns FastAPI APIRouter
# Endpoint: GET /api/stream/prices (text/event-stream)
```

Each `data:` frame is a JSON map of `{ticker: PriceUpdate.to_dict()}` for all tracked tickers, sent on change (~500ms cadence). A `: keep-alive` comment is emitted if no update goes out for 15s so idle connections aren't reaped by proxies/browsers.

### Seed Data

Default tickers: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX. Seed prices and per-ticker volatility/drift params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

## Demo

`rich` is an optional dependency (the `demo` extra), kept out of the runtime image:

```bash
uv run --extra demo market_data_demo.py   # Live terminal dashboard with simulated prices
```
