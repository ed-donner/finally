# Market Data Interface Design

Unified Python interface for market data in FinAlly. Two concrete implementations — `SimulatorDataSource` and `MassiveDataSource` — sit behind one abstract base class. All downstream code (SSE streaming, price cache, portfolio valuation, trade execution) is source-agnostic and works identically regardless of which implementation is active.

## Design Principles

- **Single abstraction**: one abstract base class, two implementations, one `PriceCache`
- **Push not pull**: data sources write to the cache on their own schedule; consumers read from it
- **Async-first**: implementations are async; the Massive client (synchronous SDK) is wrapped via `asyncio.to_thread`
- **Hot-swappable tickers**: add/remove tickers at runtime without restarting the source
- **Fail-safe**: polling errors are logged and skipped; the loop never crashes

---

## Core Data Model

```python
# backend/app/market/models.py
from dataclasses import dataclass

@dataclass
class PriceUpdate:
    """A single price update for one ticker."""
    ticker: str
    price: float
    previous_price: float
    timestamp: float          # Unix seconds (float)
    change: float             # price - previous_price
    direction: str            # "up", "down", or "flat"
```

This is the **only** data structure that leaves the market data layer. Everything downstream works with `PriceUpdate` objects.

---

## Abstract Interface

```python
# backend/app/market/interface.py
from abc import ABC, abstractmethod

class MarketDataSource(ABC):
    """Abstract interface for market data providers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Seeds the cache with initial prices."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing price updates and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Takes effect on the next poll/step."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and evict it from the cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of active tickers."""
```

Both implementations write to a shared `PriceCache`. The interface does **not** return prices directly — it pushes updates into the cache on its own schedule (500ms for the simulator, configurable poll interval for Massive).

---

## Price Cache

Thread-safe, in-memory store that data sources write to and all consumers read from.

```python
# backend/app/market/interface.py (continued)
import time
from threading import Lock

class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker."""

    def __init__(self):
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Update price for a ticker. Computes direction from previous price. Returns the PriceUpdate."""
        with self._lock:
            ts = timestamp or time.time()
            previous = self._prices.get(ticker)
            previous_price = previous.price if previous else price

            if price > previous_price:
                direction = "up"
            elif price < previous_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                timestamp=ts,
                change=round(price - previous_price, 4),
                direction=direction,
            )
            self._prices[ticker] = update
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for one ticker. Returns None if not yet available."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Return a snapshot of all current prices."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Evict a ticker from the cache (called on watchlist removal)."""
        with self._lock:
            self._prices.pop(ticker, None)
```

---

## Factory Function

Selects the right implementation at startup based on the environment variable.

```python
# backend/app/market/factory.py
import os

def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return a SimulatorDataSource or MassiveDataSource based on MASSIVE_API_KEY."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

---

## MassiveDataSource Implementation

Polls the Massive snapshot endpoint on a timer and writes results to the cache. The Massive SDK is synchronous, so it is called via `asyncio.to_thread` to avoid blocking the event loop.

```python
# backend/app/market/massive_client.py
import asyncio
import logging
from massive import RESTClient
from massive.rest.models import SnapshotMarketType
from .interface import MarketDataSource, PriceCache

logger = logging.getLogger(__name__)

class MassiveDataSource(MarketDataSource):
    """Polls the Massive REST API and writes prices to PriceCache."""

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,  # seconds; 15s = 4 req/min (safe for free tier)
    ):
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        # Poll immediately so the cache is populated before the first SSE client connects
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        try:
            snapshots = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=list(self._tickers),
            )
            for snap in snapshots:
                if snap.last_trade and snap.last_trade.price:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000,  # ms → seconds
                    )
        except Exception as e:
            logger.warning("Massive poll failed: %s", e)
            # Continue — polling loop must survive transient errors
```

---

## SimulatorDataSource Implementation

Wraps the `GBMSimulator` (see `MARKET_SIMULATOR.md`) in an async loop that ticks every 500ms.

```python
# backend/app/market/simulator.py (excerpt — full implementation in MARKET_SIMULATOR.md)
import asyncio
from .interface import MarketDataSource, PriceCache
from .gbm import GBMSimulator   # See MARKET_SIMULATOR.md

class SimulatorDataSource(MarketDataSource):
    """Generates synthetic GBM price paths and writes them to PriceCache."""

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5):
        self._cache = price_cache
        self._interval = update_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._sim: GBMSimulator | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        self._sim = GBMSimulator(tickers=self._tickers)
        # Seed cache with initial prices before first SSE client connects
        for ticker, price in self._sim.current_prices().items():
            self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            if self._sim:
                self._sim.add_ticker(ticker)
                # Immediately seed the cache so the new ticker appears in SSE
                price = self._sim.get_price(ticker)
                if price:
                    self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run_loop(self) -> None:
        while True:
            prices = self._sim.step()  # dict[str, float]
            for ticker, price in prices.items():
                self._cache.update(ticker=ticker, price=price)
            await asyncio.sleep(self._interval)
```

---

## Integration with FastAPI

### App Startup / Shutdown

```python
# backend/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .market.interface import PriceCache
from .market.factory import create_market_data_source
from .db import get_watchlist_tickers

price_cache = PriceCache()
market_source = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global market_source
    initial_tickers = await get_watchlist_tickers(user_id="default")
    market_source = create_market_data_source(price_cache)
    await market_source.start(initial_tickers)
    yield
    await market_source.stop()

app = FastAPI(lifespan=lifespan)
```

### Watchlist Endpoints

```python
# backend/app/routes/watchlist.py
@router.post("/api/watchlist")
async def add_to_watchlist(body: AddTickerRequest):
    await db_add_ticker(body.ticker)
    await market_source.add_ticker(body.ticker)
    return {"ticker": body.ticker}

@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str):
    await db_remove_ticker(ticker)
    await market_source.remove_ticker(ticker)
    return {"ticker": ticker}
```

### Trade Execution (reads from cache)

```python
# backend/app/routes/portfolio.py
@router.post("/api/portfolio/trade")
async def execute_trade(body: TradeRequest):
    update = price_cache.get(body.ticker)
    if not update:
        raise HTTPException(400, "No price available for ticker")
    current_price = update.price
    # ... validate and execute trade at current_price
```

---

## Integration with SSE

The SSE endpoint reads from `PriceCache` every 500ms and streams updates to all connected clients. This is entirely independent of how frequently the cache is updated (which depends on the data source — 500ms for simulator, 15s for Massive).

```python
# backend/app/routes/stream.py
import json
import asyncio
from fastapi.responses import StreamingResponse

async def price_event_generator(price_cache: PriceCache):
    """Async generator yielding SSE-formatted price updates."""
    while True:
        prices = price_cache.get_all()
        if prices:
            payload = {
                ticker: {
                    "ticker": p.ticker,
                    "price": p.price,
                    "previous_price": p.previous_price,
                    "change": p.change,
                    "direction": p.direction,
                    "timestamp": p.timestamp,
                }
                for ticker, p in prices.items()
            }
            yield f"data: {json.dumps(payload)}\n\n"
        await asyncio.sleep(0.5)

@router.get("/api/stream/prices")
async def stream_prices():
    return StreamingResponse(
        price_event_generator(price_cache),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # Disable nginx buffering
        },
    )
```

---

## File Structure

```
backend/
  app/
    market/
      __init__.py           # Exports: PriceCache, create_market_data_source
      models.py             # PriceUpdate dataclass
      interface.py          # MarketDataSource ABC + PriceCache
      factory.py            # create_market_data_source()
      massive_client.py     # MassiveDataSource
      simulator.py          # SimulatorDataSource (wraps GBMSimulator)
      gbm.py                # GBMSimulator class (pure math, no async)
      seed_prices.py        # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS constants
```

---

## Application Lifecycle

| Event | Action |
|-------|--------|
| App startup | Create `PriceCache`; call `create_market_data_source(cache)`; call `await source.start(watchlist_tickers)` |
| SSE client connects | Immediately gets current cache snapshot; then receives updates every 500ms |
| User adds ticker | Call `await source.add_ticker(ticker)`; cache seeded immediately |
| User removes ticker | Call `await source.remove_ticker(ticker)`; evicted from cache |
| Trade execution | Read `price_cache.get(ticker).price` — always current |
| Portfolio snapshot (background task) | Read `price_cache.get_all()` to value all positions |
| App shutdown | Call `await source.stop()` to cancel background task |

---

## Behavior Differences Between Sources

| Aspect | Simulator | Massive API |
|--------|-----------|-------------|
| Update frequency | Every 500ms | Every 15s (free tier) / 2–5s (paid) |
| Price history | Starts fresh each session | Real market history available |
| After-hours | Always "live" | May show stale prices |
| New tickers | Random seed price ($50–$300) | Real last-traded price |
| External dependency | None | Internet + API key |
| Rate limit | N/A | 5 req/min (free), unlimited (paid) |

The SSE stream always sends at 500ms intervals regardless of source — clients don't experience the 15s polling gap because the cache holds the last-known price and it's re-emitted on every SSE tick.
