# Market Data Backend — Design

Detailed, implementation-accurate design for the FinAlly market data subsystem: the unified `MarketDataSource` interface, the GBM simulator, the Massive (Polygon.io) API client, the shared price cache, and the SSE streaming endpoint.

**Status: implemented.** Everything described here exists in `backend/app/market/` and is covered by the test suite in `backend/tests/market/` (73 tests, 84% coverage — see `planning/MARKET_DATA_SUMMARY.md`). This document is the reference for engineers building the rest of the platform (portfolio, watchlist, chat) on top of this subsystem, and for anyone extending the market data layer itself.

---

## Table of Contents

1. [Architecture at a Glance](#1-architecture-at-a-glance)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters--seed_pricespy)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint--streampy)
11. [Public Package API — `__init__.py`](#11-public-package-api--__init__py)
12. [Integrating with the Rest of the Backend](#12-integrating-with-the-rest-of-the-backend)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Reference](#16-configuration-reference)

---

## 1. Architecture at a Glance

```
                 ┌─────────────────────────┐
  MASSIVE_API_KEY│  create_market_data_     │
  env var ───────►  source(price_cache)     │  (factory.py)
                 └───────────┬──────────────┘
                              │ picks one
              ┌───────────────┴────────────────┐
              ▼                                 ▼
  ┌────────────────────────┐      ┌───────────────────────────┐
  │  SimulatorDataSource    │      │  MassiveDataSource         │
  │  (GBM engine, 500ms     │      │  (Polygon.io REST poller,  │
  │   asyncio loop)         │      │   15s poll loop)            │
  └───────────┬─────────────┘      └────────────┬────────────────┘
              │        writes                    │ writes
              └───────────────┬───────────────────┘
                               ▼
                    ┌───────────────────────┐
                    │      PriceCache        │  thread-safe,
                    │  (ticker → PriceUpdate)│  version-counted
                    └───────────┬─────────────┘
                                │ reads
        ┌───────────────────────┼───────────────────────┐
        ▼                       ▼                       ▼
  GET /api/stream/prices   portfolio valuation     trade execution
  (SSE, stream.py)         (backend/app/portfolio)  (backend/app/portfolio)
```

Both data sources implement the same `MarketDataSource` ABC and are otherwise invisible to the rest of the app — everything downstream reads exclusively from `PriceCache`. This is a **push producer / pull consumer** split: producers write on their own schedule (500ms for the simulator, 15s for Massive), consumers (SSE, portfolio math) read at whatever cadence they need. Nothing downstream needs to know which source is active.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Public re-exports
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py              # MarketDataSource ABC
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
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

Each module has a single responsibility; `app/market/__init__.py` is the only import surface the rest of the backend should use.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the sole data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

```python
"""Data models for market data."""

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

- **`frozen=True`, `slots=True`** — value objects, safe to share across async tasks without copying; slimmer memory footprint since many are created per second.
- **Computed properties** — `change`, `change_percent`, `direction` derive from `price`/`previous_price` so they can never drift out of sync with a stale stored field.
- **`to_dict()`** — the single serialization point used by both the SSE endpoint and (eventually) REST responses that embed price data.

---

## 4. Price Cache — `cache.py`

The price cache is the central hub. Exactly one data source writes to it at a time; SSE streaming, portfolio valuation, and trade execution all read from it.

```python
"""Thread-safe in-memory price cache."""

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

The SSE loop polls the cache every ~500ms. Without a version counter it would re-serialize and re-send every ticker's price on every tick even when nothing changed — wasteful once Massive is only updating every 15s. Instead:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock`, not `asyncio.Lock`

- The Massive client's synchronous REST call runs inside `asyncio.to_thread()`, i.e. a real OS thread — an `asyncio.Lock` would not protect against that.
- `threading.Lock` is safe from both a sync thread and the async event loop, so one cache implementation works under either data source.
- The critical section (dict get/set) is tiny; contention is a non-issue at this scale (≤ tens of tickers, a handful of SSE readers).

---

## 5. Abstract Interface — `interface.py`

```python
"""Abstract interface for market data sources."""

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

Push, not pull, decouples timing. The simulator ticks every 500ms; Massive polls every 15s; SSE always reads the cache on its own 500ms cadence regardless of which source is active. No SSE-layer branching on source type, ever.

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond the type hints. Shared by the simulator for initial prices, GBM parameters, and sector correlation grouping.

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

A ticker added dynamically (via watchlist or LLM chat) that isn't in `SEED_PRICES`/`TICKER_PARAMS` falls back to a random seed price in `[50, 300]` and `DEFAULT_PARAMS` — see `GBMSimulator._add_ticker_internal` below.

---

## 7. GBM Simulator — `simulator.py`

Two classes live here: `GBMSimulator` (pure math engine, no asyncio) and `SimulatorDataSource` (the `MarketDataSource` implementation that drives it on a loop and writes to the cache).

### 7.1 `GBMSimulator` — the math engine

```python
"""GBM-based market simulator."""

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
          - Same tech sector:    0.6
          - Same finance sector: 0.5
          - TSLA with anything:  0.3 (it does its own thing)
          - Cross-sector:        0.3
          - Unknown tickers:     0.3
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

**Note on `get_tickers()`:** this is a public accessor (not `sim._tickers` reached from outside). `SimulatorDataSource.get_tickers()` calls it rather than touching the simulator's private list directly — keeps the class boundary clean.

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

**Key behaviors:**

- **Immediate seeding.** `start()` populates the cache with seed prices *before* the loop begins, so SSE has data on its very first tick — no blank-screen delay on a fresh boot.
- **Graceful cancellation.** `stop()` cancels the task and awaits it, swallowing `CancelledError`. Safe to call from FastAPI `lifespan` teardown, and safe to call twice.
- **Exception resilience.** The loop catches per-step exceptions so one bad tick never kills the feed for the rest of the session.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (Polygon.io) REST snapshot endpoint on a configurable interval. The Massive SDK is synchronous, so calls run inside `asyncio.to_thread()` to avoid blocking the event loop.

```python
"""Massive (Polygon.io) API client for real market data."""

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

### Why the `massive` import is top-level, not lazy

An earlier draft of this design imported `massive` lazily inside `start()`, on the theory that students without a Massive API key shouldn't need the package installed. In practice `massive` is declared as a core dependency in `pyproject.toml` (`massive>=1.0.0`) and installed unconditionally via `uv sync` — every environment has it whether or not `MASSIVE_API_KEY` is set. A lazy import added indirection with no actual benefit and was removed during code review; `massive_client.py` now imports `RESTClient` and `SnapshotMarketType` at module scope like everything else.

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error. Poller keeps running — a bad key can be fixed in `.env` and the app restarted. |
| **429 Rate Limited** | Logged as error. Next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error. Retries automatically on next cycle. |
| **Malformed snapshot** | That ticker is skipped with a warning; other tickers in the same poll still process. |
| **All tickers fail** | Cache retains last-known prices. SSE keeps streaming stale-but-present data — preferable to a blank UI. |

---

## 9. Factory — `factory.py`

```python
"""Factory for creating market data sources."""

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

Both `MassiveDataSource` and `SimulatorDataSource` are imported at module top level (matching the top-level `massive` import above — see §8). Whitespace-only keys (`"   "`) are treated as empty via `.strip()`, so `MASSIVE_API_KEY=` or an accidentally-whitespace value both fall back to the simulator rather than constructing a `MassiveDataSource` that will fail on first poll.

**Usage at app startup:**

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

---

## 10. SSE Streaming Endpoint — `stream.py`

A FastAPI route that holds a long-lived connection open and pushes price updates as `text/event-stream`.

```python
"""SSE streaming endpoint for live price updates."""

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

### Wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

Frontend consumption:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, timestamp,
    //                        change, change_percent, direction }, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        applyPriceFlash(ticker, update.direction);   // green/red CSS flash
        appendToSparkline(ticker, update.price);     // accumulate since page load
    }
};
```

### Why poll-and-push instead of event-driven

Fixed-interval polling of the cache (rather than the data source notifying the SSE layer on every write) keeps updates evenly spaced regardless of source. That matters because the frontend's sparklines accumulate points from this stream — irregular spacing would distort the chart. It also means the SSE layer needs zero knowledge of which source is active or how often it writes.

---

## 11. Public Package API — `__init__.py`

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

All downstream backend code should import from `app.market`, never from a submodule directly:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 12. Integrating with the Rest of the Backend

The market data subsystem is complete; the FastAPI app itself (`app/main.py`), portfolio, watchlist, and chat routes are not yet built. This section is the contract those modules should build against.

### FastAPI lifecycle

Start and stop the market data source using FastAPI's `lifespan` context manager so the background task's lifetime matches the app's:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # reads from SQLite, lazily initialized
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # app is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Reading prices from a route

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
    # ... validate cash/shares, record the trade at current_price, update positions ...
```

### Adding/removing watchlist tickers

```python
@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.insert_watchlist_entry(payload.ticker)
    await source.add_ticker(payload.ticker)
    return {"status": "ok"}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)
    await source.remove_ticker(ticker)
    return {"status": "ok"}
```

This is also how the LLM chat flow manages the watchlist — `watchlist_changes` in the structured chat response should call the same `add_to_watchlist`/`remove_from_watchlist` logic (or the underlying `source.add_ticker`/`remove_ticker` calls) rather than duplicating it.

---

## 13. Watchlist Coordination

### Flow: adding a ticker

```
User (or LLM) → POST /api/watchlist {ticker: "PYPL"}
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on the next poll (≤ poll_interval later)
  → Return success (ticker + current price if already available)
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

### Edge case: ticker has an open position

If the user removes a ticker from the watchlist but still holds shares, the data source must keep tracking it so portfolio valuation stays accurate. The watchlist route — not the market data layer — is responsible for this check:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # only stop tracking if nothing is held

    return {"status": "ok"}
```

`MarketDataSource` itself has no concept of "positions" — that's portfolio-layer logic composed on top of the watchlist-coordination primitives (`add_ticker`/`remove_ticker`), keeping the market data module decoupled from portfolio/database concerns.

---

## 14. Testing Strategy

The implemented suite lives in `backend/tests/market/` (73 tests total, `asyncio_mode = "auto"` via `pyproject.toml`). Representative examples:

### 14.1 `GBMSimulator` unit tests — `test_simulator.py`

```python
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:
    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_and_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.add_ticker("TSLA")
        assert "TSLA" in sim.get_tickers()
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "TSLA" in result

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        assert 50.0 <= sim.get_price("ZZZZ") <= 300.0

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # single ticker, no correlation matrix
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None
```

### 14.2 `PriceCache` unit tests — `test_cache.py`

```python
from app.market.cache import PriceCache


class TestPriceCache:
    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_up_and_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        up = cache.update("AAPL", 191.00)
        assert up.direction == "up" and up.change == 1.00
        down = cache.update("AAPL", 189.00)
        assert down.direction == "down"

    def test_version_increments_on_every_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None
```

### 14.3 `SimulatorDataSource` integration tests — `test_simulator_source.py`

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
        assert cache.get("AAPL") is not None  # seeded before the loop's first tick
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
        await source.stop()  # must not raise
```

### 14.4 `MassiveDataSource` unit tests (mocked) — `test_massive.py`

The Massive SDK is never called over the network in tests — `_fetch_snapshots` is patched so tests are hermetic and fast:

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # satisfies the _poll_once guard

        snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000),
            _make_snapshot("GOOGL", 175.25, 1707580800000),
        ]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_is_skipped_not_fatal(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()

        good = _make_snapshot("AAPL", 190.50, 1707580800000)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # triggers AttributeError, caught and logged

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash_the_loop(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # must not raise

        assert cache.get_price("AAPL") is None
```

### 14.5 Factory tests — `test_factory.py`

```python
import os
from unittest.mock import patch
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


class TestFactory:
    def test_creates_simulator_when_no_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(PriceCache())
        assert isinstance(source, SimulatorDataSource)

    def test_creates_simulator_when_api_key_whitespace(self):
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "   "}, clear=True):
            source = create_market_data_source(PriceCache())
        assert isinstance(source, SimulatorDataSource)

    def test_creates_massive_when_api_key_set(self):
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}, clear=True):
            source = create_market_data_source(PriceCache())
        assert isinstance(source, MassiveDataSource)
```

### Running the suite

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v              # all tests
uv run --extra dev pytest --cov=app       # with coverage
uv run --extra dev ruff check app/ tests/ # lint
```

### Manual/visual verification

```bash
cd backend
uv run market_data_demo.py
```

A Rich terminal dashboard showing all 10 default tickers live, sparklines, color-coded direction arrows, and an event log for shock moves. Runs for 60 seconds or until `Ctrl+C` — useful for eyeballing that the simulator "feels" right (correlated tech moves, occasional shocks) before wiring up the frontend.

---

## 15. Error Handling & Edge Cases

### 15.1 Startup with an empty watchlist

If SQLite has zero watchlist rows (user deleted everything), `start([])` is a legal call for both sources — the simulator produces no prices, the Massive poller skips its API call entirely (`if not self._tickers ...: return`). SSE sends no data until a ticker is added, at which point tracking begins immediately.

### 15.2 Cache miss on trade

A ticker that was just added and hasn't produced a price yet (Massive's first poll is in flight) must fail the trade cleanly rather than trade at `None`:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator sidesteps this by seeding the cache synchronously inside `add_ticker()`, so this branch is realistically Massive-only.

### 15.3 Invalid Massive API key

A bad key surfaces as a 401 on the first poll. The poller logs it and keeps retrying every `poll_interval` — it does not crash the app or stop trying. The SSE connection stays "connected" (green dot) with no data flowing, since SSE health and data availability are orthogonal. Fix: correct `.env` and restart the container.

### 15.4 Thread safety under load

`PriceCache._lock` is a plain mutex; the critical section is a dict read/write. At the project's actual scale (≤ tens of tickers, single user, one SSE connection) contention is negligible. If this became a real bottleneck (hundreds of tickers, many concurrent readers) the fix would be a read/write lock — deliberately not implemented, since that optimization has no payoff at this scale.

### 15.5 Simulator numerical stability

- Prices are rounded to 2 decimals in `GBMSimulator.step()`.
- The exponential formulation `exp(drift + diffusion)` is numerically stable and always positive — GBM cannot produce a negative or zero price.
- The tiny per-tick `dt` (~8.5e-8) means each step moves price by a fraction of a cent on average; visible movement accumulates over many ticks, which is what gives the simulator its "live market" feel rather than jumpy noise.

---

## 16. Configuration Reference

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set (non-whitespace), use `MassiveDataSource`; otherwise use `SimulatorDataSource`. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between `GBMSimulator.step()` calls. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive REST polls (free tier: 5 req/min). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Per-ticker, per-tick chance of a 2–5% shock move. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, as a fraction of a trading year, for 500ms ticks. |
| SSE push interval | `_generate_events()` in `stream.py` | `0.5` s | Cache polling / send cadence for the SSE endpoint. |
| SSE retry directive | `_generate_events()` in `stream.py` | `1000` ms | `EventSource` auto-reconnect delay sent to the browser. |
| `INTRA_TECH_CORR` | `seed_prices.py` | `0.6` | Correlation between two tech-sector tickers. |
| `INTRA_FINANCE_CORR` | `seed_prices.py` | `0.5` | Correlation between two finance-sector tickers. |
| `CROSS_GROUP_CORR` | `seed_prices.py` | `0.3` | Correlation between cross-sector or unrecognized tickers. |
| `TSLA_CORR` | `seed_prices.py` | `0.3` | TSLA's correlation with everything (kept low deliberately). |

### Dependencies (`backend/pyproject.toml`)

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",
    "massive>=1.0.0",
    "rich>=13.0.0",
]
```

`massive` and `numpy` are both unconditional core dependencies — every environment has them installed via `uv sync`, whether or not `MASSIVE_API_KEY` is ever set. `rich` powers `market_data_demo.py` only.
