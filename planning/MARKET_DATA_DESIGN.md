# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. It covers the
unified data-source interface, the in-memory price cache, the GBM simulator, the
Massive (Polygon.io) API client, the SSE streaming endpoint, and FastAPI lifecycle
integration.

This document reflects the **final, reviewed implementation** that lives under
`backend/app/market/`. Every code snippet is the shipped code, so this doc doubles
as both a design rationale and a reference for downstream agents (portfolio, chat,
frontend) that consume market data.

> **Status:** Built, tested (73 tests, 84% coverage), reviewed, all issues resolved.
> The earlier draft designs are archived in `planning/archive/` for history.

---

## Table of Contents

1. [Design Goals & Principles](#1-design-goals--principles)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [File Structure](#3-file-structure)
4. [Data Model — `models.py`](#4-data-model--modelspy)
5. [Price Cache — `cache.py`](#5-price-cache--cachepy)
6. [Unified Interface — `interface.py`](#6-unified-interface--interfacepy)
7. [Seed Prices & Parameters — `seed_prices.py`](#7-seed-prices--parameters--seed_pricespy)
8. [GBM Simulator — `simulator.py`](#8-gbm-simulator--simulatorpy)
9. [Massive API Client — `massive_client.py`](#9-massive-api-client--massive_clientpy)
10. [Factory — `factory.py`](#10-factory--factorypy)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint--streampy)
12. [Public API — `__init__.py`](#12-public-api--__init__py)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Data Flow Walkthroughs](#15-data-flow-walkthroughs)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Testing Strategy](#17-testing-strategy)
18. [Configuration Summary](#18-configuration-summary)
19. [Dependencies](#19-dependencies)

---

## 1. Design Goals & Principles

The market data layer has one job: **keep an always-current price for every tracked
ticker available to the rest of the app, regardless of where those prices come
from.** Four principles drive the design.

| Principle | How it's realized |
|-----------|-------------------|
| **Source-agnostic downstream** | Every consumer reads from a single `PriceCache`. Nothing outside `app/market/` knows whether prices are simulated or real. |
| **Strategy pattern** | Simulator and Massive both implement one abstract `MarketDataSource`. A factory picks one at startup based on `MASSIVE_API_KEY`. |
| **Push, don't pull** | Data sources push updates into the cache on *their* schedule (500 ms sim, 15 s poll). The SSE layer reads the cache on *its* schedule. Timing is fully decoupled. |
| **Resilience over correctness-at-all-costs** | A bad tick, a malformed snapshot, or an API 429 never kills the feed. The loop logs and continues; the cache retains last-known prices. |

---

## 2. Architecture at a Glance

```
                    ┌──────────────────────────────┐
                    │   create_market_data_source   │  reads MASSIVE_API_KEY
                    └───────────────┬──────────────┘
                                    │ picks one
              ┌─────────────────────┴─────────────────────┐
              ▼                                            ▼
   ┌───────────────────────┐                  ┌───────────────────────┐
   │  SimulatorDataSource   │                  │   MassiveDataSource    │
   │  (GBM, 500ms tick)     │                  │  (REST poll, 15s)      │
   └───────────┬───────────┘                  └───────────┬───────────┘
               │  update(ticker, price)                    │
               └───────────────────┬───────────────────────┘
                                   ▼
                        ┌────────────────────┐
                        │     PriceCache      │  thread-safe, in-memory
                        │  {ticker: PriceUpdate}│  + version counter
                        └──────────┬─────────┘
                                   │ get_all() / get() / get_price()
             ┌─────────────────────┼─────────────────────┐
             ▼                     ▼                     ▼
    ┌────────────────┐   ┌──────────────────┐   ┌──────────────────┐
    │ SSE /api/stream │   │ Portfolio valuation│  │  Trade execution  │
    │ /prices → browser│   │  (current price)  │   │ (fill at current) │
    └────────────────┘   └──────────────────┘   └──────────────────┘
```

Producers write; consumers read; the cache is the only coupling point.

---

## 3. File Structure

```
backend/app/market/
  __init__.py          # Public re-exports (PriceUpdate, PriceCache, MarketDataSource, ...)
  models.py            # PriceUpdate — immutable price snapshot
  cache.py             # PriceCache — thread-safe store + version counter
  interface.py         # MarketDataSource — abstract base class
  seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants (data only)
  simulator.py         # GBMSimulator (math) + SimulatorDataSource (async wrapper)
  massive_client.py    # MassiveDataSource — REST poller
  factory.py           # create_market_data_source()
  stream.py            # create_stream_router() — SSE endpoint
```

Each file has a single responsibility. `__init__.py` re-exports the public surface so
the rest of the backend imports from `app.market` and never reaches into submodules.

---

## 4. Data Model — `models.py`

`PriceUpdate` is the only type that crosses the boundary out of the market layer.
Every consumer — SSE, portfolio valuation, trade execution — works exclusively with it.

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

- **`frozen=True`** — Price updates are immutable value objects. Once created they never
  change, so they're safe to share across async tasks without copying.
- **`slots=True`** — Memory optimization; we create many of these per second.
- **Computed properties** (`change`, `change_percent`, `direction`) — derived from `price`
  and `previous_price`, so they can never be internally inconsistent. There's no way to
  have a stored `direction` that disagrees with the prices.
- **`to_dict()`** — a single serialization point used by both the SSE endpoint and any
  REST responses that echo prices.

---

## 5. Price Cache — `cache.py`

The cache is the central hub. Data sources write; SSE, portfolio, and trade execution
read. It is **thread-safe** because the Massive client runs its synchronous REST call in
`asyncio.to_thread()` (a real OS thread), while SSE reads happen on the event loop.

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

### Why `previous_price` is computed here, not passed in

The cache is the single place that knows the last price for a ticker, so it's the right
place to derive `previous_price`. Callers just supply the newest price; the cache diffs
it against what it already holds. This keeps every producer trivially simple — the
simulator and the Massive client both call `cache.update(ticker, price)` and nothing
else.

### Why a version counter?

The SSE loop reads the cache every ~500 ms. Without a change signal it would re-serialize
and re-send every price on every tick even when nothing changed (e.g., the Massive poller
only writes every 15 s). The monotonic `version` — bumped inside `update()` — lets the SSE
loop skip sends when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread-safety rationale

`threading.Lock` (not `asyncio.Lock`) because the Massive client's synchronous
`get_snapshot_all()` runs in `asyncio.to_thread()`, i.e. a real OS thread that an
`asyncio.Lock` would not protect. A `threading.Lock` is correct from both a worker thread
and the event loop. The critical section is tiny (a dict lookup + assignment), so
contention is negligible at this scale.

> **Note on `version`:** reading a single `int` is atomic under CPython's GIL, so the
> unlocked read in the `version` property is safe today. On a future free-threaded build
> it could be tightened, but that's out of scope.

---

## 6. Unified Interface — `interface.py`

Both data sources implement this ABC. Downstream code depends only on this contract, never
on a concrete class.

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

The push model decouples timing. The simulator ticks at 500 ms; Massive polls at 15 s; SSE
reads at 500 ms. None of them needs to know the others' cadence. Adding a third source in
the future (e.g., a WebSocket feed) requires zero changes to the SSE or portfolio layers —
it just needs to call `cache.update()`.

---

## 7. Seed Prices & Parameters — `seed_prices.py`

Pure data, no logic. Shared by the simulator for initial prices, per-ticker GBM
parameters, and the correlation structure.

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

The parameter tuning is intentional: TSLA at `sigma=0.50` is genuinely jumpy, V and JPM at
~0.17–0.18 are calm, NVDA carries the strongest drift (`mu=0.08`). This makes the
simulated tape *feel* like a real market to a viewer.

---

## 8. GBM Simulator — `simulator.py`

Two classes:

- **`GBMSimulator`** — the pure math engine. Stateful: holds current prices, advances one
  step per call. No I/O, no async.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation that wraps the engine in
  an async loop and writes to the cache.

### 8.1 The math

Each tick evolves every price by a Geometric Brownian Motion step:

```
S(t+dt) = S(t) · exp( (μ − ½σ²)·dt  +  σ·√dt·Z )
```

- `S(t)` current price · `μ` annualized drift · `σ` annualized volatility
- `dt` time step as a fraction of a trading year · `Z` a **correlated** standard normal

With 500 ms ticks over 252 trading days × 6.5 h:

```
dt = 0.5 / (252 · 6.5 · 3600) ≈ 8.48e-8
```

The tiny `dt` yields sub-cent moves per tick that accumulate naturally. Because the update
is multiplicative through `exp(...)`, **prices can never go negative**.

### 8.2 Correlated moves via Cholesky

Real sectors move together. We build a correlation matrix `C` from sector membership, take
its Cholesky factor `L = cholesky(C)`, and turn independent normals into correlated ones:

```
Z_correlated = L · Z_independent
```

The matrix is rebuilt whenever tickers are added/removed (O(n²), but n < 50). Cholesky
requires the matrix be positive semi-definite, which a well-formed correlation matrix is.

### 8.3 `GBMSimulator` — the engine

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

**Random shock events** — each tick, every ticker has a ~0.1% chance of a sudden 2–5%
move. With 10 tickers at 2 ticks/s that's an event roughly every 50 seconds: enough drama
to keep the dashboard alive, not so much that prices destabilize.

**Dynamically added tickers** — a ticker not in `SEED_PRICES` starts at a random
$50–$300 and uses `DEFAULT_PARAMS`. That keeps "add a random ticker via chat" working
without a data-entry step.

### 8.4 `SimulatorDataSource` — async wrapper

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

Three behaviors worth calling out:

- **Immediate seeding.** `start()` writes seed prices to the cache *before* the loop
  begins, so the SSE endpoint has data on its very first read — no blank-screen delay.
  `add_ticker()` seeds the new ticker the same way, so a freshly added ticker is
  immediately tradeable.
- **Graceful cancellation.** `stop()` cancels the task and awaits it, swallowing
  `CancelledError` — clean teardown during FastAPI lifespan shutdown, and safe to call
  twice.
- **Per-tick exception isolation.** `_run_loop` wraps each step in `try/except` so one bad
  tick logs and the feed keeps running.

---

## 9. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST snapshot endpoint for all watched tickers in
a **single call**, then writes results to the same `PriceCache`. The Massive SDK is
synchronous, so the network call runs in `asyncio.to_thread()` to keep the event loop
free.

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

### The snapshot endpoint

We use one endpoint for the whole watchlist:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

Via the SDK:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.last_trade.timestamp)
```

Fetching the whole watchlist in a single call is what keeps us inside the free tier's
5 req/min ceiling. We extract `last_trade.price` and convert `last_trade.timestamp` from
Unix **milliseconds** to seconds (the cache and `PriceUpdate` use seconds throughout).

### Error-handling philosophy

The poller is deliberately hard to kill:

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** (bad key) | Logged as error; poller keeps running. Fix `.env` and restart. |
| **429 Rate limited** | Logged; next poll retries after `poll_interval`. |
| **Network timeout / 5xx** | Logged; retries on the next cycle. The SDK also retries 5xx internally. |
| **Malformed snapshot** | *That ticker* is skipped with a warning; all others still process. |
| **Whole poll fails** | Cache keeps last-known prices; SSE keeps streaming slightly-stale data — better than a blank screen. |

Two nested `try/except` blocks make this work: the inner one isolates a single bad
snapshot, the outer one isolates a whole failed poll.

### Note on `massive` as a hard dependency

`massive>=1.0.0` is a core dependency in `pyproject.toml`, so the import lives at module
top level (an earlier draft used a lazy import inside `start()`; the review consolidated it
to the top since the package is always installed). If you ever want the simulator path to
have *zero* third-party market dependencies, reintroduce a lazy import here and move
`massive` to an optional extra — but that's not the current design.

---

## 10. Factory — `factory.py`

One function selects the implementation from the environment. Everything downstream
receives a `MarketDataSource` and never branches on which concrete type it got.

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

The `.strip()` matters: a `MASSIVE_API_KEY=` line in `.env` yields `""`, which correctly
falls through to the simulator. Whitespace-only keys do too.

---

## 11. SSE Streaming Endpoint — `stream.py`

A FastAPI route that holds a long-lived `text/event-stream` connection open and pushes the
full price snapshot whenever the cache version changes.

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

Each event is a single JSON object keyed by ticker:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

The client consumes it with the native `EventSource` API — no library needed:

```javascript
const es = new EventSource('/api/stream/prices');
es.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices = { AAPL: { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
  for (const [ticker, update] of Object.entries(prices)) {
    applyPriceFlash(ticker, update.direction); // green/red flash
    updateSparkline(ticker, update.price);      // accumulate since page load
  }
};
es.onerror = () => setConnectionStatus('reconnecting'); // EventSource auto-retries
```

### Design notes

- **`retry: 1000`** — instructs the browser to reconnect 1 s after a drop. Combined with
  `EventSource`'s built-in reconnection, connectivity is self-healing with no client code.
- **Version-gated sends** — only serialize/emit when `price_cache.version` changed. With a
  15 s Massive poll, this collapses ~30 redundant no-op ticks into silence.
- **Disconnect detection** — `request.is_disconnected()` breaks the loop when the client
  goes away, so we don't leak generators for closed tabs.
- **`X-Accel-Buffering: no`** — disables nginx response buffering if the app is ever put
  behind a reverse proxy, so events aren't held back.
- **Poll-and-push, not event-driven** — the endpoint reads the cache on a fixed cadence
  rather than being signaled by the data source. This produces evenly spaced updates, which
  makes the frontend sparklines clean (regular sample spacing).

---

## 12. Public API — `__init__.py`

The only surface the rest of the backend imports.

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

Downstream usage:

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

The subsystem starts and stops with the app via the `lifespan` context manager. This is the
glue the rest of the backend (main app, portfolio routes, watchlist routes) hooks into.

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

    # 1. Create the shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 2. Create the market data source (simulator or Massive, per env)
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 3. Load initial tickers from the DB watchlist, then start producing prices
    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    # 4. Register the SSE streaming router
    app.include_router(create_stream_router(price_cache))

    yield  # ---- app is running ----

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency providers for route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes reach market data through dependency injection — no globals, no reaching into
the source's internals:

```python
from fastapi import APIRouter, Depends, HTTPException

from app.market import PriceCache, MarketDataSource

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}.")
    # ... fill instantly at current_price, update positions/cash ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)  # start tracking immediately


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from watchlist table ...
    await source.remove_ticker(ticker)  # stop tracking (see §14 for the position caveat)
```

---

## 14. Watchlist Coordination

Whenever the watchlist changes — via REST or via the LLM chat tool — the data source must
be told, so it tracks exactly the right set of tickers.

**Add:**
```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT into watchlist (SQLite)
  → await source.add_ticker("PYPL")
        Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
        Massive:   appends to ticker list, appears on next poll
```

**Remove:**
```
DELETE /api/watchlist/PYPL
  → DELETE from watchlist (SQLite)
  → await source.remove_ticker("PYPL")
        Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
        Massive:   removes from ticker list, removes from cache
```

### Edge case: removing a ticker you still hold

If the user removes a ticker from the watchlist but still owns shares, the ticker must keep
streaming so portfolio valuation stays correct. The watchlist route should guard the
`remove_ticker` call:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if there is no open position
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

This keeps the price feed alive for held-but-unwatched tickers so their P&L never goes
stale.

---

## 15. Data Flow Walkthroughs

**Startup → first paint**
1. `lifespan` creates `PriceCache`, calls the factory, loads watchlist tickers.
2. `source.start(tickers)` seeds the cache with initial prices *before* the loop starts.
3. Browser opens `EventSource('/api/stream/prices')`; the first read already has data →
   the watchlist paints instantly.

**Steady state (simulator)**
1. Every 500 ms `_run_loop` calls `sim.step()` → correlated GBM prices.
2. Each price goes through `cache.update()`, bumping `version`.
3. SSE loop notices the version change, serializes `get_all()`, pushes one event.
4. Frontend flashes cells green/red and extends sparklines.

**Steady state (Massive)**
1. Every 15 s `_poll_once` fetches one snapshot batch in a worker thread.
2. Each snapshot's `last_trade.price` goes through `cache.update()`.
3. Between polls the cache is unchanged → SSE stays quiet (version-gated) → the frontend
   holds the last values. On the next poll, cells update in a batch.

**Trade**
1. `POST /api/portfolio/trade` reads `cache.get_price(ticker)`.
2. If `None` (ticker just added, no price yet), returns HTTP 400 with a wait-and-retry
   message.
3. Otherwise fills instantly at the cached price and updates positions/cash.

---

## 16. Error Handling & Edge Cases

- **Empty watchlist at startup.** `start([])` is fine: the simulator produces no prices, the
  Massive poller skips its call, SSE sends nothing. The first `add_ticker` brings the system
  to life.
- **Price-cache miss during a trade.** A ticker added seconds ago under Massive may not have
  a price until the next poll. Trades return HTTP 400 (`"Price not yet available"`). The
  simulator avoids this entirely by seeding on `add_ticker`.
- **Invalid Massive key.** First poll 401s, is logged, poller keeps retrying. SSE reports
  "connected" (the stream works) but carries no data until the key is fixed and the app
  restarts.
- **Negative prices.** Impossible — GBM is multiplicative through `exp(...)`, always
  positive.
- **Floating-point drift.** Prices are `round(..., 2)` in both `step()` and `cache.update()`;
  the exponential form is numerically stable.
- **Cholesky on a singular matrix.** Sector-based correlations produce a valid positive
  semi-definite matrix, so `np.linalg.cholesky` succeeds. With ≤1 ticker there's no matrix
  (`_cholesky = None`) and `step()` uses independent draws.

---

## 17. Testing Strategy

The subsystem ships with **73 tests across 6 modules, 84% coverage**. Structure and
representative cases:

| Test module | Focus |
|-------------|-------|
| `test_models.py` | `PriceUpdate` change/direction/percent math, `to_dict()` shape |
| `test_cache.py` | update/get/remove, first-update-is-flat, direction, version increments |
| `test_simulator.py` | GBM engine: positivity, add/remove, Cholesky rebuild, empty step |
| `test_simulator_source.py` | async lifecycle: seed-on-start, updates over time, clean stop, add/remove |
| `test_factory.py` | env-var selection (key set vs empty vs whitespace) |
| `test_massive.py` | poll updates cache, malformed snapshot skipped, API error doesn't crash (mocked) |

### GBM engine (representative)

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


def test_prices_are_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0


def test_initial_price_matches_seed():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]


def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None            # 1 ticker → no matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None        # 2 tickers → matrix exists


def test_empty_step():
    assert GBMSimulator(tickers=[]).step() == {}
```

### Price cache (representative)

```python
from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    u = cache.update("AAPL", 190.50)
    assert u.direction == "flat" and u.previous_price == 190.50


def test_direction_and_change():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    u = cache.update("AAPL", 191.00)
    assert u.direction == "up" and u.change == 1.00


def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 191.00)
    assert cache.version == v0 + 2
```

### Simulator source lifecycle (async)

```python
import asyncio
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


async def test_start_seeds_cache_then_updates():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL", "GOOGL"])

    assert cache.get("AAPL") is not None      # seeded before loop runs

    await asyncio.sleep(0.2)                    # several ticks
    assert cache.get("AAPL").timestamp > 0

    await source.stop()
    await source.stop()                         # idempotent
```

### Massive client (mocked, no network)

The Massive tests fabricate snapshot objects and patch `_fetch_snapshots`, so they run
without a network or API key:

```python
from unittest.mock import MagicMock, patch
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _snap(ticker, price, ts_ms):
    s = MagicMock()
    s.ticker = ticker
    s.last_trade.price = price
    s.last_trade.timestamp = ts_ms
    return s


async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()   # so _poll_once passes the "client exists" guard
    source._tickers = ["AAPL", "GOOGL"]

    snaps = [_snap("AAPL", 190.50, 1707580800000), _snap("GOOGL", 175.25, 1707580800000)]
    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("GOOGL") == 175.25


async def test_api_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network")):
        await source._poll_once()   # must not raise
    assert cache.get_price("AAPL") is None
```

Run them:

```bash
cd backend
uv run --extra dev pytest -v                # all tests
uv run --extra dev pytest --cov=app         # with coverage
uv run --extra dev ruff check app/ tests/   # lint
```

### Live demo

`backend/market_data_demo.py` runs a Rich terminal dashboard against the simulator (10
tickers, sparklines, colored direction arrows, an event log) for ~60 s:

```bash
cd backend
uv run market_data_demo.py
```

---

## 18. Configuration Summary

| Parameter | Location | Default | Meaning |
|-----------|----------|---------|---------|
| `MASSIVE_API_KEY` | env var | `""` | Non-empty → Massive API; empty/unset → simulator |
| `update_interval` | `SimulatorDataSource` | `0.5 s` | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource` | `15.0 s` | Time between Massive polls (free tier) |
| `event_probability` | `GBMSimulator` | `0.001` | Chance of a shock event per ticker per tick |
| `dt` | `GBMSimulator` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| SSE `interval` | `_generate_events` | `0.5 s` | SSE read/push cadence |
| SSE `retry` | `_generate_events` | `1000 ms` | Browser reconnect delay directive |

Tuning tips:
- **Faster tape:** lower `update_interval` (e.g., 0.25 s). The cache/SSE keep pace.
- **Paid Massive tier:** lower `poll_interval` to 2–5 s.
- **More drama:** raise `event_probability` (0.002–0.005). Too high looks chaotic.

---

## 19. Dependencies

From `backend/pyproject.toml` (`requires-python >= 3.12`):

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",       # Cholesky + vectorized normal draws in the simulator
    "massive>=1.0.0",     # Polygon.io SDK (RESTClient, SnapshotMarketType)
    "rich>=13.0.0",       # terminal demo dashboard
]

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0", "pytest-cov>=5.0.0", "ruff>=0.7.0"]

[tool.hatch.build.targets.wheel]
packages = ["app"]        # required for `uv sync` / Docker builds to find the package
```

`pytest-asyncio` runs in `asyncio_mode = "auto"`, so `async def test_*` functions need no
decorator.
