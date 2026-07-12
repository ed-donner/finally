# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified
`MarketDataSource` interface, the thread-safe price cache, the GBM simulator,
the Massive (Polygon.io) REST client, the SSE streaming endpoint, and how the
rest of the backend (FastAPI app, portfolio, watchlist, chat) is meant to
integrate with it.

**Status:** This subsystem is implemented and shipped at `backend/app/market/`
(see `planning/MARKET_DATA_SUMMARY.md` for the build summary and
`planning/archive/` for the original design-and-review trail). Every code
snippet below has been checked against the current source and reflects it
exactly — this document is the up-to-date reference for anyone integrating
with, or extending, the market data layer. Sections 10–11 describe
integration points (`app/main.py`, portfolio/watchlist routes) that have not
been built yet, since the rest of the platform is still in progress; they are
written as the intended design for whoever builds those parts next.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration (forward-looking)](#11-fastapi-lifecycle-integration-forward-looking)
12. [Watchlist Coordination (forward-looking)](#12-watchlist-coordination-forward-looking)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Architecture Overview

```
                    MarketDataSource (ABC)
                   /                      \
    SimulatorDataSource                MassiveDataSource
    (GBM, default,                     (Polygon.io REST poller,
     no API key needed)                 used when MASSIVE_API_KEY set)
                   \                      /
                    v                    v
                     PriceCache (thread-safe, in-memory)
                       |            |            |
                       v            v            v
              SSE /api/stream   Portfolio     Trade
              /prices           valuation     execution
```

**Strategy pattern.** Both data sources implement the same `MarketDataSource`
ABC. Downstream code (SSE streaming, portfolio valuation, trade execution)
only ever talks to a `PriceCache` — it never knows or cares which data source
is active.

**Push model, not pull.** Data sources write into the cache on their own
schedule (simulator: ~500ms; Massive: ~15s). Consumers read from the cache
at their own cadence. This decouples producer timing from consumer timing
entirely — the SSE loop doesn't need to know the active source's update
interval.

**Single source of truth.** `PriceCache` is the only place price state lives.
Nothing bypasses it: not the SSE stream, not trade execution, not portfolio
valuation.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               #   create_market_data_source, create_stream_router
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py              # MarketDataSource ABC
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py               # GBMSimulator + SimulatorDataSource
      massive_client.py          # MassiveDataSource
      factory.py                  # create_market_data_source()
      stream.py                    # SSE endpoint (FastAPI router factory)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
  market_data_demo.py           # Rich terminal demo (uv run market_data_demo.py)
```

Each module has a single responsibility; `__init__.py` re-exports the public
surface so the rest of the backend imports from `app.market` and never
reaches into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer.
Every downstream consumer works exclusively with this type.

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

### Design decisions

- **`frozen=True`** — price updates are immutable value objects, safe to
  share across async tasks without copying.
- **`slots=True`** — memory optimization; many of these are created per
  second (10 tickers × 2 ticks/sec minimum).
- **Computed properties** (`change`, `change_percent`, `direction`) —
  derived from `price`/`previous_price` so they can never drift out of sync
  with the raw values. There is no stored `direction` field that could go
  stale.
- **`to_dict()`** — the single serialization point used by both the SSE
  endpoint and any future REST responses (e.g. `GET /api/watchlist`).

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central data hub. Data sources write to it; SSE
streaming, portfolio valuation, and trade execution all read from it. It must
be thread-safe because the Massive client's synchronous HTTP call runs inside
`asyncio.to_thread()` — a real OS thread, not just another coroutine.

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

        Automatically computes direction and change from the previous price.
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
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE streaming loop polls the cache every ~500ms. Without a version
counter it would serialize and push all prices on every tick even when
nothing changed — e.g. Massive only updates every 15s, so 29 out of every 30
SSE ticks would otherwise resend an identical payload. The counter lets the
loop skip sends when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock` is used instead of `asyncio.Lock` because:

- The Massive client's synchronous `get_snapshot_all()` runs via
  `asyncio.to_thread()`, which executes in a real OS thread —
  `asyncio.Lock` provides no protection there.
- `threading.Lock` is safe to acquire from both a background thread and the
  async event loop.
- The critical sections are tiny (a dict lookup and an assignment), so lock
  contention is negligible at this project's scale (≤ tens of tickers, a
  handful of concurrent SSE readers).

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
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

### Why the source writes to the cache instead of returning prices

This push model decouples timing entirely. The simulator ticks every 500ms;
Massive polls every 15s; SSE always reads from the cache at its own fixed
cadence. Swapping which `MarketDataSource` implementation is active never
requires touching the SSE layer, and vice versa.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond what's needed for type hints.
Shared by the simulator for initial prices and GBM parameters.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist (as of project creation)
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
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
# Tickers in the same group have higher intra-group correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3  # Between sectors / unknown tickers
TSLA_CORR = 0.3  # TSLA does its own thing
```

Tickers not present in `SEED_PRICES`/`TICKER_PARAMS` (e.g. added later via
the watchlist, or by the LLM) get a random seed price in `[$50, $300]` and
`DEFAULT_PARAMS` — the simulator never rejects an unrecognized ticker (see
§7.1, `_add_ticker_internal`).

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

This file contains two classes:

- **`GBMSimulator`** — pure math engine. Stateful; holds current prices and
  advances them one step at a time. No asyncio, no I/O.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation that
  wraps `GBMSimulator` in an async loop and writes results to `PriceCache`.

### 7.1 `GBMSimulator` — the math engine

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
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where:
        S(t)   = current price
        mu     = annualized drift (expected return)
        sigma  = annualized volatility
        dt     = time step as fraction of a trading year
        Z      = correlated standard normal random variable

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix (for correlated moves)
        self._cholesky: np.ndarray | None = None

        # Initialize all starting tickers
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # Generate n independent standard normal draws
        z_independent = np.random.standard_normal(n)

        # Apply Cholesky to get correlated draws
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Return the list of currently tracked tickers."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the ticker correlation matrix.

        Called whenever tickers are added or removed. O(n^2) but n < 50.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        # Build the correlation matrix
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping.

        Correlation structure:
          - Same tech sector:   0.6
          - Same finance sector: 0.5
          - TSLA with anything: 0.3 (it does its own thing)
          - Cross-sector:       0.3
          - Unknown tickers:    0.3
        """
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is in the tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR
```

### 7.2 `SimulatorDataSource` — async wrapper

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
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
        )
        # Seed the cache with initial prices so SSE has data immediately
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
            # Seed cache immediately so the ticker has a price right away
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
        """Core loop: step the simulation, write to cache, sleep."""
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

- **Immediate seeding.** `start()` populates the cache with seed prices
  *before* the background loop begins, so the SSE endpoint has data on its
  very first tick — no blank-screen delay on page load.
- **Graceful cancellation.** `stop()` cancels the task and awaits it,
  swallowing `CancelledError`. Safe to call from FastAPI's lifespan
  shutdown, and safe to call more than once.
- **Exception resilience.** `_run_loop` catches exceptions per-step, so one
  bad tick (e.g. a numerical edge case) logs and continues instead of
  killing the whole feed.
- **Encapsulation.** `get_tickers()` on `SimulatorDataSource` calls the
  simulator's own public `get_tickers()` — it does not reach into
  `GBMSimulator`'s private `_tickers` list.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on a
configurable interval. The synchronous Massive client runs inside
`asyncio.to_thread()` so it never blocks the event loop.

```python
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
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Do an immediate first poll so the cache has data right away
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

    # --- Internal ---

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
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                    )
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
            # Don't re-raise — the loop will retry on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Massive API primer

| Field | Meaning |
|---|---|
| `RESTClient(api_key=...)` | Client constructor. `pyproject.toml` declares `massive>=1.0.0` as a core dependency, so the import is top-level, not lazy. |
| `get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])` | Single call returning current snapshots for every requested ticker — this is what keeps us within the free tier's 5 req/min limit regardless of watchlist size. |
| `snap.last_trade.price` | The price we write into the cache. |
| `snap.last_trade.timestamp` | Unix **milliseconds** — divide by 1000 before passing to `PriceCache.update()`, which expects seconds. |
| `snap.day.*` (open/high/low/close/volume/change_percent) | Available on the snapshot but not currently consumed; useful later for a detail view. |

### Error handling philosophy

The Massive poller is intentionally resilient — a bad tick never kills the
feed:

| Error | Behavior |
|---|---|
| **401 Unauthorized** | Logged as error; poller keeps running (user can fix `.env` and restart). |
| **429 Rate limited** | Logged as error; next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error; retries automatically on the next cycle. |
| **Malformed snapshot** (missing `last_trade`, etc.) | That single ticker is skipped with a warning; every other ticker in the same response is still processed. |
| **All tickers fail** | Cache retains last-known prices — SSE keeps streaming stale-but-present data, which is preferable to blanking the screen. |

### Why a top-level import, not lazy

`massive>=1.0.0` is declared as a core dependency in `pyproject.toml`, so the
import lives at module level (`from massive import RESTClient`) rather than
inside `start()`. This was a deliberate correction during code review: a
lazy import would let `patch("app.market.massive_client.RESTClient")` fail
in tests with `AttributeError` (the name wouldn't exist at module scope), and
would defer failures (missing package) from "at startup" to "on first
Massive poll" for no real benefit, since `massive` is always installed via
the lockfile regardless of whether `MASSIVE_API_KEY` is set.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage at app startup

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

`.strip()` on the env var means a `.env` file with `MASSIVE_API_KEY=` (empty)
or `MASSIVE_API_KEY=   ` (whitespace only) correctly falls back to the
simulator rather than trying to construct a `MassiveDataSource` with a blank
key.

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds open a long-lived HTTP connection and pushes
price updates to the client as `text/event-stream`.

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

    This factory pattern lets us inject the PriceCache without globals.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events in the format:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}

        Includes a retry directive so the browser auto-reconnects on
        disconnection (EventSource built-in behavior).
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

    Sends all prices every `interval` seconds. Stops when the client
    disconnects (detected via request.is_disconnected()).
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            # Check for client disconnect
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()

                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

The frontend parses this with the native `EventSource` API:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        applyPriceUpdate(ticker, update); // flash green/down, push to sparkline buffer, etc.
    }
};
```

`EventSource` reconnects automatically on drop using the `retry: 1000\n\n`
directive emitted at connection start — no manual reconnect logic is needed
on the frontend for the happy path. The header block disables proxy
buffering (`X-Accel-Buffering: no`) so events aren't queued up by an
intermediary nginx if one is ever placed in front of the container.

### Why poll-and-push instead of event-driven

The endpoint polls the cache on a fixed interval rather than being notified
by the data source directly. This is simpler (no pub/sub wiring between
producer and the SSE layer) and produces evenly-spaced updates, which
matters because the frontend accumulates these into sparklines — regular
spacing keeps that chart visually clean regardless of which data source is
active or how bursty its updates are.

---

## 11. FastAPI Lifecycle Integration (forward-looking)

`backend/app/main.py` does not exist yet — this section documents how it
should wire up the market data layer once the rest of the backend (SQLite,
portfolio, watchlist, chat) is built. The market data system starts and
stops with the app via FastAPI's `lifespan` context manager.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create and start the market data source
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist (lazily initializes
    #    the SQLite DB with seed data on first run — see PLAN.md §7)
    initial_tickers = await load_watchlist_tickers()
    await source.start(initial_tickers)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Register the SSE router directly — no need to defer this to inside lifespan,
# since create_stream_router only needs a PriceCache reference, which exists
# before startup runs.
price_cache = PriceCache()
app.include_router(create_stream_router(price_cache))


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

> **Note:** the snippet above creates `price_cache` twice for illustration
> (module scope for the router, `app.state` for lifespan) — in the real
> implementation these must be the *same* `PriceCache` instance, constructed
> once before `app = FastAPI(...)` and passed into both
> `create_stream_router()` and stored on `app.state` inside `lifespan`.
> Two separate instances would mean the SSE endpoint reads from a cache the
> market data source never writes to.

### Accessing market data from other routes

Portfolio, trade, and watchlist routes access the cache and the active
source via FastAPI dependency injection:

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
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # Insert into the watchlist table ...
    await source.add_ticker(payload.ticker)
    # ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # Delete from the watchlist table ...
    await source.remove_ticker(ticker)
    # ...
```

---

## 12. Watchlist Coordination (forward-looking)

When the watchlist changes — via the REST API or the LLM's
`watchlist_changes` action (see `PLAN.md` §9) — the active `MarketDataSource`
must be told so it tracks the right set of tickers.

### Flow: adding a ticker

```
User (or LLM) → POST /api/watchlist {ticker: "PYPL"}
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on next poll (up to poll_interval delay)
  → Return success (ticker + current price if already cached)
```

### Flow: removing a ticker

```
User (or LLM) → DELETE /api/watchlist/PYPL
  → Delete from watchlist table (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive:   removes from ticker list, removes from cache
  → Return success
```

### Edge case: ticker still has an open position

If the user removes a ticker from the watchlist but still holds shares, the
data source must keep tracking it so portfolio valuation stays accurate. The
watchlist route is responsible for this check — `MarketDataSource` itself has
no concept of positions:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if there's no open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 13. Testing Strategy

**73 tests, all passing, 84% overall coverage.** Six modules under
`backend/tests/market/`:

| Module | Tests | Coverage | What it verifies |
|---|---|---|---|
| `test_models.py` | 11 | 100% | `PriceUpdate` computed properties, `to_dict()`, edge cases (zero previous price). |
| `test_cache.py` | 13 | 100% | Update/get/get_all/remove, version increments, flat/up/down direction, thread-safety of the public API surface. |
| `test_simulator.py` | 17 | 98% | GBM step math, prices always positive, add/remove ticker rebuilds Cholesky, unknown tickers get random seed prices, duplicate add/remove are no-ops. |
| `test_simulator_source.py` | 10 | (integration) | `SimulatorDataSource.start()` seeds the cache immediately, prices evolve over time, clean/idempotent `stop()`, add/remove ticker propagates to both the simulator and the cache. |
| `test_factory.py` | 7 | 100% | Empty/whitespace `MASSIVE_API_KEY` → simulator; non-empty → Massive; correct type returned in each case. |
| `test_massive.py` | 13 | 56%* | Snapshot parsing, malformed-snapshot skip-and-continue, API-error resilience (poll doesn't crash or update cache on failure), millisecond→second timestamp conversion. |

\* `massive_client.py` coverage is lower because the real HTTP-calling code
path (`_fetch_snapshots`) is exercised via mocks, not a live API — this is
expected and intentional; hitting the real Massive API in unit tests would
make them slow, flaky, and dependent on a paid/rate-limited key.

Run the suite:

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app --cov-report=term-missing
uv run --extra dev ruff check app/ tests/
```

### Representative test patterns

**GBM math invariants** (`test_simulator.py`):

```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_unknown_ticker_gets_random_seed_price(self):
    sim = GBMSimulator(tickers=["ZZZZ"])
    price = sim.get_price("ZZZZ")
    assert 50.0 <= price <= 300.0
```

**Cache version-counter semantics** (`test_cache.py`):

```python
def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
    cache.update("AAPL", 191.00)
    assert cache.version == v0 + 2
```

**Massive poller resilience to malformed data** (`test_massive.py`):

```python
async def test_malformed_snapshot_skipped(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "BAD"]

    good_snap = _make_snapshot("AAPL", 190.50, 1707580800000)
    bad_snap = MagicMock()
    bad_snap.ticker = "BAD"
    bad_snap.last_trade = None  # Will cause AttributeError

    with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50  # Good ticker processed
    assert cache.get_price("BAD") is None      # Bad one skipped, not crashed
```

**Factory selection logic** (`test_factory.py`):

```python
def test_empty_key_uses_simulator(self, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "")
    source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)

def test_set_key_uses_massive(self, monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    source = create_market_data_source(PriceCache())
    assert isinstance(source, MassiveDataSource)
```

### Demo

A Rich terminal dashboard exercises the full simulator stack end-to-end
(not part of the automated suite, but useful for manual verification):

```bash
cd backend
uv run market_data_demo.py
```

Displays all 10 default tickers live with sparklines, color-coded
up/down arrows, and an event log for notable price moves. Runs for 60
seconds or until Ctrl+C.

---

## 14. Error Handling & Edge Cases

### 14.1 Startup with an empty watchlist

If the database has no watchlist entries, `start()` receives an empty list.
Both sources handle this gracefully: the simulator's `step()` returns `{}`
immediately (`GBMSimulator.step` short-circuits when `n == 0`), and the
Massive poller's `_poll_once()` returns immediately when `self._tickers` is
empty. The SSE endpoint sends no events until a ticker is added, at which
point tracking starts immediately.

### 14.2 Price cache miss during trade

If a user tries to trade a ticker with no cached price yet (just added,
Massive hasn't polled it), the trade route should surface a clear 400 rather
than crash on `None`:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

In practice this gap is near-zero for the simulator (it seeds the cache
synchronously inside `add_ticker()`), but can be up to `poll_interval`
seconds wide for Massive.

### 14.3 Invalid Massive API key

If `MASSIVE_API_KEY` is set but wrong, the first poll fails with 401. The
poller logs the error (`_poll_once`'s broad `except Exception`) and keeps
retrying every `poll_interval` seconds rather than crashing the background
task. The SSE endpoint keeps the connection open but streams no data for
that ticker. The fix is operational: correct the key and restart the
container.

### 14.4 Thread safety under load

`PriceCache` uses a single `threading.Lock`. Under the project's actual load
(≤ tens of tickers, 2 updates/sec from the simulator or one poll per 15s
from Massive, and a handful of concurrent SSE readers), lock contention is
negligible — the critical section is a dict lookup plus an assignment. A
`ReadWriteLock` would be the natural upgrade if this ever became a
bottleneck (hundreds of tickers, many concurrent readers), but that scale is
out of scope for this project.

### 14.5 Simulator numerical stability

- Prices are `round()`ed to 2 decimal places inside `GBMSimulator.step()`
  before being returned, and again inside `PriceCache.update()`.
- The exponential formulation (`price *= exp(drift + diffusion)`) is
  numerically stable and, being multiplicative, can never produce a
  negative or zero price.
- The tiny `dt` (~8.5e-8) produces sub-cent moves per tick that accumulate
  naturally into realistic intraday ranges over many ticks — no explicit
  clamping or bounds-checking is needed.

---

## 15. Configuration Summary

All tunable parameters and their defaults:

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | Time between simulator ticks. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | Time between Massive API polls (free-tier safe: 5 req/min). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick (~0.1%). |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, as a fraction of a trading year. |
| SSE push interval | `_generate_events()` | `0.5` (seconds) | Time between cache polls / pushes to the client. |
| SSE retry directive | `_generate_events()` | `1000` (ms) | Browser `EventSource` reconnection delay after disconnect. |

### `__init__.py` — public API

**File: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate         - Immutable price snapshot dataclass
    PriceCache          - Thread-safe in-memory price store
    MarketDataSource    - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router - FastAPI router factory for SSE endpoint
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

### Quick reference for downstream code

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # Reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices
update = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```
