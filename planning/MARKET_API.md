# Market Data API — Unified Interface

This document describes the unified market data API used throughout FinAlly. All code that needs stock prices interacts with `PriceCache` and `MarketDataSource` — never with the Massive client or simulator directly.

---

## Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource   ← default (no API key required)
└── MassiveDataSource     ← when MASSIVE_API_KEY env var is set
        │
        ▼
   PriceCache  (thread-safe, in-memory)
        │
        ├── SSE stream  (/api/stream/prices)
        ├── Portfolio valuation
        └── Trade execution
```

**Producer**: one `MarketDataSource` runs a background task that writes prices to `PriceCache`.
**Consumers**: SSE endpoint, portfolio API, and trade execution all read from `PriceCache`.
Consumers never talk to the data source directly.

---

## Module Layout

```
backend/app/market/
├── models.py          # PriceUpdate dataclass
├── interface.py       # MarketDataSource ABC
├── cache.py           # PriceCache
├── seed_prices.py     # Seed prices and GBM params for the default watchlist
├── simulator.py       # GBMSimulator + SimulatorDataSource
├── massive_client.py  # MassiveDataSource
├── factory.py         # create_market_data_source()
└── stream.py          # FastAPI SSE router factory
```

---

## Public Imports

```python
from app.market import (
    PriceCache,
    PriceUpdate,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## `PriceUpdate` — Core Data Model

Immutable frozen dataclass. Every price event is a `PriceUpdate`.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds

    # Computed properties
    @property
    def change(self) -> float: ...           # price - previous_price
    @property
    def change_percent(self) -> float: ...   # % change
    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...           # JSON-serialisable
```

---

## `PriceCache` — Shared State

Thread-safe. One writer, many readers.

```python
cache = PriceCache()

# Write (done by the data source background task)
update: PriceUpdate = cache.update(ticker="AAPL", price=191.50)

# Read
update: PriceUpdate | None = cache.get("AAPL")
price:  float | None        = cache.get_price("AAPL")
all:    dict[str, PriceUpdate] = cache.get_all()

# Remove (called when user removes ticker from watchlist)
cache.remove("AAPL")

# Change detection for SSE
version: int = cache.version   # increments on every update
```

---

## `MarketDataSource` — Abstract Interface

Both `SimulatorDataSource` and `MassiveDataSource` implement this:

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None: ...
    async def stop(self) -> None: ...
    async def add_ticker(self, ticker: str) -> None: ...
    async def remove_ticker(self, ticker: str) -> None: ...
    def get_tickers(self) -> list[str]: ...
```

---

## Factory — Source Selection

```python
from app.market import create_market_data_source

source = create_market_data_source(cache)
# Returns MassiveDataSource if MASSIVE_API_KEY is set and non-empty.
# Returns SimulatorDataSource otherwise.
```

Selection logic in `factory.py`:

```python
api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
if api_key:
    return MassiveDataSource(api_key=api_key, price_cache=cache)
else:
    return SimulatorDataSource(price_cache=cache)
```

---

## Application Lifecycle

Typically wired into FastAPI's lifespan:

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    tickers = load_watchlist_from_db()         # e.g. ["AAPL", "GOOGL", ...]
    await source.start(tickers)
    app.state.cache = cache
    app.state.source = source
    yield
    await source.stop()
```

---

## Watchlist Integration

When the user adds or removes a ticker via the watchlist API:

```python
# Add
await app.state.source.add_ticker("PYPL")

# Remove
await app.state.source.remove_ticker("GOOGL")
# Also removes from PriceCache automatically
```

The SSE stream's ticker scope is determined by `source.get_tickers()` at stream time — it includes all active watchlist tickers plus any tickers with open positions.

---

## SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)
app.include_router(router, prefix="/api")
# Registers: GET /api/stream/prices  (text/event-stream)
```

The SSE endpoint uses `cache.version` to detect changes and only emits events when prices have actually updated, preventing redundant pushes (important for the Massive free tier where poll results may not change between ticks).

---

## Environment Variables

| Variable | Effect |
|----------|--------|
| `MASSIVE_API_KEY` (set) | Uses `MassiveDataSource` — real market data |
| `MASSIVE_API_KEY` (absent/empty) | Uses `SimulatorDataSource` — GBM simulation |

---

## Seed Prices and Default Tickers

Defined in `seed_prices.py`. Used by both the simulator (starting prices) and as the application's default watchlist:

```
AAPL  $190   GOOGL $175   MSFT  $420   AMZN  $185   TSLA  $250
NVDA  $800   META  $500   JPM   $195   V     $280   NFLX  $600
```

For dynamically-added tickers not in the seed list, the simulator uses default params (`sigma=0.25, mu=0.05`) and a random starting price between $50–$300.
