# Market Data Backend — Design & Implementation Guide

This is the implementation-ready design for FinAlly's market data subsystem: a unified interface with two interchangeable price sources (a GBM simulator and a Massive/Polygon.io REST poller), a thread-safe price cache, and an SSE endpoint that streams live prices to the frontend.

**Status:** This subsystem is already built and tested (see `planning/MARKET_DATA_SUMMARY.md`). This document is the detailed reference for how it works and how to build on top of it — every snippet below is drawn directly from the code in `backend/app/market/`.

Everything lives under `backend/app/market/`:

```
backend/
  app/
    market/
      __init__.py          # Public API re-exports
      models.py             # PriceUpdate dataclass
      cache.py               # PriceCache (thread-safe in-memory store)
      interface.py           # MarketDataSource ABC — the "unified API"
      seed_prices.py         # Seed prices, GBM params, correlation groups
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py               # create_market_data_source() — picks the source
      stream.py                # SSE endpoint (FastAPI router)
  market_data_demo.py         # Rich terminal demo of the live feed
```

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [The Unified API](#2-the-unified-api)
   - 2.1 [Data Model — `models.py`](#21-data-model--modelspy)
   - 2.2 [Price Cache — `cache.py`](#22-price-cache--cachepy)
   - 2.3 [Abstract Interface — `interface.py`](#23-abstract-interface--interfacepy)
   - 2.4 [Factory — `factory.py`](#24-factory--factorypy)
3. [The Simulator](#3-the-simulator)
   - 3.1 [Seed Data — `seed_prices.py`](#31-seed-data--seed_pricespy)
   - 3.2 [GBM Math — `GBMSimulator`](#32-gbm-math--gbmsimulator)
   - 3.3 [Async Wrapper — `SimulatorDataSource`](#33-async-wrapper--simulatordatasource)
4. [The Massive API Client](#4-the-massive-api-client)
   - 4.1 [Massive REST API Reference](#41-massive-rest-api-reference)
   - 4.2 [`MassiveDataSource` Implementation](#42-massivedatasource-implementation)
   - 4.3 [Error Handling Philosophy](#43-error-handling-philosophy)
5. [SSE Streaming Endpoint](#5-sse-streaming-endpoint)
6. [Wiring It Into FastAPI](#6-wiring-it-into-fastapi)
7. [Watchlist Coordination](#7-watchlist-coordination)
8. [Testing Examples](#8-testing-examples)
9. [Configuration Reference](#9-configuration-reference)

---

## 1. Architecture

```
                MarketDataSource (ABC)
                 "unified API"
               ┌───────────────┴────────────────┐
               │                                 │
     SimulatorDataSource                MassiveDataSource
     (GBM simulator,                    (Polygon.io REST poller,
      default, no API key)               used when MASSIVE_API_KEY set)
               │                                 │
               └───────────────┬─────────────────┘
                                ▼
                          PriceCache
                    (thread-safe, in-memory,
                     single source of truth)
                                │
                ┌───────────────┼────────────────┐
                ▼               ▼                ▼
        SSE /api/stream/   Portfolio         Trade
           prices           valuation       execution
```

**Strategy pattern.** Both data sources implement the same `MarketDataSource` ABC. Nothing downstream — SSE, trade execution, portfolio valuation — knows or cares which one is active. They only ever talk to the `PriceCache`.

**Push, not pull.** Data sources write into the cache on their own schedule (simulator: every 500ms; Massive: every 15s by default). Readers (SSE, trading) always read the latest cached value instantly, regardless of how fast the underlying source updates.

**One env var decides everything.** `create_market_data_source()` inspects `MASSIVE_API_KEY` at startup and returns the right implementation. The rest of the app never branches on which source is active.

---

## 2. The Unified API

### 2.1 Data Model — `models.py`

`PriceUpdate` is the only object that leaves the market data layer. It is immutable, and its comparison fields (`change`, `change_percent`, `direction`) are computed properties — never stored — so they can never drift out of sync with `price`/`previous_price`.

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

Why `frozen=True, slots=True`: these objects are created multiple times per second per ticker and shared across async tasks. Immutability makes that safe with zero copying; slots keep the per-instance memory footprint small.

### 2.2 Price Cache — `cache.py`

The cache is the hub everything else plugs into. Producers (exactly one active data source at a time) write; consumers (SSE, trade execution, portfolio valuation) read.

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

**Why a version counter?** The SSE loop polls every ~500ms regardless of source cadence. Without a version number, it would re-serialize and re-send unchanged data every tick — wasteful when Massive only refreshes every 15s. Instead:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock`, not `asyncio.Lock`?** The Massive client's synchronous `get_snapshot_all()` runs inside `asyncio.to_thread()` — a real OS thread, which `asyncio.Lock` cannot protect against. A `threading.Lock` is safe from both a background thread and the async event loop.

### 2.3 Abstract Interface — `interface.py`

This is the unified API contract every price source must implement:

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

Five methods is the entire surface area. Any future third data source (e.g., a different vendor) only needs to implement this interface and the factory to plug it in — nothing else in the codebase changes.

### 2.4 Factory — `factory.py`

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

Usage:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 3. The Simulator

### 3.1 Seed Data — `seed_prices.py`

Pure constants — no logic. Shared by the simulator for initial prices, per-ticker volatility/drift, and correlation grouping.

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

Tickers added dynamically that aren't in `SEED_PRICES`/`TICKER_PARAMS` fall back to a random seed price between $50–$300 and `DEFAULT_PARAMS`.

### 3.2 GBM Math — `GBMSimulator`

Geometric Brownian Motion — the same log-normal price model that underlies Black-Scholes. Prices can never go negative (the update is multiplicative via `exp()`), and correlated moves across tickers are produced with a Cholesky decomposition of a sector-based correlation matrix.

$$S(t+dt) = S(t) \cdot \exp\left[(\mu - \tfrac{1}{2}\sigma^2)\,dt + \sigma\sqrt{dt}\,Z\right]$$

For 500ms ticks over a 252-day, 6.5-hour trading year: `dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8` — tiny enough to produce sub-cent moves per tick that accumulate naturally.

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

        # TSLA is in tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR
```

Random shock events give the dashboard visual drama: ~0.1% chance per tick per ticker of a 2–5% jump, so across 10 tickers at 2 ticks/sec, expect one roughly every 50 seconds.

### 3.3 Async Wrapper — `SimulatorDataSource`

`GBMSimulator` is pure math with no async or I/O concerns. `SimulatorDataSource` wraps it in an asyncio task loop and implements `MarketDataSource`:

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

Key behaviors:
- **Immediate seeding** — `start()` writes seed prices to the cache *before* the loop begins, so the very first SSE tick already has data (no blank-screen delay).
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`, so FastAPI lifespan teardown is clean.
- **Per-step exception isolation** — a bad tick is logged and the loop continues; it never takes down the feed.

---

## 4. The Massive API Client

### 4.1 Massive REST API Reference

- **Package**: `massive` (formerly `polygon-api-client` / Polygon.io) — `uv add massive`
- **Auth**: `RESTClient(api_key=...)`, sends `Authorization: Bearer <key>`
- **Rate limits**: Free tier 5 req/min → poll every 15s. Paid tiers → poll every 2–5s.

The endpoint FinAlly uses — one call fetches every watched ticker at once, which is what keeps polling within the free-tier rate limit:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change: {snap.day.change_percent}%")
    print(f"  Volume: {snap.day.volume}")
```

Fields FinAlly extracts from each snapshot:

| Field | Use |
|---|---|
| `last_trade.price` | Current price for trading and display |
| `last_trade.timestamp` | Unix **milliseconds** — divide by 1000 for the cache |

### 4.2 `MassiveDataSource` Implementation

The Massive client is synchronous, so every network call runs inside `asyncio.to_thread()` to avoid blocking the event loop.

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

### 4.3 Error Handling Philosophy

The poller is designed to degrade gracefully rather than crash the app:

| Error | Behavior |
|---|---|
| **401 Unauthorized** (bad key) | Logged as error. Poller keeps running and retries every cycle. |
| **429 Rate limited** | Logged as error. Retries on the next scheduled poll. |
| **Network timeout** | Logged as error. Retries automatically. |
| **Malformed snapshot** (missing `last_trade`) | That ticker is skipped with a warning; the rest of the batch still processes. |
| **All tickers fail** | Cache keeps its last-known prices — SSE streams stale-but-present data rather than nothing. |

---

## 5. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route holding a long-lived `text/event-stream` connection, pushing the entire price snapshot whenever `PriceCache.version` changes.

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

Wire format — one JSON object keyed by ticker per event:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Frontend consumption via the native `EventSource` API — no extra libraries needed:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
};
```

Why poll-and-push on a fixed interval rather than notify-on-write: it produces evenly-spaced updates, which matters because the frontend accumulates them into sparkline charts — irregular spacing would make those charts look jittery.

---

## 6. Wiring It Into FastAPI

The market data system starts and stops with the app via the `lifespan` context manager. This is the integration point for whoever builds `backend/app/main.py`:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router


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

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependencies for injecting market data into other route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source():
    return app.state.market_source
```

Other route handlers pull in the cache/source via FastAPI's dependency injection:

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
    source=Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)
```

---

## 7. Watchlist Coordination

When the watchlist changes (manual REST call or LLM tool call), the active data source must be told so it tracks the right tickers.

**Adding a ticker:**

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
        Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
        Massive:   appends to ticker list, appears on the next poll (up to 15s later)
  → 200 OK
```

**Removing a ticker — but keep tracking if a position is still open:**

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source=Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if there's no open position — otherwise
    # portfolio valuation would lose its price feed for held shares.
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 8. Testing Examples

The real suite lives in `backend/tests/market/` (73 tests, 84% coverage — see `planning/MARKET_DATA_SUMMARY.md`). Representative examples:

**GBM invariants** (`test_simulator.py`):

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


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
    assert sim._cholesky is not None  # Now 2 tickers, matrix exists
```

**Cache semantics** (`test_cache.py`):

```python
from app.market.cache import PriceCache


def test_direction_up():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00


def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

**End-to-end simulator source, with a fast interval for the test:**

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
async def test_add_and_remove_ticker():
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

**Massive client, mocked at the network boundary** (`test_massive.py`):

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

    mocks = [_make_snapshot("AAPL", 190.50, 1707580800000)]
    with patch.object(source, "_fetch_snapshots", return_value=mocks):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50


@pytest.mark.asyncio
async def test_api_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
        await source._poll_once()  # Should not raise

    assert cache.get_price("AAPL") is None
```

Run the suite:

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app
```

Live manual verification — a Rich terminal dashboard driven by the real simulator:

```bash
cd backend
uv run market_data_demo.py
```

---

## 9. Configuration Reference

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5`s | Time between simulator ticks |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0`s | Time between Massive API polls (free tier: keep ≥ 12s for the 5 req/min limit) |
| SSE push interval | `_generate_events()` | `0.5`s | Cache poll cadence inside the SSE generator |
| SSE retry directive | `_generate_events()` | `1000`ms | Browser `EventSource` auto-reconnect delay |

`pyproject.toml` requires `[tool.hatch.build.targets.wheel] packages = ["app"]` for `uv sync`/Docker builds to succeed — already present in `backend/pyproject.toml`.

### Public API (`backend/app/market/__init__.py`)

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

Everything downstream — trade execution, portfolio valuation, watchlist routes, the SSE endpoint — should import exclusively from `app.market`, never reach into submodules directly.
