# Market Data Backend — Design & Implementation Guide

This document describes the complete market data subsystem for FinAlly. The implementation lives in `backend/app/market/` and is **fully implemented**. This guide explains the architecture, all code, and how everything fits together.

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
10. [SSE Streaming — `stream.py`](#10-sse-streaming)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Configuration](#13-configuration)
14. [Testing](#14-testing)

---

## 1. Architecture Overview

The market data system follows a **strategy pattern** with a push-based data flow:

```
Environment Variable (MASSIVE_API_KEY)
        │
        ▼
  create_market_data_source(cache)   ← Factory selects implementation
        │
        ├── SimulatorDataSource      ← GBM simulation (default, no API key needed)
        └── MassiveDataSource        ← Polygon.io REST polling (real data)
                │
                ▼ writes every 500ms (sim) or 15s (Massive)
           PriceCache                ← Thread-safe in-memory store
                │
                ▼ reads every 500ms
          SSE /api/stream/prices     ← Push to all connected browsers
```

**Key design principles:**
- **Push model**: Data sources write to the cache on their own schedule. SSE reads from the cache independently. Neither layer knows about the other's timing.
- **Single source of truth**: `PriceCache` is the only place prices live. Trade execution, portfolio valuation, and SSE streaming all read from it.
- **Source-agnostic downstream**: All code that uses prices imports from `app.market` and works with `PriceUpdate` objects — it never knows if the data is simulated or real.
- **Thread safety**: The cache uses `threading.Lock` (not `asyncio.Lock`) because the Massive client runs synchronous API calls via `asyncio.to_thread()`, which executes in a real OS thread.


---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Public API re-exports
      models.py           # PriceUpdate dataclass
      cache.py            # PriceCache (thread-safe in-memory store)
      interface.py        # MarketDataSource ABC
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py        # GBMSimulator + SimulatorDataSource
      massive_client.py   # MassiveDataSource (Polygon.io REST poller)
      factory.py          # create_market_data_source() — selects implementation
      stream.py           # SSE FastAPI router
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_massive.py
      test_factory.py
  market_data_demo.py     # Live terminal demo (uv run market_data_demo.py)
```

The `__init__.py` re-exports the public API so downstream code only imports from `app.market`:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that crosses the market data layer boundary. All downstream consumers — SSE streaming, portfolio valuation, watchlist API — work with this type.

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

**Design decisions:**
- `frozen=True`: Immutable value object. Safe to share across async tasks without copying or locking.
- `slots=True`: Minor memory optimization — thousands of these are created per minute.
- Computed properties (`change`, `direction`, `change_percent`): Derived at access time so they're always consistent with `price` and `previous_price`. No risk of a stale `direction` field.
- `to_dict()`: Single serialization point used by SSE, watchlist GET response, and the portfolio API.


---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The cache is the central hub. Data sources write to it; all readers consume from it.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

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
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: return just the float price, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter. Incremented on every update."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**The version counter** is how the SSE layer avoids sending redundant events. The Massive API only polls every 15 seconds, but SSE checks every 500ms. Without version tracking, SSE would re-send unchanged data 30 times between Massive polls:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock` instead of `asyncio.Lock`:** The Massive client runs `get_snapshot_all()` (a synchronous blocking call) via `asyncio.to_thread()`, which executes in a real OS thread. `asyncio.Lock` only protects against concurrent coroutines on the same event loop; it provides no protection against OS threads. `threading.Lock` works correctly from both threads and the async event loop.


---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code reads from the cache, never from the source directly.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])   # Start background task
        await source.add_ticker("TSLA")               # Watchlist add
        await source.remove_ticker("GOOGL")           # Watchlist remove
        await source.stop()                           # App shutdown
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Starts background task.
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
        """Return the current list of actively tracked tickers."""
```

**Why push instead of pull:** Data sources write to the cache on their own schedule. SSE reads at its own cadence. Neither knows about the other's timing. This clean separation means:
- Switching from simulator to Massive changes zero SSE code
- SSE interval and data source interval are independently configurable
- Future multi-source scenarios (e.g., combine real-time WebSocket + REST fallback) require no changes to SSE

---

## 6. Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports. Shared by the simulator (initial prices, GBM params) and potentially the Massive client (fallback prices before first poll).

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
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility  mu: annualized drift/expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default for tickers added dynamically (not in list above)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR   = 0.3  # Between sectors / unknown tickers
TSLA_CORR          = 0.3  # TSLA is in tech but does its own thing
```


---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (the math engine) and `SimulatorDataSource` (the `MarketDataSource` implementation that wraps it in an async loop).

### GBM Math

At each time step, every stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return), e.g. 0.05 = 5%/year
- `sigma` = annualized volatility, e.g. 0.22 = 22%/year
- `dt` = time step as fraction of a trading year ≈ 8.48e-8 for 500ms ticks
- `Z` = correlated standard normal draw

```python
# 500ms expressed as a fraction of a trading year
# 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds/year
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8
```

This tiny `dt` produces sub-cent moves per tick, which accumulate naturally over simulated time. Prices can never go negative (GBM is multiplicative — `exp()` is always positive).

### Correlated Moves

Real stocks don't move independently — tech stocks tend to move together. We generate correlated random draws using **Cholesky decomposition** of a correlation matrix:

```python
def _rebuild_cholesky(self) -> None:
    """Rebuild the Cholesky decomposition when tickers change. O(n^2), n < 50."""
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
        return TSLA_CORR          # 0.3 — TSLA does its own thing

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR    # 0.6

    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR # 0.5

    return CROSS_GROUP_CORR       # 0.3
```

In the `step()` method:

```python
z_independent = np.random.standard_normal(n)
if self._cholesky is not None:
    z_correlated = self._cholesky @ z_independent  # Apply correlation
else:
    z_correlated = z_independent
```

### Random Events

Each ticker has a ~0.1% chance per tick of a sudden 2–5% move for visual drama:

```python
if random.random() < self._event_prob:  # event_prob = 0.001
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers at 2 ticks/sec, expect an event roughly every 50 seconds — enough to keep the dashboard visually interesting.

### Full GBMSimulator

```python
import math
import random
import numpy as np

class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)  # Batch init without rebuilding Cholesky
        self._rebuild_cholesky()               # Build once at end

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

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
        """Add without rebuilding Cholesky (for batch initialization)."""
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

### SimulatorDataSource

The async wrapper that ties `GBMSimulator` into the `MarketDataSource` interface:

```python
import asyncio
import logging

class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache immediately so SSE has data on first connection
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
                self._cache.update(ticker=ticker, price=price)  # Seed immediately

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

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Polygon.io (Massive) REST API for real market data. Uses the `massive` Python package, which wraps the Polygon.io API.

### Key API Call

All watched tickers are fetched in a **single API call** — critical for staying within the free tier rate limit (5 requests/minute):

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="your_key")

# One call fetches prices for all tickers
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)

for snap in snapshots:
    price = snap.last_trade.price
    timestamp_ms = snap.last_trade.timestamp  # Unix milliseconds!
    prev_close = snap.day.previous_close
```

### Full MassiveDataSource

```python
import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

class MassiveDataSource(MarketDataSource):
    """Polls Massive (Polygon.io) REST API for real market data.

    Rate limits:
      Free tier: 5 req/min → poll every 15s (default)
      Paid tiers: poll every 2-5s
    """

    def __init__(self, api_key: str, price_cache: PriceCache,
                 poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # Immediate first poll — populate cache before SSE starts
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
            # Price will appear on the next poll (~15s). No immediate fetch.

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """First poll happened in start(). Now poll on interval."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous — run in a thread to avoid blocking the event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                except (AttributeError, TypeError) as e:
                    # Skip malformed snapshots; log and continue
                    logger.warning("Skipping snapshot for %s: %s",
                                   getattr(snap, "ticker", "???"), e)
        except Exception as e:
            # Don't crash the loop — log and retry on next interval
            # Common causes: 401 (bad key), 429 (rate limit), network errors
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Synchronous API call. Runs in a thread via asyncio.to_thread()."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Important Notes

- **`asyncio.to_thread()`** runs the synchronous `RESTClient` in a thread pool executor, preventing it from blocking the event loop during the ~100-500ms HTTP round trip.
- **Error handling** is deliberate: any exception in `_poll_once` is caught and logged, but the poll loop continues. The cache retains the last known price until the next successful poll.
- **Timestamp conversion**: Massive returns Unix milliseconds; `PriceCache.update()` expects Unix seconds. Always divide by 1000.
- **Free tier**: The single `get_snapshot_all()` call counts as 1 request against the 5/minute limit. Polling every 15s uses 4 requests/minute, leaving headroom.

### Rate Limit Reference

| Tier | Limit | Recommended Interval |
|------|-------|---------------------|
| Free | 5 req/min | 15s |
| Paid | Unlimited (stay under 100 req/s) | 2-5s |


---

## 9. Factory

**File: `backend/app/market/factory.py`**

Selects the implementation based on the `MASSIVE_API_KEY` environment variable:

```python
import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation, default)

    Returns an unstarted source. Caller must: await source.start(tickers)
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

**Note:** The actual implementation uses a top-level import (not lazy imports inside the function). The pattern above with lazy imports is an alternative approach that avoids importing the `massive` package when it's not needed. Either approach works; the implemented version imports both at module level but only one is instantiated.

---

## 10. SSE Streaming

**File: `backend/app/market/stream.py`**

The SSE endpoint streams price updates to connected browser clients using the native `EventSource` API.

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

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory that returns the FastAPI router with the cache injected via closure."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint: GET /api/stream/prices

        Long-lived connection. Client uses EventSource. Server pushes all ticker
        prices every ~500ms as a single JSON object keyed by ticker symbol.

        Event format:
            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes retry directive for automatic browser reconnection.
        """
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
    """Async generator yielding SSE-formatted price events."""
    yield "retry: 1000\n\n"  # Browser: reconnect after 1s if connection drops

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:   # Only send if something changed
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Frontend EventSource Connection

```typescript
// Frontend TypeScript — connect to SSE
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }

    Object.values(prices).forEach((update: PriceUpdate) => {
        updateWatchlistRow(update);
        triggerPriceFlash(update.ticker, update.direction);
        appendSparklinePoint(update.ticker, update.price);
    });
};

eventSource.onerror = () => {
    // EventSource auto-reconnects after the retry delay (1000ms from server)
    setConnectionStatus('reconnecting');
};
```

### SSE Event Format

Each event is a single `data:` line containing a JSON object with all ticker prices:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.50, "previous_price": 190.32, "timestamp": 1743811200.0, "change": 0.18, "change_percent": 0.0945, "direction": "up"}, "GOOGL": {...}, ...}

data: {"AAPL": {"ticker": "AAPL", "price": 190.47, ...}, ...}
```

### SSE Design Decisions

- **All tickers in one event** (not one event per ticker): Simpler client code, fewer event handler calls, easier to sync the watchlist display atomically.
- **Version-based change detection**: The Massive API only updates every 15s. Without the version check, SSE would send 30 identical payloads between Massive polls. The simulator updates every 500ms so in practice SSE sends every tick.
- **`X-Accel-Buffering: no`**: Required when nginx sits in front of the app — nginx buffers streaming responses by default, which breaks SSE.
- **`retry: 1000`**: Instructs the browser's `EventSource` to wait 1 second before reconnecting. Without this, the browser uses a default retry interval that may be too short.


---

## 11. FastAPI Lifecycle Integration

The market data system is wired into FastAPI's lifespan context manager. Here is the complete integration pattern for `backend/app/main.py`:

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router

# Global instances (module-level singletons for the process lifetime)
price_cache = PriceCache()
market_source = create_market_data_source(price_cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle for background tasks."""
    # --- Startup ---
    # Load initial watchlist from the database
    initial_tickers = await db.get_watchlist_tickers()  # e.g. ["AAPL", "GOOGL", ...]

    # Start market data (seeds cache, launches background task)
    await market_source.start(initial_tickers)

    yield  # App runs here

    # --- Shutdown ---
    await market_source.stop()


app = FastAPI(lifespan=lifespan)

# Register the SSE streaming router
stream_router = create_stream_router(price_cache)
app.include_router(stream_router)

# Serve the static Next.js export
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

### Dependency Injection for Routes

Other API routes (portfolio, watchlist, chat) need access to `price_cache` and `market_source`. Use FastAPI's dependency injection:

```python
from fastapi import Depends

def get_price_cache() -> PriceCache:
    return price_cache

def get_market_source() -> MarketDataSource:
    return market_source

# In a route:
@app.get("/api/portfolio")
async def get_portfolio(cache: PriceCache = Depends(get_price_cache)):
    positions = await db.get_positions()
    for pos in positions:
        pos.current_price = cache.get_price(pos.ticker)  # Read from cache
    return build_portfolio_response(positions)
```

### Watchlist Routes

When the user adds or removes a ticker from the watchlist, the route must update both the database and the market source:

```python
@app.post("/api/watchlist")
async def add_watchlist_ticker(
    body: AddTickerRequest,
    source: MarketDataSource = Depends(get_market_source),
    cache: PriceCache = Depends(get_price_cache),
):
    ticker = body.ticker.upper().strip()

    # Persist to database
    await db.add_watchlist_ticker(ticker)

    # Tell the market source to start tracking it
    await source.add_ticker(ticker)

    # Price may already be in cache (simulator seeds immediately)
    return {"ok": True, "ticker": ticker}


@app.delete("/api/watchlist/{ticker}")
async def remove_watchlist_ticker(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper()
    await db.remove_watchlist_ticker(ticker)
    await source.remove_ticker(ticker)  # Also removes from cache
    return {"ok": True, "ticker": ticker}
```

---

## 12. Watchlist Coordination

The market source and watchlist must stay synchronized. The state transitions are:

```
User adds ticker "PYPL"
    → POST /api/watchlist {"ticker": "PYPL"}
    → db.add_watchlist_ticker("PYPL")
    → await source.add_ticker("PYPL")
        → Simulator: GBMSimulator.add_ticker("PYPL"), seed cache immediately
        → Massive: append to _tickers list, appears on next poll (~15s)
    → SSE event includes "PYPL" on next tick

User removes ticker "NFLX"
    → DELETE /api/watchlist/NFLX
    → db.remove_watchlist_ticker("NFLX")
    → await source.remove_ticker("NFLX")
        → Both: remove from internal list, remove from PriceCache
    → SSE events no longer include "NFLX"
```

**The AI chat assistant** can also add/remove tickers when it executes watchlist changes. It calls the same internal functions directly (not via HTTP), using the injected `market_source`:

```python
# In the chat endpoint handler
for change in llm_response.watchlist_changes:
    if change.action == "add":
        await db.add_watchlist_ticker(change.ticker)
        await market_source.add_ticker(change.ticker)
    elif change.action == "remove":
        await db.remove_watchlist_ticker(change.ticker)
        await market_source.remove_ticker(change.ticker)
```

---

## 13. Configuration

| Environment Variable | Default | Effect |
|---------------------|---------|--------|
| `MASSIVE_API_KEY` | (unset) | If set and non-empty, uses Massive API; otherwise uses simulator |

No other environment variables control market data behavior. Poll interval and update interval are hardcoded defaults in the class constructors (15s for Massive, 500ms for simulator) but can be overridden if needed:

```python
# Override poll interval for paid Massive tier:
source = MassiveDataSource(api_key=key, price_cache=cache, poll_interval=5.0)

# Override simulator update interval:
source = SimulatorDataSource(price_cache=cache, update_interval=0.25)  # 250ms
```

### `.env` file (project root)

```bash
# Required for LLM chat (not market data)
OPENROUTER_API_KEY=sk-or-...

# Optional: set to use real market data instead of simulator
MASSIVE_API_KEY=

# Optional: deterministic mock LLM for E2E tests
LLM_MOCK=false
```


---

## 14. Testing

Tests live in `backend/tests/market/`. Run them with:

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest tests/market/ -v
uv run --extra dev pytest --cov=app --cov-report=term-missing  # With coverage
```

### Test Files

| File | What it tests |
|------|--------------|
| `test_models.py` | `PriceUpdate` properties, `to_dict()`, direction logic |
| `test_cache.py` | Thread safety, version increments, get/update/remove |
| `test_simulator.py` | `GBMSimulator` step math, add/remove ticker, Cholesky |
| `test_simulator_source.py` | `SimulatorDataSource` lifecycle, cache seeding |
| `test_massive.py` | `MassiveDataSource` polling, error handling, timestamp conversion |
| `test_factory.py` | Factory selects correct source based on env var |

### Example Tests

**Model tests (`test_models.py`)**:
```python
def test_direction_up():
    u = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert u.change_percent == pytest.approx(0.5263, rel=1e-3)

def test_direction_flat():
    u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
    assert u.direction == "flat"
    assert u.change == 0.0
```

**Cache tests (`test_cache.py`)**:
```python
def test_version_increments_on_update():
    cache = PriceCache()
    assert cache.version == 0
    cache.update("AAPL", 190.0)
    assert cache.version == 1
    cache.update("AAPL", 191.0)
    assert cache.version == 2

def test_first_update_sets_previous_equal_to_price():
    cache = PriceCache()
    update = cache.update("AAPL", 190.0)
    assert update.previous_price == 190.0
    assert update.direction == "flat"
```

**Simulator tests (`test_simulator.py`)**:
```python
def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL", "MSFT"])
    prices = sim.step()
    assert set(prices.keys()) == {"AAPL", "GOOGL", "MSFT"}

def test_prices_stay_positive_over_many_steps():
    sim = GBMSimulator(tickers=["TSLA"])  # High volatility
    for _ in range(1000):
        prices = sim.step()
        assert prices["TSLA"] > 0

def test_add_ticker_appears_in_next_step():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("GOOGL")
    prices = sim.step()
    assert "GOOGL" in prices
```

**Simulator source tests (`test_simulator_source.py`)**:
```python
@pytest.mark.asyncio
async def test_start_seeds_cache():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL", "GOOGL"])
    # Cache should have prices immediately after start (no need to wait)
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()

@pytest.mark.asyncio
async def test_stop_cancels_background_task():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL"])
    version_after_start = cache.version
    await source.stop()
    await asyncio.sleep(0.6)  # Wait longer than one update interval
    # Version should not have changed after stop
    assert cache.version == version_after_start
```

**Factory tests (`test_factory.py`)**:
```python
def test_factory_returns_simulator_without_api_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_factory_returns_massive_with_api_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)
```

### Coverage Status (from code review)

| Module | Coverage | Notes |
|--------|----------|-------|
| `models.py` | 100% | Fully covered |
| `cache.py` | 100% | Fully covered |
| `interface.py` | 100% | Abstract class |
| `seed_prices.py` | 100% | Constants only |
| `factory.py` | 100% | Fully covered |
| `simulator.py` | 98% | Near-complete |
| `massive_client.py` | 56% | Requires live API key for full coverage |
| `stream.py` | 31% | SSE requires running ASGI server |

Overall: **84% coverage** across 73 tests.

### Running the Demo

See the simulator in action with a live terminal dashboard:

```bash
cd backend
uv run market_data_demo.py
```

This starts the GBM simulator with all 10 default tickers, displays a live-updating price table with sparklines, and logs notable moves (>1% change). Runs for 60 seconds then prints a session summary.

---

## Quick Reference: Public API

```python
from app.market import (
    PriceCache,                  # Thread-safe price store
    PriceUpdate,                 # Immutable price snapshot dataclass
    MarketDataSource,            # Abstract interface
    create_market_data_source,   # Factory function
    create_stream_router,        # SSE router factory
)

# Startup
cache = PriceCache()
source = create_market_data_source(cache)      # Reads MASSIVE_API_KEY from env
await source.start(["AAPL", "GOOGL", ...])    # Seeds cache, starts background task

# Register SSE endpoint
router = create_stream_router(cache)           # Returns FastAPI APIRouter
app.include_router(router)                     # Mounts GET /api/stream/prices

# Reading prices (e.g., in trade execution)
update: PriceUpdate | None = cache.get("AAPL")
price: float | None = cache.get_price("AAPL")
all_prices: dict[str, PriceUpdate] = cache.get_all()

# Watchlist changes
await source.add_ticker("PYPL")     # Start tracking; seeds cache immediately (simulator)
await source.remove_ticker("NFLX") # Stop tracking; removes from cache

# Shutdown
await source.stop()
```
