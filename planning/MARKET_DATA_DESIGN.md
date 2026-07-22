# Market Data Backend — Design Document

Complete implementation design for the FinAlly market data subsystem. All code in this document lives under `backend/app/market/`. This is the authoritative reference for the unified interface, price cache, GBM simulator, Massive API client, SSE streaming, and FastAPI lifecycle integration.

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
11. [Package Exports — `__init__.py`](#11-package-exports)
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Reference](#16-configuration-reference)

---

## 1. Architecture Overview

```
                         ┌─────────────────────────────────┐
                         │   MarketDataSource (ABC)         │
                         │   interface.py                   │
                         └──────────────┬──────────────────┘
                                        │ implements
               ┌────────────────────────┴────────────────────────┐
               │                                                   │
   ┌───────────▼────────────┐                     ┌──────────────▼────────────┐
   │  SimulatorDataSource   │                     │  MassiveDataSource        │
   │  simulator.py          │                     │  massive_client.py        │
   │                        │                     │                           │
   │  GBMSimulator (math)   │                     │  Polygon.io REST poller   │
   │  500ms ticks           │                     │  15s poll (free tier)     │
   └───────────┬────────────┘                     └──────────────┬────────────┘
               │ writes                                           │ writes
               └──────────────────────┬──────────────────────────┘
                                      │
                           ┌──────────▼──────────┐
                           │     PriceCache       │
                           │     cache.py         │
                           │                      │
                           │  thread-safe dict    │
                           │  version counter     │
                           └──────────┬───────────┘
                                      │ reads
               ┌───────────────┬──────┴──────────┬──────────────┐
               │               │                 │              │
   ┌───────────▼──┐  ┌─────────▼──────┐  ┌──────▼──────┐  ┌───▼───────────┐
   │  SSE stream  │  │  Trade exec    │  │  Portfolio  │  │  Watchlist    │
   │  stream.py   │  │  /api/trade    │  │  valuation  │  │  /api/watch   │
   └──────────────┘  └────────────────┘  └─────────────┘  └───────────────┘
```

### Key design decisions

- **Strategy pattern**: both data sources implement the same ABC. All downstream code is source-agnostic — no `if simulator else massive` conditionals outside `factory.py`.
- **Push to cache, not pull**: the data source writes to `PriceCache` on its own schedule. The SSE endpoint reads from the cache at its own cadence. They are fully decoupled.
- **`threading.Lock` not `asyncio.Lock`**: the Massive client's synchronous `get_snapshot_all()` runs in `asyncio.to_thread()` (a real OS thread). `asyncio.Lock` would not protect against that.
- **Version counter for SSE efficiency**: the SSE loop only serializes and sends data when the cache has actually changed, avoiding unnecessary CPU work during slow Massive polling intervals.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py          # Re-exports the public API
      models.py            # PriceUpdate dataclass
      cache.py             # PriceCache (thread-safe in-memory store)
      interface.py         # MarketDataSource ABC
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource (Polygon.io REST poller)
      factory.py           # create_market_data_source() factory
      stream.py            # FastAPI SSE router
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

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
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
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

### Design decisions

- **`frozen=True`**: price updates are immutable value objects. Safe to pass across async tasks without copying.
- **`slots=True`**: minor memory optimization — many of these are created per second.
- **Computed properties** (`change`, `direction`, `change_percent`): derived at read time from `price` and `previous_price` so they are always consistent. No risk of a stale `direction` field.
- **`to_dict()`**: single serialization point used by SSE events and REST responses alike.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Data sources write; SSE streaming and portfolio routes read. Must be thread-safe because the Massive client's synchronous poller runs in `asyncio.to_thread()`.

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
        self._version: int = 0  # Bumped on every write

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
        """Shallow copy of all current prices."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Used by SSE for change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE loop polls the cache every 500ms. Without a version counter it would re-serialize and re-send all prices on every tick, even when the Massive API hasn't delivered new data in 15 seconds. The version counter makes skipping cheap:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

Both `SimulatorDataSource` and `MassiveDataSource` implement this contract. All watchlist management code calls this interface, never the concrete classes.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Callers never read prices directly from the source.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # app runs ...
        await source.add_ticker("PYPL")
        await source.remove_ticker("GOOGL")
        # shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop producing updates. Safe to call multiple times."""

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

### Why push to cache instead of returning prices

Decouples timing. The simulator ticks at 500ms; Massive polls at 15s; SSE reads at 500ms. No layer needs to know the update frequency of any other layer.

---

## 6. Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only. Shared by the simulator (for initial prices and GBM math) and by the Massive client (as fallback until the first API poll returns).

```python
"""Seed prices and per-ticker GBM parameters for the market simulator."""

SEED_PRICES: dict[str, float] = {
    "AAPL":  190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  185.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     280.00,
    "NFLX":  600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility  mu: annualized drift (expected return)
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

# Fallback parameters for tickers not in the list above
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors (and unknown tickers)
TSLA_CORR          = 0.3   # TSLA does its own thing
```

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine) and `SimulatorDataSource` (async `MarketDataSource` wrapper).

### 7.1 The Math

GBM formula for one time step:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (e.g. 0.05 = 5% expected annual return)
- `sigma` = annualized volatility (e.g. 0.22 = 22%)
- `dt` = time step as a fraction of a trading year
- `Z` = correlated standard normal random variable

For 500ms ticks with 252 trading days × 6.5 hours/day:
```
dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.5 × 10⁻⁸
```

Correlated moves use Cholesky decomposition. Given correlation matrix `C`:
```
L = cholesky(C)
Z_correlated = L @ Z_independent
```

### 7.2 GBMSimulator — Pure Math Engine

```python
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


class GBMSimulator:
    """Geometric Brownian Motion price engine with sector correlations.

    Hot path: step() is called every 500ms. Keep it fast.
    """

    # 500ms as a fraction of a trading year (252 days × 6.5 h × 3600 s)
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8

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
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/s, expect one event every ~50 seconds
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Shock event on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

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
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition. Called whenever tickers change."""
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
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.3 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """Wraps GBMSimulator in an async loop that writes to PriceCache every 500ms."""

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
        # Seed the cache before the loop starts so SSE has data on the first tick
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

### Key behaviors

- **Immediate seeding**: `start()` populates the cache with seed prices *before* the loop begins — no blank-screen delay on first SSE connection.
- **Exception resilience**: the loop catches per-step exceptions so a single bad tick doesn't kill the feed.
- **Graceful cancellation**: `stop()` cancels and awaits the task, catching `CancelledError` for clean shutdown.
- **Cholesky rebuild cost**: O(n²) but n < 50 tickers in practice. Rebuilds only when tickers are added/removed.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) REST API snapshot endpoint. The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST snapshot API.

    One API call fetches all watched tickers simultaneously, staying within
    the free tier limit of 5 requests/minute at a 15-second poll interval.
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
        self._client: Any = None

    async def start(self, tickers: list[str]) -> None:
        from massive import RESTClient

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data before the loop begins
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(tickers),
            self._interval,
        )

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
            logger.info("Massive: added ticker %s (appears on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

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
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — loop will retry on the next interval

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs in a thread pool via asyncio.to_thread()."""
        from massive.rest.models import SnapshotMarketType

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Rate limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 req/min | 15 seconds (default) |
| Paid | Unlimited | 2–5 seconds |

### Error handling table

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error. Poller keeps retrying — user may fix the key and restart. |
| 429 Rate Limited | Logged as error. Retries after `poll_interval` seconds. |
| Network timeout | Logged as error. Next cycle retries automatically. |
| Malformed snapshot | Individual ticker skipped with warning. Others still processed. |
| All tickers fail | Cache retains last-known prices. SSE keeps streaming stale data — better than nothing. |

### Lazy import

`from massive import RESTClient` is inside `start()`, not at module level. The `massive` package is only needed when `MASSIVE_API_KEY` is set. Simulator users have zero external dependencies beyond `numpy`.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

Selects the right implementation at startup based on `MASSIVE_API_KEY`. This is the only place in the codebase that branches on the data source.

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return an unstarted MarketDataSource selected by environment.

    MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    Otherwise                         → SimulatorDataSource (GBM simulation)

    Caller must await source.start(tickers) before use.
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

### Usage

```python
cache = PriceCache()
source = create_market_data_source(cache)      # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT"]) # starts background task
```

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds open an HTTP connection and pushes all cached prices to the client every 500ms.

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
    """Factory that binds the SSE router to a specific PriceCache instance.

    Returns the module-level router (FastAPI will not re-register it on
    repeated calls — safe to call once at app startup).
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint. Client connects with EventSource('/api/stream/prices').

        Each event is a JSON object mapping ticker symbols to PriceUpdate dicts:
            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        The 'retry' directive tells the browser to reconnect after 1 second
        if the connection drops — EventSource handles this automatically.
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Prevents nginx from buffering SSE
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE-formatted strings.

    Only sends when the cache has new data (version counter changed).
    """
    yield "retry: 1000\n\n"  # Browser reconnects after 1 second on drop

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

### Wire format

Each SSE event:
```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```
(Note the trailing blank line — required by the SSE spec.)

### Frontend consumption

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        updateWatchlistRow(ticker, update);
        appendSparklinePoint(ticker, update.price);
    }
};

eventSource.onerror = () => {
    setConnectionStatus('reconnecting');
};
```

### Why poll-and-push instead of event-driven

The SSE endpoint polls the cache at a fixed 500ms interval rather than being notified by the data source. This produces evenly-spaced events regardless of the data source's update rate — important for sparkline charts where regular time spacing matters.

---

## 11. Package Exports

**File: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API — import from app.market, not from submodules:
    PriceUpdate              Immutable price snapshot dataclass
    PriceCache               Thread-safe in-memory price store
    MarketDataSource         Abstract interface for data providers
    create_market_data_source  Factory that selects simulator or Massive
    create_stream_router     FastAPI router factory for SSE endpoint
"""

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

## 12. FastAPI Lifecycle Integration

**File: `backend/app/main.py`** (relevant portion)

The market data system starts and stops with the application using the `lifespan` context manager.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    # 1. Shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Data source (simulator or Massive, based on MASSIVE_API_KEY)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register the SSE router
    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency injectors used by other route modules
def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Injecting into other routes

```python
from fastapi import APIRouter, Depends, HTTPException
from app.market import PriceCache, MarketDataSource
from app.main import get_price_cache, get_market_source

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"No price available for {trade.ticker}. Wait a moment.")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    # 1. Persist to database
    await db.insert_watchlist_entry(payload.ticker)
    # 2. Start tracking in the data source
    await source.add_ticker(payload.ticker)
    # 3. Return current price if already available
    return {"ticker": payload.ticker, "price": price_cache.get_price(payload.ticker)}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)
    # Keep tracking if user still holds shares (for portfolio valuation)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"status": "ok"}


@router.get("/watchlist")
async def get_watchlist(
    price_cache: PriceCache = Depends(get_price_cache),
):
    tickers = await db.get_watchlist_tickers()
    return [
        {
            "ticker": t,
            **((price_cache.get(t).to_dict()) if price_cache.get(t) else {"price": None}),
        }
        for t in tickers
    ]
```

---

## 13. Watchlist Coordination

When the watchlist changes the data source must be notified so it tracks the correct set of tickers.

### Adding a ticker

```
POST /api/watchlist  { "ticker": "PYPL" }
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list; price appears on the next poll cycle
  → Return { "ticker": "PYPL", "price": <current or null> }
```

### Removing a ticker

```
DELETE /api/watchlist/PYPL
  → Delete from watchlist table (SQLite)
  → Check: does user hold PYPL shares?
      Yes → do NOT call source.remove_ticker() — keep tracking for P&L
      No  → await source.remove_ticker("PYPL") → also removes from cache
  → Return { "status": "ok" }
```

### LLM-triggered watchlist changes

The chat endpoint calls the same `source.add_ticker()` / `source.remove_ticker()` functions after auto-executing watchlist changes from the structured LLM response. No special path.

---

## 14. Testing Strategy

### 14.1 Models

**`backend/tests/market/test_models.py`**

```python
import time
import pytest
from app.market.models import PriceUpdate


class TestPriceUpdate:

    def test_direction_up(self):
        u = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
        assert u.direction == "up"
        assert u.change == 1.0
        assert u.change_percent == pytest.approx(0.5263, rel=1e-3)

    def test_direction_down(self):
        u = PriceUpdate(ticker="AAPL", price=189.0, previous_price=190.0)
        assert u.direction == "down"
        assert u.change == -1.0

    def test_direction_flat(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
        assert u.direction == "flat"
        assert u.change == 0.0

    def test_to_dict_has_all_fields(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=189.0, timestamp=1000.0)
        d = u.to_dict()
        assert set(d.keys()) == {"ticker", "price", "previous_price", "timestamp",
                                  "change", "change_percent", "direction"}

    def test_immutable(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=189.0)
        with pytest.raises(Exception):
            u.price = 200.0  # frozen=True prevents mutation
```

### 14.2 Price Cache

**`backend/tests/market/test_cache.py`**

```python
from app.market.cache import PriceCache


class TestPriceCache:

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_tracks_across_updates(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        up = cache.update("AAPL", 191.00)
        assert up.direction == "up"
        down = cache.update("AAPL", 189.00)
        assert down.direction == "down"

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("GOOGL", 175.00)
        assert cache.version == v0 + 2

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_all_returns_copy(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        snapshot = cache.get_all()
        cache.update("AAPL", 191.00)
        # Snapshot is not affected by subsequent updates
        assert snapshot["AAPL"].price == 190.00

    def test_get_price_convenience(self):
        cache = PriceCache()
        cache.update("AAPL", 190.50)
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("NOPE") is None
```

### 14.3 GBM Simulator

**`backend/tests/market/test_simulator.py`**

```python
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_always_positive(self):
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim.get_tickers()) == 1

    def test_remove_nonexistent_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_empty_step(self):
        sim = GBMSimulator(tickers=[])
        assert sim.step() == {}

    def test_unknown_ticker_gets_random_seed(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert 50.0 <= price <= 300.0

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_built_for_multiple_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None
```

### 14.4 SimulatorDataSource (async integration)

**`backend/tests/market/test_simulator_source.py`**

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_seeds_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

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

    async def test_double_stop_is_safe(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Should not raise
```

### 14.5 MassiveDataSource (mocked)

**`backend/tests/market/test_massive.py`**

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, timestamp_ms: int = 1707580800000) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "GOOGL"]

        snapshots = [_make_snapshot("AAPL", 190.50), _make_snapshot("GOOGL", 175.25)]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_is_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        good = _make_snapshot("AAPL", 190.50)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # Causes AttributeError on access

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Must not raise

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        await source.add_ticker("GOOGL")
        assert "GOOGL" in source.get_tickers()

        cache.update("GOOGL", 175.0)
        await source.remove_ticker("GOOGL")
        assert "GOOGL" not in source.get_tickers()
        assert cache.get("GOOGL") is None
```

### 14.6 Factory

**`backend/tests/market/test_factory.py`**

```python
import pytest
from unittest.mock import patch
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


class TestFactory:

    def test_no_api_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict("os.environ", {"MASSIVE_API_KEY": ""}, clear=False):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_api_key_returns_massive(self):
        cache = PriceCache()
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "test-key"}, clear=False):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)

    def test_whitespace_only_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict("os.environ", {"MASSIVE_API_KEY": "   "}, clear=False):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
```

---

## 15. Error Handling & Edge Cases

### Empty watchlist on startup

`start([])` is valid. The simulator produces no prices; the Massive poller skips its API call. The SSE endpoint sends empty events. When the user adds a ticker, tracking begins immediately.

### Price cache miss during a trade

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Wait a moment and try again.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. The Massive client may have a brief gap after adding a new ticker — the HTTP 400 is the correct response.

### Ticker with open position removed from watchlist

The watchlist DELETE route checks for an open position before calling `source.remove_ticker()`. If the user still holds shares, the ticker stays in the data source so portfolio valuation remains accurate.

### Massive API key invalid at startup

The first poll fails with a 401. The poller logs the error and keeps retrying every `poll_interval` seconds. The SSE stream runs normally but carries no data. The user sees no prices until the key is corrected and the container restarted.

### Thread safety under load

`threading.Lock` is a mutex — one holder at a time. The critical section (dict lookup + assignment) executes in microseconds. With 10 tickers at 2 updates/second and a handful of SSE readers, lock contention is immeasurably small.

---

## 16. Configuration Reference

All tunable parameters with their defaults:

| Parameter | Where | Default | Notes |
|-----------|-------|---------|-------|
| `MASSIVE_API_KEY` | Environment variable | `""` | If set and non-empty, uses real market data |
| `SimulatorDataSource.update_interval` | Constructor | `0.5` s | Time between simulator ticks |
| `MassiveDataSource.poll_interval` | Constructor | `15.0` s | Time between Massive API calls (free tier: 4/min max) |
| `GBMSimulator.event_probability` | Constructor | `0.001` | Per-ticker probability of a random shock per tick |
| `GBMSimulator.dt` | Constructor | `~8.5e-8` | Time step as fraction of a trading year |
| SSE push interval | `_generate_events()` | `0.5` s | How often the SSE loop checks for new data |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser EventSource reconnection delay |

### Quick-start usage (for other backend modules)

```python
from app.market import PriceCache, create_market_data_source

# At startup (in lifespan handler)
cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY from env
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                    "NVDA", "META", "JPM", "V", "NFLX"])

# Reading prices (anywhere in the app)
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist management
await source.add_ticker("PYPL")
await source.remove_ticker("NFLX")

# At shutdown (in lifespan teardown)
await source.stop()
```
