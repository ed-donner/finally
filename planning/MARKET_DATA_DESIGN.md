# Market Data Backend — Detailed Design

This document is the implementation-level reference for the FinAlly market data subsystem: the unified `MarketDataSource` interface, the `PriceCache`, the GBM simulator, the Massive (Polygon.io) API client, and the SSE streaming endpoint that serves prices to the frontend.

**Status:** This subsystem is already built, tested, and reviewed — see `planning/MARKET_DATA_SUMMARY.md` for the outcome and `planning/archive/` for the original design/review trail. This document reflects the code as it actually ships in `backend/app/market/` (73 passing tests, 84% coverage), corrected for every issue raised in the code review (public `GBMSimulator.get_tickers()`, top-level `massive` import, `AsyncGenerator` return type on the SSE generator, `pyproject.toml` wheel packaging, etc.). Treat it as the contract for any code that consumes market data — portfolio valuation, trade execution, watchlist management, the chat/LLM layer — none of which exists yet.

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
12. [FastAPI Lifecycle Integration (for downstream code)](#12-fastapi-lifecycle-integration-for-downstream-code)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Architecture at a Glance

```
                    ┌─────────────────────────┐
                    │   MarketDataSource (ABC)  │
                    └────────────┬─────────────┘
                     ┌───────────┴───────────┐
                     ▼                       ▼
          SimulatorDataSource        MassiveDataSource
          (GBM, default, no key)     (Polygon.io REST poll, needs key)
                     │                       │
                     └───────────┬───────────┘
                                 ▼
                          PriceCache
                (thread-safe, in-memory, versioned)
                                 │
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
     SSE /api/stream/prices  Portfolio valuation  Trade execution
```

Both data sources implement the identical `MarketDataSource` contract and **push** prices into a shared `PriceCache`; nothing downstream needs to know which source is active. `create_market_data_source()` selects the implementation once, at process startup, based on `MASSIVE_API_KEY`.

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
  market_data_demo.py          # Rich terminal demo (`uv run market_data_demo.py`)
```

Every downstream module imports from the package root — `from app.market import ...` — never from a submodule directly.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the *only* type that leaves the market data layer. SSE payloads, portfolio valuation, trade execution — everything downstream works exclusively with this dataclass.

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

**Usage example:**

```python
>>> from app.market.models import PriceUpdate
>>> u = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00)
>>> u.change, u.change_percent, u.direction
(0.5, 0.2632, 'up')
>>> u.to_dict()
{'ticker': 'AAPL', 'price': 190.5, 'previous_price': 190.0, 'timestamp': 1735689600.0,
 'change': 0.5, 'change_percent': 0.2632, 'direction': 'up'}
```

### Design decisions

- **`frozen=True`** — price updates are immutable value objects, safe to hand across async tasks / threads without copying.
- **`slots=True`** — memory optimization; the app creates many of these per second.
- **Computed properties** (`change`, `change_percent`, `direction`) are derived from `price`/`previous_price`, so they can never drift out of sync with the values that produced them.
- **`to_dict()`** is the single serialization point shared by the SSE endpoint and any future REST responses that embed price data.

---

## 4. Price Cache — `cache.py`

The central data hub: producers (data sources) write, consumers (SSE, portfolio valuation, trade execution) read. Must be thread-safe because the Massive client's synchronous HTTP calls run via `asyncio.to_thread()`, i.e., in a real OS thread, not just an asyncio task.

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

**Usage examples — every function:**

```python
from app.market.cache import PriceCache

cache = PriceCache()

# update() — write a price, get back the computed PriceUpdate
u1 = cache.update("AAPL", 190.00)
print(u1.direction)                 # "flat" (no prior price)
u2 = cache.update("AAPL", 191.25)
print(u2.direction, u2.change)      # "up" 1.25

# get() — single ticker, or None
cache.get("AAPL")                   # PriceUpdate(...)
cache.get("ZZZZ")                   # None

# get_price() — convenience float accessor (used by trade execution)
cache.get_price("AAPL")             # 191.25
cache.get_price("ZZZZ")             # None

# get_all() — snapshot dict for SSE payloads
cache.update("GOOGL", 175.00)
cache.get_all()                     # {"AAPL": PriceUpdate(...), "GOOGL": PriceUpdate(...)}

# version — monotonic counter for cheap "did anything change?" checks
before = cache.version
cache.update("AAPL", 192.00)
assert cache.version == before + 1

# remove() — drop a ticker (e.g. removed from watchlist, no open position)
cache.remove("GOOGL")
assert cache.get("GOOGL") is None

# len() / in
len(cache)          # 1
"AAPL" in cache      # True
```

### Why a version counter?

The SSE loop polls the cache every ~500ms. Without a version counter it would re-serialize and re-send every price on every tick even when nothing changed (e.g., the Massive source only updates every 15s). The counter lets the SSE loop skip a send cheaply:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock`, not `asyncio.Lock`, because:
- The Massive client's synchronous `get_snapshot_all()` runs inside `asyncio.to_thread()`, which uses a real OS thread — an `asyncio.Lock` would not protect against that.
- `threading.Lock` is safe to acquire from both a plain thread and the asyncio event loop.
- The critical sections are tiny (dict read/write), so contention is negligible at this scale (≤ a few dozen tickers, sub-second cadence).

---

## 5. Abstract Interface — `interface.py`

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

**Every implementation's methods, called generically** (this is exactly how downstream code — the future watchlist/portfolio routes — should interact with the layer; it never needs an `isinstance` check):

```python
from app.market import PriceCache, create_market_data_source

async def demo(source: "MarketDataSource"):
    await source.start(["AAPL", "GOOGL", "MSFT"])
    await source.add_ticker("TSLA")
    print(source.get_tickers())          # ["AAPL", "GOOGL", "MSFT", "TSLA"]
    await source.remove_ticker("GOOGL")
    print(source.get_tickers())          # ["AAPL", "MSFT", "TSLA"]
    await source.stop()
```

### Why the source writes to the cache instead of returning prices

This push model decouples timing. The simulator ticks at 500ms; Massive polls at 15s; SSE always reads from the cache on its own 500ms cadence. SSE never needs to know which source is active or how often it updates.

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond typing. Shared by the simulator (initial prices + GBM parameters) and available as fallback data for any future feature that wants a plausible starting price for a newly-added ticker.

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
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},    # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # Low volatility (payments)
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
INTRA_TECH_CORR = 0.6      # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR = 0.3     # Between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing
```

> Note: the original design draft also defined a `DEFAULT_CORR` constant that duplicated `CROSS_GROUP_CORR` and was never referenced — the code review flagged this as dead/confusing, and it was removed. `CROSS_GROUP_CORR` alone now covers "cross-sector" and "unknown ticker" correlation.

A ticker not present in `SEED_PRICES` (e.g., one a user adds via the watchlist or the LLM) gets a random seed price in `$50–$300` and falls back to `DEFAULT_PARAMS` — see `GBMSimulator._add_ticker_internal` below.

---

## 7. GBM Simulator — `simulator.py`

Two classes live here:
- **`GBMSimulator`** — pure math engine, stateful, holds current prices and steps them forward.
- **`SimulatorDataSource`** — the `MarketDataSource` implementation wrapping `GBMSimulator` in an async loop that writes to the `PriceCache`.

### 7.1 `GBMSimulator` — the math engine

**Model.** Geometric Brownian Motion, the standard continuous-time model for stock prices (also underlies Black-Scholes):

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

where `S(t)` is price, `mu` is annualized drift, `sigma` is annualized volatility, `dt` is the time step as a fraction of a trading year, and `Z` is a (correlated) standard normal draw. Because the formula is multiplicative through `exp()`, prices can never go negative.

For 500ms ticks over a 252-day, 6.5-hour trading year: `dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8` — tiny enough to produce realistic sub-cent moves per tick that accumulate naturally.

**Correlated moves.** Real stocks don't move independently. A Cholesky decomposition `L = cholesky(C)` of a sector-based correlation matrix `C` turns independent normal draws into correlated ones: `Z_correlated = L @ Z_independent`. Tech stocks correlate at 0.6, finance at 0.5, everything else (including TSLA, deliberately) at 0.3.

**Random events.** Each tick, each ticker has a `0.1%` chance of a sudden 2–5% move — visual drama for the demo, without destabilizing the price path (with 10 tickers at 2 ticks/sec, expect one roughly every 50 seconds).

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

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

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
                corr[i, j] = corr[j, i] = rho

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
```

**Usage example — every public method:**

```python
from app.market.simulator import GBMSimulator

sim = GBMSimulator(tickers=["AAPL", "GOOGL", "TSLA"])

sim.get_price("AAPL")          # 190.0  (seed price, before any step())
sim.get_tickers()              # ["AAPL", "GOOGL", "TSLA"]

prices = sim.step()            # {"AAPL": 190.01, "GOOGL": 174.98, "TSLA": 250.34}

sim.add_ticker("MSFT")         # starts at SEED_PRICES["MSFT"] = 420.0
sim.step()                     # now includes "MSFT"

sim.remove_ticker("GOOGL")
sim.step()                     # "GOOGL" no longer in result

sim.add_ticker("ZZZZ")         # unknown ticker → random seed in [50, 300], DEFAULT_PARAMS
```

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

**Usage example — full lifecycle:**

```python
import asyncio
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource

async def main():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.5)

    await source.start(["AAPL", "GOOGL", "MSFT"])
    print(cache.get_price("AAPL"))     # available immediately, no wait for first tick

    await asyncio.sleep(2)             # ~4 ticks have run
    print(cache.get_price("AAPL"))     # has drifted from the seed

    await source.add_ticker("TSLA")
    print(source.get_tickers())        # ["AAPL", "GOOGL", "MSFT", "TSLA"]

    await source.remove_ticker("GOOGL")
    print(cache.get("GOOGL"))          # None — removed from cache too

    await source.stop()

asyncio.run(main())
```

### Key behaviors

- **Immediate seeding** — `start()` populates the cache with seed prices *before* the loop begins, so SSE has data on its very first tick — no blank-screen delay.
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`, for clean shutdown during FastAPI lifespan teardown.
- **Exception resilience** — the loop catches exceptions per-step so one bad tick never kills the whole feed.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on a configurable interval. The synchronous `massive` client runs inside `asyncio.to_thread()` so it never blocks the event loop.

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
            # Synchronous client — run in a thread to avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> seconds
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

**Usage example — full lifecycle (requires `MASSIVE_API_KEY`):**

```python
import asyncio
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource

async def main():
    cache = PriceCache()
    source = MassiveDataSource(
        api_key="pk_live_xxx",
        price_cache=cache,
        poll_interval=15.0,   # free tier
    )

    await source.start(["AAPL", "GOOGL", "MSFT"])  # blocks briefly for the first poll
    print(cache.get_price("AAPL"))                 # real market price

    await source.add_ticker("TSLA")   # appears on the *next* poll cycle, not instantly
    await source.remove_ticker("GOOGL")

    await source.stop()

asyncio.run(main())
```

**Testing without a real API key** — mock `_fetch_snapshots` directly, bypassing the network:

```python
from unittest.mock import MagicMock, patch

def make_snapshot(ticker, price, ts_ms):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    return snap

source = MassiveDataSource(api_key="test", price_cache=cache, poll_interval=60.0)
with patch.object(source, "_fetch_snapshots", return_value=[make_snapshot("AAPL", 190.5, 1707580800000)]):
    await source._poll_once()

assert cache.get_price("AAPL") == 190.5
```

### Error handling philosophy

The Massive poller is deliberately resilient — it never crashes the background task:

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error; poller keeps running (user might fix `.env` and restart). |
| **429 Rate Limited** | Logged as error; next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error; retried automatically on the next cycle. |
| **Malformed snapshot** (e.g. `last_trade` missing) | That ticker is skipped with a warning; other tickers in the same batch still process. |
| **All tickers fail** | Cache retains the last-known prices — SSE keeps streaming stale-but-present data rather than nothing. |

### Why the import is no longer lazy

The original design draft imported `massive` lazily inside `start()` so the package would be optional when only the simulator was used. In the shipped code, `massive` is declared a core dependency in `pyproject.toml` and imported at module top-level — this was one of the fixes from the code review (lazy imports made `RESTClient` un-patchable by name in tests, breaking 5 of the Massive test cases). The simulator path still has zero *runtime* dependency on Massive being reachable; it just means the package is always installed, even if unused.

### Massive REST API reference (fields we consume)

The single call that matters for polling is the multi-ticker snapshot:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    snap.ticker                     # "AAPL"
    snap.last_trade.price           # 190.50 — what we write to the cache
    snap.last_trade.timestamp       # 1707580800000 (Unix ms) — divide by 1000
    snap.day.previous_close         # 189.61 — available for a future "day change" feature
    snap.day.change_percent         # -3.50  — Massive's own computed day change
```

This single call covers the whole watchlist in one request, which is what keeps the free tier's 5 req/min limit workable at a 15s poll interval.

---

## 9. Factory — `factory.py`

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

**Usage example:**

```python
import os
from app.market import PriceCache, create_market_data_source

cache = PriceCache()

os.environ.pop("MASSIVE_API_KEY", None)
source = create_market_data_source(cache)
assert type(source).__name__ == "SimulatorDataSource"

os.environ["MASSIVE_API_KEY"] = "pk_live_xxx"
source = create_market_data_source(cache)
assert type(source).__name__ == "MassiveDataSource"

await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"])
```

This is the **single switch point** in the whole codebase for "which data source are we using" — no other module should branch on `MASSIVE_API_KEY` directly.

---

## 10. SSE Streaming Endpoint — `stream.py`

A FastAPI route that holds a long-lived HTTP connection open and pushes price updates as `text/event-stream`.

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
    yield "retry: 1000\n\n"  # browser retries after 1s if the connection drops

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

**Wire format** the browser receives:

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

**Client-side consumption:**

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
    for (const [ticker, update] of Object.entries(prices)) {
        applyPriceFlash(ticker, update.direction);   // green/red CSS flash
        appendSparklinePoint(ticker, update.price);  // accumulate for the mini-chart
    }
};
eventSource.onerror = () => setConnectionStatus('reconnecting'); // EventSource retries automatically
```

**Registering the router (factory usage):**

```python
from app.market import PriceCache, create_stream_router

cache = PriceCache()
router = create_stream_router(cache)   # returns a configured APIRouter
app.include_router(router)             # registers GET /api/stream/prices
```

### Why poll-and-push instead of event-driven?

The endpoint polls the cache on a fixed interval rather than being notified by the data source. This is simpler and produces evenly-spaced updates, which matters because the frontend accumulates them into sparkline charts — regular spacing keeps those visualizations clean regardless of which data source (500ms simulator vs. 15s Massive poll) is behind the cache.

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

Everything downstream should do exactly this:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

— never `from app.market.simulator import SimulatorDataSource` or similar; the concrete implementation is an internal detail the factory hides.

---

## 12. FastAPI Lifecycle Integration (for downstream code)

Nothing outside `app/market/` exists yet — no `app/main.py`, no portfolio/watchlist/chat routes. This section is the contract the rest of the backend should build against when those are implemented.

The market data system should start and stop with the FastAPI app via the `lifespan` context manager:

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

    initial_tickers = await load_watchlist_tickers()  # reads from SQLite (default: 10 seed tickers)
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

### Accessing market data from other routes (dependency injection)

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
    # ... execute the fill at current_price, update positions/cash in SQLite ...


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

---

## 13. Watchlist Coordination

When the watchlist changes — via the REST API or the LLM chat tool-call flow — the active market data source must be told, so it starts/stops tracking the right tickers.

### Flow: adding a ticker

```
User (or LLM) -> POST /api/watchlist {ticker: "PYPL"}
  -> INSERT into watchlist table (SQLite)
  -> await source.add_ticker("PYPL")
       Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
       Massive:   appends to ticker list, price appears after the next poll (<= 15s)
  -> Return success (ticker + current price if already available)
```

### Flow: removing a ticker

```
User (or LLM) -> DELETE /api/watchlist/PYPL
  -> DELETE from watchlist table (SQLite)
  -> await source.remove_ticker("PYPL")
       Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
       Massive:   removes from ticker list, removes from cache
  -> Return success
```

### Edge case: ticker still has an open position

If a user removes a ticker from the watchlist while still holding shares, the price feed must keep tracking it so portfolio valuation stays accurate. The watchlist route is responsible for this check — `source.remove_ticker()` itself has no notion of positions:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)   # only stop tracking if fully closed

    return {"status": "ok"}
```

---

## 14. Testing Strategy

**Actual results:** 73 tests across 6 modules in `backend/tests/market/`, 84% overall coverage. Run with:

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app --cov-report=term-missing
```

| Module | Tests | Coverage | Notes |
|--------|-------|----------|-------|
| `test_models.py` | 11 | 100% | `PriceUpdate` properties and serialization |
| `test_cache.py` | 13 | 100% | every `PriceCache` method + version semantics |
| `test_simulator.py` | 17 | 98% | `GBMSimulator` math, add/remove, correlation |
| `test_simulator_source.py` | 10 | — | `SimulatorDataSource` integration (real asyncio loop) |
| `test_factory.py` | 7 | 100% | env-var branching |
| `test_massive.py` | 13 | 56% (expected) | `MassiveDataSource`, snapshots mocked — no network |

`stream.py` has no dedicated unit tests (SSE requires a running ASGI server / `httpx.AsyncClient` test client); this is a known gap to close once the SSE endpoint is wired into the real FastAPI app in `app/main.py`.

### 14.1 `GBMSimulator` (excerpt)

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
            assert sim.step()["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_remove_ticker(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result and "AAPL" in result

    def test_unknown_ticker_gets_random_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        assert 50.0 <= sim.get_price("ZZZZ") <= 300.0

    def test_cholesky_rebuilds_on_add(self):
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None       # 1 ticker, no correlation matrix needed
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None   # 2 tickers, matrix now exists

    def test_all_ten_default_tickers_build_valid_correlation_matrix(self):
        """Regression guard: the full default watchlist must produce a valid
        (positive semi-definite) correlation matrix for Cholesky decomposition."""
        sim = GBMSimulator(tickers=list(SEED_PRICES.keys()))
        result = sim.step()
        assert len(result) == 10
```

### 14.2 `PriceCache` (excerpt)

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
        assert cache.update("AAPL", 191.00).direction == "up"
        assert cache.update("AAPL", 189.00).direction == "down"

    def test_version_increments_once_per_update(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None
```

### 14.3 `SimulatorDataSource` (async integration, excerpt)

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
        assert cache.get("AAPL") is not None   # seeded before any loop tick
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
        await source.stop()   # must not raise
```

### 14.4 `MassiveDataSource` (mocked network, excerpt)

```python
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
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
        snaps = [_snapshot("AAPL", 190.50, 1707580800000), _snapshot("GOOGL", 175.25, 1707580800000)]

        with patch.object(source, "_fetch_snapshots", return_value=snaps):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_is_skipped_not_fatal(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL", "BAD"]

        bad = MagicMock(ticker="BAD", last_trade=None)  # triggers AttributeError
        good = _snapshot("AAPL", 190.50, 1707580800000)

        with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_network_error_does_not_crash_the_loop(self):
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
        source._tickers = ["AAPL"]

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()   # must not raise

        assert cache.get_price("AAPL") is None
```

---

## 15. Error Handling & Edge Cases

### 15.1 Startup with an empty watchlist

If SQLite has no watchlist rows, `start([])` is called. Both sources handle this gracefully: the simulator produces no prices, the Massive poller's `_poll_once` returns early (`if not self._tickers ...`). SSE sends nothing until a ticker is added via `source.add_ticker()`, at which point the corresponding cache entry appears on the next cycle.

### 15.2 Price cache miss during a trade

A ticker just added to the watchlist might not have a price yet (Massive hasn't polled, or in a race before `add_ticker` seeds the cache):

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this in practice by seeding synchronously inside `add_ticker()`. The Massive client may have a real gap of up to `poll_interval` seconds — the 400 with a clear message is the correct response, not a retry loop on the server side.

### 15.3 Invalid Massive API key

A bad key fails the first poll with 401. The poller logs the error and keeps retrying every `poll_interval` — it does not crash or exit. The SSE endpoint keeps streaming (connected), just with an empty or stale payload. The user sees a "connected" status dot but no moving prices; the fix is correcting `.env` and restarting the container.

### 15.4 Thread safety under load

`PriceCache`'s `threading.Lock` serializes all reads/writes, but each critical section is a single dict operation. At the project's actual scale (≤ dozens of tickers, sub-second cadence, one SSE client per user since this is single-user), contention is negligible. If this ever became a bottleneck (hundreds of tickers, many concurrent readers), a `ReadWriteLock` would be the fix — not needed here.

### 15.5 Simulator numerical stability

- Prices are rounded to 2 decimals in `GBMSimulator.step()`.
- The exponential formulation `exp(drift + diffusion)` is numerically stable for the tiny `dt` used here.
- Prices are always strictly positive — GBM is multiplicative, `exp()` never returns ≤ 0.

---

## 16. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set and non-empty, use `MassiveDataSource`; otherwise `SimulatorDataSource`. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls (free-tier safe; lower for paid tiers). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event, per ticker, per tick. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, as a fraction of a trading year, for 500ms ticks. |
| SSE push interval | `_generate_events()` | `0.5` s | Time between SSE pushes to a connected client. |
| SSE retry directive | `_generate_events()` | `1000` ms | Browser `EventSource` auto-reconnect delay. |

All of these are constructor/function defaults, not currently read from environment variables beyond `MASSIVE_API_KEY` — if per-deployment tuning is ever needed (e.g., a faster simulator tick for a demo), thread the value through `create_market_data_source()`'s call site in `app/main.py` rather than adding ad-hoc `os.environ.get()` calls inside `app/market/`.
