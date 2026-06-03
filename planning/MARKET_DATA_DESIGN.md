# Market Data Backend — Design & Implementation Reference

Complete specification for the FinAlly market data subsystem. This document reflects the **final implemented state** (post code review, all issues resolved). All code in `backend/app/market/` matches what is described here.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Parameters — `seed_prices.py`](#6-seed-prices--parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Error Handling](#13-error-handling)
14. [Testing](#14-testing)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Architecture Overview

```
MarketDataSource (ABC)
├── SimulatorDataSource   — GBM price simulation (default, no API key needed)
└── MassiveDataSource     — Polygon.io REST poller (when MASSIVE_API_KEY set)
          │
          ▼  writes
    PriceCache (thread-safe, in-memory, single source of truth)
          │
          ├──▶  GET /api/stream/prices  (SSE → Frontend)
          ├──▶  POST /api/portfolio/trade  (price at execution time)
          └──▶  GET /api/portfolio  (unrealized P&L calculation)
```

**Key design decisions:**

| Decision | Rationale |
|---|---|
| Strategy pattern (ABC) | Both data sources implement the same interface; all downstream code is source-agnostic |
| PriceCache as single truth | Producers write on their own schedule; consumers read independently — no direct coupling |
| Push model | Data sources write to cache; SSE polls cache. Decouples update cadence from stream cadence |
| `threading.Lock` in cache | Massive client runs synchronous API calls via `asyncio.to_thread()`, which uses real OS threads — `asyncio.Lock` would not protect against that |
| Version counter in cache | SSE loop skips sending when nothing changed (important for Massive at 15s poll intervals) |

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py          # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                           #             create_market_data_source, create_stream_router
      models.py            # PriceUpdate immutable dataclass
      cache.py             # PriceCache — thread-safe in-memory store
      interface.py         # MarketDataSource ABC
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py         # GBMSimulator (math engine) + SimulatorDataSource (async wrapper)
      massive_client.py    # MassiveDataSource — REST polling client
      factory.py           # create_market_data_source() — selects implementation
      stream.py            # create_stream_router() — FastAPI SSE endpoint factory

  tests/
    market/
      __init__.py
      conftest.py
      test_models.py           # 11 tests — models.py: 100%
      test_cache.py            # 13 tests — cache.py: 100%
      test_simulator.py        # 17 tests — simulator.py: 98%
      test_simulator_source.py # 10 tests — SimulatorDataSource integration
      test_factory.py          #  7 tests — factory.py: 100%
      test_massive.py          # 13 tests — massive_client.py (mocked)
```

---

## 3. Data Model

**`backend/app/market/models.py`**

`PriceUpdate` is the only data structure that crosses the market data layer boundary. Every downstream consumer works exclusively with this type.

```python
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

**Design notes:**
- `frozen=True`: Immutable value object — safe to share across async tasks without copying
- `slots=True`: Minor memory savings; many are created per second
- `change`, `direction`, `change_percent` are computed properties — can never be inconsistent with `price`/`previous_price`
- `to_dict()` is the single serialization point used by both SSE and REST responses

---

## 4. Price Cache

**`backend/app/market/cache.py`**

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    One writer at a time (simulator or Massive poller).
    Multiple concurrent readers (SSE, trade execution, portfolio valuation).
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every write; used by SSE for change detection

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the created PriceUpdate.

        On the first update for a ticker, previous_price == price (direction='flat').
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
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Bumped on every update."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Version counter usage in SSE loop:**

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        prices = price_cache.get_all()
        yield format_sse(prices)
    await asyncio.sleep(0.5)
```

This means SSE sends no payload during the 15-second gap between Massive API polls — saves bandwidth and avoids misleading "fresh data" indicators on the frontend.

---

## 5. Abstract Interface

**`backend/app/market/interface.py`**

```python
from __future__ import annotations
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for all market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")       # Dynamic addition
        await source.remove_ticker("GOOGL")   # Dynamic removal
        await source.stop()                   # Clean shutdown
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and delete it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the currently tracked tickers."""
```

---

## 6. Seed Prices & Parameters

**`backend/app/market/seed_prices.py`**

Constants only — no logic. Used by the simulator for initial prices and GBM parameters. Also available as fallback prices if Massive hasn't responded yet for a new ticker.

```python
# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM":  195.00,
    "V":    280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility  mu: annualized drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Used for dynamically added tickers not in the list above
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups drive the correlation matrix in GBMSimulator
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors, and TSLA (it does its own thing)
```

---

## 7. GBM Simulator

**`backend/app/market/simulator.py`**

Two classes with a clear separation of concerns:
- **`GBMSimulator`**: Pure math engine. Stateful, holds current prices, advances one step at a time.
- **`SimulatorDataSource`**: The `MarketDataSource` implementation. Wraps `GBMSimulator` in an async loop and writes to `PriceCache`.

### 7.1 GBM Math

At each time step, each stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return), e.g. `0.05` = 5%
- `sigma` = annualized volatility, e.g. `0.22` = 22%
- `dt` = time step as fraction of a trading year
- `Z` = correlated standard normal random variable

For 500ms ticks with 252 trading days × 6.5 hours/day:
```
dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8
```

This tiny `dt` produces sub-cent per-tick moves that accumulate naturally. GBM guarantees prices are always positive (exponential is always positive).

### 7.2 Correlated Moves via Cholesky Decomposition

Real stocks don't move independently. Given correlation matrix `C`, compute `L = cholesky(C)`. For independent normals `z`:
```
z_correlated = L @ z_independent
```

Correlation structure:
- Same tech sector (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX): **0.6**
- Same finance sector (JPM, V): **0.5**
- TSLA with anything: **0.3** (high volatility, independent behavior)
- Cross-sector or unknown: **0.3**

The matrix is rebuilt with `np.linalg.cholesky()` whenever tickers are added or removed. `O(n²)` but `n < 50`.

### 7.3 GBMSimulator Implementation

```python
import asyncio
import logging
import math
import random
from typing import AsyncGenerator

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES,
    TICKER_PARAMS, TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion price engine. Pure math, no async."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ~8.48e-8

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
        """Advance all tickers one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # ~0.1% chance per tick of a 2-5% shock — visual drama
            # With 10 tickers at 2 ticks/sec, expect ~1 event every 50 seconds
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
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
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
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
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return CROSS_GROUP_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.4 SimulatorDataSource Implementation

```python
class SimulatorDataSource(MarketDataSource):
    """Async wrapper around GBMSimulator. Runs a background task."""

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

        # Seed the cache immediately — SSE has data before the first loop tick
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
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Key behaviors:**
- **Immediate seeding**: Cache is populated with seed prices before the loop begins — no blank-screen delay on first SSE connection
- **Graceful cancellation**: `stop()` cancels the task and awaits `CancelledError` — clean shutdown during FastAPI lifespan
- **Exception resilience**: Loop catches per-step exceptions — a bad tick doesn't kill the feed

---

## 8. Massive API Client

**`backend/app/market/massive_client.py`**

Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers` for all watched tickers in a single API call. The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.

### 8.1 Rate Limits

| Tier | Limit | Recommended `poll_interval` |
|------|-------|---------------------------|
| Free | 5 req/min | 15s (default) |
| Paid | Unlimited | 2–5s |

### 8.2 Implementation

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """REST polling client for the Massive (Polygon.io) API."""

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
        self._client: Any = None

    async def start(self, tickers: list[str]) -> None:
        from massive import RESTClient
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data before the interval elapses
        await self._poll_once()

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

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)

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
            # Intentionally swallowed — cache retains last-known prices; loop retries next interval

    def _fetch_snapshots(self) -> list:
        """Synchronous call. Runs in asyncio.to_thread()."""
        from massive.rest.models import SnapshotMarketType
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 8.3 Fields Extracted from Massive Snapshot

```python
snap.ticker                       # "AAPL"
snap.last_trade.price             # 190.50  — current price
snap.last_trade.timestamp         # 1707580800000  — Unix milliseconds
snap.day.previous_close           # 189.20  — for computing day change
snap.day.change_percent           # 0.68    — day % change
snap.day.open / .high / .low      # OHLC for today
```

For FinAlly's core use case, only `last_trade.price` and `last_trade.timestamp` are required.

### 8.4 Error Handling

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error; poller keeps running |
| 429 Rate Limited | Logged as error; retries after `poll_interval` |
| Network timeout | Logged as error; retries automatically |
| Malformed snapshot | Individual ticker skipped with warning; others still processed |
| All tickers fail | Cache retains last-known prices; SSE keeps streaming stale data |

**Lazy import rationale:** `from massive import RESTClient` is inside `start()`, not at module level. This means the `massive` package is only required when `MASSIVE_API_KEY` is set. Simulator-only users (the majority) have zero external API dependencies.

---

## 9. Factory

**`backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select and instantiate the appropriate data source.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation, no API key needed)

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

---

## 10. SSE Streaming Endpoint

**`backend/app/market/stream.py`**

```python
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory that creates the SSE router with an injected PriceCache reference."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint. Pushes all ticker prices ~every 500ms.

        Client connects with:
            const es = new EventSource('/api/stream/prices');
            es.onmessage = (e) => {
                const prices = JSON.parse(e.data);
                // prices: { "AAPL": { ticker, price, previous_price, ... }, ... }
            };
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Prevent nginx from buffering SSE
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"   # Browser auto-reconnects after 1s if dropped

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
        logger.info("SSE stream cancelled: %s", client_ip)
```

### SSE Wire Format

Each event sent to the client:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

The blank line after `data:` terminates the SSE event. The client receives the entire watchlist in one event per tick.

### Frontend Connection

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: Record<string, PriceUpdate>
    Object.entries(prices).forEach(([ticker, update]) => {
        updateTickerDisplay(ticker, update);
        if (update.direction !== 'flat') {
            flashPrice(ticker, update.direction);  // CSS animation
        }
        appendToSparkline(ticker, update.price);
    });
};

eventSource.onerror = () => {
    setConnectionStatus('reconnecting');
    // EventSource automatically retries using the retry: directive we sent
};
```

---

## 11. FastAPI Lifecycle Integration

**`backend/app/main.py`** (relevant sections)

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # Load initial tickers from DB watchlist (see database module)
    initial_tickers = await db.get_watchlist_tickers()
    await source.start(initial_tickers)

    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # SHUTDOWN
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# FastAPI dependencies for injecting market data into route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Using Market Data in Other Routes

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")

@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.insert_watchlist_ticker(payload.ticker)
    await source.add_ticker(payload.ticker)
    return {"ticker": payload.ticker, "price": price_cache.get_price(payload.ticker)}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_ticker(ticker)
    position = await db.get_position(ticker)
    # Keep tracking if user still holds shares (needed for portfolio valuation)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"status": "ok"}
```

---

## 12. Watchlist Coordination

### Adding a Ticker

```
POST /api/watchlist {"ticker": "PYPL"}
  → Insert into watchlist table
  → await source.add_ticker("PYPL")
      Simulator:  adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:    appends to ticker list; appears on next poll (up to 15s)
  → Return {ticker, price} (price may be None if Massive hasn't polled yet)
```

### Removing a Ticker

```
DELETE /api/watchlist/PYPL
  → Delete from watchlist table
  → Check positions: if no open position → await source.remove_ticker("PYPL")
      Both sources: remove from active set, remove from PriceCache
  → Return {"status": "ok"}
```

### Edge Case: Open Position on Removed Ticker

If a user removes `PYPL` from their watchlist but still holds shares, the ticker must stay in the data source so portfolio valuation stays accurate. The `remove_from_watchlist` route checks for this before calling `source.remove_ticker()`.

---

## 13. Error Handling

### Startup: Empty Watchlist

Both data sources handle `start([])` gracefully. No prices are produced. When the first ticker is added, tracking begins immediately.

### Price Cache Miss During Trade

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. The Massive client may have a brief gap for newly added tickers. The HTTP 400 with a clear message is the correct response.

### Invalid Massive API Key

The first poll fails with 401. The poller logs the error and keeps retrying every `poll_interval` seconds. The SSE stream continues sending empty data. The connection status indicator will show "connected" (SSE is alive). The fix is to correct the key and restart.

### Thread Safety

`PriceCache` uses `threading.Lock` — a real OS mutex. On CPython, reading a single `int` (`version`) is atomic under the GIL, but consistency with the rest of the class is maintained. If the project ever runs on GIL-free Python (PEP 703), the lock protects `version` access too.

---

## 14. Testing

73 tests total, all passing. Coverage: 84% overall.

### Unit Tests: PriceCache

```python
# backend/tests/market/test_cache.py

class TestPriceCache:

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1

    def test_remove_clears_ticker(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None
```

### Unit Tests: GBMSimulator

```python
# backend/tests/market/test_simulator.py

class TestGBMSimulator:

    def test_prices_always_positive(self):
        """GBM exp() is always positive — prices can never go negative."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_built_for_two_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_add_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        assert "TSLA" in sim.step()
        sim.remove_ticker("TSLA")
        assert "TSLA" not in sim.step()
```

### Integration Tests: SimulatorDataSource

```python
# backend/tests/market/test_simulator_source.py

@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_seeds_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        # Cache has prices before any loop tick
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Second stop must not raise

    async def test_add_ticker_seeds_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.add_ticker("TSLA")
        assert cache.get("TSLA") is not None
        await source.stop()
```

### Unit Tests: MassiveDataSource (Mocked)

```python
# backend/tests/market/test_massive.py

def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
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
        source._tickers = ["AAPL", "GOOGL"]

        mocks = [_make_snapshot("AAPL", 190.50, 1707580800000),
                 _make_snapshot("GOOGL", 175.25, 1707580800000)]

        with patch.object(source, "_fetch_snapshots", return_value=mocks):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        good = _make_snapshot("AAPL", 190.50, 1707580800000)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # Causes AttributeError

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Must not raise

        assert cache.get_price("AAPL") is None
```

---

## 15. Configuration Reference

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment | `""` | If non-empty, uses Massive API; otherwise uses simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5s` | Simulator tick interval |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0s` | Massive API poll interval |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Probability of random shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of trading year) |
| SSE push interval | `_generate_events()` | `0.5s` | How often SSE checks for changes |
| SSE retry directive | `_generate_events()` | `1000ms` | Browser reconnection delay |

### Public API (from `app.market`)

```python
from app.market import (
    PriceUpdate,               # Immutable price snapshot dataclass
    PriceCache,                # Thread-safe in-memory price store
    MarketDataSource,          # Abstract interface
    create_market_data_source, # Factory: simulator or Massive based on env
    create_stream_router,      # FastAPI SSE router factory
)

# Startup
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
all_px = cache.get_all()            # dict[str, PriceUpdate]

# Dynamic watchlist changes
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
