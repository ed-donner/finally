# Market Data Unified Interface

## Design Principle

A single abstract interface (`MarketDataSource`) with two implementations -- a GBM simulator (default) and a Massive API client (real data). The factory pattern selects the implementation based on the `MASSIVE_API_KEY` environment variable. All downstream code (SSE streaming, portfolio valuation, trade execution) reads from the shared `PriceCache` and is completely agnostic to the data source.

## Architecture

```
Environment Variable
    │
    ▼
┌──────────────────────┐
│ create_market_data_   │──── MASSIVE_API_KEY set ──▶ MassiveDataSource
│ source(price_cache)   │                             (polls REST API every 15s)
│                       │──── no key ──────────────▶ SimulatorDataSource
└──────────────────────┘                             (GBM ticks every 500ms)
                                     │
                                     ▼
                              ┌─────────────┐
                              │ PriceCache   │ ◀── thread-safe, in-memory
                              └─────────────┘
                                     │
                         ┌───────────┼───────────┐
                         ▼           ▼           ▼
                   SSE Stream   Portfolio    Trade
                   Endpoint     Valuation    Execution
```

## Core Types

### PriceUpdate (`app/market/models.py`)

Immutable snapshot of a single ticker's price at a point in time.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float  # Unix seconds

    # Computed properties:
    change: float           # price - previous_price
    change_percent: float   # percentage change
    direction: str          # "up", "down", or "flat"

    def to_dict(self) -> dict  # JSON serialization for SSE
```

### PriceCache (`app/market/cache.py`)

Thread-safe in-memory cache. One writer (the active data source), many readers.

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]
    def remove(self, ticker: str) -> None
    version: int  # monotonic counter, bumped on every update
```

### MarketDataSource (`app/market/interface.py`)

Abstract base class. Implementations push prices into the shared PriceCache on their own schedule.

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None
    async def stop(self) -> None
    async def add_ticker(self, ticker: str) -> None
    async def remove_ticker(self, ticker: str) -> None
    def get_tickers(self) -> list[str]
```

**Lifecycle:**

```python
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", ...])
# ... app runs, source writes to cache in background ...
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")
# ... app shutting down ...
await source.stop()
```

### Factory (`app/market/factory.py`)

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

## Implementation: SimulatorDataSource

- Wraps `GBMSimulator` in an async background task
- Calls `GBMSimulator.step()` every 500ms
- Writes each ticker's new price to `PriceCache`
- Seeds the cache with initial prices on `start()` so SSE has data immediately
- See `MARKET_SIMULATOR.md` for simulator math and design

## Implementation: MassiveDataSource

- Uses the `massive` Python client (`RESTClient`)
- Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` for all tracked tickers in a single API call
- Default poll interval: 15 seconds (free tier: 5 req/min)
- Runs the synchronous REST client in a thread via `asyncio.to_thread()` to avoid blocking the event loop
- Extracts `last_trade.price` and `last_trade.timestamp` from each snapshot
- Converts Massive timestamps (Unix milliseconds) to seconds for `PriceCache`
- Gracefully handles API errors (401, 429, network) -- logs and retries on next interval
- See `MASSIVE_API.md` for API details

## SSE Streaming (`app/market/stream.py`)

A FastAPI router factory that reads from `PriceCache` and pushes updates to clients:

```python
router = create_stream_router(price_cache)
# Endpoint: GET /api/stream/prices (text/event-stream)
```

- Sends all prices every ~500ms
- Uses `PriceCache.version` for change detection
- Includes `retry: 1000` directive for automatic client reconnection

## Public API (`app/market/__init__.py`)

All downstream code imports from the package root:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

## Module Layout

```
backend/app/market/
├── __init__.py          # Public exports
├── interface.py         # MarketDataSource ABC
├── models.py            # PriceUpdate dataclass
├── cache.py             # PriceCache (thread-safe)
├── factory.py           # Environment-driven factory
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (Polygon.io)
├── seed_prices.py       # Default tickers, prices, GBM params
└── stream.py            # SSE streaming router
```
