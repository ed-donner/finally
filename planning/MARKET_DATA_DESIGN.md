# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

**Status:** This describes the subsystem **as implemented** in `backend/app/market/` (verified against source, 2026-07-03). It consolidates and supersedes the three standalone docs `planning/MARKET_INTERFACE.md`, `planning/MARKET_SIMULATOR.md`, and `planning/MASSIVE_API.md` into one implementation-ready reference; those documents remain available for narrower deep-dives (interface design rationale, GBM math derivation, and Massive REST API reference respectively). It also supersedes `planning/archive/MARKET_DATA_DESIGN.md`, a pre-implementation sketch that contained bugs fixed during implementation — see [Appendix: Corrections vs. the Archived Sketch](#appendix-corrections-vs-the-archived-sketch).

Everything below lives under `backend/app/market/`, backed by 73 passing tests in `backend/tests/market/` (84% coverage).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
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
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Summary](#16-configuration-summary)
17. [Appendix: Corrections vs. the Archived Sketch](#appendix-corrections-vs-the-archived-sketch)

---

## 1. Architecture Overview

```
                MarketDataSource (ABC)
                 start / stop / add_ticker / remove_ticker / get_tickers
                 │
      ┌──────────┴──────────┐
      │                     │
SimulatorDataSource    MassiveDataSource
 (GBM, default,         (Polygon.io REST poller,
  no API key needed)      used when MASSIVE_API_KEY set)
      │                     │
      └──────────┬──────────┘
                  ▼
            PriceCache
        (thread-safe, in-memory,
         single point of truth)
                  │
        ┌─────────┼─────────────┐
        ▼         ▼             ▼
  SSE stream   Portfolio    Trade execution
  /api/stream  valuation    (reads current price)
  /prices
```

**Strategy pattern**: both data sources implement the same `MarketDataSource` ABC. Everything downstream — SSE streaming, portfolio valuation, trade execution — is source-agnostic; it only ever talks to the `PriceCache`.

**Push model, not pull**: data sources write to the cache on their own schedule (500ms for the simulator, 15s for Massive free tier). The SSE layer polls the cache on its own fixed ~500ms cadence, independent of how often the underlying source actually updates. This decouples timing entirely — the SSE loop never needs to know which source is active.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Public API re-exports
      models.py            # PriceUpdate dataclass
      cache.py             # PriceCache (thread-safe in-memory store)
      interface.py         # MarketDataSource ABC
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
      simulator.py         # GBMSimulator (math) + SimulatorDataSource (async wrapper)
      massive_client.py    # MassiveDataSource
      factory.py           # create_market_data_source()
      stream.py            # create_stream_router() — SSE endpoint factory
  tests/
    market/
      test_models.py            # 11 tests, 100% coverage
      test_cache.py             # 13 tests, 100% coverage
      test_simulator.py         # 17 tests, 98% coverage
      test_simulator_source.py  # 10 tests (integration)
      test_factory.py           # 7 tests, 100% coverage
      test_massive.py           # 13 tests, 56% coverage (API methods mocked)
  market_data_demo.py    # Rich terminal live-dashboard demo
```

`simulator.py` holds both `GBMSimulator` (pure math, no I/O) and `SimulatorDataSource` (the async `MarketDataSource` wrapper) in one file — the wrapper is thin enough that it has no independent reason to live elsewhere. `seed_prices.py` holds only constant dictionaries/sets, no logic.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the **only** data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type (or its `.to_dict()` form).

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

- **`frozen=True`** — price updates are immutable value objects, safe to share across async tasks without defensive copying.
- **`slots=True`** — minor memory optimization; many of these are allocated per second.
- **Computed properties**, not stored fields — `change`, `change_percent`, and `direction` are derived from `price`/`previous_price` on access, so they can never drift out of sync with the underlying values.
- **`to_dict()`** — the single serialization point used by both the SSE endpoint and any future REST responses.

---

## 4. Price Cache — `cache.py`

The central data hub. Data sources write to it; SSE streaming, portfolio valuation, and trade execution read from it.

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

**Why a version counter?** The SSE streaming loop polls the cache every ~500ms. Without a version counter it would re-serialize and re-send all prices every tick even when nothing changed (e.g., while using Massive, which only updates every 15s). The counter lets the SSE loop skip redundant sends:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock`, not `asyncio.Lock`?** The Massive client's synchronous `get_snapshot_all()` call runs via `asyncio.to_thread()` — a real OS thread, which `asyncio.Lock` would not protect against. `threading.Lock` works correctly from both a sync thread and the async event loop, so it's the only choice that covers both writers.

**Note on `version`:** the property reads `self._version` without acquiring the lock. On CPython (GIL), a single `int` read is atomic, so this is safe today. It would need to move under the lock if this project ever ran on a no-GIL build (PEP 703 / Python 3.13t+) — not a concern for the current target of Python 3.12.

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

The interface deliberately does **not** return prices from any method — it only pushes updates into a shared cache on its own timing. This is what lets a 500ms simulator and a 15s Massive poller sit behind the identical contract.

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond stdlib. Shared by the simulator for initial prices, GBM parameters, and correlation structure.

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

Tickers added dynamically that aren't in `SEED_PRICES` start at `random.uniform(50.0, 300.0)` and use `DEFAULT_PARAMS` — see `GBMSimulator._add_ticker_internal` below.

---

## 7. GBM Simulator — `simulator.py`

Two classes live here: `GBMSimulator` (pure math engine, synchronous, no asyncio/I/O) and `SimulatorDataSource` (the `MarketDataSource` implementation that drives it on a timer and writes to the cache).

### 7.1 The Math

Prices evolve under **Geometric Brownian Motion** — the standard lognormal model underlying Black-Scholes:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` = current price
- `mu` = annualized drift (expected return)
- `sigma` = annualized volatility
- `dt` = time step as a fraction of a trading year
- `Z` = a (correlated) standard normal random draw

`dt` is derived from a trading-calendar assumption, not hardcoded:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800 (252 trading days, 6.5h/day)
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8, for 500ms ticks
```

This tiny `dt` produces small, sub-cent moves per tick that accumulate naturally into realistic intraday ranges — and since GBM is multiplicative (`exp()` is always positive), prices can never go negative.

### 7.2 Correlated Moves via Cholesky Decomposition

Real stocks don't move independently. Given a correlation matrix `C`, compute `L = cholesky(C)`; then for independent standard normals `Z_independent`, `Z_correlated = L @ Z_independent` produces draws with the desired pairwise correlations.

Correlation lookup checks TSLA **before** sector membership — this ordering matters:

```python
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

    # TSLA is in tech set but behaves independently
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR
```

`TSLA` is a member of `CORRELATION_GROUPS["tech"]` (so it still participates in the tech set for other purposes), but the design intent — "TSLA does its own thing, correlation ~0.3 with everything" — requires this TSLA check to run **before** the tech-membership check. If tech/finance membership were checked first, every TSLA/other-tech pair would match `t1 in tech and t2 in tech` and return `0.6`, making the TSLA branch unreachable dead code.

### 7.3 Random Shock Events

Every step, each ticker has a small probability of a sudden 2-5% move, for visual drama:

```python
if random.random() < self._event_prob:  # default 0.001
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

At 0.1% chance per tick per ticker, with 10 tickers at 2 ticks/sec, expect a visible event roughly every 50 seconds.

### 7.4 Full Implementation

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
    """Geometric Brownian Motion simulator for correlated stock prices."""

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
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)   # no Cholesky rebuild per-ticker
        self._rebuild_cholesky()                 # ...one rebuild after batch init

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Hot path — called every 500ms."""
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

            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign

            result[ticker] = round(self._prices[ticker], 2)
        return result

    def add_ticker(self, ticker: str) -> None:
        """Rebuilds the Cholesky matrix — O(n^2), but n stays small (<50 tickers)."""
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
        """Adds price/params state without rebuilding Cholesky — used for batch init."""
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
                corr[i, j] = corr[j, i] = rho
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

`__init__` batches ticker setup via the private `_add_ticker_internal` (no Cholesky rebuild per call), followed by a single `_rebuild_cholesky()` — avoiding `O(n)` redundant `O(n^2)` rebuilds when constructing with the full default 10-ticker watchlist. The public `add_ticker()` rebuilds on every call since tickers are added one at a time after startup, and `n` stays small enough (<50) that this is not a performance concern.

`step()` is kept allocation-light for its hot-path role: one `np.random.standard_normal(n)` call, one matrix-vector multiply, then a per-ticker loop with no further NumPy calls.

### 7.5 `SimulatorDataSource` — the Async Wrapper

`GBMSimulator` itself has no knowledge of `PriceCache` or asyncio. `SimulatorDataSource` owns the background task and writes results into the cache:

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
                logger.exception("Simulator step failed")  # loop survives a bad step
            await asyncio.sleep(self._interval)
```

**Key behaviors:**

- **Immediate seeding** — both `start()` and `add_ticker()` write an initial price into the cache synchronously, rather than waiting for the next scheduled tick. This matters for the frontend: a newly-added ticker shows a price immediately instead of a blank cell for up to 500ms.
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`, for clean shutdown during FastAPI lifespan teardown. Safe to call more than once.
- **Exception resilience** — `_run_loop` catches exceptions per-tick so one bad step doesn't kill the background task; it logs and continues on the next interval.
- **`get_tickers()` delegates to `GBMSimulator.get_tickers()`** — a public method, not a reach into a private attribute (`self._sim._tickers`), keeping the class boundary clean.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST API snapshot endpoint on a timer. `massive` is a core dependency (`pyproject.toml`), imported at module level — no lazy-import indirection.

### 8.1 Endpoint Used

**Snapshot — All Tickers**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT` — returns current data for multiple tickers in a **single API call**, which is essential for staying within the free tier's 5 req/min limit.

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="your_key_here")  # or reads MASSIVE_API_KEY from env

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
```

Relevant `TickerSnapshot` fields (from `massive/rest/models/snapshot.py`): `ticker`, `last_trade` (has `.price`, `.sip_timestamp` in **nanoseconds**), `day` / `prev_day` (OHLCV `Agg` objects), `last_quote`, `todays_change_percent`.

**Two gotchas verified against the real client:**
- There is no `day.previous_close` field — previous close lives on the sibling `prev_day.close`.
- `LastTrade` has **no `.timestamp` attribute** — only `sip_timestamp` / `participant_timestamp` / `trf_timestamp`, all in nanoseconds, not milliseconds. Getting this wrong silently breaks the price feed (see §8.4).

### 8.2 Full Implementation

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
                    # Massive trade timestamps (sip_timestamp) are Unix nanoseconds → convert to seconds
                    timestamp = snap.last_trade.sip_timestamp / 1_000_000_000
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e
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

### 8.3 Error Handling Philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** (bad key) | Logged as error via the outer `except Exception`. Poller keeps running; fixing `.env` requires a restart. |
| **429 Rate Limited** | Logged as error. Next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error. Retries automatically on the next cycle. |
| **Malformed snapshot for one ticker** | Caught by the inner `except (AttributeError, TypeError)`; that ticker is skipped with a warning, others still processed. |
| **All tickers fail** | Cache retains last-known prices. SSE keeps streaming stale data — better than no data. |

`_poll_once` deliberately nests two exception scopes: the inner one isolates a single bad snapshot so a malformed field on one ticker doesn't discard the whole batch; the outer one isolates the entire poll cycle (network failure, auth failure) so the background task survives indefinitely and simply retries.

### 8.4 The Timestamp Bug (Resolved) — Why the Inner `except` Is Load-Bearing

An earlier version of `_poll_once` read `snap.last_trade.timestamp / 1000.0` (assuming milliseconds). The real `LastTrade` model has no `.timestamp` attribute at all — only `sip_timestamp` (nanoseconds). Against the live API this raised `AttributeError` **inside the per-snapshot try/except**, which logged a warning and silently skipped the cache update for every ticker, on every poll. With a real `MASSIVE_API_KEY` configured, the price cache would never populate and the SSE stream would sit frozen — with no crash, no obvious error, just silence.

This shipped because `test_massive.py`'s mock built `last_trade` as a bare `MagicMock()`, which happily accepts a `.timestamp` attribute that doesn't exist on the real class — the test validated behavior against a shape the live API never returns.

**Fix applied**: read `snap.last_trade.sip_timestamp / 1_000_000_000` (nanoseconds → seconds). The test helper now builds `last_trade` as `MagicMock(spec_set=LastTrade)` using the real model class from `massive.rest.models.trades`:

```python
from massive.rest.models.trades import LastTrade

def _make_snapshot(ticker: str, price: float, sip_timestamp_ns: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock(spec_set=LastTrade)
    snap.last_trade.price = price
    snap.last_trade.sip_timestamp = sip_timestamp_ns
    return snap
```

`spec_set` rejects reading *or* setting any attribute not on the real class, so a future regression back to `.timestamp` fails the test suite immediately instead of silently mocking a field that doesn't exist. This is the general lesson for any test that mocks a third-party API response shape: spec the mock against the real model class, not a bare `MagicMock()`.

---

## 9. Factory — `factory.py`

Selects the data source at startup based on environment, with no lazy-import indirection (both implementations are imported at module level since `massive` is a core dependency).

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

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

---

## 10. SSE Streaming Endpoint — `stream.py`

A FastAPI route that holds open a long-lived HTTP connection and pushes price updates as `text/event-stream`.

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
    yield "retry: 1000\n\n"  # Tell the browser to retry after 1s if the connection drops

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

### Wire Format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Client side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, ... }, ... }
};
```

### Design Notes

- **Poll-and-push, not event-driven.** The generator polls the cache on a fixed interval rather than being notified by the data source. This produces predictable, evenly-spaced updates, which matters because the frontend accumulates these into sparkline charts — regular spacing keeps the visualization clean.
- **Version-gated emission vs. `planning/PLAN.md` §6.** The spec calls for the server to push at a fixed ~500ms cadence "regardless of the market data source," repeating the last-known price between Massive updates so the connection and frontend state stay consistent even when the underlying price hasn't changed for up to 15s. The implementation above is a deliberate refinement: it only emits an SSE `data:` event when `price_cache.version` has actually changed, rather than resending an identical payload every tick. The connection itself stays alive regardless (via the `while True` / `is_disconnected()` loop running every `interval`), and `EventSource`'s own connection handling doesn't care about event frequency — so the spec's intent (connection resilience, consistent frontend state) is preserved while avoiding wasted bandwidth on genuinely-unchanged prices. The tradeoff: a client relying on receiving an event every single tick (rather than every genuine change) would need to be aware prices only arrive when they move.
- **`request.is_disconnected()`** is checked every loop iteration so a closed client connection tears down the generator (and its logging) promptly rather than looping forever into the void.
- **`X-Accel-Buffering: no`** disables response buffering if this ever sits behind an nginx reverse proxy — otherwise nginx could buffer the stream and defeat real-time delivery.

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

The rest of the backend imports exclusively from `app.market`, never reaching into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 12. FastAPI Lifecycle Integration

The market data system does not yet have a `main.py` to integrate into — the rest of the backend (portfolio, watchlist, chat routes, `main.py`) is still to be built per `planning/PLAN.md`. This section is prescriptive guidance for whoever wires it up, using the `lifespan` context manager pattern.

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

    # 3. Load initial tickers from the database watchlist (lazy-init DB first)
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes access the cache and source via FastAPI dependency injection:

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
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... remove from watchlist table ...
    await source.remove_ticker(ticker)
```

---

## 13. Watchlist Coordination

When the watchlist changes (via REST API or LLM chat tool call), the market data source must be told so it tracks the right ticker set.

**Adding a ticker:**

```
POST /api/watchlist {ticker: "PYPL"}
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on next poll (up to 15s later)
  → Return success (ticker + current price if available)
```

**Removing a ticker:**

```
DELETE /api/watchlist/PYPL
  → Delete from watchlist table (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive:   removes from ticker list, removes from cache
  → Return success
```

**Edge case — ticker still has an open position.** If the user removes a ticker from the watchlist while still holding shares, the data source must keep tracking it so portfolio valuation stays accurate:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # only stop tracking if no open position

    return {"status": "ok"}
```

---

## 14. Testing Strategy

**73 tests, all passing, 84% overall coverage.** Six modules under `backend/tests/market/`:

| Module | Tests | Coverage | What it exercises |
|--------|-------|----------|--------------------|
| `test_models.py` | 11 | 100% | `PriceUpdate` computed properties, `to_dict()` |
| `test_cache.py` | 13 | 100% | `update`/`get`/`get_all`/`remove`, version counter, first-update flat direction |
| `test_simulator.py` | 17 | 98% | GBM math, correlation lookup, add/remove ticker, Cholesky rebuilds |
| `test_simulator_source.py` | 10 | (integration) | `SimulatorDataSource` lifecycle: seeding, start/stop idempotency, add/remove |
| `test_factory.py` | 7 | 100% | Env-var-driven source selection |
| `test_massive.py` | 13 | 56% (expected) | Polling, malformed-snapshot skip, error resilience, timestamp conversion |

`massive_client.py`'s 56% coverage is expected, not a gap to close blindly — its API-calling methods are exercised against mocks (`_fetch_snapshots` patched), not the real Massive service, so the actual HTTP call path stays untested by design.

### 14.1 `GBMSimulator` — representative tests

```python
class TestGBMSimulator:
    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None       # 1 ticker, no correlation matrix needed
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None   # 2 tickers, matrix now exists
```

### 14.2 `PriceCache` — representative tests

```python
class TestPriceCache:
    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
```

### 14.3 `SimulatorDataSource` — integration tests

```python
@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        # Cache has seed prices immediately — before the first loop tick
        assert cache.get("AAPL") is not None
        await source.stop()

    async def test_add_and_remove_ticker(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])
        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None
        await source.remove_ticker("TSLA")
        assert cache.get("TSLA") is None
        await source.stop()
```

### 14.4 `MassiveDataSource` — mocked tests

The mock discipline here is the load-bearing detail — see §8.4 for why:

```python
from massive.rest.models.trades import LastTrade

def _make_snapshot(ticker: str, price: float, sip_timestamp_ns: int) -> MagicMock:
    """last_trade is spec'd against the real LastTrade model so that reading or
    setting a nonexistent attribute (e.g. `.timestamp`) raises AttributeError
    here too, instead of silently mocking a fake field."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock(spec_set=LastTrade)
    snap.last_trade.price = price
    snap.last_trade.sip_timestamp = sip_timestamp_ns
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000000000),
            _make_snapshot("GOOGL", 175.25, 1707580800000000000),
        ]
        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50

    async def test_timestamp_conversion(self):
        """sip_timestamp (nanoseconds) is converted to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(
            source, "_fetch_snapshots",
            return_value=[_make_snapshot("AAPL", 190.50, 1707580800000000000)],
        ):
            await source._poll_once()

        assert cache.get("AAPL").timestamp == 1707580800.0

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()

        good = _make_snapshot("AAPL", 190.50, 1707580800000000000)
        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # triggers AttributeError, caught per-snapshot

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # must not raise

        assert cache.get_price("AAPL") is None
```

Note `_client = MagicMock()` is set directly on the instance in each test rather than patching `RESTClient` at module import time — since `RESTClient` is now a top-level import (not lazy), this keeps tests independent of whether the `massive` package needs live network access to construct a client.

### 14.5 Running the Suite

```bash
cd backend
uv run --extra dev pytest -v              # All tests
uv run --extra dev pytest --cov=app       # With coverage
uv run --extra dev ruff check app/ tests/ # Lint
```

### 14.6 Demo

A Rich terminal demo exercises the full stack end-to-end (simulator → cache → live dashboard), useful as a manual sanity check outside of pytest:

```bash
cd backend
uv run market_data_demo.py
```

Displays all 10 default tickers with sparklines, color-coded direction arrows, and an event log for notable price moves; runs 60 seconds or until Ctrl+C.

---

## 15. Error Handling & Edge Cases

### 15.1 Startup with an Empty Watchlist

If the database has no watchlist entries, `start()` receives an empty list. Both data sources handle this gracefully — `GBMSimulator.step()` returns `{}` for zero tickers, and `MassiveDataSource._poll_once()` returns immediately when `self._tickers` is empty. The SSE endpoint sends no events (`if prices:` guard). When a ticker is later added, the source begins tracking it immediately.

### 15.2 Price Cache Miss During Trade

If a user (or the LLM) tries to trade a ticker with no cached price yet (just added, Massive hasn't polled):

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this in practice by seeding the cache synchronously in `add_ticker()`. The Massive client may have a real gap of up to `poll_interval` seconds — the 400 with a clear message is the correct response rather than trading at a stale or fabricated price.

### 15.3 Massive API Key Invalid

If the key is set but wrong, the first poll fails with 401, caught by `_poll_once`'s outer `except Exception`. The poller logs the error and keeps retrying every `poll_interval` seconds — it does not stop or crash. The SSE endpoint keeps streaming (connected, but empty/stale data). The user sees a green connection indicator with no prices; the fix is correcting `MASSIVE_API_KEY` and restarting.

### 15.4 Thread Safety Under Load

`PriceCache` uses `threading.Lock`, a mutex — one thread at a time. Under normal load (10 tickers, 2 updates/sec from the simulator, or one `asyncio.to_thread` write per 15s from Massive), contention is negligible; the critical section is a dict lookup plus assignment. A `ReadWriteLock` would only matter at a scale (hundreds of tickers, many concurrent SSE readers) well beyond this project's scope.

### 15.5 Simulator Numerical Behavior

- Prices are rounded to 2 decimals in `GBMSimulator.step()`; the exponential formulation (`exp(drift + diffusion)`) is numerically stable and always positive.
- The correlation matrix is guaranteed positive semi-definite for `np.linalg.cholesky` to succeed, since all pairwise correlations are fixed positive constants (0.3–0.6) with a diagonal of 1.0 — comfortably within the valid PSD range for any `n`.
- Adding/removing a ticker mid-session triggers an `O(n^2)` Cholesky rebuild — not a concern while `n` stays under ~50 tickers.

---

## 16. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|--------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | Time between simulator ticks |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | Time between Massive API polls (free-tier safe) |
| SSE push interval | `_generate_events()` | `0.5` (seconds) | Time between cache-version checks / pushes |
| SSE retry directive | `_generate_events()` | `1000` (ms) | Browser `EventSource` reconnection delay |

---

## Appendix: Corrections vs. the Archived Sketch

`planning/archive/MARKET_DATA_DESIGN.md` was written before implementation began. A code review (`planning/archive/MARKET_DATA_REVIEW.md`) caught the following, all fixed in the shipped code reflected throughout this document:

1. **`pyproject.toml` build config** — the sketch never specified `[tool.hatch.build.targets.wheel] packages = ["app"]`; without it `uv sync` fails with "Unable to determine which files to ship inside the wheel." Added.
2. **Lazy imports of `massive` removed** — the sketch imported `from massive import RESTClient` inside `start()` to make the dependency optional. Since `massive` is a core `pyproject.toml` dependency regardless, this added indirection with no benefit and made tests fragile (patching a name that doesn't exist at module level without `RESTClient` already installed). Shipped code imports at module level.
3. **`_generate_events` return type** — the sketch annotated `-> None` on an async generator; shipped code uses `-> AsyncGenerator[str, None]`.
4. **`GBMSimulator.get_tickers()`** — the sketch had `SimulatorDataSource.get_tickers()` reach into the private `self._sim._tickers`. Shipped code adds a public `GBMSimulator.get_tickers()` method.
5. **`DEFAULT_CORR` removed** — the sketch defined an unused `DEFAULT_CORR = 0.3` alongside `CROSS_GROUP_CORR = 0.3`, which `_pairwise_correlation` actually returns for the fallback case. Same value, confusingly named twice; consolidated into `CROSS_GROUP_CORR` only.
6. **Test hygiene** — unused `pytest`/`math`/`asyncio` imports removed from four test files.
7. **The timestamp bug** — see §8.4 above; this was the one substantive logic bug, not just a sketch-vs-shipped discrepancy, and it's the main reason `test_massive.py` now specs its mocks against the real `LastTrade` class.

All 73 tests pass with these fixes applied; see `planning/MARKET_DATA_SUMMARY.md` for the full review and fix history.
