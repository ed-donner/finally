# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two implementations — `SimulatorDataSource` and `MassiveDataSource` — sit behind one abstract interface. All downstream code (SSE streaming, portfolio valuation, trade execution) is source-agnostic.

---

## Module Layout

```
backend/app/market/
├── __init__.py          # Public API re-exports
├── models.py            # PriceUpdate dataclass
├── cache.py             # PriceCache (thread-safe store)
├── interface.py         # MarketDataSource ABC
├── factory.py           # create_market_data_source()
├── massive_client.py    # MassiveDataSource
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── stream.py            # SSE FastAPI router
└── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, constants
```

Public API (imported from `app.market`):
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

## Core Data Model

```python
# models.py
from dataclasses import dataclass, field
import time

@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

`PriceUpdate` is **frozen** (immutable) and uses `__slots__` for memory efficiency. It is the only data structure that crosses module boundaries — everything downstream works with `PriceUpdate` objects or their `to_dict()` serialization.

---

## Price Cache

Thread-safe in-memory store that data sources write to and the SSE streamer reads from.

```python
# cache.py
from threading import Lock

class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically incremented on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the created PriceUpdate.
        On first update for a ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices (shallow copy)."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter; increments on every update. Used for SSE change detection."""
        return self._version
```

**Key design points:**
- One `PriceCache` instance is created at app startup and shared across all components
- `version` allows the SSE generator to skip yields when nothing has changed (avoids redundant pushes)
- `get_price()` is the convenience method for trade execution (just need the float)

---

## Abstract Interface

```python
# interface.py
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices
    — it reads from the cache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Starts a background task.
        Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

Both implementations conform to this contract. The interface does **not** return prices — it pushes updates into the cache on its own schedule.

---

## Factory Function

```python
# factory.py
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select data source based on MASSIVE_API_KEY environment variable.

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        # Real market data via Massive REST API
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        # Simulated prices via GBM
        return SimulatorDataSource(price_cache=price_cache)
```

---

## MassiveDataSource

Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` for all watched tickers in one API call.

```python
# massive_client.py
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()   # Immediate seed — cache has data before first SSE push
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            # Ticker appears in cache on the next poll cycle

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            # Massive client is synchronous — run in thread pool
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", snap.ticker, e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — loop retries on next interval

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

---

## SimulatorDataSource

Wraps `GBMSimulator` in an asyncio background task. See `MARKET_SIMULATOR.md` for the GBM math.

```python
# simulator.py (SimulatorDataSource portion)
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed cache immediately so SSE has data before the first step
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

---

## SSE Streaming

The SSE endpoint reads from `PriceCache` and pushes to connected clients. It uses `version` to avoid redundant events.

```python
# stream.py
async def _generate_events(price_cache: PriceCache, request: Request, interval: float = 0.5):
    yield "retry: 1000\n\n"   # Tell browser to reconnect after 1s if dropped

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

        await asyncio.sleep(interval)
```

**SSE event format** (one event per 500ms interval if prices changed):
```
data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.32,
       "timestamp": 1711000000.0, "change": 0.18, "change_percent": 0.0945,
       "direction": "up"}, "GOOGL": {...}, ...}
```

The `EventSource` API reconnects automatically. The `retry: 1000` directive sets reconnect delay to 1 second.

Mount with:
```python
router = create_stream_router(price_cache)
app.include_router(router)
# Endpoint: GET /api/stream/prices
```

---

## Application Lifecycle

```python
# At startup (e.g., FastAPI lifespan)
price_cache = PriceCache()
market_source = create_market_data_source(price_cache)
initial_tickers = ["AAPL", "GOOGL", "MSFT", ...]  # From DB watchlist
await market_source.start(initial_tickers)

# During operation
await market_source.add_ticker("PYPL")     # User adds to watchlist
await market_source.remove_ticker("NFLX") # User removes from watchlist

# Trade execution
current_price = price_cache.get_price("AAPL")  # Read current price
if current_price is None:
    raise ValueError("No price available for AAPL")

# At shutdown
await market_source.stop()
```

---

## Behavior Comparison

| | `SimulatorDataSource` | `MassiveDataSource` |
|---|---|---|
| **Update frequency** | Every 500ms | Every 15s (free) / 2–5s (paid) |
| **External dependency** | None | Massive API, internet |
| **Cache seeded on start** | Yes — immediately from GBM initial prices | Yes — immediate first poll |
| **New ticker latency** | Immediate (seeded from GBM) | Up to one poll interval |
| **On poll failure** | Exception logged, loop continues | Exception logged, loop continues |
| **Thread safety** | GBM runs in asyncio loop; cache is thread-safe | REST call runs in thread pool; cache is thread-safe |
