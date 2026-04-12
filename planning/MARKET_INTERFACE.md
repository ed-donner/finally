# Market Data Interface Design

This document defines the unified Python interface for retrieving stock prices in the FinAlly backend. The interface abstracts over two implementations:

1. **MassiveMarketDataSource** — real market data via the Massive API (used when `MASSIVE_API_KEY` is set)
2. **SimulatorMarketDataSource** — simulated prices using geometric Brownian motion (used when no API key is set)

All downstream code (SSE streaming, price cache, trade execution, portfolio valuation) is agnostic to the source.

---

## Architecture Overview

```text
┌─────────────────────────────────────────────────────────┐
│                    FastAPI Application                    │
│                                                          │
│  SSE Stream ◄──── PriceCache ◄──── MarketDataSource     │
│  Trade Exec ◄──── PriceCache       (ABC)                │
│  Portfolio  ◄──── PriceCache         │                  │
│                                      ├── Simulator      │
│                                      └── Massive        │
└─────────────────────────────────────────────────────────┘
```

- **MarketDataSource** (ABC): Defines the contract for generating/fetching prices
- **PriceCache**: In-memory store of latest prices per ticker, shared across all consumers
- The source selection happens once at startup based on the `MASSIVE_API_KEY` environment variable

---

## Abstract Interface

```python
# backend/market/base.py

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PriceTick:
    """A single price update for a ticker."""
    ticker: str
    price: float
    prev_close: float
    timestamp: datetime


class MarketDataSource(ABC):
    """Abstract interface for market data providers."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin generating/fetching prices for the given tickers.

        Called once at startup. The source should begin its internal
        update loop (background task for simulator, polling loop for Massive).
        """
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the update loop and clean up resources."""
        ...

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the tracked set.

        The source should begin producing prices for this ticker.
        For the simulator, this means adding it to the GBM loop.
        For Massive, this means including it in the next poll cycle.
        """
        ...

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the tracked set.

        The source should stop producing prices for this ticker.
        """
        ...

    @abstractmethod
    async def get_price_now(self, ticker: str) -> PriceTick | None:
        """Fetch a fresh price for a single ticker on demand.

        Used for on-demand price seeding (e.g., AI trades an off-watchlist
        ticker). Returns None if the ticker cannot be priced.

        For the simulator: generates a price instantly from its seed table.
        For Massive: makes a synchronous snapshot API call.
        """
        ...

    @abstractmethod
    def set_price_callback(self, callback) -> None:
        """Register a callback that receives PriceTick updates.

        The callback signature: async def on_price(tick: PriceTick) -> None

        The source calls this for every price update it generates.
        The PriceCache registers itself as this callback.
        """
        ...
```

---

## Price Cache

The `PriceCache` is the central in-memory store. All consumers read from it. The market data source writes to it via the callback.

```python
# backend/market/cache.py

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class CachedPrice:
    """Current state of a ticker's price in the cache."""
    ticker: str
    price: float
    prev_price: float          # Previous tick's price (for flash direction)
    prev_close: float          # Session/day baseline (for daily change)
    day_change: float          # price - prev_close
    day_change_pct: float      # (price - prev_close) / prev_close * 100
    direction: str             # "up", "down", or "flat"
    timestamp: datetime


class PriceCache:
    """In-memory price cache, updated by the MarketDataSource."""

    def __init__(self):
        self._prices: dict[str, CachedPrice] = {}

    async def on_price(self, tick) -> None:
        """Callback registered with MarketDataSource.

        Receives PriceTick, computes derived fields, stores CachedPrice.
        """
        existing = self._prices.get(tick.ticker)
        prev_price = existing.price if existing else tick.price

        if tick.prev_close and tick.prev_close != 0:
            day_change = tick.price - tick.prev_close
            day_change_pct = day_change / tick.prev_close * 100
        else:
            day_change = 0.0
            day_change_pct = 0.0

        if tick.price > prev_price:
            direction = "up"
        elif tick.price < prev_price:
            direction = "down"
        else:
            direction = "flat"

        self._prices[tick.ticker] = CachedPrice(
            ticker=tick.ticker,
            price=tick.price,
            prev_price=prev_price,
            prev_close=tick.prev_close,
            day_change=day_change,
            day_change_pct=day_change_pct,
            direction=direction,
            timestamp=tick.timestamp,
        )

    def get(self, ticker: str) -> Optional[CachedPrice]:
        """Get the latest cached price for a ticker."""
        return self._prices.get(ticker)

    def get_all(self) -> dict[str, CachedPrice]:
        """Get all cached prices."""
        return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache."""
        self._prices.pop(ticker, None)
```

---

## Source Factory

```python
# backend/market/factory.py

import os
from .base import MarketDataSource
from .cache import PriceCache


def create_market_data_source() -> MarketDataSource:
    """Create the appropriate market data source based on environment."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive import MassiveMarketDataSource
        return MassiveMarketDataSource(api_key=api_key)
    else:
        from .simulator import SimulatorMarketDataSource
        return SimulatorMarketDataSource()
```

---

## Massive Implementation Sketch

```python
# backend/market/massive.py

import asyncio
from datetime import datetime, timezone
import httpx
from .base import MarketDataSource, PriceTick

BASE_URL = "https://api.massive.com"

# Polling interval: 12 seconds = 5 calls/min (free tier limit)
FREE_TIER_POLL_INTERVAL = 12.0


class MassiveMarketDataSource(MarketDataSource):
    def __init__(self, api_key: str, poll_interval: float = FREE_TIER_POLL_INTERVAL):
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._prev_closes: dict[str, float] = {}
        self._callback = None
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None

    def set_price_callback(self, callback) -> None:
        self._callback = callback

    async def start(self, tickers: list[str]) -> None:
        self._tickers = set(tickers)
        self._client = httpx.AsyncClient(timeout=10.0)
        # Fetch initial snapshots to populate prev_close
        await self._poll_snapshots()
        # Start polling loop
        self._task = asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    async def add_ticker(self, ticker: str) -> None:
        self._tickers.add(ticker)
        # Fetch initial snapshot for the new ticker
        snap = await self._fetch_single_snapshot(ticker)
        if snap and self._callback:
            await self._callback(snap)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers.discard(ticker)
        self._prev_closes.pop(ticker, None)

    async def get_price_now(self, ticker: str) -> PriceTick | None:
        return await self._fetch_single_snapshot(ticker)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            try:
                await self._poll_snapshots()
            except Exception:
                pass  # Log error, continue polling

    async def _poll_snapshots(self) -> None:
        """Fetch batch snapshot for all tracked tickers."""
        if not self._tickers:
            return

        ticker_str = ",".join(sorted(self._tickers))
        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
        params = {"tickers": ticker_str, "apiKey": self._api_key}

        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            return

        now = datetime.now(timezone.utc)
        for t in data.get("tickers", []):
            ticker = t["ticker"]
            price = self._extract_price(t)
            if price is None:
                continue

            prev_close = t.get("prevDay", {}).get("c")
            if prev_close:
                self._prev_closes[ticker] = prev_close

            tick = PriceTick(
                ticker=ticker,
                price=price,
                prev_close=self._prev_closes.get(ticker, price),
                timestamp=now,
            )
            if self._callback:
                await self._callback(tick)

    async def _fetch_single_snapshot(self, ticker: str) -> PriceTick | None:
        """Fetch snapshot for a single ticker (on-demand seeding)."""
        url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
        params = {"apiKey": self._api_key}

        resp = await self._client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            return None

        t = data.get("ticker", {})
        price = self._extract_price(t)
        if price is None:
            return None

        prev_close = t.get("prevDay", {}).get("c", price)
        self._prev_closes[ticker] = prev_close

        return PriceTick(
            ticker=ticker,
            price=price,
            prev_close=prev_close,
            timestamp=datetime.now(timezone.utc),
        )

    @staticmethod
    def _extract_price(snapshot: dict) -> float | None:
        """Extract best available price from a snapshot object.

        Priority: lastQuote midpoint > lastTrade price > day close.
        """
        lq = snapshot.get("lastQuote", {})
        if lq.get("p") and lq.get("P"):
            return (lq["p"] + lq["P"]) / 2

        lt = snapshot.get("lastTrade", {})
        if lt.get("p"):
            return lt["p"]

        day = snapshot.get("day", {})
        if day.get("c"):
            return day["c"]

        return None
```

---

## Wiring at Startup

```python
# backend/main.py (relevant excerpt)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from market.factory import create_market_data_source
from market.cache import PriceCache

price_cache = PriceCache()
market_source = create_market_data_source()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load initial watchlist + position tickers from DB
    tickers = await get_tracked_tickers()

    # Wire up and start
    market_source.set_price_callback(price_cache.on_price)
    await market_source.start(tickers)

    yield

    await market_source.stop()


app = FastAPI(lifespan=lifespan)
```

---

## Tracked Tickers Management

The set of tracked tickers is the **union of watchlist tickers and open position tickers**. The application layer manages this:

```python
async def on_watchlist_add(ticker: str) -> None:
    """Called when a ticker is added to the watchlist."""
    await market_source.add_ticker(ticker)

async def on_watchlist_remove(ticker: str) -> None:
    """Called when a ticker is removed from the watchlist."""
    # Only stop tracking if user has no open position
    if not await has_open_position(ticker):
        await market_source.remove_ticker(ticker)
        price_cache.remove(ticker)

async def on_position_closed(ticker: str) -> None:
    """Called when a position is fully closed (quantity reaches 0)."""
    # Only stop tracking if ticker is not on the watchlist
    if not await is_on_watchlist(ticker):
        await market_source.remove_ticker(ticker)
        price_cache.remove(ticker)
```

---

## SSE Stream

The SSE endpoint reads from `PriceCache` and pushes updates to connected clients:

```python
# backend/api/stream.py

from sse_starlette.sse import EventSourceResponse

async def price_stream(request):
    async def event_generator():
        while True:
            prices = price_cache.get_all()
            for cached in prices.values():
                yield {
                    "event": "price",
                    "data": json.dumps({
                        "ticker": cached.ticker,
                        "price": cached.price,
                        "prev_price": cached.prev_price,
                        "prev_close": cached.prev_close,
                        "day_change": cached.day_change,
                        "day_change_pct": cached.day_change_pct,
                        "direction": cached.direction,
                        "timestamp": cached.timestamp.isoformat(),
                    })
                }
            await asyncio.sleep(0.5)  # Push every 500ms

    return EventSourceResponse(event_generator())
```

---

## File Structure

```text
backend/
└── market/
    ├── __init__.py
    ├── base.py          # MarketDataSource ABC + PriceTick dataclass
    ├── cache.py          # PriceCache
    ├── factory.py        # create_market_data_source()
    ├── massive.py        # MassiveMarketDataSource
    └── simulator.py      # SimulatorMarketDataSource
```
