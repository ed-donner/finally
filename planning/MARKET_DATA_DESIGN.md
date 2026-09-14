# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified `MarketDataSource` interface, the in-memory `PriceCache`, the GBM simulator, the Massive (Polygon.io) API client, the SSE streaming endpoint, and how the rest of the backend (still to be built) wires into all of it.

**Status note:** the market data layer itself (`backend/app/market/`, 8 modules) is already built, tested (73 tests, 84% coverage) and code-reviewed — see `planning/MARKET_DATA_SUMMARY.md`, `planning/MARKET_INTERFACE.md`, `planning/MARKET_SIMULATOR.md`, and `planning/MASSIVE_API.md` for the three-way split of that work. This document consolidates those into one implementation-ready reference, verified line-by-line against the current source in `backend/app/market/` as of this writing, and extends into the parts that don't exist yet: the FastAPI `main.py` lifecycle wiring, the portfolio/watchlist routes that consume the cache, and the database-side coordination. Sections 1–9 describe **built** code; sections 10–14 are **design** for the next slice of work.

This supersedes `planning/archive/MARKET_DATA_DESIGN.md`, an earlier pre-implementation draft. That draft differs from what actually shipped in a few places — it lazy-imports `massive` inside `start()`, carries a dead `DEFAULT_CORR` constant, and has `SimulatorDataSource.get_tickers()` reach into `GBMSimulator._tickers` directly — all fixed during the code review recorded in `planning/archive/MARKET_DATA_REVIEW.md`. The code below reflects the fixed, current state.

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Data Model — `models.py`](#2-data-model)
3. [Price Cache — `cache.py`](#3-price-cache)
4. [Abstract Interface — `interface.py`](#4-abstract-interface)
5. [Seed Prices & Ticker Parameters — `seed_prices.py`](#5-seed-prices--ticker-parameters)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client)
8. [Factory — `factory.py`](#8-factory)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI Lifecycle Integration (not yet built)](#10-fastapi-lifecycle-integration-not-yet-built)
11. [Watchlist Coordination (not yet built)](#11-watchlist-coordination-not-yet-built)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               # create_market_data_source, create_stream_router
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py              # MarketDataSource ABC
      seed_prices.py             # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py                # GBMSimulator + SimulatorDataSource
      massive_client.py            # MassiveDataSource
      factory.py                    # create_market_data_source()
      stream.py                      # SSE endpoint (FastAPI router)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each file has a single responsibility. `__init__.py` re-exports the public API so the rest of the backend imports from `app.market`, never from a submodule directly:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

---

## 2. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only object that leaves the market data layer — SSE streaming, portfolio valuation, and trade execution all work exclusively with this type.

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

Design decisions:

- **`frozen=True`** — price updates are immutable value objects; once created they never change, so they're safe to share across async tasks without copying.
- **`slots=True`** — a memory optimisation that matters here specifically because many of these are created per second.
- **Computed properties** (`change`, `change_percent`, `direction`) — derived from `price` and `previous_price` instead of stored, so there is exactly one source of truth and no risk of a stale `direction` field drifting out of sync.
- **`to_dict()`** — the single serialisation point used by both the SSE endpoint and any future REST responses that echo a price.

---

## 3. Price Cache

**File: `backend/app/market/cache.py`**

The central hub: both data sources write to it, and every reader (SSE stream, portfolio valuation, trade execution) reads from it. Thread-safety matters because `MassiveDataSource` runs its synchronous HTTP calls via `asyncio.to_thread`, so a write can arrive from a worker thread while the event loop thread is reading.

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

**Why a version counter.** The SSE loop polls the cache every ~500ms. Without a counter it would serialise and send every price on every tick, even when the source hasn't produced anything new (e.g. the Massive poller only updates every 15s). Comparing `version` between ticks lets the loop skip the send entirely when nothing changed:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock` and not `asyncio.Lock`.** `asyncio.Lock` only protects against other coroutines on the same event loop — it does nothing for a write arriving from a real OS thread. Since `MassiveDataSource` runs `get_snapshot_all()` via `asyncio.to_thread`, the lock has to be a real mutex. `threading.Lock` works correctly from both a sync worker thread and the async event loop thread.

**Known limitation** (carried over from the code review, `planning/archive/MARKET_DATA_REVIEW.md` §3.4): the `version` property getter reads `self._version` without acquiring the lock. Safe today under CPython's GIL (a single `int` read is atomic), but would need a lock if the project ever ran on a no-GIL build (PEP 703). Not worth fixing pre-emptively at this scale.

---

## 4. Abstract Interface

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

Two concrete implementations exist: `SimulatorDataSource` (§6.2) and `MassiveDataSource` (§7). Both write into the same `PriceCache`, so the SSE layer, trade execution, and portfolio valuation never need to know which one is active — this push model decouples timing: the simulator ticks at 500ms, Massive polls at 15s, but the SSE reader always reads from the cache at its own fixed cadence regardless of source.

---

## 5. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib. Shared by the simulator for initial prices, per-ticker GBM parameters, and the correlation structure.

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

A ticker added dynamically that isn't in `SEED_PRICES` starts at a random price drawn uniformly from `$50`–`$300`, and gets `DEFAULT_PARAMS` if it isn't in `TICKER_PARAMS` either — see `GBMSimulator._add_ticker_internal` in §6.

Note: the pre-implementation draft in `planning/archive/MARKET_DATA_DESIGN.md` also defined a `DEFAULT_CORR = 0.3` constant intended for unmatched ticker pairs, but `_pairwise_correlation` (§6) always falls through to `CROSS_GROUP_CORR` (also 0.3) for that case, making `DEFAULT_CORR` dead code. It was removed during the review rather than kept as an unused, confusingly-named duplicate.

---

## 6. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes live here: `GBMSimulator`, a synchronous math engine with no `asyncio` or cache dependency (easy to unit test in isolation), and `SimulatorDataSource`, the async `MarketDataSource` implementation that drives it on a timer and writes results into the shared `PriceCache`.

### 6.1 GBM Math

At each time step, a price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualised drift (expected return), e.g. `0.05`
- `sigma` — annualised volatility, e.g. `0.20`
- `dt` — time step as a fraction of a trading year
- `Z` — a (correlated) standard normal random draw

`dt` is derived from the actual tick interval and a trading-calendar assumption, not hand-tuned:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR    # ~8.48e-8, for 500ms ticks
```

This tiny `dt` produces sub-cent moves per tick that compound naturally into realistic intraday ranges over many ticks — no separate smoothing pass is needed. Prices can never go negative, since GBM is multiplicative (`exp()` is always positive) — no clamping logic required.

### 6.2 Correlated Moves

Real stocks don't move independently — tech names tend to move together. Correlated random draws come from **Cholesky decomposition** of a sector-based correlation matrix: given correlation matrix `C`, compute `L = cholesky(C)`, then for independent standard normals `Z_independent`, `Z_correlated = L @ Z_independent`.

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    if t1 == "TSLA" or t2 == "TSLA":     # checked before sector membership
        return TSLA_CORR
    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

TSLA is technically listed inside the `"tech"` set in `CORRELATION_GROUPS`, but the `TSLA` check runs first, so it always gets the weaker `TSLA_CORR` (0.3) pull rather than `INTRA_TECH_CORR` (0.6) — it moves more independently than AAPL/GOOGL/MSFT do relative to each other. The correlation matrix is guaranteed positive semi-definite for `np.linalg.cholesky` because it's built from this fixed, valid table of pairwise correlations — an ad hoc/user-editable matrix would need explicit validation that this fixed one doesn't.

### 6.3 Random Events

Every step, each ticker independently has a small chance of a sudden 2–5% move, for visual drama:

```python
event_probability = 0.001   # ~0.1% chance per tick per ticker

if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

At 2 ticks/second, ~0.1% per tick per ticker works out to roughly one event every ~500 seconds per ticker; across the 10-ticker default watchlist that's a visible shock somewhere roughly every ~50 seconds — often enough to keep a live dashboard interesting without making prices look erratic.

### 6.4 `GBMSimulator` Implementation

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
            self._add_ticker_internal(ticker)   # batch init, no Cholesky rebuild per ticker
        self._rebuild_cholesky()                 # ... built once at the end

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
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        """Public accessor — SimulatorDataSource uses this instead of reaching
        into a private attribute (see §6.5)."""
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky — used for batch init in __init__."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2) but n stays small (well under 50 tickers in practice)."""
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

Adding/removing a ticker mid-session rebuilds the Cholesky decomposition — O(n²), but n is small (well under 50 in practice), so it's cheap even on every watchlist edit.

### 6.5 `SimulatorDataSource` — Async Wrapper

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
        # Seed the cache with initial prices so SSE has data immediately —
        # no blank/loading state on the very first poll.
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
            price = self._sim.get_price(ticker)   # seed cache immediately, same reasoning
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
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")   # never let one bad step kill the loop
            await asyncio.sleep(self._interval)
```

Key behaviours: the cache is seeded *before* `_run_loop` starts, so the SSE endpoint has data on its first send; `stop()` cancels and awaits the task, swallowing `CancelledError`, for clean shutdown during FastAPI lifespan teardown; the loop catches exceptions per-step so one bad tick can't kill the whole feed.

---

## 7. Massive API Client

**File: `backend/app/market/massive_client.py`**

Massive (formerly Polygon.io — rebranded 2025-10-30, same API/keys) is used when `MASSIVE_API_KEY` is set. `MassiveDataSource` polls the batched snapshot endpoint on a timer; the synchronous `massive` client runs via `asyncio.to_thread` so a slow HTTP call never stalls the event loop (and therefore never stalls the SSE stream or trade execution for other users of the same process).

Unlike the pre-implementation draft, `massive` is imported at **module level**, not lazily inside `start()` — `massive>=1.0.0` is a required dependency in `backend/pyproject.toml`, not an optional one, so there's no import-time branching to reason about.

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
        await self._poll_once()          # immediate first poll — no cold-start delay
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
            snapshots = await asyncio.to_thread(self._fetch_snapshots)  # sync client off the event loop
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # swallow and retry next interval — 401/429/network errors must not kill the loop

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Key fields consumed

Per `planning/MASSIVE_API.md`, only three fields off the snapshot are used: `snap.ticker` (cache key), `snap.last_trade.price` (current price), `snap.last_trade.timestamp` (Unix **milliseconds**, converted to seconds before writing to `PriceCache`). `day.previous_close` / `todaysChangePerc` are available on the response but not consumed — `PriceCache` computes its own `change`/`change_percent` relative to whatever it had cached before, per §3.

### Error handling

| Error | Behaviour |
|---|---|
| **401 Unauthorized** | Logged, poller keeps running (user might fix `.env` and restart) |
| **429 Rate limit** | Logged, next poll retries after `poll_interval` |
| **Network timeout** | Logged, retries automatically next cycle |
| **Malformed individual snapshot** | That ticker skipped with a warning; the other nine keep updating |
| **All tickers fail** | Cache retains last-known prices; SSE keeps streaming stale data rather than nothing |

`add_ticker` / `remove_ticker` just mutate the in-memory ticker list — a newly added ticker's price appears on the *next* scheduled poll; there's no per-ticker on-demand fetch.

### Endpoints available but not used at runtime

`planning/MASSIVE_API.md` documents these for completeness — none are called by `massive_client.py` today:

- **Single ticker snapshot** (`client.get_snapshot_ticker(...)`) — could back a future per-ticker detail view.
- **Previous close** (`client.get_previous_close_agg(ticker)`) — the endpoint to use if seed prices are ever refreshed from real data instead of the hardcoded `seed_prices.py` table.
- **Aggregates/bars** (`client.list_aggs(...)`) — would back a historical chart feature if one is added.
- **Last trade / last quote** (`client.get_last_trade`, `client.get_last_quote`) — superseded by the batched snapshot call.
- **Unified/universal snapshot** (`GET /v3/snapshot?ticker.any_of=...`) — a newer cross-asset-class endpoint (stocks/options/forex/crypto in one call, up to 250 tickers). Worth revisiting only if FinAlly ever adds non-stock tickers to the watchlist; the current stocks-only snapshot endpoint is simpler and sufficient for this app.

---

## 8. Factory

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

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

Both branches import `MassiveDataSource` and `SimulatorDataSource` at module level in `factory.py` — again, `massive` is a required dependency, so there's no lazy-import branching here either.

---

## 9. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds a long-lived connection open and pushes price updates as `text/event-stream`, using `PriceCache.version` (§3) to send only when something actually changed.

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
    yield "retry: 1000\n\n"   # tells EventSource to auto-reconnect after 1s on drop

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

Client side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, ... }, ... }
};
```

### Why poll-and-push instead of event-driven

The generator polls the cache on a fixed interval rather than being woken by the data source directly. Simpler, and it produces predictable, evenly-spaced updates — the frontend accumulates these into sparkline charts (per `planning/PLAN.md` §10), and regular spacing keeps that visualisation clean regardless of which source (simulator vs. Massive) is actually producing the changes underneath.

---

## 10. FastAPI Lifecycle Integration (not yet built)

Nothing in `backend/app/` outside `app/market/` exists yet — there is no `main.py`, no routes, no database layer. This section is forward design for whoever wires the market data layer into the running app, following the `lifespan` context manager pattern from `planning/PLAN.md` §3/§7.

**In `backend/app/main.py`:**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # reads from SQLite; lazily initializes DB if needed
    await source.start(initial_tickers)

    stream_router = create_stream_router(price_cache)
    app.include_router(stream_router)

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

`load_watchlist_tickers()` is the one piece of the database layer this depends on — per `planning/PLAN.md` §7, it reads the `watchlist` table (lazily seeding the schema and the 10 default tickers on first run if the SQLite file doesn't exist yet).

### Accessing market data from other routes

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
    # ... validate cash/shares, write positions + trades rows at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)
    # ... return ticker + current price if already cached ...
```

---

## 11. Watchlist Coordination (not yet built)

When the watchlist changes — via the REST API or the LLM chat's `watchlist_changes` (per `planning/PLAN.md` §9) — the market data source must be told, so it tracks the right set of tickers.

### Adding a ticker

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list; price appears on the next poll
  → Return success (ticker + current price if already cached)
```

### Removing a ticker

```
DELETE /api/watchlist/PYPL
  → DELETE FROM watchlist (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive:   removes from ticker list, removes from cache
  → Return success
```

### Edge case: ticker still held as a position

If the user removes a ticker from the watchlist while still holding shares, the data source must keep tracking it so portfolio valuation (the heatmap, positions table, P&L) stays accurate even though it's no longer displayed in the watchlist panel:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)   # only stop tracking if nothing depends on the price

    return {"status": "ok"}
```

This mirrors `planning/PLAN.md` §7's rule that a `positions` row is deleted outright once `quantity` hits exactly 0 — so "no open position" here is just "no positions row for this ticker."

---

## 12. Testing Strategy

The market data layer already has 73 tests across 6 modules (`backend/tests/market/`), 84% overall coverage — see `planning/MARKET_DATA_SUMMARY.md` for the full breakdown. Representative examples, matching what's actually in the test suite:

### 12.1 `GBMSimulator` (`test_simulator.py`, 17 tests, 98% coverage)

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:
    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_add_ticker_rebuilds_correlation(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None          # 1 ticker, no correlation matrix needed
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None       # 2 tickers, matrix now exists

    def test_unknown_ticker_gets_random_seed_price(self):
        price = GBMSimulator(tickers=["ZZZZ"]).get_price("ZZZZ")
        assert 50.0 <= price <= 300.0
```

### 12.2 `PriceCache` (`test_cache.py`, 13 tests, 100% coverage)

```python
from app.market.cache import PriceCache


class TestPriceCache:
    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.previous_price == 190.50

    def test_direction_and_change(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_version_increments_per_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2
```

### 12.3 `SimulatorDataSource` integration (`test_simulator_source.py`, 10 tests)

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_seeds_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        assert cache.get("AAPL") is not None   # populated before the loop's first tick
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
```

### 12.4 `MassiveDataSource` (`test_massive.py`, 13 tests, 56% coverage — expected, API calls mocked)

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
class TestMassiveDataSource:
    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]

        with patch.object(source, "_fetch_snapshots", return_value=snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # must not raise

        assert cache.get_price("AAPL") is None
```

**Note on the `massive` package requirement.** `test_massive.py` mocks `source._fetch_snapshots` directly (a bound method on the instance), which works regardless of whether `massive` is installed for the three tests structured that way — but per `planning/archive/MARKET_DATA_REVIEW.md` §3.2, since `massive_client.py` now imports `RESTClient` at module level (§7), the whole test module requires the `massive` package to be importable to collect at all. That's expected and correct given `massive>=1.0.0` is a hard dependency in `pyproject.toml` — `uv sync` installs it, so `uv run pytest` runs cleanly.

### 12.5 Coverage gaps worth knowing about if extending this layer

- `stream.py` sits at 31% coverage — no dedicated SSE test exists yet. Testing it needs an ASGI test client (`httpx.AsyncClient(app=app, ...)`) driving a real `StreamingResponse`, which wasn't set up as part of the market-data-only slice of work. Worth adding once `main.py` exists.
- No concurrent-writer test for `PriceCache` — the lock usage looks correct by inspection, but nothing exercises multiple threads writing simultaneously.
- No test exercises `GBMSimulator` with the full 10-ticker default watchlist at once (tests use 1–2 tickers) — worth adding to catch any correlation-matrix edge case specific to that size.

---

## 13. Error Handling & Edge Cases

### 13.1 Startup with an empty watchlist

If the database has no watchlist rows (user deleted everything), `start([])` is called. Both sources handle this: the simulator's `step()` returns `{}` for zero tickers, the Massive poller's `_poll_once()` returns immediately (`if not self._tickers: return`). The SSE endpoint sends nothing until a ticker is added, at which point `add_ticker()` makes it appear immediately.

### 13.2 Price cache miss during a trade

A ticker just added to the watchlist may not have a cached price yet (simulator: seeded immediately, so this is really a Massive-only case — the gap between `add_ticker()` returning and the next scheduled poll).

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

### 13.3 Invalid `MASSIVE_API_KEY`

The first poll fails with 401. The poller logs and keeps retrying on `poll_interval`. SSE keeps streaming — empty, since the cache never got populated. Frontend shows the green "connected" status dot (the SSE connection itself is fine) but no prices. Fix is to correct `.env` and restart the container; there's no in-process key-reload.

### 13.4 Thread safety under load

`PriceCache`'s `threading.Lock` is a plain mutex — one thread at a time. At the project's actual scale (10 tickers, 2 updates/sec, one SSE consumer per browser tab) contention is negligible; the critical section is a dict lookup and assignment. A `ReadWriteLock` would only matter at hundreds of tickers and many concurrent readers — not warranted here.

### 13.5 Numerical precision

`round()` to 2 decimals happens once, at cache-write time (§3) and again inside `GBMSimulator.step()` before results leave the simulator — every downstream reader gets already-clean prices. The `exp(drift + diffusion)` formulation is numerically stable at the tiny `dt` values involved, and multiplicative GBM structurally can't produce a negative price.

---

## 14. Configuration Summary

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | Time between Massive API polls (free-tier-safe; lower on paid tiers) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random 2–5% shock event, per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step, as a fraction of a trading year |
| SSE push interval | `_generate_events()` | `0.5` (seconds) | How often the SSE loop checks `PriceCache.version` |
| SSE retry directive | `_generate_events()` | `1000` (ms) | Browser `EventSource` reconnection delay after a drop |

### `__init__.py` — public API surface

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
