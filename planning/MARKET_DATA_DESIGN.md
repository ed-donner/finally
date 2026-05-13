# Market Data Backend — Design & Implementation Reference

This document is the definitive implementation guide for the FinAlly market data subsystem. It covers the unified interface, price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration. All code reflects the actual implementation in `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy)
6. [Seed Prices & Parameters — `seed_prices.py`](#6-seed-prices--parameters--seed_pricespy)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint--streampy)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Testing Strategy](#14-testing-strategy)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Architecture Overview

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation (GET /api/portfolio)
        └──→ Trade execution (POST /api/portfolio/trade)
```

**Push model**: data sources write to the `PriceCache` on their own schedule. All readers (SSE, portfolio, trades) read from the cache. There is no direct coupling between producers and consumers.

**Strategy pattern**: both data sources implement the same `MarketDataSource` ABC. All downstream code is source-agnostic. The factory selects the correct implementation at startup based on environment variables.

**Thread safety**: `PriceCache` uses `threading.Lock` because `MassiveDataSource` runs synchronous Polygon.io calls in `asyncio.to_thread()` — a real OS thread — where `asyncio.Lock` gives no protection.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                          #             create_market_data_source, create_stream_router
      models.py           # PriceUpdate frozen dataclass
      cache.py            # PriceCache — thread-safe in-memory store
      interface.py        # MarketDataSource ABC
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py        # GBMSimulator + SimulatorDataSource
      massive_client.py   # MassiveDataSource (Polygon.io REST polling)
      factory.py          # create_market_data_source() — env-driven factory
      stream.py           # create_stream_router() — FastAPI SSE endpoint
```

Each file has a single responsibility. The `__init__.py` is the only import surface used by the rest of the backend.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the only data structure that leaves the market data layer. All consumers work exclusively with this type.

```python
# backend/app/market/models.py

from __future__ import annotations

import time
from dataclasses import dataclass, field


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

**Key design decisions:**

- `frozen=True` — price updates are immutable value objects, safe to share across async tasks without copying
- `slots=True` — minor memory optimization; we create many of these per second
- Computed properties (`change`, `direction`, `change_percent`) — derived from `price` and `previous_price`, so they can never be stale or inconsistent
- `to_dict()` — single serialization point used by both SSE and REST responses

---

## 4. Price Cache — `cache.py`

The central data hub. Data sources write to it; SSE streaming and portfolio valuation read from it.

```python
# backend/app/market/cache.py

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        If this is the first update for the ticker, previous_price == price (direction='flat').
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
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Bumped on every update. Used by SSE for change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Why a version counter?**

The SSE loop polls the cache every 500ms. Without a version counter, it would serialize and send all prices every tick even when nothing changed (e.g., Massive API only updates every 15s). The version counter lets SSE skip transmissions when no new data has arrived:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

---

## 5. Abstract Interface — `interface.py`

```python
# backend/app/market/interface.py

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes from PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

**Why the source writes to the cache instead of returning prices**: this push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but SSE always reads from the cache at its own 500ms cadence. The SSE layer needs no knowledge of which data source is active or what its update interval is.

---

## 6. Seed Prices & Parameters — `seed_prices.py`

Constants only — no logic, no runtime imports. Shared by both the simulator (for GBM initial state) and the Massive client (potential fallback).

```python
# backend/app/market/seed_prices.py

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement per tick)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong upward drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added via watchlist)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups used to build the Cholesky correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Pairwise correlation coefficients
INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors / unknown tickers
TSLA_CORR          = 0.3   # TSLA is in the tech set but does its own thing
```

---

## 7. GBM Simulator — `simulator.py`

Two classes in one file:

- **`GBMSimulator`** — pure math engine; stateful, advances prices one tick at a time
- **`SimulatorDataSource`** — `MarketDataSource` implementation; wraps `GBMSimulator` in an async loop and writes to `PriceCache`

### 7.1 GBMSimulator — The Math Engine

**Formula**: `S(t+dt) = S(t) × exp((μ - σ²/2)·dt + σ·√dt·Z)`

Where `Z` is a correlated standard normal draw (via Cholesky decomposition of the sector correlation matrix). The tiny `dt` (~8.5×10⁻⁸ for 500ms ticks) produces sub-cent moves per tick that accumulate naturally into realistic price action.

```python
# backend/app/market/simulator.py (GBMSimulator portion)

import math
import random
import numpy as np

from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES, TICKER_PARAMS, TSLA_CORR,
)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec → an event roughly every 50 seconds
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker and rebuild the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and rebuild the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add ticker without rebuilding Cholesky (used during batch init)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild Cholesky decomposition of the correlation matrix.

        Called on every add/remove. O(n²) but n is always < 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Sector-based correlation between two tickers."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.2 SimulatorDataSource — Async Wrapper

```python
# backend/app/market/simulator.py (SimulatorDataSource portion)

import asyncio
import logging
from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator."""

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache before the loop starts — SSE has data on its first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)  # Seed immediately
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

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

**Key behaviors:**

- **Immediate seeding**: cache is populated with seed prices *before* the loop begins — no blank-screen delay on first SSE connection
- **Graceful cancellation**: `stop()` cancels the task and awaits it, catching `CancelledError` — clean shutdown during FastAPI lifespan teardown
- **Exception resilience**: per-step exception handling means one bad tick never kills the feed

---

## 8. Massive API Client — `massive_client.py`

Polls the Polygon.io snapshot REST endpoint on a configurable interval. The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.

```python
# backend/app/market/massive_client.py

from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      Free tier: 5 req/min → poll every 15s (default)
      Paid tiers: poll every 2-5s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # Immediate first poll so cache has data right away
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch all snapshots, update cache."""
        if not self._tickers or not self._client:
            return

        try:
            # Synchronous Massive client runs in a thread — never blocks the event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — loop retries on next interval.
            # Common failures: 401 bad key, 429 rate limit, network timeout.

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs inside asyncio.to_thread()."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

**Error handling philosophy:**

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error. Poller continues — user can fix the key and restart. |
| 429 Rate Limited | Logged as error. Retries automatically after `poll_interval` seconds. |
| Network timeout | Logged as error. Retries automatically on next cycle. |
| Malformed snapshot | Individual ticker skipped with warning. Others still processed. |
| All tickers fail | Cache retains last-known prices. SSE keeps streaming stale (but non-empty) data. |

---

## 9. Factory — `factory.py`

```python
# backend/app/market/factory.py

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select the market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

**Usage at startup:**

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 10. SSE Streaming Endpoint — `stream.py`

The SSE endpoint holds open a long-lived HTTP connection and pushes price updates to the client as `text/event-stream`. The factory function injects the `PriceCache` without globals.

```python
# backend/app/market/stream.py

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator: yields SSE-formatted price events every ~500ms.

    Uses version-based change detection to skip sends when no new data exists
    (important for Massive API which only updates every 15s).
    """
    yield "retry: 1000\n\n"  # Client reconnects after 1s on disconnect

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

**Wire format** — each event looks like:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

**Frontend consumption:**

```typescript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
    // prices["AAPL"] = { ticker, price, previous_price, change, change_percent, direction, timestamp }
};

eventSource.onerror = () => {
    // EventSource retries automatically — the `retry: 1000` directive
    // tells it to wait 1s before reconnecting.
};
```

**Why poll-and-push instead of event-driven?** Regular 500ms intervals produce evenly-spaced updates, which is important for the frontend sparkline charts. An event-driven approach (notify SSE when cache updates) would produce bursty updates at the data source's cadence.

---

## 11. FastAPI Lifecycle Integration

The market data system starts and stops with the FastAPI application using the `lifespan` context manager.

```python
# backend/app/main.py (sketch — full implementation TBD by Backend Engineer)

from contextlib import asynccontextmanager
from fastapi import FastAPI, APIRouter, Depends, HTTPException

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create the market data source (simulator or Massive based on env)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await db.get_watchlist_tickers()  # ["AAPL", "GOOGL", ...]
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# FastAPI dependencies for injecting market data into route handlers

def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

**Accessing market data from route handlers:**

```python
router = APIRouter(prefix="/api")

@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(
            status_code=400,
            detail=f"Price not yet available for {trade.ticker}. Please wait a moment.",
        )
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    await db.insert_watchlist(payload.ticker)
    await source.add_ticker(payload.ticker)
    current_price = price_cache.get_price(payload.ticker)
    return {"ticker": payload.ticker, "price": current_price}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist(ticker)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # Keep tracking if position open
    return {"status": "ok"}
```

---

## 12. Watchlist Coordination

When the watchlist changes (via REST API or LLM chat), the market data source must be notified.

### Adding a Ticker

```
User/LLM → POST /api/watchlist {"ticker": "PYPL"}
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on next poll (up to 15s delay)
  → Return {ticker: "PYPL", price: <current or None>}
```

### Removing a Ticker

```
User/LLM → DELETE /api/watchlist/PYPL
  → Delete from watchlist table (SQLite)
  → Check for open position (positions table)
  → If no position: await source.remove_ticker("PYPL")
      Both sources: remove from active set + remove from cache
  → Return {"status": "ok"}
```

### Edge Case: Ticker has an Open Position

If a user removes a ticker from the watchlist but still holds shares, the data source must keep tracking it for portfolio valuation. The route checks the positions table before calling `remove_ticker()`:

```python
position = await db.get_position(ticker=ticker, user_id="default")
if position is None or position.quantity == 0:
    await source.remove_ticker(ticker)
# If position exists, ticker stays in the data source silently
```

---

## 13. Error Handling & Edge Cases

### Empty Watchlist at Startup

If the database has no watchlist entries, `start()` receives `[]`. Both sources handle this gracefully — the simulator produces no prices, the Massive poller skips its API call. SSE sends empty events. When the user adds a ticker, the source starts tracking it immediately.

### Price Cache Miss During Trade

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. Massive may have a brief gap after adding a new ticker — the HTTP 400 with a clear message is the correct response.

### Massive API Key Invalid

First poll fails with 401. The poller logs the error and keeps retrying on interval. SSE streams empty data. Fix: correct the key and restart the container.

### Thread Safety Under Load

`PriceCache` uses `threading.Lock` — a mutex. Under normal load (10 tickers, 2 updates/sec), lock contention is negligible. The critical section is a dict lookup and assignment.

### Simulator Numerical Stability

GBM with tiny `dt` produces very small per-tick moves. The exponential formulation is numerically stable, and prices are `round()`ed to 2 decimal places in `GBMSimulator.step()`. Prices are guaranteed positive (exponential of any real number is always positive).

---

## 14. Testing Strategy

All tests are in `backend/tests/market/`. Run with `uv run --extra dev pytest -v`.

### PriceCache Tests

```python
# backend/tests/market/test_cache.py

from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.50)
    assert update.direction == "flat"
    assert update.previous_price == 190.50

def test_direction_up():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00

def test_direction_down():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 189.00)
    assert update.direction == "down"

def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
    cache.update("AAPL", 191.00)
    assert cache.version == v0 + 2

def test_remove():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None

def test_get_price_convenience():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("NOPE") is None
```

### GBMSimulator Tests

```python
# backend/tests/market/test_simulator.py

from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    result = sim.step()
    assert set(result.keys()) == {"AAPL", "GOOGL"}

def test_prices_always_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_initial_prices_match_seeds():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

def test_add_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("TSLA")
    result = sim.step()
    assert "TSLA" in result

def test_remove_ticker():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    sim.remove_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" not in result and "AAPL" in result

def test_empty_step():
    sim = GBMSimulator(tickers=[])
    assert sim.step() == {}

def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None  # 1 ticker: no correlation matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None  # 2 tickers: matrix exists
```

### SimulatorDataSource Integration Tests

```python
# backend/tests/market/test_simulator_source.py

import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        # Cache should have seed prices immediately (before first loop tick)
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Second stop should not raise

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

        await source.stop()
```

### MassiveDataSource Tests (Mocked)

```python
# backend/tests/market/test_massive.py

from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        snapshots = [
            make_snapshot("AAPL", 190.50, 1707580800000),
            make_snapshot("GOOGL", 175.25, 1707580800000),
        ]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        good = make_snapshot("AAPL", 190.50, 1707580800000)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # Causes AttributeError in _poll_once

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None
```

### Factory Tests

```python
# backend/tests/market/test_factory.py

import pytest
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


def test_no_api_key_returns_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)


def test_api_key_set_returns_massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-abc123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)


def test_empty_api_key_returns_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

---

## 15. Configuration Reference

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Shock event probability per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step (fraction of a trading year) |
| SSE push interval | `_generate_events()` | `0.5` s | Time between SSE pushes to the client |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser EventSource reconnect delay |

### Module Public API (`__init__.py`)

```python
# backend/app/market/__init__.py

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

**Minimal usage example** — all you need to get market data running:

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# Startup
cache = PriceCache()
source = create_market_data_source(cache)          # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices (anywhere in the app)
update = cache.get("AAPL")          # PriceUpdate or None
price  = cache.get_price("AAPL")    # float or None
all_px = cache.get_all()            # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# SSE endpoint (register on FastAPI app)
app.include_router(create_stream_router(cache))

# Shutdown
await source.stop()
```
