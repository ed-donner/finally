# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. It covers the
unified data-source interface, the in-memory price cache, the GBM simulator, the
Massive (Polygon.io) REST client, the SSE streaming endpoint, and how the whole
thing plugs into the FastAPI application lifecycle.

Everything described here lives under `backend/app/market/`. The design is
source-agnostic by construction: the simulator and the Massive client are
interchangeable implementations of a single abstract base class, and every
downstream consumer (SSE, portfolio valuation, trade execution) talks only to a
shared `PriceCache` — never to a data source directly.

---

## Table of Contents

1. [Design Goals & Principles](#1-design-goals--principles)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [File Structure](#3-file-structure)
4. [Data Model — `models.py`](#4-data-model--modelspy)
5. [Price Cache — `cache.py`](#5-price-cache--cachepy)
6. [Abstract Interface — `interface.py`](#6-abstract-interface--interfacepy)
7. [Seed Prices & Ticker Parameters — `seed_prices.py`](#7-seed-prices--ticker-parameters--seed_pricespy)
8. [GBM Simulator — `simulator.py`](#8-gbm-simulator--simulatorpy)
9. [Massive API Client — `massive_client.py`](#9-massive-api-client--massive_clientpy)
10. [Factory — `factory.py`](#10-factory--factorypy)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint--streampy)
12. [Package Exports — `__init__.py`](#12-package-exports--__init__py)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Massive API Reference Notes](#15-massive-api-reference-notes)
16. [Testing Strategy](#16-testing-strategy)
17. [Error Handling & Edge Cases](#17-error-handling--edge-cases)
18. [Configuration Summary](#18-configuration-summary)

---

## 1. Design Goals & Principles

| Goal | How the design achieves it |
|------|----------------------------|
| **Source interchangeability** | Both data sources implement one ABC (`MarketDataSource`). The factory picks one at startup based on `MASSIVE_API_KEY`. Nothing downstream knows which is active. |
| **Decoupled timing** | Data sources *push* into a `PriceCache` on their own cadence (sim: 500 ms, Massive: 15 s). The SSE endpoint *pulls* from the cache on its own cadence (500 ms). Neither knows the other's schedule. |
| **Single source of truth** | The `PriceCache` is the one place current prices live. Trade execution and portfolio valuation read the same cache the SSE stream does. |
| **Thread safety** | The Massive client's synchronous SDK runs in a worker thread via `asyncio.to_thread`; the cache uses a `threading.Lock` so it is safe from both threads and the event loop. |
| **Resilience** | Every background loop wraps its body in `try/except` so one bad tick or one failed poll never kills the feed. Stale-but-present data beats a blank screen. |
| **Zero-config default** | With no API key, the GBM simulator runs entirely in-process — no network, no external service, realistic-looking prices out of the box. |

The overarching pattern is **producer / shared-buffer / consumer**: one producer
(the active data source) writes to a shared buffer (the cache), and multiple
consumers (SSE clients, REST handlers) read from it.

---

## 2. Architecture at a Glance

```
                 ┌──────────────────────────────┐
                 │  create_market_data_source()  │  reads MASSIVE_API_KEY
                 └───────────────┬──────────────┘
                                 │ selects one
         ┌───────────────────────┴───────────────────────┐
         ▼                                                ▼
┌────────────────────┐                        ┌────────────────────────┐
│ SimulatorDataSource│                        │   MassiveDataSource     │
│  (GBMSimulator)    │                        │  (Polygon REST poller)  │
│  ticks every 500ms │                        │  polls every 15s        │
└─────────┬──────────┘                        └───────────┬────────────┘
          │  writes                                        │  writes
          └────────────────────┬───────────────────────────┘
                               ▼
                     ┌───────────────────┐
                     │    PriceCache      │  thread-safe, in-memory
                     │  {ticker: Update}  │  + monotonic version counter
                     └─────────┬─────────┘
             reads             │ reads              reads
        ┌──────────────────────┼──────────────────────────┐
        ▼                      ▼                           ▼
┌────────────────┐   ┌──────────────────┐        ┌──────────────────┐
│ SSE /api/stream │   │ Trade execution  │        │ Portfolio value  │
│    /prices      │   │ (current price)  │        │  (mark-to-market)│
└────────────────┘   └──────────────────┘        └──────────────────┘
```

---

## 3. File Structure

```
backend/
  app/
    market/
      __init__.py          # Public API re-exports
      models.py            # PriceUpdate dataclass
      cache.py             # PriceCache (thread-safe, version-counted)
      interface.py         # MarketDataSource ABC
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource
      factory.py           # create_market_data_source()
      stream.py            # create_stream_router() — SSE endpoint
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_massive.py
      test_factory.py
  market_data_demo.py      # Rich terminal dashboard (manual demo)
```

Each module has a single responsibility. `__init__.py` re-exports the public
surface so the rest of the backend imports from `app.market` and never reaches
into submodules.

---

## 4. Data Model — `models.py`

`PriceUpdate` is the only type that crosses the market-data boundary. SSE
serialization, trade execution, and portfolio valuation all work exclusively
with it.

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

- **`frozen=True`** — Price updates are immutable value objects. Once created
  they never change, which makes them safe to share across async tasks and
  threads without defensive copying.
- **`slots=True`** — Memory micro-optimization; many of these are created per
  second (10 tickers × 2 ticks/s).
- **Computed properties** (`change`, `change_percent`, `direction`) — Derived
  from `price`/`previous_price` so they can never drift out of sync. There is no
  way to store a stale `direction`.
- **`to_dict()`** — A single serialization point shared by the SSE endpoint and
  any REST responses that surface prices.

> **Note on `change` semantics.** `previous_price` is the *last tick's* price,
> so `change`/`change_percent` here are tick-over-tick deltas. The *day* change
> percentage shown in the watchlist UI is a separate concern the frontend
> computes (or a future field sourced from `day.previous_close` when running on
> real Massive data — see §15).

---

## 5. Price Cache — `cache.py`

The central data hub. Data sources write; SSE, trade execution, and portfolio
valuation read. It is thread-safe because the Massive poller and any offloaded
simulator work run in real OS threads while SSE reads happen on the event loop.

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
        If this is the first update for the ticker, previous_price == price
        (direction='flat').
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

The SSE loop polls the cache every ~500 ms. Without a change signal it would
re-serialize and re-send every price on every tick even when nothing changed
(e.g. Massive only updates once per 15 s). The monotonic `version` lets the SSE
loop skip sends when there's nothing new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread-safety rationale

`threading.Lock` (not `asyncio.Lock`) because:

- The Massive SDK's synchronous `get_snapshot_all()` runs inside
  `asyncio.to_thread()`, i.e. in a real worker thread. An `asyncio.Lock` would
  not guard against that.
- `threading.Lock` is correct when acquired from both worker threads and the
  event-loop thread; the critical sections here (a dict lookup + assignment) are
  tiny, so contention is negligible even under load.

---

## 6. Abstract Interface — `interface.py`

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

This push model is what decouples timing. The simulator ticks at 500 ms, Massive
polls at 15 s, but the SSE layer always reads the cache at its own 500 ms
cadence and never needs to know which source is active or how often it updates.
It also generalizes cleanly to future multi-user scenarios: many readers, one
shared buffer, unchanged data layer.

---

## 7. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond stdlib types. Shared by the
simulator (initial prices, GBM params, correlation structure). Could also serve
as fallback prices for the Massive client before its first poll returns.

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

`CROSS_GROUP_CORR` doubles as the correlation for unknown/dynamically-added
tickers, so there is no separate `DEFAULT_CORR` constant to keep in sync.

---

## 8. GBM Simulator — `simulator.py`

Two classes:

- **`GBMSimulator`** — the pure math engine. Stateful; holds current prices and
  advances them one step at a time. No async, no cache — trivially unit-testable.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation that wraps the
  engine in an async loop and writes results to the `PriceCache`.

### 8.1 `GBMSimulator` — the math engine

Geometric Brownian Motion is the standard model for stock-price simulation:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

where `μ` is annualized drift, `σ` is annualized volatility, `dt` is the time
step as a fraction of a trading year, and `Z` is a (correlated) standard normal
draw. Because the update is multiplicative through `exp()`, prices can never go
negative.

To make sectors move together, the per-ticker `Z` values are correlated via the
**Cholesky decomposition** of a sector-based correlation matrix: draw `n`
independent normals, multiply by the lower-triangular Cholesky factor `L`, and
the result has the target correlation structure.

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

        # Generate n independent standard normal draws, then correlate them
        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM step
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker → a 2-5% jolt.
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds.
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
        """Rebuild the Cholesky decomposition of the correlation matrix.

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
        """Correlation between two tickers based on sector grouping.

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

**On `dt`.** A 500 ms tick is a minuscule fraction of a trading year
(`0.5 / 5,896,800 ≈ 8.5e-8`), so the deterministic drift term is essentially
zero per tick and the diffusion term produces sub-cent random walks that
accumulate into believable intraday movement over minutes. Volatility, not
drift, dominates what the user sees — exactly as in real markets on short
horizons.

**On the correlation matrix.** Sector correlations of 0.6/0.5 are positive and
the matrix is diagonally dominant, so it is positive-definite and
`np.linalg.cholesky` succeeds. With ≤ 1 ticker there is no matrix
(`_cholesky = None`) and `step()` uses the independent draws directly.

### 8.2 `SimulatorDataSource` — async wrapper

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
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

### Key behaviors

- **Immediate seeding** — `start()` (and `add_ticker()`) populate the cache with
  the current price *before* the loop needs it, so the SSE endpoint and trade
  execution have data with no blank-screen delay or "price not available" race.
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing
  `CancelledError`, so FastAPI lifespan teardown is clean.
- **Exception resilience** — the loop catches per-step exceptions so one bad tick
  never kills the feed.

---

## 9. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST snapshot endpoint for the union of
watched tickers in a single call, then writes results to the `PriceCache`. The
`massive` SDK is synchronous, so each call runs in a worker thread via
`asyncio.to_thread()` to avoid blocking the event loop.

`massive` is a declared project dependency (see `pyproject.toml`), so its imports
live at module top level — there is no lazy-import dance.

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
            # Don't re-raise — the loop retries on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Error-handling philosophy

The poller is intentionally forgiving — a live financial feed should degrade, not
crash:

| Failure | Behavior |
|---------|----------|
| **401 Unauthorized** (bad key) | Logged as error; poller keeps running so a corrected `.env` + restart recovers. |
| **429 Rate limited** | Logged; next poll retries after `poll_interval`. Default 15 s keeps free tier (5 req/min) safe. |
| **Network timeout / 5xx** | Logged; retries next cycle. The SDK also has built-in retries. |
| **Malformed single snapshot** | That ticker is skipped with a warning; the rest still process (per-item `try/except`). |
| **Entire poll fails** | Cache retains last-known prices; SSE keeps streaming slightly stale data. |

### Two-level try/except

The inner `try` guards per-snapshot parsing so one bad record doesn't abort the
batch; the outer `try` guards the whole network call so a transient failure just
waits for the next interval. Together they guarantee the loop is effectively
un-killable short of cancellation.

---

## 10. Factory — `factory.py`

Selects the implementation at startup from a single environment variable.

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

`.strip()` means a whitespace-only key falls back to the simulator — a friendly
default for a half-filled `.env`. The factory returns an *unstarted* source; the
caller owns the `start(...)` call so the initial ticker set can be loaded from
the database first.

---

## 11. SSE Streaming Endpoint — `stream.py`

A FastAPI route holding a long-lived `text/event-stream` connection, pushing all
cached prices to the browser's native `EventSource`.

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

    Sends all prices every `interval` seconds, but only when something changed
    (version check). Stops when the client disconnects.
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
                    payload = json.dumps(data)
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### SSE wire format

Each event is a full snapshot of all known tickers, keyed by symbol:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12, ...}}

```

Client consumption:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);   // { AAPL: {ticker, price, direction, ...}, ... }
    for (const [ticker, u] of Object.entries(prices)) {
        applyPriceFlash(ticker, u.direction);   // brief green/red highlight
        appendSparklinePoint(ticker, u.price);  // accumulate since page load
    }
};
```

### Design notes

- **Full snapshot per event, not deltas** — With ~10 tickers the payload is tiny
  (< 2 KB) and a self-contained snapshot means a client that misses an event
  self-heals on the next one. No client-side reconciliation logic needed.
- **`retry: 1000`** — Instructs `EventSource` to reconnect 1 s after a drop.
  Reconnection is otherwise fully automatic in the browser.
- **`X-Accel-Buffering: no`** — Prevents nginx (if the app is ever reverse-proxied)
  from buffering the stream and defeating real-time delivery.
- **Disconnect detection** — `request.is_disconnected()` ends the generator when
  the browser goes away, so tasks don't leak per abandoned connection.
- **Poll-and-push, not event-driven** — The endpoint samples the cache on a fixed
  cadence rather than subscribing to source callbacks. This produces evenly
  spaced updates, which makes for clean frontend sparklines, and keeps the SSE
  layer ignorant of the data source entirely.

---

## 12. Package Exports — `__init__.py`

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate               - Immutable price snapshot dataclass
    PriceCache                - Thread-safe in-memory price store
    MarketDataSource          - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router      - FastAPI router factory for SSE endpoint
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

Downstream code imports only these names:

```python
from app.market import (
    PriceCache,
    PriceUpdate,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## 13. FastAPI Lifecycle Integration

The subsystem starts and stops with the app via the `lifespan` context manager.
The shared cache and source are stashed on `app.state` and handed to routes
through dependency injection.

**In `backend/app/main.py`:**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import (
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---
    # 1. Shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create the market data source (simulator or Massive, per env)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load the initial ticker set from the DB watchlist, then start
    initial_tickers = await load_watchlist_tickers()  # reads SQLite
    await source.start(initial_tickers)

    # 4. Mount the SSE router (bound to this cache)
    app.include_router(create_stream_router(price_cache))

    yield  # ---- app is running ----

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# --- Dependency-injection accessors ---
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Consuming market data from other routes

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
        raise HTTPException(400, f"Price not yet available for {trade.ticker}.")
    # ... fill at current_price, update positions/cash, snapshot portfolio ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)  # start tracking + seed the cache


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from watchlist table ...
    await source.remove_ticker(ticker)  # (subject to the open-position guard, §14)
```

---

## 14. Watchlist Coordination

When the watchlist changes — via REST or the LLM chat tool — the data source must
be told so it tracks the right set of tickers.

### Adding a ticker

```
User / LLM → POST /api/watchlist {ticker: "PYPL"}
  → INSERT into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
        Simulator: add to GBMSimulator, rebuild Cholesky, seed cache immediately
        Massive:   append to poll set, appears on next poll cycle
  → 200 (ticker + current price if already cached)
```

### Removing a ticker

```
User / LLM → DELETE /api/watchlist/PYPL
  → DELETE from watchlist table (SQLite)
  → await source.remove_ticker("PYPL")
        Simulator: drop from GBMSimulator, rebuild Cholesky, remove from cache
        Massive:   drop from poll set, remove from cache
  → 200
```

### Edge case: removing a ticker you still hold

If the user removes a ticker from the watchlist but still holds shares, keep
tracking it so portfolio valuation stays correct. The delete handler guards on
open positions:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)   # only stop tracking if no exposure

    return {"status": "ok"}
```

This keeps the tracked set equal to `watchlist ∪ {tickers with open positions}`,
which is exactly the set of prices any part of the app might need.

---

## 15. Massive API Reference Notes

Reference details for the `massive` SDK, relevant to `massive_client.py`.

- **Package**: `massive` (`uv add massive`), Python 3.9+. Auth via
  `MASSIVE_API_KEY` env var or `RESTClient(api_key=...)`; the client sets the
  `Authorization: Bearer <key>` header itself.
- **Primary endpoint**: snapshot-all —
  `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`.
  Returns **all requested tickers in one call**, which is what keeps us inside
  the free tier's 5 req/min. The SDK wrapper is
  `client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])`.
- **Fields we read** from each snapshot:
  - `snap.last_trade.price` — the current price we cache and trade on.
  - `snap.last_trade.timestamp` — **Unix milliseconds**; divide by 1000 for the
    seconds our cache/`PriceUpdate` expect.
  - `snap.day.previous_close` / `snap.day.change_percent` — available for a
    true *day* change if we later surface it (the current cache tracks
    tick-over-tick change only).
- **Rate limits**: Free = 5 req/min → 15 s poll. Paid → 2-5 s poll (tune
  `poll_interval`).
- **Errors surfaced by the SDK**: 401 (bad key), 403 (plan lacks endpoint), 429
  (rate limit), 5xx (server; SDK retries ~3×). All are caught by the poller's
  outer `try/except` and simply retried next interval.
- **Market-hours caveat**: outside regular hours `last_trade.price` reflects the
  last trade (possibly after-hours), and the `day` object may still carry the
  previous session — fine for a simulated trading toy.

Example raw shape (trimmed) for one ticker:

```json
{
  "ticker": "AAPL",
  "day": { "open": 129.61, "previous_close": 129.61, "change_percent": -3.50 },
  "last_trade": { "price": 125.07, "size": 100, "timestamp": 1675190399000 }
}
```

Other endpoints the SDK exposes (`get_snapshot_ticker`, `get_previous_close_agg`,
`list_aggs`, `get_last_trade`/`get_last_quote`) are not needed for live polling
but are available if we later add a historical detail chart or want to fetch real
seed prices at startup.

---

## 16. Testing Strategy

Tests live in `backend/tests/market/`, run with `pytest` (asyncio mode auto).
The layered design makes almost everything testable without a network or a
running event loop.

| Test module | Target | What it covers |
|-------------|--------|----------------|
| `test_models.py` | `PriceUpdate` | `change` / `change_percent` / `direction` math, zero-previous guard, `to_dict()` shape |
| `test_cache.py` | `PriceCache` | update/get/get_all/remove, first-update-is-flat, direction transitions, version increments |
| `test_simulator.py` | `GBMSimulator` | prices stay positive, seed prices, add/remove, Cholesky rebuild, empty step |
| `test_simulator_source.py` | `SimulatorDataSource` | start seeds cache, prices move over time, clean double-stop, add/remove |
| `test_massive.py` | `MassiveDataSource` | mocked snapshots update cache, malformed record skipped, API error doesn't crash |
| `test_factory.py` | `create_market_data_source` | env-var branch selection, whitespace key → simulator |

### The GBM engine is pure — test it directly

```python
def test_prices_are_positive():
    """GBM can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0


def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None          # 1 ticker → no matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None      # 2 tickers → matrix exists
```

### Mock the Massive SDK at `_fetch_snapshots`

The seam between "our code" and "the network" is the synchronous
`_fetch_snapshots()`. Patch it and the entire poll path is testable offline. Set
a long `poll_interval` so the background loop never fires during the test, and
call `_poll_once()` directly.

```python
from unittest.mock import MagicMock, patch


def _snap(ticker, price, ts_ms):
    s = MagicMock()
    s.ticker = ticker
    s.last_trade.price = price
    s.last_trade.timestamp = ts_ms
    return s


async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
    snaps = [_snap("AAPL", 190.50, 1707580800000), _snap("GOOGL", 175.25, 1707580800000)]
    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()
    assert cache.get_price("AAPL") == 190.50


async def test_api_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network")):
        await source._poll_once()          # must NOT raise
    assert cache.get_price("AAPL") is None
```

### Async source lifecycle

```python
async def test_start_populates_cache():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])
    assert cache.get("AAPL") is not None    # seeded before the first loop tick
    await source.stop()
    await source.stop()                     # double-stop is safe
```

Target: unit coverage on the pure/logic modules (`models`, `cache`, `simulator`,
`factory`) near 100%; `massive_client` is partly mocked, so its network methods
show lower line coverage by design.

---

## 17. Error Handling & Edge Cases

**Empty watchlist at startup.** `start([])` is valid: the simulator produces no
prices, the Massive poller no-ops (`if not self._tickers`), and the SSE stream
sends nothing until the user adds a ticker — at which point tracking begins
immediately.

**Trading a ticker with no cached price.** The simulator eliminates this by
seeding the cache inside `add_ticker()`. With Massive there can be a brief gap
before the first poll returns; trade execution should answer with a clear
`400`:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Try again shortly.")
```

**Invalid Massive key.** The first poll 401s; it's logged and the poller keeps
looping. SSE stays connected but streams empty data (the connection dot is green,
there just are no prices). Fix the key and restart.

**Thread safety under load.** `PriceCache` holds a mutex only for a dict
lookup + assignment. At 10 tickers × 2 updates/s with a handful of SSE readers,
contention is immeasurable. A read/write lock would be the escalation path if we
ever reached hundreds of tickers and many concurrent readers — unnecessary here.

**Numerical stability.** GBM's `exp(...)` formulation is always positive and the
tiny `dt` keeps per-tick moves small; prices are `round(_, 2)` on the way out, so
there's no float drift accumulation in what users see.

**Correlation matrix validity.** Sector correlations are positive and the matrix
is diagonally dominant, so it's positive-definite and `np.linalg.cholesky`
always succeeds for the built-in ticker set. Dynamically added unknown tickers
use `CROSS_GROUP_CORR = 0.3`, which preserves that property.

---

## 18. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | env var | `""` | Set → Massive API; empty/whitespace → simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5 s` | Simulator tick period |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0 s` | Massive poll period (free-tier safe) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Per-ticker, per-tick chance of a 2-5% shock |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM step as a fraction of a trading year (500 ms) |
| SSE push interval | `_generate_events(interval=...)` | `0.5 s` | Cache sampling / push cadence |
| SSE retry directive | `_generate_events` | `1000 ms` | Browser `EventSource` reconnect delay |
| `poll → 2-5 s` | (ops) | — | Recommended `poll_interval` on paid Massive tiers |

### Dependencies (`backend/pyproject.toml`)

`fastapi`, `uvicorn[standard]`, `numpy` (GBM + Cholesky), `massive` (real data),
`rich` (demo dashboard); dev extras `pytest`, `pytest-asyncio`, `pytest-cov`,
`ruff`. `massive` is a core dependency, so its imports are top-level — the
simulator path simply never touches them.

### Manual demo

```bash
cd backend
uv run market_data_demo.py     # live Rich terminal dashboard, ~60s
```

Renders all 10 tickers with sparklines, colored direction arrows, and an event
log — a fast visual sanity check that the simulator and cache behave.
```
