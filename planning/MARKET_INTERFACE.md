# Market Data Interface

The market data subsystem lives in `backend/app/market/`. It provides a unified interface over two implementations — a GBM simulator (default) and the Massive REST API — selected automatically based on the `MASSIVE_API_KEY` environment variable.

## Module Layout

```
backend/app/market/
├── __init__.py          # Public exports
├── models.py            # PriceUpdate dataclass
├── interface.py         # MarketDataSource abstract base class
├── cache.py             # PriceCache (thread-safe in-memory store)
├── seed_prices.py       # Seed prices and GBM params for default tickers
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (Polygon.io REST poller)
├── factory.py           # create_market_data_source() factory function
└── stream.py            # SSE streaming endpoint (FastAPI router)
```

## Data Flow

```
SimulatorDataSource ──┐
                      ├──→ PriceCache ──→ SSE /api/stream/prices
MassiveDataSource ────┘         │
                                ├──→ Portfolio valuation
                                └──→ Trade execution (current price)
```

Producers (data sources) write to the cache. Consumers (SSE, portfolio, trades) read from it. Nothing downstream is coupled to the data source implementation.

---

## Core Types

### `PriceUpdate` (`models.py`)

Immutable frozen dataclass representing one price tick.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float        # Unix seconds

    # Computed properties:
    @property
    def change(self) -> float: ...          # Absolute change from previous_price
    @property
    def change_percent(self) -> float: ...  # % change from previous_price
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON-serializable dict for SSE
```

`previous_price` is the price from the prior tick (not the prior day's close). It drives the flash animation direction. The first update for a ticker sets `previous_price == price` so direction is `"flat"`.

---

### `PriceCache` (`cache.py`)

Thread-safe in-memory store. One writer (the data source), many readers.

```python
cache = PriceCache()

# Writing (called by data source background task)
update: PriceUpdate = cache.update(ticker="AAPL", price=191.50)

# Reading
update: PriceUpdate | None = cache.get("AAPL")
price: float | None        = cache.get_price("AAPL")
all_prices: dict[str, PriceUpdate] = cache.get_all()

# Removal (when ticker removed from watchlist)
cache.remove("AAPL")

# SSE change detection
version: int = cache.version   # increments on every update
```

The `version` counter is a monotonic integer that increments on every `update()` call. The SSE endpoint polls it to detect changes without comparing individual prices.

---

### `MarketDataSource` (`interface.py`)

Abstract base class. Both implementations conform to this contract.

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Call once at app startup."""

    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Takes effect on next poll/tick."""

    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the cache."""

    def get_tickers(self) -> list[str]:
        """Current list of actively tracked tickers."""
```

Downstream code (watchlist API, SSE) only holds a reference to `MarketDataSource` — never to the concrete implementation. This makes the simulator/Massive swap transparent.

---

## Factory

`factory.py` selects the implementation at startup:

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY from env
```

Selection logic:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

---

## Lifecycle

```python
# Application startup (FastAPI lifespan)
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                    "NVDA", "META", "JPM", "V", "NFLX"])

# Watchlist changes (from API endpoints)
await source.add_ticker("AMD")
await source.remove_ticker("NFLX")

# Application shutdown
await source.stop()
```

---

## SSE Streaming (`stream.py`)

The SSE router is created from the cache and attached to the FastAPI app:

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)
app.include_router(router, prefix="/api")
# Endpoint: GET /api/stream/prices
```

The SSE generator polls `cache.version` every 100ms. When the version increments, it serializes all changed prices and pushes them as `price` events. This decouples the SSE push rate from the data source's update rate.

---

## Public Imports

`__init__.py` exports the full public surface:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## Ticker Validation

The `MassiveDataSource` normalizes tickers to uppercase before adding them. The simulator accepts any string and falls back to a random seed price in `[50, 300]` for unknown tickers. Full ticker validation (rejecting unknown symbols) is handled at the API layer before calling `add_ticker`.

For Massive, the API itself rejects unknown symbols — snapshots for invalid tickers are silently absent from the response, so `_poll_once` logs a debug warning and skips them.

---

## Adding a New Data Source

1. Create a new module in `app/market/` that imports and subclasses `MarketDataSource`
2. Implement all five abstract methods (`start`, `stop`, `add_ticker`, `remove_ticker`, `get_tickers`)
3. Write prices to `self._cache.update(ticker, price)` in your background task
4. Update `factory.py` to conditionally return your new source

Nothing else in the codebase changes.
