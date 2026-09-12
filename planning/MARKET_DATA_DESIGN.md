# Market Data Backend — Design

Implementation-ready design for the FinAlly market data subsystem: a unified interface behind which either a GBM price simulator or a real Massive (Polygon.io) REST poller can run, backed by a single thread-safe price cache and exposed to the frontend over SSE.

**Status:** This design is implemented in full at `backend/app/market/` (8 modules) with 73 passing tests. This document is the canonical reference — it reflects the code as built, including fixes from code review. See `planning/MARKET_DATA_SUMMARY.md` for a short status summary and `planning/archive/` for the original design-phase documents.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Reference](#8-massive-api-reference)
9. [Massive API Client — `massive_client.py`](#9-massive-api-client)
10. [Factory — `factory.py`](#10-factory)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint)
12. [Package Exports — `__init__.py`](#12-package-exports)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Testing Strategy](#15-testing-strategy)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Configuration Summary](#17-configuration-summary)

---

## 1. Architecture Overview

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, versioned)
        │
        ├──→ SSE stream endpoint  (GET /api/stream/prices)
        ├──→ Portfolio valuation  (reads price_cache.get_price(ticker))
        └──→ Trade execution      (reads price_cache.get_price(ticker) at fill time)
```

**Strategy pattern.** Both data sources implement the same `MarketDataSource` ABC. Neither returns prices directly to its caller — each pushes `PriceUpdate`s into a shared `PriceCache` on its own schedule (simulator: ~500ms; Massive: 15s). This decouples timing: the SSE layer always reads from the cache at its own fixed cadence regardless of which source is active or how often it actually writes.

**One cache, many readers.** `PriceCache` is the single point of truth. Producers (exactly one data source at a time) write; consumers (SSE stream, portfolio valuation, trade execution) read. No component needs to know which data source is active.

**Selection is environment-driven.** `create_market_data_source()` picks `MassiveDataSource` if `MASSIVE_API_KEY` is set and non-empty, otherwise `SimulatorDataSource`. All downstream code is source-agnostic.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               #             create_market_data_source, create_stream_router
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py              # MarketDataSource ABC
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py               # GBMSimulator + SimulatorDataSource
      massive_client.py          # MassiveDataSource
      factory.py                 # create_market_data_source()
      stream.py                  # SSE endpoint (FastAPI router factory)
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

Each module has a single responsibility. `app/market/__init__.py` re-exports the public API so the rest of the backend imports from `app.market` without reaching into submodules:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
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

- **`frozen=True`** — Price updates are immutable value objects. Once created they never change, so they're safe to hand to async tasks or SSE generators without defensive copying.
- **`slots=True`** — Minor memory optimization; the app creates several of these per tick.
- **Computed properties** (`change`, `change_percent`, `direction`) — Derived from `price`/`previous_price` so they can never drift out of sync with the underlying values. There is no stored `direction` field that could go stale.
- **`to_dict()`** — Single serialization point used by both the SSE endpoint and any future REST API responses.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Data sources write to it; SSE streaming, portfolio valuation, and trade execution read from it. It must be thread-safe because the price-generating loop and the SSE readers can be on different execution contexts.

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

The SSE loop polls the cache every ~500ms. Without a version counter it would re-serialize and re-send every price on every tick, even when nothing changed (e.g. Massive only updates every 15s). The counter lets the SSE loop skip a send when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock` (not `asyncio.Lock`) is used because:

- The Massive client's synchronous `get_snapshot_all()` runs via `asyncio.to_thread()`, which executes in a real OS thread — `asyncio.Lock` would not protect against that.
- `threading.Lock` works correctly whether the caller is a plain thread or a coroutine on the event loop.
- The critical sections are tiny (dict read/write), so lock contention is negligible at this scale (≲50 tickers, one writer, a handful of SSE readers).

---

## 5. Abstract Interface

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

This push model decouples timing. The simulator ticks every 500ms, Massive polls every 15s, but SSE always reads from the cache at its own 500ms cadence. The SSE layer never needs to know which data source is active or what its update interval is — it just asks the cache "did anything change since I last looked?"

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond the standard library. Shared by the simulator (initial prices and GBM parameters) and available as a fallback reference for any future consumer.

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

Tickers added dynamically that aren't in `SEED_PRICES`/`TICKER_PARAMS` fall back to a random seed price in `[50, 300]` and `DEFAULT_PARAMS` (`sigma=0.25`, `mu=0.05`) — see `GBMSimulator._add_ticker_internal` below.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes live in this module:

- **`GBMSimulator`** — pure math engine, stateful, holds current prices and advances them one step at a time.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation that wraps `GBMSimulator` in an async loop and writes results to the `PriceCache`.

### 7.1 The math

Geometric Brownian Motion is the standard model underlying Black-Scholes: prices evolve continuously with random noise, can never go negative, and reproduce the lognormal return distribution seen in real markets.

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

where `S(t)` is the current price, `mu` is annualized drift, `sigma` is annualized volatility, `dt` is the time step expressed as a fraction of a trading year, and `Z` is a (correlated) standard normal draw.

For 500ms ticks over a 252-day, 6.5-hour trading year:

```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally into realistic intraday ranges over time, and keeps the exponential formulation numerically stable (prices stay strictly positive).

### 7.2 Correlated moves via Cholesky decomposition

Real stocks don't move independently. Given a correlation matrix `C`, `L = cholesky(C)` transforms independent standard normals into correlated ones: `Z_correlated = L @ Z_independent`. The correlation structure used here:

| Pair | Correlation |
|---|---|
| Two tech tickers (`AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX`) | 0.6 |
| Two finance tickers (`JPM, V`) | 0.5 |
| Either ticker is `TSLA` | 0.3 (it does its own thing) |
| Cross-sector or unknown ticker | 0.3 |

The matrix is rebuilt (O(n²), fine for n < 50) whenever a ticker is added or removed.

### 7.3 Random shock events

Each tick, each ticker has a small independent chance (`event_probability`, default `0.001`) of a sudden 2–5% move in either direction — added purely for visual drama on the dashboard. With 10 tickers at 2 ticks/sec, expect roughly one shock every ~50 seconds.

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
        """Rebuild the Cholesky decomposition of the ticker correlation matrix."""
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

- **Immediate seeding.** `start()` populates the cache with seed prices *before* the loop begins, so the SSE endpoint has data to send on its very first poll — no blank-screen delay.
- **Graceful cancellation.** `stop()` cancels the task and awaits it, swallowing `CancelledError`. Safe to call multiple times (idempotent).
- **Exception resilience.** The loop catches exceptions per-step so one bad tick can't kill the whole feed.
- **Clean encapsulation.** `GBMSimulator.get_tickers()` is a public method — `SimulatorDataSource.get_tickers()` calls it rather than reaching into a private attribute.

---

## 8. Massive API Reference

Reference material for the Massive (formerly Polygon.io) REST API, as used by `massive_client.py`.

- **Python package:** `massive` (declared as a core dependency in `pyproject.toml`, so it's always installed — no optional/lazy-import dance needed)
- **Min Python:** 3.9+ (project runs 3.12)
- **Auth:** API key via `RESTClient(api_key=...)`, sent as `Authorization: Bearer <key>` by the client
- **Rate limits:** Free tier 5 req/min → poll every 15s (the default); paid tiers support 2–5s polling

### Primary endpoint: snapshot for all tickers

One API call returns current prices for every requested ticker — the reason polling can stay within the free-tier rate limit even with a full watchlist.

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
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
```

Key fields FinAlly extracts: `snap.ticker`, `snap.last_trade.price`, `snap.last_trade.timestamp` (Unix **milliseconds** — must be divided by 1000 before handing to `PriceCache.update`, which expects seconds).

### Other endpoints (not currently wired up, useful for future work)

| Endpoint | Client call | Use case |
|---|---|---|
| Single-ticker snapshot | `client.get_snapshot_ticker(market_type=..., ticker="AAPL")` | Detail view for one ticker (bid/ask, day range) |
| Previous close | `client.get_previous_close_agg(ticker="AAPL")` | Seeding a ticker's prior-day baseline |
| Aggregates (bars) | `client.list_aggs(ticker=..., multiplier=1, timespan="day", from_=..., to=...)` | Historical charting |
| Last trade / quote | `client.get_last_trade(ticker="AAPL")` / `get_last_quote(...)` | One-off spot checks |

### Error behavior

| Error | Meaning | Client behavior |
|---|---|---|
| 401 | Invalid API key | Exception raised — caught and logged by `_poll_once`, poller keeps retrying |
| 403 | Plan doesn't include this endpoint | Same as above |
| 429 | Rate limit exceeded | Same as above; next scheduled poll retries |
| 5xx | Server error | Client has built-in retry (3 attempts) before raising |

During market-closed hours `last_trade.price` reflects the last traded price (may include after-hours); the `day` object resets at market open and may lag during pre-market.

---

## 9. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the snapshot-all endpoint on a configurable interval. The synchronous `massive` client runs inside `asyncio.to_thread()` so it never blocks the event loop.

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
      - Paid tiers: higher limits → poll every 2-15s depending on tier
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

        # Immediate first poll so the cache has data right away
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
                    # Massive timestamps are Unix milliseconds -> convert to seconds
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

### Notes on this implementation

- **`massive` is a top-level import**, not a lazy one. Since `massive>=1.0.0` is a core dependency in `pyproject.toml` (not optional), the module-level import is simpler and — importantly — makes `unittest.mock.patch("app.market.massive_client.RESTClient")` work directly in tests without `create=True`.
- **Error handling is deliberately resilient**, not defensive-for-its-own-sake: a bad ticker's snapshot is skipped and logged (`AttributeError`/`TypeError`), a wholesale poll failure (network error, 401, 429) is logged and the loop simply tries again next interval. The cache retains its last-known values in the meantime — stale data displayed is preferable to the feed dying.
- **Ticker normalization** (`.upper().strip()`) happens in `add_ticker`/`remove_ticker` so watchlist input from the UI or LLM chat doesn't need to pre-sanitize ticker casing.

---

## 10. Factory

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

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real market data)
    - Otherwise -> SimulatorDataSource (GBM simulation)

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
await source.start(initial_tickers)  # e.g. ["AAPL", "GOOGL", ...]
```

---

## 11. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

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

    This factory pattern injects the PriceCache without module-level globals.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events shaped like:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}
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

    Sends all prices every `interval` seconds (only when the cache's version
    has changed). Stops when the client disconnects.
    """
    yield "retry: 1000\n\n"  # Browser auto-reconnects after 1s on drop

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
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Client-side (`EventSource` — no library needed, built into every browser):

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // { "AAPL": { ticker, price, previous_price, timestamp, change, change_percent, direction }, ... }
};
```

### Why poll-and-push instead of event-driven?

The generator polls the cache on a fixed interval rather than being notified by the data source. This is simpler and produces evenly-spaced updates, which matters because the frontend accumulates them into sparkline charts — regular spacing keeps those visualizations clean regardless of whether the underlying source ticks at 500ms (simulator) or 15s (Massive).

---

## 12. Package Exports

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

---

## 13. FastAPI Lifecycle Integration

The market data system starts and stops with the FastAPI app via the `lifespan` context manager.

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

    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
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

### Accessing market data from other routes

Trade execution, portfolio valuation, and watchlist management access the cache/source via FastAPI dependency injection:

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

## 14. Watchlist Coordination

When the watchlist changes (via REST API or LLM chat), the active market data source must be told so it tracks the right ticker set.

### Adding a ticker

```
User (or LLM) -> POST /api/watchlist {ticker: "PYPL"}
  -> INSERT INTO watchlist (SQLite)
  -> await source.add_ticker("PYPL")
       Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
       Massive:   appends to tracked list, appears on next poll (≤15s later)
  -> return success
```

### Removing a ticker

```
User (or LLM) -> DELETE /api/watchlist/PYPL
  -> DELETE FROM watchlist (SQLite)
  -> await source.remove_ticker("PYPL")
       Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
       Massive:   removes from tracked list, removes from cache
  -> return success
```

### Edge case: ticker still has an open position

If a user removes a ticker from the watchlist while still holding shares, the data source must keep tracking it so portfolio valuation stays accurate. The watchlist route — not the market data layer — is responsible for this check:

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

## 15. Testing Strategy

**73 tests across 6 modules in `backend/tests/market/`, all passing, 84% overall coverage.**

| Module | Tests | Coverage | Focus |
|---|---|---|---|
| `test_models.py` | 11 | 100% | `PriceUpdate` computed properties, `to_dict()` |
| `test_cache.py` | 13 | 100% | Update/get/remove, version counter, first-update-is-flat |
| `test_simulator.py` | 17 | 98% | GBM math correctness, add/remove ticker, Cholesky rebuild, edge cases |
| `test_simulator_source.py` | 10 | — (integration) | Async lifecycle: start/stop/add/remove against a real event loop |
| `test_factory.py` | 7 | 100% | Env-var-driven selection between simulator and Massive |
| `test_massive.py` | 13 | 56%† | Poll cycle with mocked `_fetch_snapshots`, malformed-snapshot handling, error resilience |

† Lower coverage here is expected — the real Massive HTTP calls aren't exercised; only `_poll_once`'s orchestration logic is, via mocks.

### Representative tests

**GBM invariants** (`test_simulator.py`):

```python
class TestGBMSimulator:
    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # single ticker, no correlation matrix
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None
```

**Cache semantics** (`test_cache.py`):

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

**Async lifecycle** (`test_simulator_source.py`):

```python
@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_populates_cache(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        assert cache.get("AAPL") is not None  # seeded before the loop's first tick
        await source.stop()
```

**Massive poll cycle, mocked** (`test_massive.py`):

```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        good = _make_snapshot("AAPL", 190.50, 1707580800000)
        bad = MagicMock(ticker="BAD", last_trade=None)  # triggers AttributeError

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None  # skipped, not crashed
```

### Running the suite

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app
uv run --extra dev ruff check app/ tests/
```

### What isn't unit-tested (by design)

- `stream.py` (SSE generator) has low direct coverage because exercising it meaningfully needs a running ASGI server. An `httpx.AsyncClient`-based integration test would be the natural next step if this layer grows more logic.
- Concurrent multi-thread writes to `PriceCache` aren't stress-tested; the `Lock` usage is straightforward enough that this is a reasonable place to trust code review over an explicit test.

---

## 16. Error Handling & Edge Cases

### Empty watchlist at startup

If the database has no watchlist entries, `start([])` is called. Both sources handle this gracefully — the simulator produces no prices, the Massive poller skips its API call entirely (`if not self._tickers: return`). The SSE endpoint simply sends nothing until a ticker is added, at which point `add_ticker()` starts tracking it immediately.

### Price cache miss during a trade

If a trade targets a ticker with no cached price yet (just added, Massive hasn't polled), the trade route should reject clearly rather than executing at a nonsensical price:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this entirely by seeding the cache synchronously inside `add_ticker()`. Massive has an unavoidable gap of up to `poll_interval` seconds — the 400 with a clear message is the correct behavior, not a bug to route around.

### Invalid Massive API key

A bad key causes every poll to fail with 401. `_poll_once` catches it, logs an error, and the poller keeps retrying every `poll_interval` — it does not crash or exit. The SSE connection itself stays healthy (so the header's connection-status dot shows green) even though no price data is flowing; the fix is to correct `.env` and restart the container.

### Thread safety under load

`PriceCache`'s `threading.Lock` is a plain mutex — one thread at a time. At this project's scale (≲50 tickers, one writer, a handful of SSE readers each polling twice a second) contention is negligible; the critical sections are a dict read or a dict write. If this ever became a bottleneck at a much larger scale, a read/write lock would be the fix — not needed here.

### Numerical stability of the simulator

- Prices are `round()`ed to 2 decimals in both `GBMSimulator.step()` and `PriceCache.update()`.
- The exponential formulation (`exp(drift + diffusion)`) is always positive — GBM prices can mathematically never go to zero or negative.
- The tiny `dt` (~8.5e-8) keeps per-tick multiplicative moves small and well-conditioned for floating point.

---

## 17. Configuration Summary

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` sec | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` sec | Time between Massive API polls (free-tier safe) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| SSE push interval | `_generate_events()` | `0.5` sec | How often the SSE loop checks the cache for a new version |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser `EventSource` reconnection delay |

### Demo

A Rich-based terminal dashboard exercises the whole stack end-to-end without needing a browser:

```bash
cd backend
uv run market_data_demo.py
```

Shows all 10 default tickers live, with sparklines, color-coded direction arrows, and an event log for notable moves. Runs 60 seconds or until Ctrl+C — useful for sanity-checking simulator tuning (volatility, correlation, shock frequency) before wiring up the frontend.
