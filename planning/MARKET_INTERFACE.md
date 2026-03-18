# Market Data Interface

Unified Python interface for market data in FinAlly. Two implementations — `SimulatorDataSource` and `MassiveDataSource` — sit behind one abstract interface. All downstream code (SSE streaming, portfolio valuation, trade execution) is source-agnostic.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│  MarketDataSource (ABC)                         │
│  ├── SimulatorDataSource  (default, no key)     │
│  └── MassiveDataSource    (when MASSIVE_API_KEY)│
│              │                                  │
│              ▼  writes prices                   │
│         PriceCache (thread-safe, in-memory)     │
│              │                                  │
│    ┌─────────┼──────────────────┐               │
│    ▼         ▼                  ▼               │
│  SSE stream  Portfolio value   Trade execution  │
└─────────────────────────────────────────────────┘
```

The data source runs a background asyncio task that writes `PriceUpdate` objects into the `PriceCache`. Consumers read from the cache — they never touch the data source directly.

---

## File Structure

```
backend/app/market/
├── __init__.py          # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
│                        #             create_market_data_source, create_stream_router
├── models.py            # PriceUpdate frozen dataclass
├── interface.py         # MarketDataSource abstract base class
├── cache.py             # PriceCache (thread-safe)
├── factory.py           # create_market_data_source()
├── massive_client.py    # MassiveDataSource
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
└── stream.py            # create_stream_router() — FastAPI SSE endpoint
```

---

## Core Data Model

```python
# models.py
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""
    ticker: str
    price: float           # Current price, rounded to 2 decimal places
    previous_price: float  # Price from the prior update
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute change: price - previous_price."""

    @property
    def change_percent(self) -> float:
        """Percentage change from previous_price."""

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""

    def to_dict(self) -> dict:
        """Serialize to dict for JSON/SSE. Includes all properties."""
```

`PriceUpdate` is the only data structure that leaves the market data layer. It is frozen (immutable) and uses `__slots__` for memory efficiency.

---

## Abstract Interface

```python
# interface.py
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Starts a background task.
        Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the currently tracked tickers."""
```

`start/stop/add_ticker/remove_ticker` are all `async` so implementations can do async I/O. `get_tickers` is synchronous — it reads in-memory state only.

---

## Price Cache

```python
# cache.py
from threading import Lock
from .models import PriceUpdate

class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker.

    Writers: one MarketDataSource background task.
    Readers: SSE endpoint, portfolio valuation, trade execution.
    """

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Computes direction/change from prior value.
        First update for a ticker: previous_price == price, direction == 'flat'."""

    def get(self, ticker: str) -> PriceUpdate | None:
        """Latest PriceUpdate for a ticker, or None."""

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price float, or None."""

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all current prices."""

    def remove(self, ticker: str) -> None:
        """Remove a ticker (called on watchlist removal)."""

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Bumped on every update.
        Used by the SSE endpoint for change detection."""
```

The `version` counter lets the SSE streamer skip unchanged snapshots — it only sends an event when the cache has actually changed since the last send.

---

## Factory Function

```python
# factory.py
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select the data source based on environment.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise                          → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        return SimulatorDataSource(price_cache=price_cache)
```

---

## Massive Implementation

Polls `GET /snapshot?type=stocks&ticker_any_of=...` on a timer. One API call fetches all watched tickers.

```python
# massive_client.py
import asyncio
from massive import RESTClient

class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        # poll_interval: 15s for free tier (5 req/min), 2-5s for paid tiers
        ...

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()                                    # Immediate first poll
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_once(self) -> None:
        # RESTClient is synchronous — run in thread pool
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        for snap in snapshots:
            self._cache.update(
                ticker=snap.ticker,
                price=snap.last_trade.price,
                timestamp=snap.last_trade.timestamp / 1000.0,  # ms → seconds
            )

    def _fetch_snapshots(self) -> list:
        return list(self._client.list_universal_snapshots(
            type="stocks",
            ticker_any_of=self._tickers,
        ))
```

**Key design decisions:**
- `asyncio.to_thread()` wraps the synchronous Massive client — never blocks the event loop
- An immediate first poll in `start()` means the cache has data before the first SSE client connects
- Poll errors are logged and swallowed — the loop retries on the next interval, keeping stale prices visible rather than crashing
- `add_ticker` appends to `self._tickers`; it takes effect on the next poll cycle

---

## Simulator Implementation

See `MARKET_SIMULATOR.md` for the GBM math. The `SimulatorDataSource` wraps `GBMSimulator` in an asyncio loop.

```python
# simulator.py
import asyncio

class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        # update_interval: 500ms — matches SSE push cadence
        ...

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers)
        # Seed cache immediately so SSE has data on first connect
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())

    async def _run_loop(self) -> None:
        while True:
            prices = self._sim.step()          # dict[str, float]
            for ticker, price in prices.items():
                self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)

    async def add_ticker(self, ticker: str) -> None:
        self._sim.add_ticker(ticker)
        # Seed cache immediately — ticker has a price before the next loop tick
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)
```

**Key design decisions:**
- `step()` is synchronous and fast (pure NumPy math) — no need for `to_thread()`
- New tickers are seeded into the cache immediately in `add_ticker()`, not on the next loop tick
- The loop catches exceptions and continues — a bad step never crashes the streamer

---

## SSE Streaming Integration

The SSE endpoint reads from `PriceCache` and pushes to connected clients:

```python
# stream.py — registered as GET /api/stream/prices
async def _generate_events(price_cache: PriceCache, request: Request):
    yield "retry: 1000\n\n"     # Tell browser to reconnect after 1s on drop
    last_version = -1

    while True:
        if await request.is_disconnected():
            break

        current_version = price_cache.version
        if current_version != last_version:
            last_version = current_version
            prices = price_cache.get_all()
            if prices:
                data = {ticker: update.to_dict() for ticker, update in prices.items()}
                yield f"data: {json.dumps(data)}\n\n"

        await asyncio.sleep(0.5)
```

**SSE event format:**
```
data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.25,
        "timestamp": 1711234567.89, "change": 0.25, "change_percent": 0.1314,
        "direction": "up"}, ...}
```

The `version` counter means we only push when prices have actually changed — at 2 ticks/second with 10 tickers, that's effectively every tick. If the Massive poller hasn't updated in 15 seconds, no event is sent during that window (no spurious empty events).

---

## Lifecycle

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# 1. App startup (FastAPI lifespan)
cache = PriceCache()
source = create_market_data_source(cache)    # Reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", ...])   # Starts background task

# 2. Register SSE router
app.include_router(create_stream_router(cache))

# 3. Watchlist changes
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# 4. Read prices (portfolio valuation, trade execution)
update = cache.get("AAPL")           # PriceUpdate | None
price  = cache.get_price("AAPL")     # float | None
all_p  = cache.get_all()             # dict[str, PriceUpdate]

# 5. App shutdown
await source.stop()
```

---

## Public Imports

Everything downstream needs is re-exported from `app.market`:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```
