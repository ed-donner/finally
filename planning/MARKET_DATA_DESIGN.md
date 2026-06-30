# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. It documents
the **as-built** code under `backend/app/market/`: the unified `MarketDataSource`
interface, the thread-safe in-memory `PriceCache`, the GBM simulator, the Massive
(Polygon.io) REST poller, the factory that selects between them, and the SSE
streaming endpoint that pushes prices to the browser.

> **Status:** This subsystem is complete, tested (73 tests), and reviewed. This
> document is the canonical design reference; a one-page summary lives in
> `planning/MARKET_DATA_SUMMARY.md` and the original design drafts are preserved
> in `planning/archive/`.

---

## Table of Contents

1. [Goals & Constraints](#1-goals--constraints)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [File Structure](#3-file-structure)
4. [Data Model — `models.py`](#4-data-model--modelspy)
5. [Price Cache — `cache.py`](#5-price-cache--cachepy)
6. [Abstract Interface — `interface.py`](#6-abstract-interface--interfacepy)
7. [Seed Prices & Parameters — `seed_prices.py`](#7-seed-prices--parameters--seed_pricespy)
8. [GBM Simulator — `simulator.py`](#8-gbm-simulator--simulatorpy)
9. [Massive API Client — `massive_client.py`](#9-massive-api-client--massive_clientpy)
10. [Factory — `factory.py`](#10-factory--factorypy)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint--streampy)
12. [Public API — `__init__.py`](#12-public-api--__init__py)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Testing Strategy](#15-testing-strategy)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Configuration Summary](#17-configuration-summary)
18. [Dependencies](#18-dependencies)

---

## 1. Goals & Constraints

The market data layer must satisfy these requirements from `PLAN.md`:

- **Two interchangeable sources behind one interface** — a GBM simulator (default,
  zero external dependencies at runtime beyond `numpy`) and a Massive/Polygon.io
  REST poller (when `MASSIVE_API_KEY` is set). All downstream code is source-agnostic.
- **A single in-memory price cache** as the one point of truth. Producers write,
  consumers (SSE, portfolio valuation, trade execution) read. This decouples timing
  and makes the layer multi-consumer ready.
- **SSE push to the browser** at ~500ms cadence via `EventSource`, with automatic
  client reconnection.
- **Dynamic watchlist** — tickers can be added/removed at runtime via REST or the LLM
  chat, and the active source must track the right set.
- **Resilience** — a long-running background task must survive a bad tick or a failed
  API poll without dying.

### Why a push-to-cache model (not request/response)

The simulator ticks every 500ms; Massive polls every 15s; the SSE stream reads at its
own 500ms cadence. By having each source *push* into a shared cache on its own
schedule, the SSE layer never needs to know which source is active or how fast it
updates. The cache absorbs the rate mismatch.

---

## 2. Architecture at a Glance

```
            ┌────────────────────────────────────────────┐
            │            MarketDataSource (ABC)            │
            │   start / stop / add_ticker / remove_ticker  │
            └───────────────┬──────────────┬───────────────┘
                            │              │
          ┌─────────────────┘              └──────────────────┐
          ▼                                                   ▼
 ┌──────────────────────┐                          ┌────────────────────────┐
 │ SimulatorDataSource  │                          │   MassiveDataSource     │
 │  (GBMSimulator, 500ms)│                          │  (REST poll, 15s)       │
 └──────────┬───────────┘                          └───────────┬─────────────┘
            │  writes                                          │  writes
            └──────────────────┐          ┌────────────────────┘
                               ▼          ▼
                      ┌────────────────────────────┐
                      │   PriceCache (thread-safe)  │
                      │   latest PriceUpdate/ticker │
                      │   + monotonic version count │
                      └──────────────┬──────────────┘
                                     │ reads
                ┌────────────────────┼─────────────────────┐
                ▼                    ▼                      ▼
       ┌─────────────────┐  ┌─────────────────┐   ┌────────────────────┐
       │ SSE /api/stream │  │ Portfolio value │   │  Trade execution   │
       │   /prices       │  │   (read price)  │   │  (read fill price) │
       └────────┬────────┘  └─────────────────┘   └────────────────────┘
                │ EventSource
                ▼
            Frontend
```

The **factory** (`create_market_data_source`) picks the concrete source at startup
based on `MASSIVE_API_KEY`. Everything below the cache is identical regardless of
source.

---

## 3. File Structure

```
backend/
  app/
    market/
      __init__.py        # Public re-exports
      models.py          # PriceUpdate dataclass
      cache.py           # PriceCache (thread-safe in-memory store)
      interface.py       # MarketDataSource ABC
      seed_prices.py     # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation consts
      simulator.py       # GBMSimulator + SimulatorDataSource
      massive_client.py  # MassiveDataSource
      factory.py         # create_market_data_source()
      stream.py          # create_stream_router() — SSE endpoint
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each module has a single responsibility. `__init__.py` re-exports the public surface so
the rest of the backend imports from `app.market` and never reaches into submodules.

---

## 4. Data Model — `models.py`

`PriceUpdate` is the **only** type that leaves the market data layer. SSE, portfolio
valuation, and trade execution all work exclusively with it.

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

- **`frozen=True`** — price updates are immutable value objects, safe to share across
  async tasks without copying.
- **`slots=True`** — small memory win; many of these are created per second.
- **Computed properties** (`change`, `change_percent`, `direction`) — derived from
  `price` and `previous_price`, so they can never be inconsistent or stale.
- **`to_dict()`** — single serialization point used by both SSE and REST responses.

---

## 5. Price Cache — `cache.py`

The central data hub. Data sources write; SSE/portfolio/trades read. It is **thread-safe**
because the Massive client's synchronous REST call runs in `asyncio.to_thread()` (a real
OS thread), while SSE reads happen on the event loop.

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

The SSE loop polls the cache every ~500ms. Without a change signal it would re-serialize
and re-send every price on every tick — wasteful when the source updates infrequently
(Massive only changes the cache every 15s). The monotonic `version` lets the SSE loop
skip sends when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Why `threading.Lock` (not `asyncio.Lock`)?

`asyncio.Lock` only coordinates coroutines on one event loop. The Massive client runs its
synchronous REST call via `asyncio.to_thread()`, i.e. in a separate OS thread, so an
`asyncio.Lock` would not protect the dict against that writer. `threading.Lock` works
correctly from both the event loop and worker threads. The critical section is tiny (one
dict lookup + one assignment), so contention is negligible at this scale.

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

Note the asymmetry: `start/stop/add_ticker/remove_ticker` are `async` (they touch
background tasks and may await), while `get_tickers` is a plain synchronous read.

---

## 7. Seed Prices & Parameters — `seed_prices.py`

Constants only — no logic, no imports. Shared by the simulator (for initial prices, GBM
parameters, and the correlation structure).

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

Tickers added dynamically that are *not* in `SEED_PRICES` start at a random price in
`[50, 300]` and use `DEFAULT_PARAMS`.

---

## 8. GBM Simulator — `simulator.py`

Two classes:

- `GBMSimulator` — pure math engine. Stateful; holds current prices and advances them one
  step at a time. Synchronous and easily unit-testable.
- `SimulatorDataSource` — the `MarketDataSource` implementation that drives `GBMSimulator`
  in an async loop and writes into the `PriceCache`.

### 8.1 The math

At each step a price evolves under Geometric Brownian Motion:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift
- `sigma` — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — a **correlated** standard normal draw

`dt` for a 500ms tick over a 252-day, 6.5-hour trading year:

```
dt = 0.5 / (252 * 6.5 * 3600) = 0.5 / 5,896,800 ≈ 8.48e-8
```

This tiny `dt` produces sub-cent moves per tick that accumulate naturally. GBM is
multiplicative (`exp(...)` is always positive), so **prices can never go negative**.

### 8.2 Correlated moves via Cholesky

Real stocks don't move independently. Given a correlation matrix `C`, compute its Cholesky
factor `L = cholesky(C)`; then for independent normals `Z_ind`, the product
`Z_corr = L @ Z_ind` is a vector of correlated normals with the desired covariance.
Cholesky requires `C` to be positive semi-definite, which a valid correlation matrix is.
The matrix is rebuilt whenever tickers are added/removed — O(n²), but n is small (<50).

Pairwise correlation rules: same-tech `0.6`, same-finance `0.5`, anything involving TSLA
`0.3`, everything else (cross-sector / unknown) `0.3`.

### 8.3 Random shock events

Each ticker has a ~0.1% chance per tick of a sudden 2–5% move for visual drama. With 10
tickers at 2 ticks/sec, expect a shock roughly every ~50 seconds somewhere on the board.

### 8.4 `GBMSimulator` — the engine

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

        # Initialize all starting tickers, then build the correlation matrix once
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

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker → 2-5% shock
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

        # TSLA is in the tech set but behaves independently
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

> **Implementation note.** Construction uses a private `_add_ticker_internal()` in a loop
> and rebuilds Cholesky **once** at the end, rather than rebuilding on every insert. The
> public `add_ticker()` rebuilds immediately because it's a one-off runtime mutation.

### 8.5 `SimulatorDataSource` — async wrapper

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

- **Immediate seeding** — `start()` and `add_ticker()` write seed prices into the cache
  *before* the loop runs, so SSE has data on the very first tick (no blank screen).
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing
  `CancelledError`. Safe to call twice.
- **Exception resilience** — the loop catches exceptions per step, so one bad tick never
  kills the feed.

---

## 9. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) snapshot endpoint for all watched tickers in a
**single** REST call per cycle (critical for the free tier's 5 req/min limit). The
synchronous client runs in `asyncio.to_thread()` so it never blocks the event loop.

> **Dependency note.** `massive` is a declared core dependency in `pyproject.toml`, so the
> imports are top-level (not lazy). The factory still ensures the client is only
> *instantiated* when an API key is present.

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

### Snapshot response shape (fields we use)

The `get_snapshot_all` call returns one snapshot object per ticker. We extract just two
fields:

| Field | Use |
|-------|-----|
| `snap.ticker` | Cache key |
| `snap.last_trade.price` | Current price (display, valuation, fills) |
| `snap.last_trade.timestamp` | Unix **milliseconds** → divide by 1000 for seconds |

`day.change_percent`, `last_quote`, OHLC, etc. are available if a future feature needs
them, but the live feed only needs price + timestamp; the cache derives `change`/`direction`
from the previous price itself.

### Error-handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** (bad key) | Logged; poller keeps running so a fixed key + restart recovers |
| **429 Rate limited** | Logged; next cycle retries after `poll_interval` |
| **Network timeout** | Logged; retries automatically next cycle |
| **Malformed single snapshot** | That ticker skipped with a warning; others still processed |
| **Whole poll fails** | Cache retains last-known prices; SSE keeps streaming stale-but-present data |

The poller is deliberately *fail-soft*: a long-running feed should degrade, not crash.

---

## 10. Factory — `factory.py`

Selects the concrete source at startup from the environment. Returns an **unstarted**
source — the caller awaits `start(tickers)`.

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

Usage:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 11. SSE Streaming Endpoint — `stream.py`

A FastAPI route factory that holds open a long-lived `text/event-stream` connection and
pushes the full price snapshot to the client whenever the cache version changes.

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

        Streams all tracked ticker prices every ~500ms in the format:
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

    Sends all prices whenever the cache version changes. Stops when the client
    disconnects (detected via request.is_disconnected()).
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

### Wire format

Each event sent to the browser:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

### Client usage

```javascript
const eventSource = new EventSource('/api/stream/prices');

eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices is { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
  for (const [ticker, p] of Object.entries(prices)) {
    updateTickerCell(ticker, p);   // flash green/up or red/down, append to sparkline buffer
  }
};

eventSource.onerror = () => {
  // EventSource auto-reconnects using the `retry: 1000` directive above.
  // Surface a "reconnecting" status dot in the header here.
};
```

### Design points

- **Version-gated sends** — only serializes/pushes when `price_cache.version` changes,
  avoiding redundant payloads when the source is slow (Massive).
- **`retry: 1000`** — instructs `EventSource` to reconnect after 1s on a drop. Reconnection
  is otherwise fully automatic in the browser.
- **`X-Accel-Buffering: no`** — disables nginx buffering if the app is ever proxied, so
  events aren't held back.
- **Disconnect detection** — `request.is_disconnected()` ends the generator cleanly when
  the client goes away.

---

## 12. Public API — `__init__.py`

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

Downstream code imports only from `app.market`:

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

The market data system starts and stops with the app via the `lifespan` context manager.
This is the integration point the rest of the backend (portfolio, watchlist, chat) builds
on.

```python
# backend/app/main.py
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

    # Load the watchlist from SQLite (seeded with the 10 defaults on first run)
    initial_tickers = await load_watchlist_tickers()
    await source.start(initial_tickers)

    # Mount the SSE router (factory injects the cache, no globals)
    app.include_router(create_stream_router(price_cache))

    yield  # app runs

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency providers for other routers
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Consuming the cache and source from other routes

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
    # ... fill at current_price, update positions, snapshot portfolio ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.add_watchlist_entry(payload.ticker)
    await source.add_ticker(payload.ticker)  # start tracking immediately
```

---

## 14. Watchlist Coordination

When the watchlist changes (REST or LLM chat), the active source must be told to track the
right tickers.

### Adding a ticker

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT into watchlist (SQLite)
  → await source.add_ticker("PYPL")
       Simulator: add to GBMSimulator, rebuild Cholesky, seed cache (price available now)
       Massive:   append to ticker list, appears on the next poll
  → return ticker (+ current price if available)
```

### Removing a ticker

```
DELETE /api/watchlist/PYPL
  → DELETE from watchlist (SQLite)
  → await source.remove_ticker("PYPL")
       Simulator: remove from GBMSimulator, rebuild Cholesky, drop from cache
       Massive:   drop from ticker list, drop from cache
```

### Edge case — removing a ticker you still hold

If the user removes a ticker from the watchlist while still holding shares, keep tracking it
so portfolio valuation stays accurate:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)   # only stop tracking if not held

    return {"status": "ok"}
```

---

## 15. Testing Strategy

Tests live in `backend/tests/market/` and run under `pytest` with `asyncio_mode = "auto"`.
The suite is 73 tests across 6 modules.

| Module | Focus |
|--------|-------|
| `test_models.py` | `PriceUpdate` change/percent/direction math, `to_dict`, immutability |
| `test_cache.py` | update/get/get_all/remove, first-update-is-flat, version increments, direction |
| `test_simulator.py` | `GBMSimulator`: positivity, add/remove, Cholesky rebuild, empty step, drift over time |
| `test_simulator_source.py` | `SimulatorDataSource` async lifecycle: start seeds cache, prices move, clean double-stop |
| `test_factory.py` | env-var selection (key set → Massive, absent → Simulator) |
| `test_massive.py` | `_poll_once` with mocked snapshots, malformed-snapshot skip, poll-error resilience |

### Representative simulator tests

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


def test_prices_are_positive():
    """GBM is multiplicative (exp() > 0), so price can never go negative."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0


def test_initial_price_matches_seed():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]


def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None          # 1 ticker → no matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None      # 2 tickers → matrix exists


def test_full_default_watchlist_decomposes():
    """The 10-ticker default correlation matrix must be positive semi-definite."""
    sim = GBMSimulator(tickers=list(SEED_PRICES))
    assert sim.step().keys() == SEED_PRICES.keys()
```

### Async source lifecycle

```python
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
async def test_start_seeds_cache_then_stops_clean():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL", "GOOGL"])

    assert cache.get("AAPL") is not None      # seeded before first loop tick
    await asyncio.sleep(0.2)                   # a few cycles

    await source.stop()
    await source.stop()                        # idempotent — must not raise
```

### Massive client (mocked — no network)

Mock `_fetch_snapshots` so the synchronous REST call is never made:

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _snap(ticker, price, ts_ms):
    s = MagicMock()
    s.ticker = ticker
    s.last_trade.price = price
    s.last_trade.timestamp = ts_ms
    return s


@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="x", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()           # presence check in _poll_once
    source._tickers = ["AAPL", "GOOGL"]
    snaps = [_snap("AAPL", 190.50, 1707580800000), _snap("GOOGL", 175.25, 1707580800000)]

    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("GOOGL") == 175.25


@pytest.mark.asyncio
async def test_poll_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="x", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network")):
        await source._poll_once()          # must swallow, not raise

    assert cache.get_price("AAPL") is None
```

> Use a long `poll_interval` (e.g. 60s) in unit tests and call `_poll_once()` directly so
> the background loop never fires during the test.

### Coverage profile

Overall 84%. `simulator.py` ~98% and `cache.py`/`models.py`/`factory.py`/`interface.py` at
100%. `massive_client.py` is ~56% (the real network methods can't run without live API
access) and `stream.py` is lower because the SSE generator needs a running ASGI server; an
`httpx.AsyncClient` integration test against the app is the recommended way to raise it.

---

## 16. Error Handling & Edge Cases

**Empty watchlist at startup** — `start([])` is safe: the simulator produces no prices, the
Massive poller skips its API call, and the SSE stream sends nothing until a ticker is added.

**Trade before a price exists** — a ticker added to the watchlist might not have a cached
price yet (Massive hasn't polled). Trade routes must guard:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Try again in a moment.")
```

The simulator avoids this gap entirely by seeding the cache inside `add_ticker()`.

**Invalid Massive API key** — the first poll 401s; it's logged and the poller keeps
running. SSE stays "connected" but carries no data until the key is fixed and the app
restarts.

**Stale prices on outage** — if Massive is unreachable, the cache keeps the last-known
prices and SSE keeps streaming them. Stale data beats a blank board.

**No-GIL caveat** — `PriceCache.version` is read without the lock. On CPython with the GIL
an `int` read is atomic, so this is safe today; on a future free-threaded build it would be
a minor race worth revisiting.

**Numerical stability** — GBM prices stay positive (multiplicative `exp`), and every price
is `round(..., 2)` when written to the cache, so floating-point drift never accumulates in
displayed values.

---

## 17. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | env var | `""` | Set → Massive API; empty → simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick interval |
| `event_probability` | `GBMSimulator` / `SimulatorDataSource` | `0.001` | Shock chance per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive REST poll interval |
| `interval` (SSE) | `_generate_events` | `0.5` s | SSE push cadence |
| SSE retry | `_generate_events` | `1000` ms | `EventSource` reconnect delay |

---

## 18. Dependencies

From `backend/pyproject.toml` (Python ≥ 3.12):

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",        # GBM vectors + Cholesky decomposition
    "massive>=1.0.0",      # Polygon.io REST client (real market data)
    "rich>=13.0.0",        # terminal demo dashboard
]

[project.optional-dependencies]
dev = ["pytest>=8.3.0", "pytest-asyncio>=0.24.0", "pytest-cov>=5.0.0", "ruff>=0.7.0"]

[tool.hatch.build.targets.wheel]
packages = ["app"]        # required so `uv sync` can build the wheel
```

`massive` is a hard dependency (not optional), which is why `massive_client.py` imports it
at module level. The factory still guarantees the Massive client is only *instantiated*
when `MASSIVE_API_KEY` is present — simulator-only runs never touch the network.

### Try it

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v        # run the 73-test suite
uv run market_data_demo.py          # live terminal dashboard of simulated prices
```
