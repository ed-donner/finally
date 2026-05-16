# Market Data Backend — Implementation Design

Complete implementation guide for the FinAlly market data subsystem. All code lives under `backend/app/market/`. Read this document top-to-bottom; each section builds on the previous one.

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
13. [Package `__init__.py`](#13-package-__init__py)
14. [Testing](#14-testing)
15. [Configuration Reference](#15-configuration-reference)
16. [Error Handling Reference](#16-error-handling-reference)

---

## 1. Architecture Overview

```
MarketDataSource (ABC)
├── SimulatorDataSource   →  GBM price simulation (default, no API key needed)
└── MassiveDataSource     →  Polygon.io REST polling (when MASSIVE_API_KEY set)
        │
        ▼  (both write to)
   PriceCache  (thread-safe in-memory store, single source of truth)
        │
        ├──→  GET /api/stream/prices  (SSE — frontend price flashing, sparklines)
        ├──→  GET /api/portfolio      (portfolio valuation using live prices)
        └──→  POST /api/portfolio/trade  (trade execution at current price)
```

**Key design decisions:**

| Decision | Rationale |
|---|---|
| Strategy pattern (ABC) | Both sources implement the same interface; all downstream code is source-agnostic |
| Push into cache, not pull from source | Decouples producer timing from consumer timing; SSE always reads at 500ms regardless of whether Massive polls every 2s or 15s |
| `threading.Lock` in cache | The Massive client's synchronous REST calls run via `asyncio.to_thread` (real OS thread); `asyncio.Lock` would not protect against that |
| Lazy `massive` import | The package is only imported when `MASSIVE_API_KEY` is set; simulator users don't need it installed |
| Immediate cache seeding | Both sources populate the cache before the loop starts so SSE sends real data on the first tick |

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Re-exports public API
      models.py           # PriceUpdate dataclass
      cache.py            # PriceCache (thread-safe)
      interface.py        # MarketDataSource ABC
      seed_prices.py      # Constants: SEED_PRICES, TICKER_PARAMS, correlation groups
      simulator.py        # GBMSimulator + SimulatorDataSource
      massive_client.py   # MassiveDataSource
      factory.py          # create_market_data_source()
      stream.py           # FastAPI SSE router
  tests/
    market/
      __init__.py
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

`PriceUpdate` is the only type that leaves the market data layer. All downstream code works exclusively with this.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at one point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from the previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update."""
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
- `frozen=True` — immutable value objects; safe to share across async tasks without copying
- `slots=True` — minor memory saving; we create many of these per second
- Computed properties (`change`, `direction`, `change_percent`) derive from the stored fields so they can never be inconsistent (no risk of a stale `direction` value)
- `to_dict()` is the single serialization point used by both the SSE endpoint and REST responses

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The cache is the central data hub. Data sources write to it; the SSE endpoint, portfolio valuation, and trade execution read from it. Must be thread-safe: the Massive client's synchronous REST calls run in `asyncio.to_thread` (a real OS thread).

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory store of the latest price per ticker.

    Writers: one SimulatorDataSource or MassiveDataSource at a time.
    Readers: SSE endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every write

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

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
        """Latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: the price float only, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all current prices. Safe to iterate after return."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Remove a ticker (e.g. when removed from the watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)
            self._version += 1

    @property
    def version(self) -> int:
        """Monotonically increasing counter. Read by the SSE loop for change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Why the version counter?**

The SSE loop runs at 500ms. Without versioning it would serialize and send all prices on every tick, even when nothing has changed (e.g. Massive API only updates every 15s). The version lets the SSE loop skip sends when there are no new writes:

```python
last_version = -1
while True:
    current_version = price_cache.version
    if current_version != last_version:
        last_version = current_version
        payload = price_cache.get_all()
        yield format_sse(payload)
    await asyncio.sleep(0.5)
```

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

Both `SimulatorDataSource` and `MassiveDataSource` implement this contract. All downstream code that needs to add/remove tickers receives a `MarketDataSource` — it never knows which implementation is active.

```python
from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code reads from the cache — it never calls the source
    directly for prices.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # app runs ...
        await source.add_ticker("PYPL")
        await source.remove_ticker("GOOGL")
        # app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task. Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), no further writes to cache.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Current list of actively tracked tickers."""
```

**Why sources write to the cache instead of returning prices:**

This push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but the SSE endpoint always reads the cache at its own 500ms cadence. The SSE layer doesn't need to know which data source is active or what its schedule is.

---

## 6. Seed Prices & Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports. Shared by the simulator (for initial prices and GBM parameters) and used as fallback prices when a new ticker is added.

```python
"""Seed prices and per-ticker GBM parameters for the market simulator."""

# Realistic starting prices for the default 10-ticker watchlist
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
# sigma: annualized volatility (higher = more per-tick price movement)
# mu:    annualized drift / expected return
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

# Default parameters for dynamically added tickers not in the list above
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups for Cholesky-correlated GBM moves
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Pairwise correlation coefficients
INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Cross-sector or unknown tickers
TSLA_CORR          = 0.3   # TSLA is in tech but does its own thing
```

**Volatility rationale:** TSLA at `sigma=0.50` produces roughly the right intraday range for a highly volatile stock. V at `sigma=0.17` reflects a stable, mature payments company. These are annualized; the tiny `dt` scales them to sub-cent moves per 500ms tick.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes in one file:
- `GBMSimulator` — pure math engine; stateful; advances prices step-by-step
- `SimulatorDataSource` — `MarketDataSource` implementation that wraps `GBMSimulator` in an async loop

### 7.1 GBM Math

At each time step, a stock price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

Where `Z` is a standard normal random variable (or a correlated draw — see §7.3).

The time step `dt` for 500ms ticks:
```
dt = 0.5 / (252 trading days * 6.5 hours/day * 3600 seconds/hour)
   = 0.5 / 5,896,800
   ≈ 8.48 × 10⁻⁸
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally into realistic intraday ranges.

**Why GBM?**
- Prices are always positive (exponential function)
- Log-normally distributed returns — matches real markets
- Two parameters (`mu`, `sigma`) are intuitive and tunable per ticker
- It's the model underlying Black-Scholes — students recognize it

### 7.2 Correlated Moves

Real stocks don't move independently — tech stocks move together. We use **Cholesky decomposition** to generate correlated draws from the correlation matrix `C`:

```python
L = cholesky(C)           # Lower triangular factor
Z_correlated = L @ Z_independent   # Z_independent ~ N(0,1)^n
```

The resulting `Z_correlated` has the desired pairwise correlations. This is O(n³) for the decomposition but n < 50 tickers and it only rebuilds when tickers are added or removed.

### 7.3 GBMSimulator Class

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
    """Geometric Brownian Motion price simulator for multiple correlated tickers."""

    # 500ms as a fraction of a trading year
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

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            # GBM update
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock event (~0.1% chance = roughly once per 500 seconds per ticker)
            # With 10 tickers at 2 ticks/sec: expect a visible event every ~50 seconds
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock
                logger.debug(
                    "Shock event on %s: %.1f%% %s",
                    ticker,
                    abs(shock) * 100,
                    "up" if shock > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Rebuilds the correlation matrix. No-op if already present."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the correlation matrix. No-op if not present."""
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
        """Current list of tracked tickers."""
        return list(self._tickers)

    # --- Internal ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky. Used during batch initialization."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild Cholesky decomposition of the correlation matrix.

        Called on every add/remove. O(n²) to build the matrix, O(n³) for
        Cholesky — negligible for n < 50.
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
        """Correlation coefficient between two tickers based on sector."""
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

### 7.4 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator.

    Runs an asyncio background task that calls GBMSimulator.step() every
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

        # Seed cache with initial prices immediately — SSE has data on the first tick
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("GBM simulator started with %d tickers", len(tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("GBM simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Core loop: step → write to cache → sleep. Exceptions are caught per-step."""
        while True:
            try:
                if self._sim:
                    prices = self._sim.step()
                    for ticker, price in prices.items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed — continuing")
            await asyncio.sleep(self._interval)
```

**Key behaviors:**
- **Immediate seeding** — cache is populated with seed prices in `start()` before the loop begins, so SSE has real data from tick one
- **Graceful cancellation** — `stop()` cancels and awaits the task, catching `CancelledError`; clean during FastAPI lifespan teardown
- **Exception resilience** — a single bad tick doesn't kill the data feed

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (formerly Polygon.io) snapshot endpoint for all watched tickers in a single API call. The synchronous Massive client runs in `asyncio.to_thread` to avoid blocking the event loop.

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

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call on a configurable interval.

    Rate limits:
      Free tier:  5 req/min  →  poll every 15s (default)
      Paid tiers: unlimited  →  poll every 2–5s
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
        self._client: Any = None  # Set in start() via lazy import

    async def start(self, tickers: list[str]) -> None:
        # Lazy import: only import when MASSIVE_API_KEY is set.
        # Keeps the massive package optional for simulator-only users.
        from massive import RESTClient  # type: ignore[import]

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll — cache has data before the loop's first sleep
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, poll every %.1fs",
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
            logger.info("Massive: added %s (appears on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. The first poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Fetch snapshots for all tickers and write to cache."""
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
                except (AttributeError, TypeError) as exc:
                    logger.warning(
                        "Skipping malformed snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        exc,
                    )
            logger.debug("Poll complete: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as exc:
            # Don't re-raise — retry on next interval.
            # Common failures: 401 bad key, 429 rate limit, network timeout.
            logger.error("Massive poll failed: %s", exc)

    def _fetch_snapshots(self) -> list:
        """Synchronous API call. Runs in a thread pool via asyncio.to_thread."""
        from massive.rest.models import SnapshotMarketType  # type: ignore[import]

        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Snapshot response structure

The Massive `get_snapshot_all()` call returns one object per ticker with this shape:

```python
snap.ticker                    # "AAPL"
snap.last_trade.price          # 190.50  (current price — what we use)
snap.last_trade.size           # 100
snap.last_trade.timestamp      # 1707580800000  (Unix milliseconds)
snap.day.open                  # 188.00
snap.day.high                  # 192.00
snap.day.low                   # 187.50
snap.day.close                 # 190.50
snap.day.volume                # 45_000_000
snap.day.previous_close        # 189.00
snap.day.change                # 1.50
snap.day.change_percent        # 0.79
snap.last_quote.bid_price      # 190.49
snap.last_quote.ask_price      # 190.51
```

We extract `last_trade.price` and `last_trade.timestamp`. Everything else is ignored for the live price feed (can be used for future detailed ticker views).

### Error handling table

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged as error. Poller keeps running (user can fix `.env` and restart). |
| 429 Rate Limited | Logged as error. Next poll retries after `poll_interval` seconds. |
| Network timeout | Logged as error. Retries automatically on next cycle. |
| Malformed snapshot | Individual ticker skipped with warning; other tickers still processed. |
| All tickers fail | Cache retains last-known prices; SSE keeps streaming stale-but-present data. |

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
    """Select the market data source based on environment variables.

    MASSIVE_API_KEY set and non-empty  →  MassiveDataSource (real market data)
    Otherwise                          →  SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource

        logger.info("Market data: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource

        logger.info("Market data: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

**Usage at startup:**

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A long-lived HTTP connection that pushes price updates to the browser using the `text/event-stream` format. The browser uses `EventSource` which handles reconnection automatically.

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
    """Factory that creates the SSE router with a reference to the price cache.

    Called once during FastAPI lifespan setup. Using a factory avoids module-level
    globals and makes the cache injection explicit.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint — streams all ticker prices every ~500ms.

        Client connects with:
            const es = new EventSource('/api/stream/prices');
            es.onmessage = (e) => { const prices = JSON.parse(e.data); };

        Each event payload:
            {
              "AAPL": {"ticker":"AAPL","price":190.50,"previous_price":190.42,
                       "change":0.08,"change_percent":0.042,"direction":"up",
                       "timestamp":1707580800.5},
              "GOOGL": { ... },
              ...
            }
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
    """Async generator that yields SSE-formatted strings.

    Sends all prices whenever the cache version changes. Stops when the client
    disconnects (detected via request.is_disconnected()).
    """
    # Tell the browser to wait 1s before reconnecting if the connection drops
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

### SSE wire format

The raw bytes the client receives for a single event:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

(Note: SSE events are terminated by a blank line `\n\n`.)

### Frontend JavaScript

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices: { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        updateTickerDisplay(ticker, update);
    }
};

eventSource.onerror = () => {
    // EventSource auto-reconnects after the retry interval (1000ms)
    setConnectionStatus('reconnecting');
};

eventSource.onopen = () => {
    setConnectionStatus('connected');
};
```

### Why poll-and-push instead of event-driven?

The SSE endpoint polls the cache on a fixed 500ms cadence rather than being notified when prices change. This is simpler and produces evenly spaced updates — important because the frontend accumulates SSE events into sparkline charts and uneven spacing would distort the visualization.

---

## 11. FastAPI Lifecycle Integration

**File: `backend/app/main.py`** (relevant excerpt)

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi import Depends

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.interface import MarketDataSource
from app.market.stream import create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---

    # 1. Shared price cache (single instance for the app's lifetime)
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Data source (simulator or Massive, based on env)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the database watchlist
    initial_tickers = await load_watchlist_tickers_from_db()
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # ← app is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency injection helpers used by other route files
def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Using market data in other route files

```python
# backend/app/routes/portfolio.py
from fastapi import APIRouter, Depends, HTTPException
from app.main import get_price_cache
from app.market.cache import PriceCache

router = APIRouter(prefix="/api/portfolio")

@router.post("/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(
            status_code=400,
            detail=f"No price available for {trade.ticker}. Please wait a moment.",
        )
    # ... execute trade at current_price ...
```

```python
# backend/app/routes/watchlist.py
from fastapi import APIRouter, Depends
from app.main import get_price_cache, get_market_source
from app.market.cache import PriceCache
from app.market.interface import MarketDataSource

router = APIRouter(prefix="/api/watchlist")

@router.post("")
async def add_ticker(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = payload.ticker.upper().strip()
    # 1. Persist to database
    await db.insert_watchlist_entry(ticker)
    # 2. Start tracking — data source seeds the cache immediately
    await source.add_ticker(ticker)
    # 3. Return with current price (available right away for the simulator)
    return {"ticker": ticker, "price": price_cache.get_price(ticker)}

@router.delete("/{ticker}")
async def remove_ticker(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper()
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if no open position (needed for portfolio valuation)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 12. Watchlist Coordination

### Adding a Ticker

```
User/LLM  →  POST /api/watchlist  {"ticker": "PYPL"}
             ├── INSERT INTO watchlist (user_id, ticker) VALUES ('default', 'PYPL')
             └── await source.add_ticker("PYPL")
                     Simulator:  adds to GBMSimulator, rebuilds Cholesky, seeds cache now
                     Massive:    appends to ticker list, appears on next poll (up to 15s later)
                 price_cache.get_price("PYPL")  →  immediate price for simulator, None briefly for Massive
             →  Return {"ticker": "PYPL", "price": 127.50}
```

### Removing a Ticker

```
User/LLM  →  DELETE /api/watchlist/PYPL
             ├── DELETE FROM watchlist WHERE ticker = 'PYPL'
             ├── Check open position — if none:
             │       await source.remove_ticker("PYPL")
             │           Simulator:  removes from GBMSimulator, removes from cache
             │           Massive:    removes from poll list, removes from cache
             └── Return {"status": "ok"}
```

### Edge Case: Position Still Open

If the user removes a ticker from the watchlist but still holds shares, keep it in the data source so portfolio valuation remains accurate. The route logic shown in §11 handles this.

---

## 13. Package `__init__.py`

**File: `backend/app/market/__init__.py`**

```python
"""Market data subsystem for FinAlly.

Public API — import from 'app.market', not from submodules:

    from app.market import PriceCache, create_market_data_source
    from app.market import PriceUpdate, MarketDataSource
    from app.market import create_stream_router
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

**Upstream usage example (in any backend module):**

```python
from app.market import PriceCache, create_market_data_source

cache = PriceCache()
source = create_market_data_source(cache)  # reads MASSIVE_API_KEY
await source.start(["AAPL", "GOOGL", "MSFT", ...])

# Read prices anywhere
update = cache.get("AAPL")           # PriceUpdate | None
price  = cache.get_price("AAPL")     # float | None
all_p  = cache.get_all()             # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("PYPL")
await source.remove_ticker("GOOGL")

# Shutdown
await source.stop()
```

---

## 14. Testing

### 14.1 Models

```python
# backend/tests/market/test_models.py
from app.market.models import PriceUpdate


class TestPriceUpdate:

    def test_direction_up(self):
        u = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
        assert u.direction == "up"
        assert u.change == 1.0
        assert u.change_percent > 0

    def test_direction_down(self):
        u = PriceUpdate(ticker="AAPL", price=189.0, previous_price=190.0)
        assert u.direction == "down"
        assert u.change == -1.0

    def test_direction_flat(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
        assert u.direction == "flat"
        assert u.change == 0.0

    def test_to_dict_keys(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=188.0)
        d = u.to_dict()
        assert set(d.keys()) == {
            "ticker", "price", "previous_price", "timestamp",
            "change", "change_percent", "direction",
        }

    def test_frozen(self):
        u = PriceUpdate(ticker="AAPL", price=190.0, previous_price=188.0)
        import pytest
        with pytest.raises(Exception):
            u.price = 200.0  # Should raise FrozenInstanceError
```

### 14.2 Cache

```python
# backend/tests/market/test_cache.py
from app.market.cache import PriceCache


class TestPriceCache:

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

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
        assert update.previous_price == 190.00

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_price_none_for_unknown(self):
        cache = PriceCache()
        assert cache.get_price("ZZZZ") is None

    def test_get_all_returns_copy(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        all_p = cache.get_all()
        all_p["FAKE"] = None  # Mutating the copy should not affect the cache
        assert "FAKE" not in cache.get_all()

    def test_version_increments_on_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_version_increments_on_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        v = cache.version
        cache.remove("AAPL")
        assert cache.version == v + 1

    def test_contains(self):
        cache = PriceCache()
        assert "AAPL" not in cache
        cache.update("AAPL", 190.00)
        assert "AAPL" in cache
```

### 14.3 GBM Simulator

```python
# backend/tests/market/test_simulator.py
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_always_positive(self):
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_unknown_ticker_gets_random_price_in_range(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert price is not None
        assert 50.0 <= price <= 300.0

    def test_empty_step(self):
        sim = GBMSimulator(tickers=[])
        assert sim.step() == {}

    def test_add_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert sim.get_tickers().count("AAPL") == 1

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_remove_nonexistent_is_noop(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_exists_for_two_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None

    def test_full_default_watchlist_no_error(self):
        """Cholesky decomposition succeeds for the full 10-ticker default set."""
        tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
        sim = GBMSimulator(tickers=tickers)
        result = sim.step()
        assert len(result) == 10
        assert all(p > 0 for p in result.values())

    def test_get_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        assert sorted(sim.get_tickers()) == ["AAPL", "GOOGL"]
```

### 14.4 SimulatorDataSource (Integration)

```python
# backend/tests/market/test_simulator_source.py
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_populates_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=10.0)
        await source.start(["AAPL", "GOOGL"])
        # Cache seeded before loop starts — no sleep needed
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Second stop should not raise

    async def test_add_ticker_seeds_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=10.0)
        await source.start(["AAPL"])
        await source.add_ticker("TSLA")
        assert cache.get("TSLA") is not None
        assert "TSLA" in source.get_tickers()
        await source.stop()

    async def test_remove_ticker_clears_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=10.0)
        await source.start(["AAPL", "TSLA"])
        await source.remove_ticker("TSLA")
        assert cache.get("TSLA") is None
        assert "TSLA" not in source.get_tickers()
        await source.stop()

    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])
        v_initial = cache.version
        await asyncio.sleep(0.3)  # ~6 update cycles
        assert cache.version > v_initial
        await source.stop()
```

### 14.5 MassiveDataSource (Mocked)

```python
# backend/tests/market/test_massive.py
from unittest.mock import AsyncMock, MagicMock, patch
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
        source._client = MagicMock()  # Skip lazy import
        source._tickers = ["AAPL", "GOOGL"]

        snapshots = [_make_snapshot("AAPL", 190.50), _make_snapshot("GOOGL", 175.25)]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_timestamp_converted_from_ms_to_seconds(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        snapshots = [_make_snapshot("AAPL", 190.50, ts_ms=1707580800000)]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update.timestamp == pytest.approx(1707580800.0, rel=1e-3)

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = ["AAPL", "BAD"]

        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None  # AttributeError when accessing .price

        snapshots = [_make_snapshot("AAPL", 190.50), bad_snap]
        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Must not raise

        assert cache.get_price("AAPL") is None

    async def test_add_ticker(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        await source.add_ticker("GOOGL")
        assert "GOOGL" in source.get_tickers()

    async def test_add_duplicate_is_noop(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = ["AAPL"]

        await source.add_ticker("AAPL")
        assert source.get_tickers().count("AAPL") == 1

    async def test_remove_ticker_clears_cache(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_empty_tickers_skips_poll(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._client = MagicMock()
        source._tickers = []

        # Should return without calling _fetch_snapshots
        fetch_mock = MagicMock()
        with patch.object(source, "_fetch_snapshots", fetch_mock):
            await source._poll_once()

        fetch_mock.assert_not_called()
```

### 14.6 Factory

```python
# backend/tests/market/test_factory.py
import os
from unittest.mock import patch
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


class TestFactory:

    def test_no_api_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MASSIVE_API_KEY", None)
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_empty_api_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_whitespace_only_api_key_returns_simulator(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "   "}):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_real_api_key_returns_massive(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "real-key-here"}):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveDataSource)
```

---

## 15. Configuration Reference

| Parameter | Where | Default | Description |
|-----------|-------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` | If set → Massive API; otherwise → Simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5s` | Time between GBM ticks |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock per ticker per tick |
| `dt` | `GBMSimulator.DEFAULT_DT` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0s` | Time between Massive API calls |
| SSE push interval | `_generate_events()` | `0.5s` | Cadence of SSE sends to the browser |
| SSE retry directive | `_generate_events()` | `1000ms` | Browser auto-reconnect delay |

---

## 16. Error Handling Reference

### Price not available for a ticker (Massive latency)

The simulator seeds the cache in `add_ticker()`, so prices are available immediately. The Massive client's new ticker appears only on the next poll (up to 15s later). Handle the gap at the trade endpoint:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment.",
    )
```

### Empty watchlist at startup

If the database watchlist is empty, `start()` receives `[]`. Both sources handle this gracefully — the simulator produces no prices, the Massive poller skips its API call. When the user adds a ticker, the source starts tracking it immediately via `add_ticker()`.

### Massive API key invalid

The first poll fails with a 401. The error is logged and the poller keeps retrying. The SSE endpoint streams an empty payload or last-known prices if any were cached before the key rotated. The fix is correcting `.env` and restarting the container.

### Cholesky decomposition failure

The correlation matrix must be positive semi-definite for Cholesky to succeed. The chosen correlation values (all between 0.3 and 0.6) guarantee this for any subset of tickers. Unknown/dynamic tickers all get `CROSS_GROUP_CORR = 0.3` — safely positive semi-definite. If a custom `_pairwise_correlation` implementation ever produces an invalid matrix, `numpy.linalg.cholesky` raises `LinAlgError`; catch it in `_rebuild_cholesky` and fall back to `self._cholesky = None` (uncorrelated moves).
