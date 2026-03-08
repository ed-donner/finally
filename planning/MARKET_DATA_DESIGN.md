# Market Data Backend — Design

Implementation-ready design for the FinAlly market data subsystem. Covers
the unified interface, in-memory price cache, GBM simulator, Massive API
client, SSE streaming endpoint, and FastAPI lifecycle integration.

All code lives under `backend/app/market/`.

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
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Testing Strategy](#14-testing-strategy)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  MarketDataSource (ABC)                                      │
│  ├── SimulatorDataSource  ← GBM + Cholesky (default)        │
│  └── MassiveDataSource    ← Polygon.io REST poll            │
│            │  (both write to)                               │
│            ▼                                                │
│       PriceCache  (thread-safe, in-memory)                  │
│            │  (readers)                                     │
│  ┌─────────┼────────────────────────────┐                  │
│  ▼         ▼                            ▼                  │
│ SSE     Portfolio valuation       Trade execution           │
│ /api/stream/prices                                          │
└─────────────────────────────────────────────────────────────┘
```

**Key design principles:**

- **Strategy pattern** — both data sources implement the same `MarketDataSource`
  ABC; all downstream code is source-agnostic.
- **Push model** — sources write prices into the cache on their own schedule.
  Consumers (SSE, portfolio) read from the cache on their own schedule. No
  tight coupling between producer timing and consumer timing.
- **Single point of truth** — `PriceCache` is the only place where current
  prices live. There are no other price fields anywhere in the application.
- **Factory selection** — `create_market_data_source()` reads `MASSIVE_API_KEY`
  from the environment and returns the appropriate implementation. Zero
  conditionals anywhere else.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Public re-exports
      models.py           # PriceUpdate dataclass
      cache.py            # PriceCache (thread-safe in-memory store)
      interface.py        # MarketDataSource ABC
      seed_prices.py      # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS
      simulator.py        # GBMSimulator + SimulatorDataSource
      massive_client.py   # MassiveDataSource (Polygon.io REST poller)
      factory.py          # create_market_data_source()
      stream.py           # SSE router + /api/stream/prices endpoint
```

`__init__.py` re-exports the public API:

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

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer.
Every downstream consumer — SSE streaming, portfolio valuation, trade
execution — works exclusively with this type.

```python
# backend/app/market/models.py
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    frozen=True: value objects, safe to share across async tasks.
    slots=True: minor memory optimization — we create many per second.
    """

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

### Design notes

- **Computed properties** (`change`, `direction`, `change_percent`): Derived on
  the fly from `price` and `previous_price`, so they can never be stale or
  inconsistent. No stored `direction` field that could drift out of sync.
- **`to_dict()`**: Single serialization point used by both the SSE endpoint and
  REST API responses. Callers never build the dict themselves.
- **`previous_price == price` on first tick**: When a ticker is added for the
  first time, `direction` is `"flat"` and `change` is `0.0`. This is the
  correct, safe default.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central data hub. Data sources write to it; SSE
streaming and portfolio valuation read from it. Thread-safe because the
Massive client calls `asyncio.to_thread()`, which runs in a real OS thread.

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker.

    Writers: one of SimulatorDataSource or MassiveDataSource (never both).
    Readers: SSE endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every write; used by SSE for change detection

    # ------------------------------------------------------------------ writes

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the PriceUpdate that was stored.

        If this is the first update for the ticker, previous_price == price
        (direction='flat', change=0.0).
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
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

    def remove(self, ticker: str) -> None:
        """Remove a ticker (e.g., when removed from the watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    # ------------------------------------------------------------------ reads

    def get(self, ticker: str) -> PriceUpdate | None:
        """Latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all current prices (snapshot, not live view)."""
        with self._lock:
            return dict(self._prices)

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Bumped on every update().
        Used by the SSE loop to detect whether anything changed."""
        return self._version

    # ------------------------------------------------------------------ misc

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Version counter and SSE efficiency

Without a version counter, the SSE loop would serialize and re-send all prices
every 500ms even when nothing has changed (Massive API only updates every 15s).
The version counter makes no-op cycles free:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        yield format_sse_event(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` instead of `asyncio.Lock`

The Massive client calls `asyncio.to_thread(self._client.get_snapshot_all, ...)`
which runs the synchronous Polygon REST call in a real OS thread. `asyncio.Lock`
only works within a single thread; `threading.Lock` works from both sync threads
and the async event loop.

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
# backend/app/market/interface.py
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract that all market data providers must satisfy.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the source for prices — it reads the
    cache.

    Lifecycle:
        cache = PriceCache()
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", "MSFT", ...])
        await source.add_ticker("PYPL")       # dynamic watchlist change
        await source.remove_ticker("MSFT")    # dynamic watchlist change
        await source.stop()                   # clean shutdown
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given initial tickers.

        Starts a background asyncio Task. Call exactly once at app startup.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times (idempotent). After stop(), no further
        writes will happen to the PriceCache.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include prices for this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache so stale prices don't linger.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

---

## 6. Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no external imports. Shared by the simulator for
initial prices and GBM parameters.

```python
# backend/app/market/seed_prices.py
"""Seed prices and per-ticker GBM parameters for the market simulator."""

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
# sigma: annualized volatility  (0.50 = highly volatile like TSLA)
# mu:    annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # high vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # high vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers added dynamically (not in the list above)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups used for the Cholesky correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR    = 0.6  # tech stocks move together
INTRA_FINANCE_CORR = 0.5  # finance stocks move together
CROSS_GROUP_CORR   = 0.3  # cross-sector or unknown pairs
TSLA_CORR          = 0.3  # TSLA does its own thing
```

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes in one file:

- `GBMSimulator` — pure math engine; holds current prices and advances them
  one time step at a time using Geometric Brownian Motion with correlated
  Cholesky draws.
- `SimulatorDataSource` — the `MarketDataSource` implementation; wraps
  `GBMSimulator` in an async loop and writes results to `PriceCache`.

### 7.1 GBM Math

At each tick:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt  +  sigma * sqrt(dt) * Z)
```

| Symbol | Meaning |
|--------|---------|
| `S(t)` | current price |
| `mu` | annualized drift (e.g. `0.05` = 5% annual return) |
| `sigma` | annualized volatility (e.g. `0.22` for AAPL) |
| `dt` | time step as fraction of a trading year |
| `Z` | correlated standard normal draw |

For 500ms ticks over 252 trading days × 6.5 hours/day:

```python
dt = 0.5 / (252 * 6.5 * 3600)  # ≈ 8.5e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally.
Using `exp()` guarantees prices stay positive (no negative stock prices).

### 7.2 Correlated Moves (Cholesky Decomposition)

Real stocks don't move independently — tech stocks tend to move together.
Cholesky decomposition of a correlation matrix `C` gives a lower-triangular
matrix `L` such that `L @ L.T = C`. Multiplying independent standard normals
`Z_independent` by `L` produces correlated draws:

```
Z_correlated = L @ Z_independent
```

```python
corr = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        rho = _pairwise_correlation(tickers[i], tickers[j])
        corr[i, j] = corr[j, i] = rho

cholesky = np.linalg.cholesky(corr)

# Per tick:
z_independent = np.random.standard_normal(n)
z_correlated  = cholesky @ z_independent
```

### 7.3 Random Shock Events

Each ticker has a ~0.1% chance per tick of a sudden 2–5% move to add drama:

```python
if random.random() < 0.001:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

With 10 tickers running at 500ms ticks, expect roughly one shock event
somewhere in the watchlist every ~50 seconds.

### 7.4 Full Implementation

```python
# backend/app/market/simulator.py
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)

# dt for 500ms ticks: 0.5 seconds / (252 trading days * 6.5 hours/day * 3600 s/hour)
_TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
_DT = 0.5 / _TRADING_SECONDS_PER_YEAR  # ≈ 8.5e-8


class GBMSimulator:
    """Generates correlated GBM price paths for multiple tickers.

    Pure math engine — no async, no I/O. The SimulatorDataSource wraps this
    in an async loop.
    """

    def __init__(self, tickers: list[str], event_probability: float = 0.001) -> None:
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)

    # --------------------------------------------------------- public API

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. No-op if already present."""
        if ticker not in self._prices:
            self._add_ticker_internal(ticker)

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. No-op if not present."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance one time step. Returns {ticker: new_price} for all tickers."""
        n = len(self._tickers)
        if n == 0:
            return {}

        # Correlated random draws
        z_indep = np.random.standard_normal(n)
        z = self._cholesky @ z_indep if self._cholesky is not None else z_indep

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu    = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            # GBM step
            drift     = (mu - 0.5 * sigma ** 2) * _DT
            diffusion = sigma * math.sqrt(_DT) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1.0 + shock)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --------------------------------------------------------- private

    def _add_ticker_internal(self, ticker: str) -> None:
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        self._rebuild_cholesky()

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky factor of the correlation matrix.

        Called whenever the ticker set changes. O(n²) but n < 50 in practice.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = _pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _get_sector(ticker: str) -> str | None:
        for sector, members in CORRELATION_GROUPS.items():
            if ticker in members:
                return sector
        return None


def _pairwise_correlation(t1: str, t2: str) -> float:
    """Return the correlation coefficient for a pair of tickers."""
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR

    tech    = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    t1_tech = t1 in tech
    t2_tech = t2 in tech
    t1_fin  = t1 in finance
    t2_fin  = t2 in finance

    if t1_tech and t2_tech:
        return INTRA_TECH_CORR
    if t1_fin and t2_fin:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR  # cross-sector, unknown tickers, or mixed


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio Task that calls GBMSimulator.step() every
    update_interval seconds and writes results to PriceCache.
    """

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers)
        # Seed the cache with initial prices immediately
        for ticker, price in self._sim.step().items():
            self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator")

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
            # Seed the cache immediately so the ticker appears on the next SSE poll
            price = self._sim.get_price(ticker)
            if price:
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
                prices = self._sim.step()  # type: ignore[union-attr]
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Polygon.io REST API (via the `massive` Python package) on a
configurable interval. Uses `asyncio.to_thread()` to run the synchronous
REST call without blocking the event loop.

### 8.1 Massive API Basics

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

# Initialize (reads MASSIVE_API_KEY from env automatically, or pass explicitly)
client = RESTClient(api_key="your_key_here")

# Fetch current prices for multiple tickers in ONE API call
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change: {snap.day.change_percent:.2f}%")
    print(f"  Bid/Ask: {snap.last_quote.bid_price} / {snap.last_quote.ask_price}")
    print(f"  Trade time: {snap.last_trade.timestamp}")  # Unix milliseconds
```

**Key fields extracted per snapshot:**

| Field | Used for |
|-------|----------|
| `snap.ticker` | ticker symbol |
| `snap.last_trade.price` | current price → `PriceCache.update()` |
| `snap.last_trade.timestamp` | trade timestamp (ms) → convert to seconds |
| `snap.day.previous_close` | previous day's close (for day-change %) |
| `snap.day.change_percent` | day % change (informational) |

### 8.2 Rate Limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 req/min | 15 seconds |
| Paid | Unlimited | 2–5 seconds |

All tickers in the watchlist are fetched in **one** `get_snapshot_all()` call,
so rate consumption is flat regardless of watchlist size.

### 8.3 Full Implementation

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
    """MarketDataSource backed by the Polygon.io REST API (via `massive` package).

    Polls get_snapshot_all() on a timer. Runs the synchronous REST call in a
    thread pool via asyncio.to_thread() to avoid blocking the event loop.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
    ) -> None:
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(tickers)
        # Poll immediately on start so the cache has data before first SSE read
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive_poller")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --------------------------------------------------------- private

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    self._cache.update(
                        ticker=snap.ticker,
                        price=snap.last_trade.price,
                        timestamp=snap.last_trade.timestamp / 1000.0,  # ms → seconds
                    )
                except (AttributeError, TypeError):
                    logger.warning("Malformed snapshot for %s — skipped", getattr(snap, "ticker", "?"))
        except Exception:
            logger.exception("Massive API poll failed — will retry on next interval")

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call — runs in a thread pool via asyncio.to_thread()."""
        return list(
            self._client.get_snapshot_all(
                market_type=SnapshotMarketType.STOCKS,
                tickers=list(self._tickers),  # copy to avoid mutation mid-call
            )
        )
```

### 8.4 Error Handling

| HTTP error | Meaning | Behavior |
|-----------|---------|---------|
| `401` | Invalid API key | Logged; poll loop continues (key may be fixed) |
| `403` | Plan doesn't include endpoint | Logged; poll loop continues |
| `429` | Rate limit exceeded | Logged; next poll waits the full interval |
| `5xx` | Server error | `massive` client retries 3× internally |
| Any exception | Anything unexpected | Caught in `_poll_once`; logged; next poll continues |

The `try/except` around `_poll_once()` in `_poll_loop()` guarantees the
background task never dies silently.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

Reads `MASSIVE_API_KEY` from the environment and returns the appropriate
`MarketDataSource`. All downstream code calls this once at startup and is
never aware of which backend is active.

```python
# backend/app/market/factory.py
from __future__ import annotations

import os

from .cache import PriceCache
from .interface import MarketDataSource


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return SimulatorDataSource or MassiveDataSource based on environment.

    Reads MASSIVE_API_KEY from the environment:
    - Set and non-empty → MassiveDataSource (real Polygon.io data)
    - Absent or empty   → SimulatorDataSource (GBM simulator, default)
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        from .massive_client import MassiveDataSource
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        return SimulatorDataSource(price_cache=price_cache)
```

The `massive` package import is deferred inside the `if` branch so that the
package is not required when running in simulator mode. This keeps the
development environment lighter.

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

Implements `GET /api/stream/prices` as a FastAPI `StreamingResponse`. The
endpoint is a factory function so that the `PriceCache` instance can be
injected at startup rather than accessed as a module-level global.

```python
# backend/app/market/stream.py
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create a FastAPI router with the SSE price streaming endpoint.

    Call once at app startup, passing the shared PriceCache instance.
    """
    router = APIRouter()

    @router.get("/stream/prices")
    async def price_stream() -> StreamingResponse:
        """SSE stream of live price updates for all watched tickers.

        Client connects with EventSource('/api/stream/prices').
        Server pushes a JSON payload containing all current prices every
        ~500ms when any price has changed.

        Event format:
            retry: 1000\\n
            data: {"AAPL": {...}, "GOOGL": {...}, ...}\\n\\n
        """
        return StreamingResponse(
            _generate_events(price_cache),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # Disable Nginx buffering
                "Connection": "keep-alive",
            },
        )

    return router


async def _generate_events(price_cache: PriceCache) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted strings.

    Uses a version counter to skip sends when no prices have changed
    (important for Massive API mode where updates arrive every 15s).
    """
    # First event: tell the browser to reconnect after 1s on disconnect
    yield "retry: 1000\n\n"

    last_version = -1
    while True:
        try:
            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                snapshot = price_cache.get_all()
                if snapshot:
                    payload = {
                        ticker: update.to_dict()
                        for ticker, update in snapshot.items()
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
        except Exception:
            logger.exception("SSE event generation failed")

        await asyncio.sleep(0.5)
```

### SSE Protocol Details

The `EventSource` API on the frontend:

```javascript
// Frontend: connect to the SSE endpoint
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices = { "AAPL": { ticker, price, previous_price, change, direction, ... }, ... }
    updateWatchlist(prices);
};

es.onerror = () => {
    // EventSource auto-reconnects after the `retry:` interval (1000ms)
    console.warn('SSE reconnecting...');
};
```

The `retry: 1000\n\n` directive instructs the browser to wait 1 second before
reconnecting if the connection drops, preventing reconnect storms.

### SSE Event Shape

Each `data:` line contains a JSON object keyed by ticker:

```json
{
  "AAPL": {
    "ticker": "AAPL",
    "price": 191.43,
    "previous_price": 191.28,
    "timestamp": 1709890234.12,
    "change": 0.15,
    "change_percent": 0.0784,
    "direction": "up"
  },
  "GOOGL": {
    "ticker": "GOOGL",
    "price": 174.22,
    "previous_price": 174.55,
    "timestamp": 1709890234.12,
    "change": -0.33,
    "change_percent": -0.189,
    "direction": "down"
  }
}
```

---

## 11. FastAPI Lifecycle Integration

The market data subsystem integrates with FastAPI via a lifespan context
manager. The `PriceCache` and `MarketDataSource` are stored in `app.state` so
they can be accessed from any route handler.

```python
# backend/app/main.py (relevant excerpts)
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .market import PriceCache, create_market_data_source, create_stream_router
from .db import init_db, get_watchlist_tickers

load_dotenv()  # reads .env from project root

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: initialize DB, start market data. Shutdown: stop market data."""
    # 1. Initialize database (creates tables + seeds default data if needed)
    await init_db()

    # 2. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 3. Load initial tickers from the database (uses defaults on first run)
    initial_tickers = await get_watchlist_tickers() or DEFAULT_TICKERS

    # 4. Start the market data source (simulator or Massive based on env)
    source = create_market_data_source(price_cache)
    await source.start(initial_tickers)
    app.state.market_source = source

    yield  # app is running

    # Shutdown
    await source.stop()


app = FastAPI(lifespan=lifespan)

# Register the SSE router — inject the cache instance from app state
# (done after app creation so we can reference app.state in the closure)
@app.on_event("startup")  # alternative pattern using a startup hook
async def register_stream_router():
    stream_router = create_stream_router(app.state.price_cache)
    app.include_router(stream_router, prefix="/api")
```

> **Note:** The cleaner pattern is to create the router inside `lifespan` and
> include it before `yield`. Both patterns work; the key constraint is that
> `create_stream_router(price_cache)` must receive the same `PriceCache`
> instance that the market source writes to.

### Accessing the market source from route handlers

```python
# backend/app/api/watchlist.py
from fastapi import APIRouter, Request

router = APIRouter()

@router.post("/watchlist")
async def add_to_watchlist(request: Request, body: AddTickerRequest):
    ticker = body.ticker.upper().strip()
    # ... validate, write to DB ...
    source = request.app.state.market_source
    await source.add_ticker(ticker)
    return {"ticker": ticker, "status": "added"}

@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(request: Request, ticker: str):
    ticker = ticker.upper().strip()
    # ... remove from DB ...
    source = request.app.state.market_source
    await source.remove_ticker(ticker)
    return {"ticker": ticker, "status": "removed"}
```

### Accessing the price cache from route handlers

```python
# backend/app/api/portfolio.py
from fastapi import APIRouter, Request

router = APIRouter()

@router.get("/portfolio")
async def get_portfolio(request: Request):
    cache: PriceCache = request.app.state.price_cache
    positions = await db.get_positions(user_id="default")

    result = []
    for pos in positions:
        update = cache.get(pos.ticker)
        current_price = update.price if update else pos.avg_cost
        unrealized_pnl = (current_price - pos.avg_cost) * pos.quantity
        result.append({
            "ticker": pos.ticker,
            "quantity": pos.quantity,
            "avg_cost": pos.avg_cost,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "direction": update.direction if update else "flat",
        })
    return {"positions": result}
```

---

## 12. Watchlist Coordination

When the user adds or removes a ticker (manually or via AI chat), three things
must happen atomically from the user's perspective:

1. **Database** — update the `watchlist` table
2. **Market source** — call `add_ticker()` or `remove_ticker()`
3. **Price cache** — cache is updated by (2) automatically

This pattern belongs in the API route handler:

```python
@router.post("/watchlist")
async def add_to_watchlist(request: Request, body: AddTickerRequest):
    ticker = body.ticker.upper().strip()
    if not ticker or not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(status_code=400, detail="Invalid ticker symbol")

    # 1. Database
    await db.add_watchlist_ticker(user_id="default", ticker=ticker)

    # 2. Market source (also seeds the cache)
    source = request.app.state.market_source
    await source.add_ticker(ticker)

    # 3. Return current price if available
    cache: PriceCache = request.app.state.price_cache
    update = cache.get(ticker)
    return {
        "ticker": ticker,
        "price": update.price if update else None,
        "direction": update.direction if update else "flat",
    }


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(request: Request, ticker: str):
    ticker = ticker.upper().strip()

    # 1. Database
    await db.remove_watchlist_ticker(user_id="default", ticker=ticker)

    # 2. Market source (also removes from cache)
    source = request.app.state.market_source
    await source.remove_ticker(ticker)

    return {"ticker": ticker, "status": "removed"}
```

---

## 13. Error Handling & Edge Cases

### Ticker not in cache

`PriceCache.get()` returns `None` for unknown tickers. All consumers must
handle this:

```python
update = cache.get("UNKNOWN")
price  = update.price if update else None   # None, not KeyError
```

The simulator seeds the cache before the first SSE poll (see `start()` above),
so in normal operation `get()` returning `None` means the ticker was just
added and the next tick hasn't fired yet.

### Massive API: market closed hours

During non-trading hours, `last_trade.price` reflects the last traded price.
The `day` object resets at market open; during pre-market it may reflect the
previous session. For FinAlly's purposes this is fine — we display the last
known price with no "market hours" indicator.

### Massive API: ticker not found in snapshot

If a ticker is not found in the snapshot response (e.g., delisted, typo), the
snapshot simply doesn't include that ticker. The cache retains the last known
price for it. No error is thrown.

### Simulator: ticker added mid-session

When `add_ticker()` is called after `start()`, the `GBMSimulator` rebuilds
its Cholesky matrix. This is O(n²) but n < 50 in practice and takes < 1ms.

### Simulator: prices never go negative

GBM is multiplicative (`exp(...)` is always positive). Even with extreme
volatility parameters, prices can never reach zero.

### Empty watchlist

Both data sources handle an empty ticker list gracefully: `GBMSimulator.step()`
returns `{}`, and `MassiveDataSource._poll_once()` skips the API call if
`self._tickers` is empty.

### Background task crash recovery

Both `_run_loop()` and `_poll_loop()` wrap their main body in `try/except
Exception` and log the error before continuing. This means a single bad tick
(e.g., a numpy error on a pathological price) does not kill the streaming
task.

---

## 14. Testing Strategy

### Unit tests

**`test_models.py`**
- `PriceUpdate.direction` returns `"up"`, `"down"`, `"flat"` correctly
- `change` and `change_percent` compute correctly including divide-by-zero guard
- `to_dict()` contains all expected keys
- Frozen dataclass: mutations raise `FrozenInstanceError`

**`test_cache.py`**
- `update()` stores and returns a `PriceUpdate` with correct `previous_price`
- First update: `previous_price == price`
- `get()` returns `None` for unknown tickers
- `get_all()` returns a snapshot copy (not the live dict)
- `remove()` eliminates the ticker
- `version` increments on each `update()` and not on `remove()`
- Thread safety: concurrent writes from multiple threads don't corrupt state

**`test_simulator.py`**
- `GBMSimulator.step()` produces prices for all tickers
- Prices never go negative after 1000 steps
- `add_ticker()` / `remove_ticker()` maintain correct state
- Cholesky is rebuilt after ticker changes
- With all 10 default tickers, `_rebuild_cholesky()` succeeds (valid correlation matrix)
- `event_probability=1.0` triggers a shock every step

**`test_simulator_source.py`** (integration)
- `SimulatorDataSource.start()` seeds the cache immediately
- After 1 second, cache has prices for all tickers
- `add_ticker()` adds to cache; `remove_ticker()` removes from cache
- `stop()` stops the background task cleanly

**`test_factory.py`**
- With `MASSIVE_API_KEY` unset → returns `SimulatorDataSource`
- With `MASSIVE_API_KEY=""` → returns `SimulatorDataSource`
- With `MASSIVE_API_KEY="somekey"` → returns `MassiveDataSource`

**`test_massive.py`**
- `_poll_once()` with mocked client updates the cache correctly
- Malformed snapshot (missing `last_trade`) is skipped with a warning
- `add_ticker()` / `remove_ticker()` update `_tickers`
- `stop()` cancels the task
- Timestamp conversion: milliseconds → seconds

### SSE integration test

```python
# backend/tests/market/test_stream.py
import json
import asyncio
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.mark.anyio
async def test_sse_stream_delivers_prices():
    """SSE endpoint delivers price data within 2 seconds of startup."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        received = []
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert "text/event-stream" in response.headers["content-type"]
            async for line in response.aiter_lines():
                if line.startswith("data:"):
                    payload = json.loads(line[5:].strip())
                    received.append(payload)
                    if len(received) >= 2:
                        break  # got at least 2 events

    assert len(received) >= 1
    first = received[0]
    # Default tickers should be present
    assert "AAPL" in first
    assert "price" in first["AAPL"]
    assert first["AAPL"]["price"] > 0
```

---

## 15. Configuration Reference

| Environment Variable | Default | Behavior |
|---------------------|---------|---------|
| `MASSIVE_API_KEY` | _(unset)_ | Unset or empty → GBM simulator. Set → Polygon.io REST poller. |
| _(hardcoded)_ | `0.5` s | Simulator update interval |
| _(hardcoded)_ | `15.0` s | Massive API poll interval (free tier) |
| _(hardcoded)_ | `0.001` | Simulator shock event probability per ticker per tick |
| `LLM_MOCK` | `false` | Unrelated to market data; controls LLM mock mode for tests |

### pyproject.toml dependencies

```toml
[project]
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn[standard]>=0.27.0",
    "python-dotenv>=1.0.0",
    "numpy>=1.26.0",
    "massive>=1.0.0",    # Polygon.io REST client
]

[tool.hatch.build.targets.wheel]
packages = ["app"]      # Required for uv/hatchling to find the app package
```

### Quick start (simulator mode, no API key needed)

```bash
cd backend
uv sync
uv run uvicorn app.main:app --reload --port 8000
# → SSE stream at http://localhost:8000/api/stream/prices
```

### Quick start (real market data)

```bash
export MASSIVE_API_KEY=your_key_here
cd backend
uv run uvicorn app.main:app --reload --port 8000
```
