# Market Data Interface Design

Unified Python interface for retrieving stock prices. The backend selects the implementation based on environment variables: Massive API if `MASSIVE_API_KEY` is set, otherwise the built-in simulator.

## Core Data Model

```python
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceUpdate:
    """A single price update for one ticker."""
    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    direction: str  # "up", "down", "unchanged"
```

This is the universal output format. Both implementations produce `PriceUpdate` objects. The SSE stream serializes these to JSON for the frontend.

## Abstract Interface

```python
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Interface that both the simulator and Massive client implement."""

    @abstractmethod
    async def start(self) -> None:
        """Start producing price updates (background task, polling loop, etc.)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down."""

    @abstractmethod
    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        """Add a ticker to track. For the simulator, seed_price sets the starting
        price. For Massive, seed_price is ignored (real price is fetched on next poll)."""

    @abstractmethod
    def unregister_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker. Removes it from the price cache."""

    @abstractmethod
    def get_latest(self, ticker: str) -> PriceUpdate | None:
        """Get the most recent price update for a ticker, or None if unknown."""

    @abstractmethod
    def get_all_latest(self) -> dict[str, PriceUpdate]:
        """Get the most recent price update for every tracked ticker."""
```

## Price Cache

Both implementations write to a shared in-memory `PriceCache`. The SSE endpoint reads from it.

```python
import threading
from datetime import datetime, timezone


class PriceCache:
    """Thread-safe cache of the latest price per ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = threading.Lock()

    def update(self, ticker: str, price: float, timestamp: datetime) -> PriceUpdate:
        """Update the cache and return a PriceUpdate with direction computed."""
        with self._lock:
            prev = self._prices.get(ticker)
            prev_price = prev.price if prev else price

            if price > prev_price:
                direction = "up"
            elif price < prev_price:
                direction = "down"
            else:
                direction = "unchanged"

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                prev_price=prev_price,
                timestamp=timestamp,
                direction=direction,
            )
            self._prices[ticker] = update
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)
```

## Factory Function

```python
import os


def create_market_data_source() -> MarketDataSource:
    """Create the appropriate market data source based on environment config."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveMarketData
        return MassiveMarketData(api_key=api_key)
    else:
        from .simulator import SimulatorMarketData
        return SimulatorMarketData()
```

## Implementation: Massive Client

```python
import asyncio
import httpx
from datetime import datetime, timezone


class MassiveMarketData(MarketDataSource):

    BASE_URL = "https://api.polygon.io"

    def __init__(self, api_key: str, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._poll_interval = poll_interval  # 15s for free tier
        self._tickers: set[str] = set()
        self._cache = PriceCache()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        self._tickers.add(ticker.upper())

    def unregister_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker.upper())
        self._cache.remove(ticker.upper())

    def get_latest(self, ticker: str) -> PriceUpdate | None:
        return self._cache.get(ticker.upper())

    def get_all_latest(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient() as client:
            while True:
                if self._tickers:
                    await self._fetch_snapshots(client)
                await asyncio.sleep(self._poll_interval)

    async def _fetch_snapshots(self, client: httpx.AsyncClient) -> None:
        ticker_str = ",".join(sorted(self._tickers))
        url = f"{self.BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        try:
            resp = await client.get(
                url,
                params={"tickers": ticker_str},
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=10.0,
            )
            resp.raise_for_status()
            data = resp.json()

            now = datetime.now(timezone.utc)
            for t in data.get("tickers", []):
                ticker = t["ticker"]
                last_trade = t.get("lastTrade", {})
                price = last_trade.get("p")
                if price is not None:
                    self._cache.update(ticker, float(price), now)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                # Rate limited — back off for one interval
                pass
            else:
                raise
        except httpx.RequestError:
            # Network error — skip this cycle, retry next interval
            pass
```

## Implementation: Simulator

See [MARKET_SIMULATOR.md](./MARKET_SIMULATOR.md) for full design. The simulator implements the same `MarketDataSource` interface:

```python
class SimulatorMarketData(MarketDataSource):

    def __init__(self, tick_interval: float = 0.5) -> None:
        self._tick_interval = tick_interval
        self._cache = PriceCache()
        self._engine: SimulationEngine  # GBM price generation
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._tick_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def register_ticker(self, ticker: str, seed_price: float | None = None) -> None:
        self._engine.add_ticker(ticker, seed_price)

    def unregister_ticker(self, ticker: str) -> None:
        self._engine.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_latest(self, ticker: str) -> PriceUpdate | None:
        return self._cache.get(ticker)

    def get_all_latest(self) -> dict[str, PriceUpdate]:
        return self._cache.get_all()

    async def _tick_loop(self) -> None:
        while True:
            updates = self._engine.tick()
            now = datetime.now(timezone.utc)
            for ticker, price in updates.items():
                self._cache.update(ticker, price, now)
            await asyncio.sleep(self._tick_interval)
```

## Integration with FastAPI

```python
# In the FastAPI app lifespan
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    source = create_market_data_source()

    # Register seed tickers from the database
    watchlist_tickers = get_watchlist_tickers_from_db()
    for ticker in watchlist_tickers:
        source.register_ticker(ticker)

    await source.start()
    app.state.market_data = source
    yield
    await source.stop()
```

## SSE Streaming Endpoint

```python
from fastapi import Request
from fastapi.responses import StreamingResponse
import asyncio
import json


async def price_stream(request: Request):
    source: MarketDataSource = request.app.state.market_data

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            updates = source.get_all_latest()
            for ticker, update in updates.items():
                data = json.dumps({
                    "ticker": update.ticker,
                    "price": update.price,
                    "prev_price": update.prev_price,
                    "timestamp": update.timestamp.isoformat(),
                    "direction": update.direction,
                })
                yield f"data: {data}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

## File Structure

```
backend/
  src/
    market/
      __init__.py
      models.py              # PriceUpdate dataclass
      cache.py               # PriceCache
      interface.py           # MarketDataSource ABC
      factory.py             # create_market_data_source()
      massive_client.py      # MassiveMarketData
      simulator.py           # SimulatorMarketData
      engine.py              # SimulationEngine (GBM math)
      seed_prices.py         # Default ticker seed prices
```

## Watchlist Integration

When tickers are added/removed via the REST API or AI chat:

```python
# In the watchlist route handler
@router.post("/api/watchlist")
async def add_to_watchlist(request: Request, body: AddTickerRequest):
    ticker = body.ticker.upper()
    # 1. Insert into database
    add_ticker_to_db(ticker)
    # 2. Notify market data source to start tracking
    request.app.state.market_data.register_ticker(ticker)
    return {"ticker": ticker, "status": "added"}


@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(request: Request, ticker: str):
    ticker = ticker.upper()
    # 1. Remove from database
    remove_ticker_from_db(ticker)
    # 2. Notify market data source to stop tracking
    request.app.state.market_data.unregister_ticker(ticker)
    return {"ticker": ticker, "status": "removed"}
```

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| ABC with two implementations | Clean separation; downstream code never knows the source |
| PriceCache as separate class | Shared concern between implementations; thread-safe |
| `direction` computed in cache | Single place for up/down/unchanged logic |
| httpx for Massive client | Async-native, modern Python HTTP client |
| Factory function, not DI framework | Simple; one env var, one if-statement |
| 500ms SSE push interval | Matches simulator tick rate; smooth enough for UI animations |
| Polling REST, not WebSocket | Simpler; works on all Massive tiers; sufficient for our update rate |
