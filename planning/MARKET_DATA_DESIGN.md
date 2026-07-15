# Market Data Backend — Implementation Design

Complete, implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration. Incorporates all findings from the code review.

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
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory)
        │
        ├──→ SSE stream endpoint  (/api/stream/prices)
        ├──→ Portfolio valuation  (GET /api/portfolio)
        └──→ Trade execution      (POST /api/portfolio/trade)
```

**Key design decisions:**

- **Strategy pattern** — both data sources implement the same ABC. All downstream code is source-agnostic.
- **Push model** — data sources write to the cache on their own schedule. SSE reads from the cache on its own schedule. No direct coupling between the two.
- **PriceCache as single source of truth** — one object holds the latest price for every ticker. All consumers read from it.
- **Thread-safe cache** — uses `threading.Lock` (not `asyncio.Lock`) because the Massive client's synchronous HTTP calls run in `asyncio.to_thread()`, which uses real OS threads.
- **Lazy import for `massive`** — the package is only imported when `MASSIVE_API_KEY` is set, so the simulator path has zero external dependencies beyond `numpy`.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py           # Public re-exports
      models.py             # PriceUpdate dataclass
      cache.py              # PriceCache (thread-safe in-memory store)
      interface.py          # MarketDataSource ABC
      seed_prices.py        # Constants: SEED_PRICES, TICKER_PARAMS, correlation groups
      simulator.py          # GBMSimulator + SimulatorDataSource
      massive_client.py     # MassiveDataSource
      factory.py            # create_market_data_source()
      stream.py             # SSE streaming router
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each file has a single responsibility. `__init__.py` re-exports the public API so the rest of the backend imports from `app.market` without reaching into submodules.

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every downstream consumer works exclusively with this type.

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

**Design notes:**
- `frozen=True`: Price updates are immutable value objects — safe to share across async tasks without copying.
- `slots=True`: Minor memory optimization — many instances are created per second.
- Computed properties (`change`, `direction`, `change_percent`): Derived from `price` and `previous_price` so they can never be inconsistent with each other.
- `to_dict()`: Single serialization point used by SSE and REST responses.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Data sources write to it; SSE streaming and portfolio valuation read from it.

```python
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

        If this is the first update for a ticker, previous_price == price (direction='flat').
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
        """Current version counter. Used by SSE for change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Note on `version` under lock:** The `version` property acquires the lock before reading `_version`. On CPython, reading a single `int` is already atomic due to the GIL, but acquiring the lock makes this correct on no-GIL Python builds (PEP 703, Python 3.13t+) as well. The overhead is negligible.

**Why `threading.Lock` instead of `asyncio.Lock`:**
The Massive client's synchronous HTTP calls run in `asyncio.to_thread()`, which operates in a real OS thread. `asyncio.Lock` only protects against concurrent coroutines on the same event loop — it provides no protection against threads. `threading.Lock` is correct for both cases.

**Why a version counter:**
The SSE loop polls the cache every 500ms. Without a version counter, it would serialize and transmit all prices on every tick even when nothing changed (e.g., Massive API updates every 15s). Version-based change detection eliminates redundant payloads:

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

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
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
        # ... shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined.
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

**Why the source pushes to the cache instead of returning prices:**
This push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but SSE always reads at its own 500ms cadence. There is no need for the SSE layer to know which data source is active or what its update interval is.

---

## 6. Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib. Shared by the simulator and used as fallback prices in the Massive client path.

```python
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
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift (expected return)
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

# Default for dynamically added tickers not in TICKER_PARAMS
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups for the Cholesky correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Pairwise correlation coefficients
INTRA_TECH_CORR: float = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR: float = 0.5  # Finance stocks move together
CROSS_GROUP_CORR: float = 0.3    # Between sectors, TSLA, and unknown tickers
```

**Volatility rationale:**
- TSLA at `sigma=0.50` reflects real-world Tesla volatility (~50% annualized).
- V at `sigma=0.17` reflects Visa's stability as a payment network.
- NVDA's `mu=0.08` gives it stronger upward drift to reflect recent growth trends.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine) and `SimulatorDataSource` (async wrapper that implements the `MarketDataSource` ABC).

### 7.1 GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return), e.g. `0.05` (5%)
- `sigma` = annualized volatility, e.g. `0.22` (22%)
- `dt` = time step as fraction of a trading year
- `Z` = standard normal random variable

For 500ms updates with 252 trading days and 6.5 hours/day:
```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally over time, producing realistic intraday price paths.

### 7.2 Correlated Moves

Real stocks don't move independently — tech stocks tend to move together. The simulator uses **Cholesky decomposition** of a correlation matrix to generate correlated random draws:

```
Given correlation matrix C, compute L = cholesky(C).
For independent normals Z_independent:
    Z_correlated = L @ Z_independent
```

The sector-based correlation structure:
- Tech stocks (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX): `rho = 0.6`
- Finance stocks (JPM, V): `rho = 0.5`
- TSLA with anything: `rho = 0.3` (it does its own thing)
- Cross-sector: `rho = 0.3`

### 7.3 GBMSimulator Implementation

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
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    # 500ms as a fraction of a trading year (252 days * 6.5h * 3600s)
    TRADING_SECONDS_PER_YEAR: float = 252 * 6.5 * 3600
    DEFAULT_DT: float = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

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

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

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

            # Random shock: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect ~1 event every 50 seconds
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock
                logger.debug(
                    "Shock event on %s: %+.1f%%",
                    ticker,
                    shock * 100,
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Rebuilds the Cholesky matrix — O(n²) but n < 50."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the Cholesky matrix."""
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

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky — for batch initialization only."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Recompute Cholesky decomposition of the correlation matrix."""
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

### 7.4 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache.
    """

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
        # Seed the cache with initial prices so SSE has data immediately on first tick
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

**Key behaviors:**
- **Immediate seeding**: Before the loop starts, `start()` populates the cache with seed prices. SSE has data on its very first tick — no blank-screen delay.
- **`get_tickers()` uses `GBMSimulator.get_tickers()`**: Avoids reaching into private attributes — the public method is the clean boundary.
- **Exception resilience**: `_run_loop` catches all exceptions per-step. A single bad tick doesn't kill the data feed.
- **Graceful cancellation**: `stop()` cancels and awaits the task, catching `CancelledError`.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) REST API snapshot endpoint on a configurable interval. The synchronous Massive client runs in `asyncio.to_thread()` to avoid blocking the event loop.

### 8.1 Massive API Overview

- **Package**: `massive` (`uv add massive`)
- **Auth**: `Authorization: Bearer <MASSIVE_API_KEY>` (handled automatically by client)
- **Primary endpoint**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`
  - Fetches all requested tickers in **one API call** — essential for staying within free-tier rate limits
- **Rate limits**: Free tier = 5 req/min → poll every 15s. Paid tiers → poll every 2-5s.
- **Timestamps**: API returns Unix milliseconds; must be divided by 1000 for seconds.

### 8.2 Response Structure (per ticker)

```json
{
  "ticker": "AAPL",
  "last_trade": {
    "price": 190.50,
    "size": 100,
    "timestamp": 1707580800000
  },
  "day": {
    "open": 188.00,
    "high": 192.00,
    "low": 187.50,
    "close": 190.50,
    "previous_close": 189.00,
    "change": 1.50,
    "change_percent": 0.79,
    "volume": 45000000
  }
}
```

Fields we use:
- `last_trade.price` — current price for display and trade execution
- `last_trade.timestamp` — when the price was recorded (ms → seconds)

### 8.3 MassiveDataSource Implementation

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls the snapshot endpoint for all watched tickers in a single call,
    then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
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
        self._client: Any = None  # Set in start() after lazy import

    async def start(self, tickers: list[str]) -> None:
        # Lazy import: only require 'massive' when actually using real data.
        # Simulator path has no dependency on this package.
        from massive import RESTClient

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data right away
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
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
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
                        "Skipping malformed snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — loop retries on the next interval.
            # Common causes: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs in a thread via asyncio.to_thread()."""
        from massive.rest.models import SnapshotMarketType

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 8.4 Error Handling Policy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error. Poller keeps running (user might fix `.env` and restart). |
| **429 Rate Limited** | Logged as error. Retries automatically after `poll_interval` seconds. |
| **Network timeout** | Logged as error. Retries on next cycle. |
| **Malformed snapshot** | Individual ticker skipped with warning. Other tickers still processed. |
| **All tickers fail** | Cache retains last-known prices. SSE keeps streaming stale (but non-zero) data. |

### 8.5 Lazy Import Strategy

`from massive import RESTClient` happens inside `start()`, not at module import time. This means:
- The `massive` package is only required when `MASSIVE_API_KEY` is set.
- Students using the simulator don't need the package at all.
- Test patches must target `app.market.massive_client.MassiveDataSource._fetch_snapshots` (instance method), not the lazy-imported `RESTClient`. See [Testing Strategy](#14-testing-strategy).

---

## 9. Factory

**File: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select and create the appropriate market data source.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
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

**Usage at app startup:**

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)  # Reads MASSIVE_API_KEY
await source.start(initial_tickers)
```

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

The SSE endpoint holds open a long-lived HTTP connection and pushes price updates to clients as `text/event-stream`.

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
    """Create the SSE streaming router with a reference to the price cache.

    Factory pattern avoids globals — injects PriceCache via closure.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Pushes all tracked ticker prices to the client every ~500ms.
        Client connects with the browser's native EventSource API.
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
    """Async generator that yields SSE-formatted price events.

    Sends all prices every `interval` seconds when the cache has changed.
    Stops when the client disconnects.
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

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

**Correct return type annotation:** `_generate_events` is an async generator, so its return type is `AsyncGenerator[str, None]` — not `None`. Using `None` misleads type checkers.

### 10.1 SSE Wire Format

Each event looks like:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Note the blank line after `data: ...` — this is required by the SSE spec to signal end of event.

### 10.2 Frontend Integration

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices = { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    updateWatchlist(prices);
};

eventSource.onerror = () => {
    // EventSource auto-reconnects after the retry delay (1000ms)
    // Update UI connection status indicator
    setConnectionStatus('reconnecting');
};
```

### 10.3 Why Poll-and-Push Instead of Event-Driven

The SSE endpoint polls the cache on a fixed interval rather than being notified by the data source. This produces predictable, evenly-spaced updates for the frontend — important because the frontend accumulates price history for sparkline charts. Regular 500ms intervals produce smooth sparklines; event-driven pushes (irregular timing from the simulator) would create uneven charts.

---

## 11. FastAPI Lifecycle Integration

**In `backend/app/main.py`:**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Depends

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Select and create the market data source
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite

    # 4. Start the data source (begins producing prices)
    await source.start(initial_tickers)

    yield  # App is running

    # 5. Graceful shutdown
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Register the SSE router after app is created
# (stream_router is created inside lifespan where price_cache is available)
# Alternative: create it here with a module-level cache reference

# --- Dependency Injection Helpers ---

def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

**Note on router registration:** The SSE router is created inside `lifespan` where `price_cache` is available, then registered on the app. Since FastAPI allows adding routes during startup, this pattern works correctly.

### 11.1 Accessing Market Data from Other Routes

```python
from fastapi import APIRouter, Depends, HTTPException

portfolio_router = APIRouter(prefix="/api")

@portfolio_router.post("/portfolio/trade")
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
    # Execute trade at current_price ...


@portfolio_router.get("/watchlist")
async def get_watchlist(
    price_cache: PriceCache = Depends(get_price_cache),
):
    # Return watchlist entries enriched with live prices from cache
    ...


@portfolio_router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAddRequest,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = payload.ticker.upper().strip()
    # Validate and insert into database ...
    await source.add_ticker(ticker)
    # ...


@portfolio_router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # Remove from database ...
    await source.remove_ticker(ticker)
    # ...
```

---

## 12. Watchlist Coordination

When the watchlist changes (via REST or LLM chat), the data source must be notified.

### Adding a Ticker

```
User/LLM → POST /api/watchlist {ticker: "PYPL"}
  → Validate ticker format
  → INSERT into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive: appends to ticker list (appears on next poll in ≤15s)
  → Return {ticker: "PYPL", price: <current_price_or_null>}
```

### Removing a Ticker

```
User/LLM → DELETE /api/watchlist/PYPL
  → DELETE from watchlist table (SQLite)
  → Check if user holds an open position in PYPL
      If YES: keep tracking (portfolio valuation still needs the price)
      If NO:  await source.remove_ticker("PYPL")  (also removes from cache)
  → Return {status: "ok"}
```

### Edge Case: Ticker Has an Open Position

If the user removes a watchlist entry but still holds shares, the ticker must stay tracked for portfolio valuation to work correctly:

```python
@portfolio_router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper()
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 13. Error Handling & Edge Cases

### Empty Watchlist on Startup

If the database has no watchlist entries, `start()` receives `[]`. Both data sources handle this gracefully:
- Simulator: no prices generated, cache is empty.
- Massive: API call is skipped (no tickers to query).
- SSE endpoint: sends empty `data: {}\n\n` events.

When the user adds a ticker, the source immediately starts tracking it.

### Price Cache Miss During Trade

If a user tries to trade a ticker with no cached price (just added to watchlist, Massive hasn't polled yet):

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this by seeding the cache in `add_ticker()`. The Massive client may have a brief gap (up to 15s) — the 400 with a clear message is the correct response.

### Invalid Massive API Key

If `MASSIVE_API_KEY` is set but invalid, the first poll fails with a 401. The poller logs the error and keeps retrying. SSE streams empty data. The user sees no prices. Fix: correct the key and restart.

### Simulator Prices Never Go Negative

GBM uses the exponential function: `S * exp(...)`. Since `exp(x) > 0` for all `x`, prices can never go negative or reach zero regardless of how large the negative shock. This is one of the key properties of GBM.

### Cholesky Matrix Validity

The correlation matrix must be positive semi-definite for Cholesky decomposition to succeed. Our correlation structure (all values between 0.3 and 0.6, diagonal = 1.0) guarantees this by construction. Adding unknown tickers with `CROSS_GROUP_CORR = 0.3` preserves this property.

---

## 14. Testing Strategy

### 14.1 Build Configuration

Before running any tests, ensure `pyproject.toml` has the hatchling package discovery config. Without it, `uv sync` fails:

```toml
[tool.hatch.build.targets.wheel]
packages = ["app"]
```

### 14.2 `test_models.py`

```python
from app.market.models import PriceUpdate


def test_direction_up():
    u = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert u.change_percent > 0

def test_direction_down():
    u = PriceUpdate(ticker="AAPL", price=189.0, previous_price=190.0)
    assert u.direction == "down"

def test_direction_flat():
    u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
    assert u.direction == "flat"

def test_to_dict_keys():
    u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=189.5)
    d = u.to_dict()
    assert set(d.keys()) == {"ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"}

def test_change_percent_zero_previous():
    u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=0.0)
    assert u.change_percent == 0.0  # No ZeroDivisionError

def test_immutability():
    u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=189.5)
    with pytest.raises(Exception):
        u.price = 200.0  # frozen=True prevents mutation
```

### 14.3 `test_cache.py`

```python
from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.50)
    assert update.direction == "flat"
    assert update.previous_price == 190.50

def test_direction_tracking():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    up = cache.update("AAPL", 191.00)
    assert up.direction == "up"
    down = cache.update("AAPL", 189.00)
    assert down.direction == "down"

def test_version_increments_on_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
    cache.update("GOOGL", 175.00)
    assert cache.version == v0 + 2

def test_remove_clears_entry():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None

def test_get_all_returns_snapshot():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    snapshot = cache.get_all()
    assert set(snapshot.keys()) == {"AAPL", "GOOGL"}
    # Modifying the returned dict doesn't affect the cache
    snapshot["FAKE"] = None
    assert "FAKE" not in cache.get_all()

def test_get_price_convenience():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("NOPE") is None

def test_thread_safety():
    """Multiple threads writing simultaneously should not corrupt the cache."""
    import threading
    cache = PriceCache()
    errors = []

    def writer(ticker, price):
        try:
            for i in range(100):
                cache.update(ticker, price + i)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(f"T{i}", i * 10)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(cache.get_all()) == 10
```

### 14.4 `test_simulator.py`

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    result = sim.step()
    assert set(result.keys()) == {"AAPL", "GOOGL"}

def test_prices_always_positive():
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_initial_price_matches_seed():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

def test_unknown_ticker_gets_random_seed():
    sim = GBMSimulator(tickers=["ZZZZ"])
    price = sim.get_price("ZZZZ")
    assert 50.0 <= price <= 300.0

def test_add_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("TSLA")
    assert "TSLA" in sim.step()

def test_remove_ticker():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    sim.remove_ticker("GOOGL")
    result = sim.step()
    assert "GOOGL" not in result
    assert "AAPL" in result

def test_add_duplicate_is_noop():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("AAPL")
    assert sim.get_tickers().count("AAPL") == 1

def test_remove_nonexistent_is_noop():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.remove_ticker("NOPE")  # Should not raise

def test_empty_simulator():
    sim = GBMSimulator(tickers=[])
    assert sim.step() == {}

def test_cholesky_is_none_with_one_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None

def test_cholesky_is_built_with_two_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    assert sim._cholesky is not None

def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None

def test_full_default_watchlist():
    """Cholesky decomposition succeeds for all 10 default tickers."""
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
    sim = GBMSimulator(tickers=tickers)
    result = sim.step()
    assert set(result.keys()) == set(tickers)

def test_get_tickers_public_method():
    """get_tickers() is a public method, not private attribute access."""
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    tickers = sim.get_tickers()
    assert set(tickers) == {"AAPL", "GOOGL"}
```

### 14.5 `test_simulator_source.py`

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        # Cache is seeded before the loop starts
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])
        v0 = cache.version
        await asyncio.sleep(0.3)  # Allow several update cycles
        assert cache.version > v0  # Version has incremented
        await source.stop()

    async def test_stop_is_clean(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Double stop should not raise

    async def test_add_ticker_seeds_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.add_ticker("TSLA")
        assert cache.get("TSLA") is not None  # Seeded immediately, not on next loop tick
        await source.stop()

    async def test_remove_ticker_clears_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "TSLA"])
        await source.remove_ticker("TSLA")
        assert cache.get("TSLA") is None
        assert cache.get("AAPL") is not None
        await source.stop()

    async def test_get_tickers(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        assert set(source.get_tickers()) == {"AAPL", "GOOGL"}
        await source.stop()
```

### 14.6 `test_massive.py`

**Mocking strategy:** Since `massive` uses a lazy import inside `start()`, we cannot patch `app.market.massive_client.RESTClient` at import time. Instead, we patch the `_fetch_snapshots` instance method directly — this avoids the lazy import entirely and is more robust:

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, ts_ms: int = 1707580800000) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "GOOGL"]

        snapshots = [_make_snapshot("AAPL", 190.50), _make_snapshot("GOOGL", 175.25)]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_timestamp_conversion(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        ts_ms = 1707580800000
        snapshots = [_make_snapshot("AAPL", 190.50, ts_ms)]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update.timestamp == pytest.approx(ts_ms / 1000.0)

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        good = _make_snapshot("AAPL", 190.50)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # Will cause AttributeError

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

    async def test_add_remove_ticker(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()

        cache.update("TSLA", 250.00)
        await source.remove_ticker("TSLA")
        assert "TSLA" not in source.get_tickers()
        assert cache.get("TSLA") is None

    async def test_stop_cancels_task(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        # Start without triggering real imports
        source._tickers = ["AAPL"]
        import asyncio
        source._task = asyncio.create_task(asyncio.sleep(9999))

        await source.stop()
        assert source._task is None
```

### 14.7 `test_factory.py`

```python
import pytest
from unittest.mock import patch
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


def test_no_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {}, clear=True):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_empty_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {"MASSIVE_API_KEY": ""}):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_whitespace_only_api_key_returns_simulator():
    cache = PriceCache()
    with patch.dict("os.environ", {"MASSIVE_API_KEY": "   "}):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_valid_api_key_returns_massive():
    cache = PriceCache()
    with patch.dict("os.environ", {"MASSIVE_API_KEY": "test-api-key"}):
        source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)
```

---

## 15. Configuration Reference

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between GBM simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls (free tier limit: 5 req/min) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Probability of a random shock event per ticker per tick (~once every 50s across 10 tickers) |
| `dt` | `GBMSimulator.DEFAULT_DT` | `~8.48e-8` | GBM time step (500ms as fraction of a trading year) |
| SSE push interval | `_generate_events()` | `0.5` s | How often SSE checks the cache and sends to clients |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser EventSource reconnection delay |

### `__init__.py` Public API

**File: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate              - Immutable price snapshot dataclass
    PriceCache               - Thread-safe in-memory price store
    MarketDataSource         - Abstract interface for data providers
    create_market_data_source - Factory: selects simulator or Massive based on env
    create_stream_router     - FastAPI router factory for the SSE endpoint
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

### Downstream Usage Pattern

```python
from app.market import PriceCache, create_market_data_source

# --- Startup ---
cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"])

# --- Read prices ---
update = cache.get("AAPL")           # PriceUpdate or None
price = cache.get_price("AAPL")      # float or None
all_prices = cache.get_all()         # dict[str, PriceUpdate]

# --- Dynamic watchlist ---
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# --- Shutdown ---
await source.stop()
```
