# Market Data Backend — Detailed Design

**Status:** As-built. Every code snippet in this document is the real implementation in `backend/app/market/` (73 tests passing, 84% coverage), except where a section is explicitly marked **Proposed** or **Not yet implemented**.

This is the implementation reference for the FinAlly market data subsystem: the unified data-source interface, the shared price cache, the GBM simulator, the Massive (Polygon.io) REST client, the factory, the SSE endpoint, and how the rest of the backend plugs into all of it.

Predecessor documents (`MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MASSIVE_API.md`, the pre-implementation `MARKET_DATA_DESIGN.md`, and `MARKET_DATA_REVIEW.md`) live in `planning/archive/` and are superseded by this file.

---

## Table of Contents

1. [Architecture at a Glance](#1-architecture-at-a-glance)
2. [Module Map](#2-module-map)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Unified Interface — `interface.py`](#5-unified-interface--interfacepy)
6. [Seed Prices & Parameters — `seed_prices.py`](#6-seed-prices--parameters--seed_pricespy)
7. [Simulator — `simulator.py`](#7-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming — `stream.py`](#10-sse-streaming--streampy)
11. [Package Surface — `__init__.py`](#11-package-surface--__init__py)
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Consumer Recipes](#14-consumer-recipes)
15. [Testing](#15-testing)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Configuration Reference](#17-configuration-reference)
18. [Known Gaps & Proposed Extensions](#18-known-gaps--proposed-extensions)
19. [Terminal Demo](#19-terminal-demo)

---

## 1. Architecture at a Glance

One producer, one cache, many consumers. The producer is chosen at startup by an environment variable; nothing downstream knows or cares which one is running.

```
              ┌──────────────────────────────┐
              │  create_market_data_source() │   reads MASSIVE_API_KEY
              └──────────────┬───────────────┘
                             │ returns one of
            ┌────────────────┴─────────────────┐
            ▼                                  ▼
  SimulatorDataSource                  MassiveDataSource
  (GBM, 500ms ticks)                   (REST poll, 15s default)
            │                                  │
            └──────────────┬───────────────────┘
                           │  .update(ticker, price, timestamp)
                           ▼
                  ┌─────────────────┐
                  │   PriceCache    │  thread-safe dict + version counter
                  └────────┬────────┘
                           │  .get() / .get_price() / .get_all() / .version
        ┌──────────────────┼──────────────────┬──────────────────┐
        ▼                  ▼                  ▼                  ▼
  SSE endpoint      Portfolio valuation   Trade execution   Snapshot task
  /api/stream/prices   (GET /api/portfolio)  (POST .../trade)  (every 30s)
        │
        ▼
  Browser EventSource → watchlist, sparklines, charts
```

**The three invariants that make this work:**

1. **Data sources push, consumers pull.** A source never returns prices to a caller; it writes into the cache on its own schedule. So a 15-second Massive poll and a 500ms simulator tick look identical to every consumer.
2. **`PriceUpdate` is the only type that crosses the boundary.** Immutable, self-consistent, JSON-serializable.
3. **The cache is the single point of truth.** Trade fills, portfolio valuation, and the SSE stream all read the same value, so a fill price can never disagree with the price the user just saw.

---

## 2. Module Map

```
backend/app/market/
├── __init__.py          Public API re-exports
├── models.py            PriceUpdate (frozen dataclass)
├── cache.py             PriceCache (thread-safe store + version counter)
├── interface.py         MarketDataSource (ABC)
├── seed_prices.py       SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py         GBMSimulator (math) + SimulatorDataSource (async wrapper)
├── massive_client.py    MassiveDataSource (REST poller)
├── factory.py           create_market_data_source()
└── stream.py            create_stream_router() — SSE endpoint

backend/tests/market/
├── test_models.py            11 tests
├── test_cache.py             13 tests
├── test_simulator.py         17 tests
├── test_simulator_source.py  10 tests
├── test_factory.py            7 tests
└── test_massive.py           13 tests
```

Dependency direction is strictly one-way — nothing imports "upward":

```
models.py  ←  cache.py  ←  simulator.py / massive_client.py  ←  factory.py
                  ↑
              stream.py
interface.py  ←  simulator.py / massive_client.py / factory.py
seed_prices.py  ←  simulator.py
```

---

## 3. Data Model — `models.py`

`PriceUpdate` is the only structure that leaves the market data layer.

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

### Why it looks like this

| Choice | Reason |
|---|---|
| `frozen=True` | Value object. Once created it never mutates, so it can be handed to any number of async tasks and SSE generators without defensive copying. |
| `slots=True` | We create ~20 of these per second per ticker set; slots trims per-instance memory and speeds attribute access. |
| `change` / `change_percent` / `direction` as **properties**, not fields | They are derived from `price` and `previous_price`, so they cannot go stale or disagree with the price. A `direction="up"` field on a down-tick is impossible by construction. |
| `to_dict()` on the model | One serialization point shared by the SSE stream and any REST response that embeds a price. The frontend contract is defined in exactly one place. |
| `timestamp` defaults to `time.time()` | Simulator has no natural timestamp; Massive supplies one from the exchange. Both paths produce the same shape. |

### Semantics of `change` — read this before building the frontend

`change` and `change_percent` are **tick-to-tick**, not day-over-day. `previous_price` is the price the cache held on the previous `update()` call — roughly 500ms ago for the simulator, 15s for Massive.

- Use them for the **price flash** (green/red on change) — that is exactly what they mean.
- Do **not** use them for the watchlist's "daily change %" column. See [§18.1](#181-daily-change--not-yet-implemented) for the proposed extension.

### Example

```python
>>> u = PriceUpdate(ticker="AAPL", price=190.55, previous_price=190.42, timestamp=1707580800.5)
>>> u.change, u.change_percent, u.direction
(0.13, 0.0683, 'up')
>>> u.price = 200            # frozen — raises
FrozenInstanceError: cannot assign to field 'price'
```

---

## 4. Price Cache — `cache.py`

The hub. Sources write, everyone else reads.

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

### Rounding happens here, once

`update()` rounds both `price` and `previous_price` to 2 decimals. This is the single rounding point in the system, which matters for trade math: the price the user sees, the price the SSE stream sends, and the price a trade fills at are byte-identical. The simulator keeps unrounded state internally (see §7) so rounding never accumulates into the random walk.

### The version counter

Without it, the SSE loop would re-serialize and re-send the entire price map every 500ms even when nothing changed — which, on the Massive path (15s polls), means ~29 out of every 30 pushes are pure waste.

```python
last_version = -1
while True:
    if price_cache.version != last_version:      # only send when something changed
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

It is a monotonically increasing `int`, bumped inside the write lock on every `update()`. Consumers only ever compare it for inequality — never interpret the magnitude.

### Why `threading.Lock` and not `asyncio.Lock`

The Massive client's synchronous `RESTClient` call runs inside `asyncio.to_thread()`, i.e. a real OS thread from the default executor. An `asyncio.Lock` provides no protection there — it only serializes coroutines on one event loop. `threading.Lock` is correct from both the event loop and a worker thread, and the critical section (one dict lookup plus one assignment) is short enough that contention is unmeasurable at 10 tickers × 2 Hz.

### First update is always `flat`

When a ticker is seen for the first time there is no previous price, so `previous_price` is set to `price`. Direction is `"flat"`, change is `0.0`. The frontend therefore never flashes a spurious green/red on the very first paint.

---

## 5. Unified Interface — `interface.py`

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

### The contract, precisely

| Method | Must | Must not |
|---|---|---|
| `start(tickers)` | Populate the cache with at least one price per ticker **before returning**, then launch the background task | Be called twice on the same instance |
| `stop()` | Cancel the background task and await it; be idempotent | Raise on a second call, or write to the cache afterwards |
| `add_ticker(t)` | Be a no-op if `t` is already tracked | Block for longer than a tick |
| `remove_ticker(t)` | Remove from the tracked set **and** from the cache | Raise if `t` was never tracked |
| `get_tickers()` | Return a **copy** — callers must not be able to mutate internal state | Be async (it's a cheap in-memory read) |

The "populate before returning" rule in `start()` is what removes the blank-screen delay on page load: by the time the app accepts its first HTTP request, `/api/stream/prices` already has a full price map to send.

`get_tickers()` is deliberately synchronous — it's a list copy, and making it `async` would force `await` into route handlers and templates for no benefit.

### Adding a third source

Everything the rest of the app needs is in this ABC. A hypothetical `AlpacaDataSource` or `CsvReplayDataSource` needs only: hold a `PriceCache`, implement the five methods, and get returned from the factory. No consumer changes.

---

## 6. Seed Prices & Parameters — `seed_prices.py`

Constants only — no logic, no imports. Shared by the simulator for initial prices, GBM parameters, and the correlation structure.

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

The parameters are tuned for **visual** realism: TSLA at `sigma=0.50` visibly jumps around while V at `0.17` crawls, which is what a trader expects to see. A ticker not in `SEED_PRICES` (user adds `PYPL` via chat) starts at a random price in `$50–$300` with `DEFAULT_PARAMS`.

To add a ticker to the curated set, add one entry to `SEED_PRICES`, one to `TICKER_PARAMS`, and optionally to a `CORRELATION_GROUPS` set. Nothing else changes.

---

## 7. Simulator — `simulator.py`

Two classes with a clean split: `GBMSimulator` is pure synchronous math with no I/O and no asyncio; `SimulatorDataSource` is the async wrapper that drives it and writes to the cache. This split is what makes the math trivially unit-testable — 17 of the tests never touch an event loop.

### 7.1 The math

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

| Symbol | Meaning | Where it comes from |
|---|---|---|
| `S(t)` | current price | `self._prices[ticker]`, unrounded |
| `μ` | annualized drift | `TICKER_PARAMS[ticker]["mu"]` |
| `σ` | annualized volatility | `TICKER_PARAMS[ticker]["sigma"]` |
| `dt` | timestep as a fraction of a trading year | `0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` |
| `Z` | correlated standard normal | `cholesky @ standard_normal(n)` |

Two properties fall out of this formulation for free: **prices can never go negative** (multiplying by `exp()`, which is strictly positive), and the price distribution is **lognormal**, matching how real equity returns are modelled.

`dt` is derived from wall-clock: 252 trading days × 6.5 hours × 3600 seconds = 5,896,800 trading seconds per year, and a tick is 0.5s of that. At `σ=0.22` this gives a per-tick standard deviation of about `0.22 × √8.48e-8 ≈ 0.0000640`, i.e. ~0.0064% — around 1.2 cents on a $190 stock. Small enough to look like real tape, large enough to see the flash.

### 7.2 Correlated moves via Cholesky

Independent random draws would produce ten tickers wandering in ten unrelated directions, which reads as fake. Real sectors move together. Given a correlation matrix `C`, the Cholesky factor `L` (where `L·Lᵀ = C`) turns independent normals into correlated ones:

```
Z_correlated = L @ Z_independent
```

The matrix is rebuilt whenever the ticker set changes — O(n²) to build, O(n³) to factor, with n < 50, so it is irrelevant next to the 500ms tick budget.

### 7.3 `GBMSimulator` — the math engine

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

Three details that are easy to get wrong:

- **Internal state stays unrounded.** `self._prices[ticker]` holds full float precision; only the *returned* dict is rounded to 2 decimals. Rounding the state would bias the random walk over thousands of ticks.
- **`_add_ticker_internal` vs `add_ticker`.** Construction adds N tickers then factors the matrix **once**. The public `add_ticker` adds one and refactors. Same end state, N-times less linear algebra at startup.
- **The `n <= 1` guard.** `np.linalg.cholesky` on a 1×1 matrix works, but with a single ticker there is nothing to correlate, so `_cholesky` stays `None` and `step()` uses the raw independent draw.

### 7.4 The shock events

```python
if random.random() < self._event_prob:              # 0.001 per ticker per tick
    shock_magnitude = random.uniform(0.02, 0.05)    # 2–5%
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

At 2 ticks/second with 10 tickers, expected rate is `0.001 × 2 × 10 = 0.02/s` — roughly one visible 2–5% jump somewhere on screen every 50 seconds. That's the cadence that makes a demo feel alive without destabilizing the portfolio.

### 7.5 `SimulatorDataSource` — the async wrapper

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

- **Immediate seeding in `start()` and `add_ticker()`** — the cache is populated before the loop even runs, so a ticker added via chat is instantly tradeable rather than "no price available for 500ms".
- **The `try` is inside the `while`, and `sleep` is outside the `try`.** One bad step logs and the loop continues; the feed cannot die from a transient error.
- **`stop()` cancels then awaits**, swallowing `CancelledError`. Idempotent — the `self._task = None` at the end means a second call is a no-op, which the tests assert explicitly.

---

## 8. Massive API Client — `massive_client.py`

Used when `MASSIVE_API_KEY` is set. REST polling, not WebSocket — it works on every tier including free, and needs no reconnection logic.

### 8.1 The API surface we use

**Endpoint:** `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`

One call returns every requested ticker, which is what makes the free tier's 5 req/min workable — the whole watchlist costs one request.

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
```

Per-ticker response shape:

```json
{
  "ticker": "AAPL",
  "day": {
    "open": 129.61, "high": 130.15, "low": 125.07, "close": 125.07,
    "volume": 111237700, "previous_close": 129.61,
    "change": -4.54, "change_percent": -3.50
  },
  "last_trade": {
    "price": 125.07,
    "size": 100, "exchange": "XNYS",
    "timestamp": 1675190399000
  },
  "last_quote": { "bid_price": 125.06, "ask_price": 125.08, "spread": 0.02 },
  "prev_daily_bar": { "…": "previous day OHLCV" }
}
```

| Field | Used for |
|---|---|
| `ticker` | cache key |
| `last_trade.price` | **the price** — display, fills, valuation |
| `last_trade.timestamp` | `PriceUpdate.timestamp` (÷1000) |
| `day.previous_close`, `day.change_percent` | not read today; the natural inputs for the daily-change extension in [§18.1](#181-daily-change--not-yet-implemented) |

**Timestamps are Unix milliseconds** and must be divided by 1000 — `PriceUpdate.timestamp` is seconds everywhere else in the system.

| Tier | Rate limit | Recommended `poll_interval` |
|---|---|---|
| Free | 5 req/min | `15.0` (default) — 4 req/min, leaves headroom |
| Paid | effectively unlimited (stay < 100 req/s) | `2.0`–`5.0` |

### 8.2 Implementation

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

### 8.3 Design notes

**`asyncio.to_thread` is mandatory, not stylistic.** `RESTClient.get_snapshot_all` is a blocking HTTP call. Called directly from the coroutine it would freeze the entire event loop — every SSE stream, every REST request — for the duration of the round trip. Offloading it to the default thread executor keeps the loop responsive, and is precisely why `PriceCache` uses a `threading.Lock` (§4).

**Sleep-then-poll, not poll-then-sleep.** `start()` performs the first poll inline, so `_poll_loop` sleeps first. Two consequences: the cache is warm before `start()` returns (satisfying the interface contract), and there is no duplicate request in the first interval.

**Two-level error handling.**

| Failure | Where it's caught | Behavior |
|---|---|---|
| 401 invalid key | outer `except Exception` | Logged as error; poller keeps retrying (fix `.env`, restart) |
| 429 rate limited | outer | Logged; next poll is one full interval later |
| Network timeout / 5xx | outer (client also retries 3× internally) | Logged; automatic retry next cycle |
| One malformed snapshot | inner `except (AttributeError, TypeError)` | That ticker skipped with a warning; **all others still processed** |
| Everything fails | — | Cache retains last-known prices; SSE keeps streaming slightly stale data, which beats a blank screen |

The inner/outer split is the important part: one bad ticker in the response must never cost you the other nine.

**Imports are top-level.** An earlier revision lazy-imported `massive` inside `start()` to make the package optional. Since `massive>=1.0.0` is a declared core dependency in `pyproject.toml`, the lazy import bought nothing and broke `patch("app.market.massive_client.RESTClient")` in tests. Top-level is correct here.

**Ticker normalization is asymmetric.** `add_ticker`/`remove_ticker` upper-case and strip; `start()` does not. The route layer should normalize before it ever reaches the source — see [§13.3](#133-ticker-normalization).

---

## 9. Factory — `factory.py`

The single place the simulator/live decision is made.

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

`.strip()` matters: `MASSIVE_API_KEY=` and `MASSIVE_API_KEY="   "` in a `.env` both fall through to the simulator, which is what a user who commented out their key expects. The returned source is **unstarted** — the caller owns the lifecycle.

The factory does not read `.env` itself; it reads the process environment. Loading `.env` is the app's job (Docker `--env-file`, or `python-dotenv` at startup) and must happen **before** this is called.

---

## 10. SSE Streaming — `stream.py`

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

First frame on every connection:

```
retry: 1000

```

Then, whenever the cache version has moved:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,"previous_price":175.20,"timestamp":1707580800.5,"change":-0.08,"change_percent":-0.0456,"direction":"down"}}

```

Note the double newline terminating each frame — that is the SSE record separator, and omitting it is the classic reason a stream "works" but the browser never fires `onmessage`.

Each event carries the **full price map**, not a delta. At 10 tickers that's ~1.5 KB per push, twice a second — trivial, and it means a client that reconnects is fully resynced by the first frame it receives, with no replay or sequence-number logic anywhere.

### Headers, and why each one is there

| Header | Purpose |
|---|---|
| `media_type="text/event-stream"` | Required for `EventSource` to accept the response |
| `Cache-Control: no-cache` | Stops any intermediary caching the stream |
| `Connection: keep-alive` | Long-lived connection |
| `X-Accel-Buffering: no` | Disables nginx response buffering; without it a proxied stream arrives in chunks or not at all |

### Client-side usage

```javascript
const es = new EventSource('/api/stream/prices');

es.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // { AAPL: { ticker, price, previous_price, timestamp, change, change_percent, direction }, ... }
  for (const [ticker, update] of Object.entries(prices)) {
    applyPrice(ticker, update);          // flash green/red using update.direction
    appendSparklinePoint(ticker, update.price, update.timestamp);
  }
  setConnectionStatus('connected');      // green dot
};

es.onerror = () => setConnectionStatus('reconnecting');  // yellow dot; EventSource retries on its own
```

The frontend accumulates sparkline history from this stream since page load — there is no historical price endpoint, by design (PLAN.md §2). The 500ms fixed cadence matters here: evenly spaced samples make clean sparklines.

### Why poll-and-push rather than event-driven

The generator polls the cache on a fixed interval instead of being woken by the producer. This decouples the two sides completely: the Massive path updates every 15s and the simulator every 500ms, but the client sees a uniform stream in both cases, and no producer needs to know how many SSE clients exist. Version checking means the 15s case still costs almost nothing.

### Disconnect handling

`request.is_disconnected()` is checked each iteration, so a closed tab ends the generator within one interval instead of leaking a task. `CancelledError` is caught for the shutdown path (server stopping mid-stream) so it logs cleanly rather than dumping a traceback.

---

## 11. Package Surface — `__init__.py`

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

Downstream code imports from `app.market` only:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```

`GBMSimulator`, `SimulatorDataSource`, and `MassiveDataSource` are deliberately **not** exported — nothing outside the package should name a concrete source. (Tests import them directly from their modules, which is the intended exception.)

---

## 12. FastAPI Lifecycle Integration

The market data system starts and stops with the app via the `lifespan` context manager.

```python
# backend/app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""
    # --- STARTUP ---

    # 1. Database first — lazily create schema + seed the default watchlist
    await init_database()

    # 2. Shared price cache
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 3. Data source, chosen by MASSIVE_API_KEY
    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # 4. Start it with whatever the DB says we're watching
    #    (union of watchlist tickers and tickers with open positions)
    initial_tickers = await load_tracked_tickers()
    await source.start(initial_tickers)   # returns with the cache already warm

    yield  # --- app is serving ---

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Routers are registered at import time, not inside lifespan.
# The SSE router closes over a cache created in lifespan, so it needs one
# created here too — see note below.
```

### One ordering subtlety

`create_stream_router(price_cache)` closes over the cache instance, but routers must be registered **before** the app starts serving. Two clean options:

**Option A — create the cache at module scope** (simplest, and what the demo does):

```python
price_cache = PriceCache()
app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

`lifespan` then uses that same module-level instance rather than constructing its own.

**Option B — resolve the cache per request from `app.state`**, and register a router that doesn't need the cache at construction time. More indirection, no real benefit for a single-process app.

Note also that `stream.py` creates its `APIRouter` at module scope, so calling `create_stream_router()` twice registers `/prices` twice on the same router object. Harmless in production (called once), but tests that build multiple apps should either restructure the router into the factory or reuse one router instance. See [§18.2](#182-module-level-router-in-streampy).

### Dependency injection for other routes

```python
from fastapi import Depends, Request

def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

Reading off `request.app.state` (rather than closing over a global `app`) keeps these usable from any router module without circular imports.

---

## 13. Watchlist Coordination

The watchlist lives in SQLite; the tracked-ticker set lives in the data source. Any route that changes one must change the other, in that order.

### 13.1 Add

```
POST /api/watchlist {"ticker": "PYPL"}
  1. normalize → "PYPL"
  2. INSERT INTO watchlist (idempotent via UNIQUE(user_id, ticker))
  3. await source.add_ticker("PYPL")
       Simulator → adds to GBM, rebuilds Cholesky, seeds cache  (price available immediately)
       Massive   → appends to ticker list                        (price on next poll, ≤15s)
  4. return {ticker, price: cache.get_price("PYPL")}   # may be null on the Massive path
```

```python
@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = normalize_ticker(payload.ticker)
    await db.add_watchlist_entry(ticker)
    await source.add_ticker(ticker)
    return {"ticker": ticker, "price": price_cache.get_price(ticker)}
```

### 13.2 Remove — and the open-position trap

A ticker removed from the watchlist but still **held** must keep streaming, or portfolio valuation silently loses that position's price.

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = normalize_ticker(ticker)
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if there is no open position in this ticker
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

The mirror of this rule applies at startup — `load_tracked_tickers()` must return the **union** of watchlist tickers and tickers with a non-zero position:

```python
async def load_tracked_tickers() -> list[str]:
    """Tickers the data source must track: watchlist ∪ open positions."""
    watchlist = await db.get_watchlist_tickers()
    held = await db.get_position_tickers()          # quantity != 0
    return sorted(set(watchlist) | set(held))
```

Without the union, a restart while holding a de-watchlisted position produces a position with no price.

### 13.3 Ticker normalization

Normalize at the route boundary so the DB, the cache, and the source can never disagree on casing:

```python
import re

_TICKER_RE = re.compile(r"^[A-Z][A-Z.\-]{0,9}$")


def normalize_ticker(raw: str) -> str:
    """Uppercase, strip, and validate a ticker symbol."""
    ticker = raw.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise HTTPException(400, f"Invalid ticker symbol: {raw!r}")
    return ticker
```

Apply it in the REST routes **and** in the LLM action executor — the model may well emit `"aapl"`.

### 13.4 Trades and the tracked set

A trade can only execute on a ticker that has a cached price, and the price comes from being tracked. Buying a ticker that isn't on the watchlist therefore requires adding it to the tracked set first (or rejecting the trade). Simplest consistent rule, and the one the LLM path should follow: **any ticker you can trade is a ticker you are tracking.** Add it to the watchlist as part of the buy.

---

## 14. Consumer Recipes

Copy-paste patterns for the routes that consume market data.

### 14.1 Trade execution

```python
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = normalize_ticker(trade.ticker)

    price = price_cache.get_price(ticker)
    if price is None:
        raise HTTPException(
            status_code=400,
            detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
        )

    if trade.quantity <= 0:
        raise HTTPException(400, "Quantity must be positive")

    # Fill instantly at the cached price — the same price the user is looking at
    return await portfolio.execute(ticker=ticker, side=trade.side, quantity=trade.quantity, price=price)
```

Reading the fill price from the same cache the SSE stream reads is what guarantees "the price I clicked is the price I got".

### 14.2 Portfolio valuation

```python
def value_portfolio(positions: list[Position], cash: float, price_cache: PriceCache) -> dict:
    """Mark positions to market using cached prices."""
    prices = price_cache.get_all()          # one consistent snapshot for the whole calculation
    rows, holdings_value = [], 0.0

    for pos in positions:
        update = prices.get(pos.ticker)
        current = update.price if update else pos.avg_cost   # fall back to cost basis
        market_value = current * pos.quantity
        cost_basis = pos.avg_cost * pos.quantity
        holdings_value += market_value

        rows.append({
            "ticker": pos.ticker,
            "quantity": pos.quantity,
            "avg_cost": pos.avg_cost,
            "current_price": current,
            "market_value": round(market_value, 2),
            "unrealized_pnl": round(market_value - cost_basis, 2),
            "pnl_percent": round((current / pos.avg_cost - 1) * 100, 2) if pos.avg_cost else 0.0,
            "stale": update is None,
        })

    return {
        "cash_balance": round(cash, 2),
        "positions": rows,
        "holdings_value": round(holdings_value, 2),
        "total_value": round(cash + holdings_value, 2),
    }
```

Take **one** `get_all()` snapshot per calculation. Calling `get_price()` per position lets prices move mid-loop, so the position rows and the total can disagree.

The `avg_cost` fallback means a missing price shows zero P&L rather than a crash or a nonsense number; `stale: true` lets the UI mark it.

### 14.3 Portfolio snapshot background task

PLAN.md §7 requires a `portfolio_snapshots` row every 30s and after every trade.

```python
async def snapshot_loop(app: FastAPI, interval: float = 30.0) -> None:
    """Record total portfolio value every `interval` seconds."""
    while True:
        try:
            total = await compute_total_value(app.state.price_cache)
            await db.insert_portfolio_snapshot(total_value=total)
        except Exception:
            logger.exception("Portfolio snapshot failed")
        await asyncio.sleep(interval)
```

Started and stopped in `lifespan` alongside the data source, using the same cancel-and-await pattern as `SimulatorDataSource.stop()`.

### 14.4 Watchlist with live prices

```python
@router.get("/watchlist")
async def get_watchlist(price_cache: PriceCache = Depends(get_price_cache)):
    tickers = await db.get_watchlist_tickers()
    prices = price_cache.get_all()
    return [
        {"ticker": t, **(prices[t].to_dict() if t in prices else {"price": None})}
        for t in tickers
    ]
```

This is the initial paint; the SSE stream takes over for updates.

### 14.5 LLM chat context

The chat flow (PLAN.md §9) builds portfolio context from the same cache:

```python
def build_market_context(price_cache: PriceCache, watchlist: list[str]) -> str:
    """Compact price block for the LLM system prompt."""
    prices = price_cache.get_all()
    lines = [
        f"{t}: ${prices[t].price:.2f} ({prices[t].direction})"
        for t in watchlist if t in prices
    ]
    return "Current prices:\n" + "\n".join(lines)
```

---

## 15. Testing

73 tests, all passing; 84% coverage overall.

```bash
cd backend
uv sync --extra dev
uv run --extra dev pytest -v                  # 73 passed
uv run --extra dev pytest --cov=app           # coverage report
uv run --extra dev ruff check app/ tests/     # lint
```

| Module | Tests | Coverage | Notes |
|---|---|---|---|
| `test_models.py` | 11 | `models.py` 100% | properties, frozen-ness, `to_dict()` |
| `test_cache.py` | 13 | `cache.py` 100% | update/get/remove, direction, version counter |
| `test_simulator.py` | 17 | `simulator.py` 98% | GBM math, Cholesky, add/remove, positivity |
| `test_simulator_source.py` | 10 | (integration) | lifecycle, cache seeding, clean stop |
| `test_factory.py` | 7 | `factory.py` 100% | env-var selection incl. empty/whitespace key |
| `test_massive.py` | 13 | `massive_client.py` 56% | mocked REST; real HTTP paths not exercised |
| — | — | `stream.py` 31% | **gap** — no ASGI-level SSE test yet |

`pyproject.toml` sets `asyncio_mode = "auto"`, so async tests need no `@pytest.mark.asyncio` decorator (existing tests carry it anyway, which is harmless).

### 15.1 Math properties worth asserting

The valuable simulator tests are the invariants, not specific numbers:

```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0


def test_cholesky_rebuilds_on_add(self):
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None       # 1 ticker → nothing to correlate
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None   # 2 tickers → matrix exists


def test_full_default_watchlist_factors(self):
    """Cholesky must succeed for the real 10-ticker correlation matrix."""
    sim = GBMSimulator(tickers=list(SEED_PRICES))
    assert len(sim.step()) == 10
```

The last one is the real safety net on `_pairwise_correlation`: an inconsistent correlation structure produces a non-positive-definite matrix and `np.linalg.cholesky` raises `LinAlgError`. Any future edit to the correlation constants is caught here.

### 15.2 Mocking the Massive client

Two things must be faked: the client object (to satisfy the `_poll_once` guard) and the fetch itself.

```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


async def test_poll_updates_cache(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "GOOGL"]
    source._client = MagicMock()          # satisfy the `if not self._client` guard

    snapshots = [
        _make_snapshot("AAPL", 190.50, 1707580800000),
        _make_snapshot("GOOGL", 175.25, 1707580800000),
    ]
    with patch.object(source, "_fetch_snapshots", return_value=snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("GOOGL") == 175.25
```

`poll_interval=60.0` keeps the background loop from firing a second poll mid-test. Patching `_fetch_snapshots` (rather than `RESTClient`) is the cleanest seam: `asyncio.to_thread` happily runs the `MagicMock` in the executor, and no HTTP stack is involved.

The error paths matter as much as the happy one:

```python
async def test_api_error_does_not_crash(self):
    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
        await source._poll_once()        # must not raise
    assert cache.get_price("AAPL") is None


async def test_malformed_snapshot_skipped(self):
    bad = MagicMock(); bad.ticker = "BAD"; bad.last_trade = None
    with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
        await source._poll_once()
    assert cache.get_price("AAPL") == 190.50   # good one still landed
    assert cache.get_price("BAD") is None
```

### 15.3 Async lifecycle tests

```python
async def test_start_populates_cache(self):
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])
    # Contract: cache is warm the moment start() returns, before any loop tick
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()


async def test_stop_is_idempotent(self):
    source = SimulatorDataSource(price_cache=PriceCache(), update_interval=0.1)
    await source.start(["AAPL"])
    await source.stop()
    await source.stop()          # second call must not raise
```

Use a short `update_interval` (0.05–0.1s) in tests, and always `await source.stop()` — a leaked task keeps writing into a dead test's cache and produces confusing cross-test failures.

### 15.4 Proposed: SSE integration test

**Not yet implemented.** `stream.py` sits at 31% coverage; this closes it using `httpx`'s ASGI transport, no live server needed.

```python
import json

import httpx
import pytest
from fastapi import FastAPI

from app.market import PriceCache, create_stream_router


@pytest.mark.asyncio
async def test_sse_stream_emits_prices():
    cache = PriceCache()
    cache.update("AAPL", 190.00)

    app = FastAPI()
    app.include_router(create_stream_router(cache))

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")

            frames = []
            async for line in response.aiter_lines():
                if line.startswith("retry:"):
                    frames.append(line)
                elif line.startswith("data: "):
                    payload = json.loads(line.removeprefix("data: "))
                    assert payload["AAPL"]["price"] == 190.00
                    assert payload["AAPL"]["direction"] == "flat"
                    break        # one data frame is enough; the loop is infinite

            assert frames[0].startswith("retry:")
```

Requires `httpx` (already present transitively via `fastapi`/`starlette`'s test client, but add it to the `dev` extra to depend on it explicitly). Break out of the iteration explicitly — the generator never terminates on its own.

### 15.5 Proposed: cache thread-safety test

**Not yet implemented.** Exercises the lock the way the Massive path actually does — from real OS threads.

```python
from concurrent.futures import ThreadPoolExecutor


def test_concurrent_writes_are_consistent():
    cache = PriceCache()
    tickers = [f"T{i}" for i in range(10)]

    def hammer(ticker: str) -> None:
        for i in range(1000):
            cache.update(ticker, 100.0 + i * 0.01)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(hammer, tickers))

    assert len(cache) == 10
    assert cache.version == 10_000          # every write counted exactly once
```

---

## 16. Error Handling & Edge Cases

### 16.1 Empty watchlist at startup

`start([])` is valid on both sources. The simulator's `step()` returns `{}` immediately (the `n == 0` guard); the Massive poller's `_poll_once` returns early on `not self._tickers` and burns no API quota. `_generate_events` skips the send when `prices` is empty, so the client gets `retry: 1000` and then silence until the user adds a ticker — at which point the source picks it up on the next cycle with no restart.

### 16.2 Trade on a ticker with no cached price

Only reachable on the Massive path, in the ≤15s window between adding a ticker and the next poll (the simulator seeds synchronously in `add_ticker`). Return a clear 400:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Please wait a moment and try again.")
```

Do **not** invent a price. Do not fall back to a seed. A fabricated fill price corrupts the position's cost basis permanently.

### 16.3 Invalid Massive API key

First poll fails with 401, logged as an error, and the poller keeps retrying every interval. From the user's perspective the SSE connection is healthy (green dot) but no prices arrive. That's deliberate — the alternative, crashing the app at startup, is worse for a student who mistyped a key. The log line is the diagnostic:

```
ERROR app.market.massive_client: Massive poll failed: 401 Unauthorized
```

If real prices matter more than uptime, a startup validation call could fail fast instead; the current choice favors "app always starts".

### 16.4 Market closed

Massive's `last_trade.price` returns the last traded price outside market hours, so the cache holds a valid but static price. Version stops incrementing, the SSE loop stops pushing, and the UI shows flat prices with no flashing. This is correct behavior, but a first-time user may read a frozen screen as a bug — worth a "market closed" hint in the UI if the live path is the common case. The simulator has no market hours and runs continuously.

### 16.5 Removing a ticker mid-stream

`remove_ticker` drops the ticker from both the source and the cache. The next SSE frame simply omits that key. Clients must treat a missing ticker as "stop displaying", not "price unchanged" — a client that only merges incoming keys into local state will keep a removed ticker on screen forever.

### 16.6 Float precision

Prices are rounded to 2 decimals at the cache boundary, but quantities are floats (fractional shares) and portfolio math accumulates. Round for display only; if exact cent-level accounting ever matters, `Decimal` in the portfolio layer is the fix — the market layer's job is only to publish a correctly rounded price.

### 16.7 Simulator drift over long sessions

`mu` is annualized, so over a multi-hour session the drift term is negligible next to the diffusion term — prices wander but don't systematically inflate. Shock events are sign-symmetric (`random.choice([-1, 1])`), so they add variance without bias. A container left running for days will drift meaningfully from seed prices; that is expected GBM behavior, and restarting resets to seeds.

### 16.8 Multiple browser tabs

Each tab opens its own SSE connection and its own `_generate_events` task, all reading the same cache. There is no per-client state and no fan-out registry, so N tabs cost N poll loops and nothing else. Fine for single-user; a real multi-user deployment would want one shared broadcast task instead of one per connection.

---

## 17. Configuration Reference

| Parameter | Location | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | env var | `""` | Non-empty → Massive API; empty/absent → simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick rate |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | `0.001` | Shock chance per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `≈8.48e-8` | GBM timestep (fraction of a trading year) |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive REST poll cadence (free tier safe) |
| `interval` | `_generate_events` | `0.5` s | SSE push cadence |
| retry directive | `_generate_events` | `1000` ms | Browser `EventSource` reconnect delay |
| `SEED_PRICES` | `seed_prices.py` | 10 tickers | Simulator starting prices |
| `TICKER_PARAMS` | `seed_prices.py` | per-ticker σ/μ | Simulator volatility and drift |

Only `MASSIVE_API_KEY` is environment-driven. Everything else is a constructor default — deliberately, since these are tuning knobs, not deployment config. To change one, pass it at the construction site in `factory.py`:

```python
# e.g. a paid Massive tier polling every 3 seconds
return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=3.0)
```

### Dependencies

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",      # Cholesky decomposition + normal draws
    "massive>=1.0.0",    # Polygon.io REST client
    "rich>=13.0.0",      # terminal demo only
]

[tool.hatch.build.targets.wheel]
packages = ["app"]       # required — uv sync fails without it
```

---

## 18. Known Gaps & Proposed Extensions

Everything in this section is **not implemented**. Listed so downstream agents know what they're inheriting.

### 18.1 Daily change % — not yet implemented

PLAN.md §10 requires a **daily change %** column in the watchlist. `PriceUpdate.change_percent` is tick-to-tick (§3) and is not that number. Two options:

**Simulator path** — record each ticker's session-open price and compute against it:

```python
# in PriceCache
def __init__(self) -> None:
    ...
    self._session_open: dict[str, float] = {}

def update(self, ticker, price, timestamp=None) -> PriceUpdate:
    with self._lock:
        ...
        self._session_open.setdefault(ticker, round(price, 2))
        ...

def day_change_percent(self, ticker: str) -> float | None:
    """Percent change since this ticker's first price of the session."""
    with self._lock:
        open_price = self._session_open.get(ticker)
        current = self._prices.get(ticker)
    if open_price is None or current is None or open_price == 0:
        return None
    return round((current.price - open_price) / open_price * 100, 2)
```

**Massive path** — the API already provides it: read `snap.day.previous_close` and `snap.day.change_percent` in `_poll_once` and store them alongside the price.

The clean unification is an optional `day_open: float | None` field on `PriceUpdate` (populated from session-open for the simulator, from `day.previous_close` for Massive) plus a `day_change_percent` property, so `to_dict()` carries it and the frontend reads one field regardless of source. That is an additive change — existing consumers are unaffected.

### 18.2 Module-level router in `stream.py`

`router = APIRouter(...)` is created at import time and `create_stream_router()` attaches `/prices` to it. Calling the factory twice (two test apps in one session) registers the route twice. Fix is one line — move construction inside the factory:

```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    ...
    return router
```

Deferred because it's latent, not live: production calls it exactly once.

### 18.3 `PriceCache.version` read outside the lock

```python
@property
def version(self) -> int:
    return self._version        # no lock
```

Reading an `int` is atomic under CPython's GIL, so this is safe today and the SSE loop only tests for inequality. On a free-threaded build (PEP 703) it would be a torn-read risk. Wrapping it in `with self._lock:` costs nothing measurable if the project ever targets 3.13t.

### 18.4 Asymmetric ticker normalization

`MassiveDataSource.add_ticker`/`remove_ticker` upper-case and strip; `start()` and every `SimulatorDataSource` method do not. Mixed-case tickers reaching the source produce duplicate cache keys (`aapl` and `AAPL`). Normalizing at the route boundary (§13.3) fully covers this; pushing `normalize_ticker` into the sources themselves would make it defense-in-depth.

### 18.5 SSE coverage

`stream.py` is the primary consumer of `PriceCache` and has no ASGI-level test. §15.4 has a ready-to-use one.

### 18.6 Historical bars

Massive exposes `list_aggs()` for OHLCV history, and PLAN.md deliberately does not use it — sparklines and the main chart accumulate from the SSE stream since page load. If a "1D / 1W / 1M" chart selector is ever added, `list_aggs` is the endpoint, and it needs a new interface method (the simulator would have to backfill synthetic history to match).

---

## 19. Terminal Demo

`backend/market_data_demo.py` runs the whole subsystem headless — useful for eyeballing simulator behavior without the frontend.

```bash
cd backend
uv run market_data_demo.py
```

A Rich live dashboard: all 10 default tickers, sparklines, color-coded direction arrows, and an event log for notable moves. Runs 60 seconds or until Ctrl+C. It exercises exactly the production path — `PriceCache` + `create_market_data_source()` + `start()`/`stop()` — so if the demo looks right, the SSE stream will too.

The minimal version of what it does:

```python
import asyncio

from app.market import PriceCache, create_market_data_source

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


async def main() -> None:
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(DEFAULT_TICKERS)
    try:
        for _ in range(120):                        # 60 seconds at 500ms
            for ticker, update in sorted(cache.get_all().items()):
                arrow = {"up": "▲", "down": "▼", "flat": "="}[update.direction]
                print(f"{ticker:6} {update.price:8.2f} {arrow} {update.change:+.2f}")
            await asyncio.sleep(0.5)
    finally:
        await source.stop()                          # always stop, even on Ctrl+C


asyncio.run(main())
```

Set `MASSIVE_API_KEY` before running to exercise the live path instead of the simulator — no code change required. That interchangeability is the whole point of the design.
