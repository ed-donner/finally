# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified provider interface, the in-memory price cache, the GBM simulator, the Massive (Polygon.io) API client, the SSE streaming endpoint, and how the rest of the backend wires into all of it.

**Status:** This subsystem is implemented and tested (see `planning/MARKET_DATA_SUMMARY.md` for the build/test summary). This document is the reference design — the code snippets below are the actual, current implementation in `backend/app/market/`, not a proposal. Section 10 (FastAPI lifecycle integration) is forward-looking: `backend/app/main.py` does not exist yet, so that section specifies how the not-yet-built app entrypoint and other routers (portfolio, watchlist) should wire into this subsystem.

---

## Table of Contents

1. [Design Goals](#1-design-goals)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Abstract Interface — `interface.py`](#4-abstract-interface)
5. [Price Cache — `cache.py`](#5-price-cache)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration (forward-looking)](#11-fastapi-lifecycle-integration-forward-looking)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Testing Strategy](#14-testing-strategy)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Design Goals

- **Source-agnostic downstream code.** SSE streaming, portfolio valuation, and trade execution never know whether prices come from the simulator or Massive. They only see `PriceCache` and `PriceUpdate`.
- **Push, not pull.** Data sources write into a shared cache on their own schedule (500ms for the simulator, 15s for Massive free tier). Consumers read the cache at their own cadence. This decouples producer timing from consumer timing.
- **Zero external dependencies for the default path.** With no `MASSIVE_API_KEY`, the simulator runs entirely in-process — no network calls, no API key required, works offline.
- **Graceful degradation.** A bad Massive poll, an invalid API key, or a malformed snapshot never crashes the background task or blanks out prices — the cache simply retains the last good value.
- **Cheap to extend.** Adding a new ticker or a new data source (e.g., a websocket-based provider later) should not require touching the SSE endpoint, portfolio code, or the cache.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               #   create_market_data_source, create_stream_router
      models.py                # PriceUpdate dataclass
      interface.py              # MarketDataSource ABC
      cache.py                  # PriceCache (thread-safe in-memory store)
      seed_prices.py             # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py                # GBMSimulator + SimulatorDataSource
      massive_client.py            # MassiveDataSource
      factory.py                    # create_market_data_source()
      stream.py                      # SSE endpoint (FastAPI router factory)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each module has a single responsibility. `app/market/__init__.py` re-exports the public API so the rest of the backend imports from `app.market` without reaching into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

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

### Design decisions

- **`frozen=True`** — Price updates are immutable value objects, safe to share across async tasks without copying.
- **`slots=True`** — Memory optimization; the system creates many of these per second.
- **Computed properties** (`change`, `change_percent`, `direction`) — Derived from `price` and `previous_price`, so they can never fall out of sync. There is no stored `direction` field that could go stale.
- **`to_dict()`** — Single serialization point, used by both the SSE endpoint and (eventually) REST responses like `/api/watchlist` and `/api/portfolio`.

---

## 4. Abstract Interface

**File: `backend/app/market/interface.py`**

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

This push model decouples timing. The simulator ticks every 500ms; Massive polls every 15s (free tier). SSE always reads from the cache at its own 500ms cadence regardless of which source is active — it never needs to know the source's update interval, and adding a third data source later requires no change to the SSE layer.

---

## 5. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central hub: data sources write to it; SSE streaming, portfolio valuation, and trade execution read from it. It must be thread-safe because a data source's background work may run in a thread pool executor (`asyncio.to_thread`, used by the Massive client) while SSE reads happen on the async event loop.

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

The SSE loop polls the cache every ~500ms. Without a version counter it would serialize and re-send every price on every tick, even when nothing changed (e.g., Massive only updates every 15s). The counter lets the SSE loop skip a send when nothing is new:

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
- The Massive client's synchronous `get_snapshot_all()` call runs inside `asyncio.to_thread()`, which executes in a real OS thread — `asyncio.Lock` would not protect against concurrent access from that thread.
- `threading.Lock` works correctly whether the caller is a sync thread or the async event loop.
- The critical sections are tiny (a dict read or write plus an int increment), so contention is negligible even at the target scale (≤ dozens of tickers, one writer, a handful of SSE readers).

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib types. Shared by the simulator (initial prices, GBM parameters, correlation structure) and available as a fallback if the Massive client needs seed values before its first successful poll.

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

Tickers not in `SEED_PRICES` (added dynamically via the watchlist or LLM chat) start at a random price between $50–$300 and use `DEFAULT_PARAMS`.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes live here:
- **`GBMSimulator`** — pure math engine, stateful, holds current prices and advances them one step at a time.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation that wraps `GBMSimulator` in an async loop and writes results to the `PriceCache`.

### 7.1 The math

Geometric Brownian Motion is the standard model behind Black-Scholes: prices evolve continuously with random noise, never go negative, and follow a lognormal distribution — the same statistical shape observed in real markets.

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where `S(t)` is the current price, `mu` is annualized drift, `sigma` is annualized volatility, `dt` is the time step as a fraction of a trading year, and `Z` is a (correlated) standard normal draw.

For 500ms updates over a 252-day, 6.5-hour trading year:

```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8
```

This tiny `dt` produces small, realistic per-tick moves that accumulate naturally into believable intraday ranges.

### 7.2 Correlated moves via Cholesky decomposition

Real stocks don't move independently. Given a correlation matrix `C`, the Cholesky factor `L = cholesky(C)` transforms independent standard normals into correlated ones: `Z_correlated = L @ Z_independent`. The correlation structure used here:

| Pairing | Correlation |
|---|---|
| Two tech tickers | 0.6 |
| Two finance tickers | 0.5 |
| Anything involving TSLA | 0.3 (it moves on its own) |
| Cross-sector or unknown tickers | 0.3 |

### 7.3 Random shock events

Every step, each ticker has a small (~0.1%) chance of a sudden 2–5% move, for visual drama on the dashboard. At 10 tickers and 2 ticks/sec, expect roughly one event every ~50 seconds.

### 7.4 Implementation

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

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

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

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker, shock_magnitude * 100, "up" if shock_sign > 0 else "down",
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

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR


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

- **Immediate seeding.** `start()` populates the cache with seed prices before the loop begins, so the SSE endpoint has data to send on its very first tick — no blank-screen delay on page load.
- **Graceful cancellation.** `stop()` cancels the background task and awaits it, catching `CancelledError`, for clean shutdown during FastAPI lifespan teardown.
- **Exception resilience.** The loop catches exceptions per-step so a single bad tick (e.g., a numerical edge case) never kills the entire feed.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on a configurable interval. The synchronous `massive` SDK client runs inside `asyncio.to_thread()` so it never blocks the event loop.

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
            len(tickers), self._interval,
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
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop retries on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Massive API reference (as used here)

**Client init** — `RESTClient(api_key=...)`, or `RESTClient()` to read `MASSIVE_API_KEY` from the environment automatically.

**Primary endpoint — snapshot, all tickers in one call:**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)
for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
```

Getting all watched tickers in a single call is what keeps the free tier (5 req/min) viable — polling ticker-by-ticker would blow the budget instantly with a 10-ticker watchlist.

Key fields extracted per snapshot: `snap.ticker`, `snap.last_trade.price`, `snap.last_trade.timestamp` (Unix **milliseconds**, converted to seconds before writing to the cache).

**Rate limits:**

| Tier | Limit | FinAlly poll interval |
|---|---|---|
| Free | 5 req/min | 15s |
| Paid | much higher | 2–5s |

### Error handling philosophy

The poller is intentionally resilient — a live trading terminal should never crash because of a flaky upstream API:

| Error | Behavior |
|---|---|
| 401 Unauthorized (bad key) | Logged as error; poller keeps running so a corrected `.env` + restart recovers cleanly. |
| 429 Rate limited | Logged as error; next poll retries after `poll_interval`. |
| Network timeout | Logged as error; retried automatically on the next cycle. |
| Malformed snapshot for one ticker | That ticker is skipped with a warning; other tickers in the same batch are still processed. |
| All tickers fail | Cache retains last-known prices — SSE keeps streaming stale-but-present data rather than going blank. |

### Lazy dependency, not lazy import

Unlike an earlier draft of this design, `massive_client.py` imports `RESTClient` and `SnapshotMarketType` at module level. `massive>=1.0.0` is declared as a core dependency in `pyproject.toml`, so it is always installed — `uv sync` pulls it in regardless of whether `MASSIVE_API_KEY` is set. The **optionality is behavioral, not import-time**: `factory.py` (below) only *constructs* a `MassiveDataSource` when the key is present, so the client is never instantiated — and never makes a network call — in simulator mode.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

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

### Usage at app startup

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

This single function is the only place in the codebase that branches on `MASSIVE_API_KEY`. Everything downstream — SSE, portfolio valuation, watchlist routes, the LLM chat tool that executes trades — only ever sees a `MarketDataSource` and a `PriceCache`.

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds open a long-lived HTTP connection and pushes price updates to the browser as `text/event-stream`.

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
    yield "retry: 1000\n\n"  # Tell the client to retry after 1s if the connection drops

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

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

Frontend consumption (per PLAN.md §10, `EventSource` is the required client API):

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    // Use `direction` to trigger the green/red flash animation,
    // and accumulate each ticker's `price` client-side to build sparklines.
};
```

### Why poll-and-push instead of event-driven?

The endpoint polls the cache on a fixed interval rather than being notified by the data source. This is simpler, and — more importantly — it produces evenly-spaced updates regardless of source cadence, which matters because the frontend accumulates these into sparkline charts; even spacing keeps that visualization clean, whether the underlying source is a 500ms simulator tick or a 15s Massive poll (in the latter case, most 500ms ticks are no-ops thanks to the version check, and the sparkline simply has fewer, evenly-spaced points).

---

## 11. FastAPI Lifecycle Integration (forward-looking)

`backend/app/main.py` has not been written yet — the rest of the platform (portfolio, watchlist, chat, database) is still to be built per `PLAN.md`. This section specifies how that entrypoint should start and stop the market data subsystem using FastAPI's `lifespan` context manager, and how other routers should access it.

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

    # 2. Create the market data source (reads MASSIVE_API_KEY)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist (lazy-init DB if needed)
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite `watchlist` table
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependencies for injecting shared state into route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Accessing market data from other routers

Portfolio, trade execution, and watchlist routes access the cache and source via FastAPI dependency injection — never by importing a module-level singleton:

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
    # ... validate cash/shares, insert into `trades` and `positions`, at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into `watchlist` table ...
    await source.add_ticker(payload.ticker)
    # ... return ticker + current price if already cached ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from `watchlist` table (see §12 for the open-position edge case) ...
    await source.remove_ticker(ticker)
```

The LLM chat tool-execution path (structured `trades` / `watchlist_changes` from the model, per `PLAN.md` §9) should route through these same functions rather than duplicating trade/watchlist logic — auto-executed LLM actions and manually-triggered REST actions must share one code path so validation stays consistent.

---

## 12. Watchlist Coordination

When the watchlist changes — via the REST API or LLM chat — the market data source must be told, so it tracks the right set of tickers.

### Flow: adding a ticker

```
User (or LLM) → POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on the next poll (up to `poll_interval` delay)
  → Response: ticker + current price (if already cached)
```

### Flow: removing a ticker

```
User (or LLM) → DELETE /api/watchlist/PYPL
  → DELETE FROM watchlist (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive:   removes from ticker list, removes from cache
  → Response: success
```

### Edge case: ticker has an open position

If the user removes a ticker from the watchlist while still holding shares, the data source must keep tracking it — otherwise portfolio valuation and the positions table lose their price feed. The watchlist route must check for an open position before calling `remove_ticker()`:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    # else: keep tracking — portfolio valuation still needs live prices for it

    return {"status": "ok"}
```

Symmetrically, buying a ticker that isn't currently on the watchlist (possible via LLM-initiated trades) should call `source.add_ticker()` even if no watchlist row is created, so the new position gets priced immediately.

---

## 13. Error Handling & Edge Cases

### 13.1 Startup with an empty watchlist

If the database has no watchlist rows (e.g. the user deleted every ticker), `start([])` is called. Both sources handle this gracefully: the simulator produces no prices, the Massive poller skips its API call entirely (`if not self._tickers: return`). The SSE endpoint sends no events until a ticker is added, at which point tracking begins immediately.

### 13.2 Price cache miss during trade execution

A ticker can be requested for trading before it has a cached price (just added, Massive hasn't polled yet). The trade route must surface this as a clear client error rather than crashing or trading at a null price:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this window entirely by seeding the cache synchronously inside `add_ticker()`. Massive has an inherent gap of up to `poll_interval`; the 400 response with a clear message is the correct behavior for that gap, not a bug to "fix" with blocking/waiting logic.

### 13.3 Invalid Massive API key

If the key is set but wrong, the first poll fails with 401. The poller logs the error and keeps retrying every `poll_interval` — it does not crash or exit. The SSE endpoint keeps streaming (connection stays "connected" in the UI's status indicator) but with empty or stale price data, since the cache never gets populated. The fix is operational: correct `.env` and restart the container.

### 13.4 Thread safety under load

`PriceCache`'s `threading.Lock` is a plain mutex — one thread holds it at a time. At the target scale (≤ dozens of tickers, one writer, a handful of concurrent SSE readers in this single-user app), contention is negligible; the critical section is a dict read/write plus an int increment. A `ReadWriteLock` would only matter at a scale (hundreds of tickers, many concurrent readers) this project never needs.

### 13.5 Simulator numerical stability

- Prices are rounded to 2 decimal places inside `GBMSimulator.step()`.
- The exponential formulation (`exp(drift + diffusion)`) is numerically stable and always yields a positive result — GBM prices cannot go negative or hit exactly zero.
- Tiny `dt` (~8.5e-8) keeps per-tick moves small; volatility accumulates correctly over many ticks rather than producing implausible single-tick jumps (outside of the deliberate random shock events).

---

## 14. Testing Strategy

Tests live in `backend/tests/market/`, one module per source file, following `PLAN.md` §12's backend testing guidance (pytest, `pytest-asyncio`).

| Test module | What it covers |
|---|---|
| `test_models.py` | `PriceUpdate` computed properties (`change`, `change_percent`, `direction`) and `to_dict()` serialization, including edge cases like `previous_price == 0`. |
| `test_cache.py` | `update`/`get`/`get_all`/`get_price`/`remove`, first-update-is-flat behavior, version counter increments, `__len__`/`__contains__`. |
| `test_simulator.py` | GBM math properties: prices always positive, `step()` returns all tracked tickers, add/remove ticker rebuilds Cholesky correctly, duplicate add / missing remove are no-ops, unknown tickers get a random seed in `[50, 300]`, prices drift after many steps. |
| `test_simulator_source.py` | Integration: `start()` seeds the cache immediately (no blank window), prices actually change over time, `stop()` is idempotent (safe to call twice), `add_ticker`/`remove_ticker` propagate to both the simulator and the cache. |
| `test_factory.py` | `create_market_data_source` returns `SimulatorDataSource` when `MASSIVE_API_KEY` is unset/empty, `MassiveDataSource` when set — via `monkeypatch.setenv`/`delenv`, not real network calls. |
| `test_massive.py` | `_poll_once` updates the cache from mocked snapshot objects; a malformed snapshot for one ticker is skipped without affecting others; an exception from `_fetch_snapshots` is swallowed (poller doesn't crash) and leaves the cache untouched for that ticker. |

Representative patterns:

```python
# test_simulator.py
def test_prices_are_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0


def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None  # Only 1 ticker, no correlation matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None
```

```python
# test_massive.py — mock the SDK boundary, not the network
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


async def test_malformed_snapshot_skipped():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "BAD"]

    good = _make_snapshot("AAPL", 190.50, 1707580800000)
    bad = MagicMock(ticker="BAD", last_trade=None)  # triggers AttributeError

    with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("BAD") is None
```

Because `massive_client.py` imports `RESTClient` and `SnapshotMarketType` at module level (§8), and `massive` is a core dependency, `test_massive.py` runs without needing `create=True` patch tricks — patch targets exist at import time as long as `uv sync` (not `uv sync --no-dev` against a stripped lockfile) has installed the declared dependency.

SSE (`stream.py`) is best covered with an ASGI test client once `main.py` exists (e.g. `httpx.AsyncClient` against the FastAPI app), asserting that a connected client receives at least one `data:` event containing a known seeded ticker.

---

## 15. Configuration Summary

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | Time between simulator ticks. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | Time between Massive API polls (free-tier safe; lower for paid tiers). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, as a fraction of a trading year. |
| SSE push interval | `_generate_events()` | `0.5` (seconds) | Time between cache-version checks / pushes to the client. |
| SSE retry directive | `_generate_events()` | `1000` (ms) | Browser `EventSource` reconnection delay after a dropped connection. |

### Public package API (`backend/app/market/__init__.py`)

```python
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
