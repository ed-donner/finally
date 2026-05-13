# Market Data Interface — Unified Python Design

## Purpose

This document defines the shared abstraction layer that decouples downstream code (SSE streaming, portfolio P&L, API routes) from the specific market data source in use. All code outside `backend/app/market/` must interact with prices only through this interface.

---

## Design Goals

1. **Pluggable source** — switch between the simulator and Massive API by setting one environment variable; zero other code changes.
2. **In-memory cache** — the SSE stream and all API routes read from a shared cache rather than calling the data source directly on every request.
3. **Single background task** — one asyncio task per source drives cache updates; no per-request polling.
4. **Async-first** — all I/O (HTTP calls, sleep loops) is async; cache reads are synchronous (dict lookup — fast enough to skip async overhead).

---

## Data Models

Defined in `backend/app/market/models.py`:

```python
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class PriceUpdate:
    ticker: str
    price: float
    prev_price: float
    timestamp: datetime
    change: float           # absolute dollar change
    change_pct: float       # percentage change (e.g. 1.23 = 1.23%)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' — used by frontend flash animation."""
        if self.change > 0:
            return "up"
        elif self.change < 0:
            return "down"
        return "flat"


@dataclass
class DailyBar:
    ticker: str
    date: str               # YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float | None = None
```

---

## Price Cache

Defined in `backend/app/market/cache.py`. The single shared mutable state that ties everything together.

```python
import asyncio
from datetime import datetime
from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory store for the latest price of every tracked ticker.

    The background market data task writes here; the SSE endpoint and API
    routes read here. Reads are plain dict lookups — no locking needed because
    CPython's GIL makes dict reads atomic. Writes use a lock to prevent
    torn writes during bulk updates.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = asyncio.Lock()
        self._subscribers: list[asyncio.Queue] = []

    async def update(self, updates: list[PriceUpdate]) -> None:
        async with self._lock:
            for u in updates:
                self._prices[u.ticker] = u
        # Notify all SSE subscribers
        for queue in self._subscribers:
            for u in updates:
                await queue.put(u)

    def get(self, ticker: str) -> PriceUpdate | None:
        return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        return dict(self._prices)

    def get_tickers(self) -> list[str]:
        return list(self._prices.keys())

    def subscribe(self) -> asyncio.Queue:
        """Return a queue that receives every PriceUpdate as it arrives.
        Used by the SSE endpoint to fan out updates to connected clients.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
```

---

## Abstract Interface

Defined in `backend/app/market/interface.py`:

```python
from abc import ABC, abstractmethod
from .models import PriceUpdate, DailyBar


class MarketDataSource(ABC):
    """Abstract base class for all market data sources.

    Concrete implementations: MassiveClient, MarketSimulator.
    Neither should be used directly outside backend/app/market/.
    All external code reads from PriceCache.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background polling/simulation loop.

        Args:
            tickers: Initial list of ticker symbols to track.
                     Implementations should watch for watchlist changes
                     by re-reading from the cache or database.
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the background task."""
        ...

    @abstractmethod
    def add_ticker(self, ticker: str) -> None:
        """Register a new ticker to be tracked on the next poll cycle."""
        ...

    @abstractmethod
    def remove_ticker(self, ticker: str) -> None:
        """Deregister a ticker. Its entry remains in cache until overwritten."""
        ...

    @abstractmethod
    async def get_daily_bars(
        self, ticker: str, from_date: str, to_date: str
    ) -> list[DailyBar]:
        """Fetch historical daily OHLCV bars.

        Args:
            ticker: Ticker symbol.
            from_date: ISO date string YYYY-MM-DD.
            to_date: ISO date string YYYY-MM-DD.

        Returns:
            List of DailyBar sorted ascending by date.
        """
        ...
```

---

## Massive API Implementation

Defined in `backend/app/market/massive_client.py`.

Polls the Massive API snapshot endpoint on a configurable interval and writes results to the shared `PriceCache`.

```python
import asyncio
import logging
import httpx
from datetime import datetime, timezone

from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar

logger = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"


class MassiveClient(MarketDataSource):
    """Polls the Massive (formerly Polygon.io) REST snapshot endpoint.

    Poll interval:
      - Free tier (5 req/min): set poll_interval=15.0
      - Paid tiers (unlimited): set poll_interval=2.0–5.0
    """

    def __init__(self, api_key: str, cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = cache
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._task: asyncio.Task | None = None
        self._headers = {"Authorization": f"Bearer {api_key}"}

    async def start(self, tickers: list[str]) -> None:
        self._tickers = set(tickers)
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("MassiveClient started, polling every %.1fs", self._poll_interval)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MassiveClient stopped")

    def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker.upper())

    def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker.upper())

    async def _poll_loop(self) -> None:
        async with httpx.AsyncClient(timeout=10.0) as http:
            while True:
                if self._tickers:
                    updates = await self._fetch_snapshots(http)
                    if updates:
                        await self._cache.update(updates)
                await asyncio.sleep(self._poll_interval)

    async def _fetch_snapshots(self, http: httpx.AsyncClient) -> list[PriceUpdate]:
        tickers_param = ",".join(sorted(self._tickers))
        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        try:
            response = await http.get(
                url,
                params={"tickers": tickers_param},
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                logger.warning("Rate limited; backing off 60s")
                await asyncio.sleep(60)
            else:
                logger.error("HTTP error %d from Massive API", e.response.status_code)
                await asyncio.sleep(5)
            return []
        except httpx.RequestError as e:
            logger.error("Network error polling Massive API: %s", e)
            await asyncio.sleep(5)
            return []

        return self._parse_snapshots(data)

    def _parse_snapshots(self, data: dict) -> list[PriceUpdate]:
        updates = []
        for t in data.get("tickers", []):
            try:
                # Use lastTrade.p if available (requires Advanced plan), else day.c
                last_trade = t.get("lastTrade") or {}
                price = last_trade.get("p") or t["day"]["c"]
                prev_price = t["prevDay"]["c"]
                change = price - prev_price
                change_pct = (change / prev_price * 100) if prev_price else 0.0
                ts_ns = t.get("updated", 0)
                timestamp = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
                updates.append(PriceUpdate(
                    ticker=t["ticker"],
                    price=price,
                    prev_price=prev_price,
                    timestamp=timestamp,
                    change=change,
                    change_pct=change_pct,
                ))
            except (KeyError, TypeError, ZeroDivisionError) as e:
                logger.warning("Failed to parse snapshot for %s: %s", t.get("ticker"), e)
        return updates

    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
        params = {"adjusted": "true", "sort": "asc", "limit": 5000}
        async with httpx.AsyncClient(timeout=15.0) as http:
            try:
                response = await http.get(url, params=params, headers=self._headers)
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, Exception) as e:
                logger.error("Failed to fetch daily bars for %s: %s", ticker, e)
                return []

        bars = []
        for r in data.get("results", []):
            dt = datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc)
            bars.append(DailyBar(
                ticker=ticker,
                date=dt.strftime("%Y-%m-%d"),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=int(r["v"]),
                vwap=r.get("vw"),
            ))
        return bars
```

---

## Simulator Implementation (stub)

Defined in `backend/app/market/simulator.py`. See `MARKET_SIMULATOR.md` for the full design.

```python
from .interface import MarketDataSource
from .cache import PriceCache
from .models import PriceUpdate, DailyBar


class MarketSimulator(MarketDataSource):
    """Generates synthetic price data using Geometric Brownian Motion.
    See MARKET_SIMULATOR.md for the full design and implementation.
    """

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        # ... (see MARKET_SIMULATOR.md)

    async def start(self, tickers: list[str]) -> None: ...
    async def stop(self) -> None: ...
    def add_ticker(self, ticker: str) -> None: ...
    def remove_ticker(self, ticker: str) -> None: ...
    async def get_daily_bars(self, ticker: str, from_date: str, to_date: str) -> list[DailyBar]:
        return []  # Simulator doesn't have historical data; return empty list
```

---

## Factory

Defined in `backend/app/market/factory.py`. Reads the environment variable and returns the correct implementation.

```python
import os
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(cache: PriceCache) -> MarketDataSource:
    """Return the appropriate MarketDataSource based on environment config.

    - If MASSIVE_API_KEY is set and non-empty → MassiveClient
    - Otherwise → MarketSimulator

    The poll interval for MassiveClient is derived from MASSIVE_POLL_INTERVAL
    (seconds, float). Defaults to 15.0 (safe for the free tier).
    """
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveClient
        poll_interval = float(os.getenv("MASSIVE_POLL_INTERVAL", "15.0"))
        logger.info("Using Massive API (poll interval: %.1fs)", poll_interval)
        return MassiveClient(api_key=api_key, cache=cache, poll_interval=poll_interval)

    from .simulator import MarketSimulator
    logger.info("Using market simulator (no MASSIVE_API_KEY set)")
    return MarketSimulator(cache=cache)
```

---

## Module Structure

```
backend/app/market/
├── __init__.py          # re-exports: PriceCache, PriceUpdate, DailyBar, create_market_data_source
├── models.py            # PriceUpdate, DailyBar dataclasses
├── cache.py             # PriceCache — shared in-memory store + SSE fan-out
├── interface.py         # MarketDataSource abstract base class
├── massive_client.py    # MassiveClient: polls Massive REST API
├── simulator.py         # MarketSimulator: GBM-based synthetic prices
└── factory.py           # create_market_data_source() factory function
```

---

## Integration with FastAPI

In `backend/app/main.py`, the cache and source are created at startup and stored as app state:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from .market import PriceCache, create_market_data_source
from .database import get_default_watchlist

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                   "NVDA", "META", "JPM", "V", "NFLX"]

@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    # Load tickers from DB (falls back to defaults on fresh install)
    tickers = await get_default_watchlist() or DEFAULT_TICKERS
    await source.start(tickers)
    app.state.cache = cache
    app.state.market = source
    yield
    await source.stop()

app = FastAPI(lifespan=lifespan)
```

### SSE Endpoint

```python
from fastapi import Request
from fastapi.responses import StreamingResponse
import json

@app.get("/api/stream/prices")
async def stream_prices(request: Request):
    cache: PriceCache = request.app.state.cache
    queue = cache.subscribe()

    async def event_generator():
        try:
            # Send current state immediately on connect
            for update in cache.get_all().values():
                payload = {
                    "ticker": update.ticker,
                    "price": update.price,
                    "prev_price": update.prev_price,
                    "change": update.change,
                    "change_pct": update.change_pct,
                    "direction": update.direction,
                    "timestamp": update.timestamp.isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n"

            # Then stream new updates as they arrive
            while not await request.is_disconnected():
                update = await asyncio.wait_for(queue.get(), timeout=30.0)
                payload = {
                    "ticker": update.ticker,
                    "price": update.price,
                    "prev_price": update.prev_price,
                    "change": update.change,
                    "change_pct": update.change_pct,
                    "direction": update.direction,
                    "timestamp": update.timestamp.isoformat(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        finally:
            cache.unsubscribe(queue)

    return StreamingResponse(event_generator(), media_type="text/event-stream")
```

### Watchlist Add/Remove (updating the source)

```python
@app.post("/api/watchlist")
async def add_ticker(body: dict, request: Request):
    ticker = body["ticker"].upper()
    request.app.state.market.add_ticker(ticker)
    # ... save to DB

@app.delete("/api/watchlist/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    request.app.state.market.remove_ticker(ticker.upper())
    # ... remove from DB
```

---

## Design Decisions

**Why not use the official `massive` Python library?**
The official library wraps the API but is synchronous. Since FastAPI is async, using `httpx.AsyncClient` directly gives full async/await support and avoids blocking the event loop. The library is fine for scripts and notebooks but adds complexity in an async context.

**Why a shared cache instead of calling the API on every request?**
The SSE stream pushes to all connected clients (potentially many) and polling the API on every SSE tick would immediately exhaust the free-tier rate limit. The cache decouples update frequency (limited by API rate) from push frequency (driven by user demand).

**Why does `PriceCache.subscribe()` return an asyncio.Queue?**
SSE connections are long-lived; each connected browser tab is one consumer. The queue-per-consumer fan-out pattern is the standard asyncio approach: the background task puts once, multiple queues receive independently without contention.
