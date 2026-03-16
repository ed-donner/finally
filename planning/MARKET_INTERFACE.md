# Market Data Unified Interface

Design specification for the unified Python API that abstracts over real (Massive) and simulated market data sources.

## Design Goals

1. **Source-agnostic consumers** — SSE streaming, portfolio valuation, and trade execution never know where prices come from
2. **Environment-driven selection** — set `MASSIVE_API_KEY` for real data, leave it unset for simulation
3. **Dynamic watchlist** — tickers can be added/removed at runtime without restart
4. **Thread-safe reads** — multiple async consumers can read the price cache concurrently

## Architecture

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
        └──→ Trade execution pricing
```

Producers (data sources) write to the cache. Consumers read from it. No direct coupling between producers and consumers.

---

## Core Types

### PriceUpdate

Immutable data object representing a single price snapshot.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float  # Unix seconds, defaults to time.time()

    # Computed properties
    change: float           # price - previous_price (4 decimal places)
    change_percent: float   # percentage change (4 decimal places)
    direction: str          # "up", "down", or "flat"

    def to_dict(self) -> dict:
        """Serialize all fields + computed properties for JSON/SSE."""
```

### PriceCache

Thread-safe in-memory price store. Single point of truth for current prices.

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]
    def remove(self, ticker: str) -> None

    @property
    def version(self) -> int  # Monotonic counter, increments on every update
```

**Key behaviors:**
- `update()` auto-computes `previous_price` from the last cached value (or uses current price on first update)
- Prices are rounded to 2 decimal places on write
- `version` enables efficient SSE change detection — only push when version changes

---

## Abstract Interface

```python
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for all market data providers.

    Lifecycle: start() → add_ticker()/remove_ticker() → stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.
        Starts a background task. Called exactly once at app startup."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.
        Safe to call multiple times (idempotent)."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

---

## Implementations

### SimulatorDataSource

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001)
```

- Wraps the `GBMSimulator` math engine (see MARKET_SIMULATOR.md)
- Runs an async loop calling `simulator.step()` every `update_interval` seconds
- Seeds cache with initial prices immediately on `start()`
- `add_ticker()` / `remove_ticker()` modify the simulator and cache in real time
- No external dependencies beyond `numpy`

### MassiveDataSource

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0)
```

- Polls `client.get_snapshot_all()` on a timer (see MASSIVE_API.md)
- Synchronous SDK call wrapped with `asyncio.to_thread()` for non-blocking I/O
- Extracts `snap.last_trade.price` and `snap.last_trade.timestamp` from each snapshot
- `add_ticker()` appends to the ticker list; picked up on next poll cycle
- `remove_ticker()` removes from list and cache immediately
- Catches all exceptions in the poll loop — logs and retries on next interval

### Behavioral Differences

| Behavior | Simulator | Massive |
|----------|-----------|---------|
| Update frequency | Every 0.5s | Every 15s (free tier) |
| First data available | Immediately (seed prices) | After first successful poll |
| Unknown tickers | Random seed price 50–300 | No data until market recognizes it |
| Market hours | Always active | Data stale outside trading hours |
| Network required | No | Yes |

---

## Factory

```python
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select implementation based on environment.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource
    - Otherwise → SimulatorDataSource

    Returns an unstarted source. Caller must: await source.start(tickers)
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

Decision is made once at startup. No runtime switching.

---

## SSE Streaming

The SSE endpoint reads from `PriceCache`, not from any data source directly.

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """GET /api/stream/prices — SSE endpoint."""
```

**Protocol:**
- First event: `retry: 1000\n\n` (client auto-reconnects after 1s)
- Subsequent events: `data: {JSON payload}\n\n` (only when `cache.version` changes)
- Poll interval: 0.5s
- Checks `request.is_disconnected()` to clean up

**Payload format:**
```json
data: {
  "AAPL": {"ticker":"AAPL","price":190.50,"previous_price":190.25,"timestamp":1710507600.1,"change":0.25,"change_percent":0.1316,"direction":"up"},
  "GOOGL": {"ticker":"GOOGL","price":175.30,...}
}
```

---

## Integration — App Startup

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# In FastAPI lifespan handler:
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                     "NVDA", "META", "JPM", "V", "NFLX"])

# Mount SSE router
stream_router = create_stream_router(cache)
app.include_router(stream_router, prefix="/api/stream")

# On shutdown:
await source.stop()
```

**Consumer usage (e.g., trade execution):**
```python
# Get current price for a trade
price = cache.get_price("AAPL")  # float or None
if price is None:
    raise ValueError("No price available for AAPL")

# Get full snapshot
update = cache.get("AAPL")  # PriceUpdate with price, change, direction, etc.

# Dynamic watchlist
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")
```

---

## Module Structure

```
backend/app/market/
├── __init__.py          # Public exports: PriceCache, PriceUpdate, MarketDataSource,
│                        #   create_market_data_source, create_stream_router
├── models.py            # PriceUpdate dataclass
├── interface.py         # MarketDataSource ABC
├── cache.py             # PriceCache (thread-safe, versioned)
├── seed_prices.py       # Seed prices, GBM params, correlation groups
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (REST polling)
├── factory.py           # create_market_data_source()
└── stream.py            # create_stream_router() (SSE endpoint)
```

## Public API (5 exports)

| Export | Type | Purpose |
|--------|------|---------|
| `PriceUpdate` | dataclass | Immutable price snapshot |
| `PriceCache` | class | Thread-safe price store |
| `MarketDataSource` | ABC | Interface for type hints |
| `create_market_data_source()` | function | Factory (env-driven) |
| `create_stream_router()` | function | SSE endpoint factory |
