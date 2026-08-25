# Market Data Backend — Detailed Design

**Component:** `backend/app/market/`
**Status:** Implemented and tested. This document is the authoritative design reference for the market data subsystem — the contract other agents build against.

This document specifies, with working code, everything needed to implement the market data layer of FinAlly:

- a **unified API** (`MarketDataSource` ABC + `PriceCache`) that makes the price source interchangeable,
- a **GBM simulator** used by default, with correlated moves and random shock events,
- a **Massive (Polygon.io) REST client** used when `MASSIVE_API_KEY` is present,
- the **SSE endpoint** that pushes prices to the browser, and
- the **lifecycle, watchlist, testing and failure behavior** that ties it together.

---

## Table of Contents

1. [Design Goals](#1-design-goals)
2. [Architecture](#2-architecture)
3. [File Structure](#3-file-structure)
4. [Data Model — `PriceUpdate`](#4-data-model--priceupdate)
5. [Price Cache](#5-price-cache)
6. [Unified API — `MarketDataSource`](#6-unified-api--marketdatasource)
7. [Seed Prices & Ticker Parameters](#7-seed-prices--ticker-parameters)
8. [Simulator — `GBMSimulator`](#8-simulator--gbmsimulator)
9. [Simulator — `SimulatorDataSource`](#9-simulator--simulatordatasource)
10. [Massive API — `MassiveDataSource`](#10-massive-api--massivedatasource)
11. [Factory](#11-factory)
12. [SSE Streaming Endpoint](#12-sse-streaming-endpoint)
13. [FastAPI Lifecycle Integration](#13-fastapi-lifecycle-integration)
14. [Watchlist Coordination](#14-watchlist-coordination)
15. [Consuming Prices Downstream](#15-consuming-prices-downstream)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Testing Strategy](#17-testing-strategy)
18. [Configuration Reference](#18-configuration-reference)
19. [Demo Harness](#19-demo-harness)
20. [Open Design Items](#20-open-design-items)

---

## 1. Design Goals

| Goal | How it is met |
|---|---|
| The rest of the app must not know where prices come from | One ABC (`MarketDataSource`), one data model (`PriceUpdate`), one read surface (`PriceCache`) |
| Zero-config default | No API key → GBM simulator, in-process, no network |
| Real data when available | `MASSIVE_API_KEY` set → REST poller, same interface |
| Prices must feel alive | ~500ms ticks, correlated sector moves, occasional 2–5% shocks |
| Never crash the app | Every background loop catches and logs; a failed poll retries on the next interval |
| Cheap to read | Latest price per ticker only — memory is O(tickers), reads are a dict lookup under a lock |
| Testable without a network | Simulator is deterministic under a seeded RNG; Massive client is fully mockable |

**Non-goals:** order books, historical bars in the cache, tick-by-tick persistence, multi-user fan-out. The cache is a latest-price store; history for the P&L chart lives in `portfolio_snapshots` (see `PLAN.md` §7), and watchlist sparklines are accumulated client-side from the SSE stream.

---

## 2. Architecture

```
                    ┌──────────────────────────────────┐
                    │   create_market_data_source()    │
                    │   reads MASSIVE_API_KEY          │
                    └───────────────┬──────────────────┘
                                    │ selects one
              ┌─────────────────────┴─────────────────────┐
              ▼                                           ▼
  ┌───────────────────────┐                  ┌────────────────────────┐
  │  SimulatorDataSource  │                  │   MassiveDataSource    │
  │  GBM, ~500ms tick     │                  │   REST poll, ~15s      │
  │  in-process asyncio   │                  │   asyncio.to_thread    │
  └───────────┬───────────┘                  └───────────┬────────────┘
              │            both implement                │
              │            MarketDataSource              │
              └─────────────────┬────────────────────────┘
                                │ .update(ticker, price, ts)
                                ▼
                    ┌───────────────────────┐
                    │      PriceCache       │  latest price per ticker
                    │  dict + Lock + version│  single point of truth
                    └───────────┬───────────┘
                                │ .get() / .get_all() / .get_price()
          ┌─────────────────────┼─────────────────────┐
          ▼                     ▼                     ▼
   SSE /api/stream/prices   Trade execution     Portfolio valuation
   (500ms, version-gated)   (fill price)        (mark-to-market, snapshots)
```

**The one rule:** producers write to the cache, consumers read from the cache. Nothing downstream ever calls a data source to get a price. This is what makes the simulator and Massive interchangeable, and it is what will allow multiple concurrent SSE clients later without touching the data layer.

---

## 3. File Structure

```
backend/
├── app/
│   ├── __init__.py
│   └── market/
│       ├── __init__.py           # Public exports
│       ├── models.py             # PriceUpdate
│       ├── interface.py          # MarketDataSource (ABC)
│       ├── cache.py              # PriceCache
│       ├── seed_prices.py        # Seed prices, GBM params, correlation config
│       ├── simulator.py          # GBMSimulator + SimulatorDataSource
│       ├── massive_client.py     # MassiveDataSource
│       ├── factory.py            # create_market_data_source()
│       └── stream.py             # create_stream_router() — SSE endpoint
├── tests/
│   ├── conftest.py
│   └── market/
│       ├── test_models.py
│       ├── test_cache.py
│       ├── test_simulator.py
│       ├── test_simulator_source.py
│       ├── test_massive.py
│       └── test_factory.py
├── market_data_demo.py           # Rich terminal demo
└── pyproject.toml
```

The package exposes exactly five names:

```python
# backend/app/market/__init__.py
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

Concrete classes (`GBMSimulator`, `SimulatorDataSource`, `MassiveDataSource`) are deliberately **not** exported. Downstream code should never name them — it gets one from the factory and holds it as a `MarketDataSource`.

---

## 4. Data Model — `PriceUpdate`

The only structure that leaves the market data layer.

```python
# backend/app/market/models.py
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

- **`frozen=True, slots=True`** — immutability means a `PriceUpdate` handed to the SSE generator can never be mutated underneath it by the producer task; `slots` keeps these small, since one is allocated per ticker per tick (20/sec at 10 tickers).
- **Derived fields are properties, not stored fields.** `change`, `change_percent`, and `direction` are functions of `price` and `previous_price`. Storing them would allow the two to disagree; computing them cannot. They are cheap and only evaluated on serialization.
- **`timestamp` is Unix *seconds*, float.** The simulator uses `time.time()`; the Massive client divides its millisecond timestamps by 1000. Normalizing at the boundary means no consumer needs to know which source is active.
- **`previous_price` is the previous *tick*, not the previous *close*.** This drives the frontend's flash animation. It is **not** the daily change — see [§20 Open Design Items](#20-open-design-items).
- **First update for a ticker sets `previous_price == price`**, so `direction == "flat"` and nothing flashes on page load.

### Examples

```python
>>> u = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1700000000.0)
>>> u.change
0.5
>>> u.change_percent
0.2632
>>> u.direction
'up'

>>> flat = PriceUpdate(ticker="MSFT", price=420.0, previous_price=420.0)
>>> flat.direction
'flat'
>>> flat.change
0.0

>>> PriceUpdate(ticker="X", price=10.0, previous_price=0.0).change_percent  # no ZeroDivisionError
0.0
```

---

## 5. Price Cache

The shared, thread-safe store. Producers write; SSE, trading and valuation read.

```python
# backend/app/market/cache.py
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

### Why the cache computes `previous_price`

The data sources supply only *the current price*. Chaining — `previous_price` for tick N is the `price` from tick N−1 — happens in exactly one place. Neither the simulator nor the Massive client has to track prior state for display purposes, and both get identical flash semantics for free.

### Why rounding happens here

Prices are rounded to 2 decimals **on write**, so the value stored is the value displayed, streamed, and *traded at*. A trade filled at `190.50` reconciles exactly with the price the user saw. Rounding later — in the serializer, say — would let trade math run on 190.4999999 while the UI showed 190.50.

### Why a version counter

The SSE generator wakes every 500ms but must not re-send an unchanged payload (idle tickers, a paused poller, a 15s Massive interval). A monotonic counter bumped on every write makes "has anything changed since I last sent?" a single integer comparison, with no payload diffing:

```python
current_version = price_cache.version
if current_version != last_version:
    last_version = current_version
    ...  # serialize and yield
```

With the Massive poller at 15s, this turns 30 redundant 10-ticker payloads per interval into one.

### Thread-safety rationale

The simulator writes from the event loop thread, but the Massive client's REST call runs in a worker thread via `asyncio.to_thread`. A `threading.Lock` (not `asyncio.Lock`) is therefore required: it is the only primitive that protects against both. Every mutating and reading method holds it, so `get_all()` can never observe a half-written dict.

`get_all()` returns a **shallow copy**. That is safe precisely because `PriceUpdate` is frozen — the caller gets a stable snapshot of immutable values, and iteration cannot raise `RuntimeError: dictionary changed size during iteration` while the producer keeps writing.

---
## 6. Unified API — `MarketDataSource`

The abstraction that makes simulator and Massive interchangeable.

```python
# backend/app/market/interface.py
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

### Contract notes

- **The interface returns no prices.** `start()` hands over a ticker list and the implementation begins pushing into the cache on its own cadence. If the interface returned prices, every consumer would need to know the source's cadence — 500ms for the simulator, 15s for Massive — and would either block or poll. Push-to-cache decouples the two rates entirely: the SSE loop reads at its own 500ms rhythm regardless of which source is behind it.
- **The cache is injected via the constructor, not passed to `start()`.** A source is bound to one cache for its whole life.
- **`add_ticker`/`remove_ticker` are `async`** even though both current implementations do no awaiting in them. This keeps the door open for an implementation that must talk to the network to subscribe (a WebSocket source, say) without changing every call site.
- **`get_tickers()` is sync** — it reads local state and is called from request handlers.
- **`stop()` is idempotent.** Calling it twice, or before `start()`, must not raise. FastAPI shutdown paths are not always exercised in order.
- **Both implementations own the cache removal on `remove_ticker`** so a de-watchlisted ticker stops appearing in the SSE payload immediately, rather than lingering at a stale price.

Because both concrete classes satisfy this ABC, every consumer is written once:

```python
source: MarketDataSource = create_market_data_source(cache)
await source.start(tickers)
# nothing below this line knows or cares which implementation it got
```

---

## 7. Seed Prices & Ticker Parameters

All simulator tuning constants live in one module, separate from logic, so they can be adjusted without touching code paths.

```python
# backend/app/market/seed_prices.py
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

The `sigma` values are the ones that matter visually: TSLA at 0.50 visibly jumps around next to V at 0.17, which is the realism the watchlist is selling. Tickers the user adds that aren't in `TICKER_PARAMS` fall back to `DEFAULT_PARAMS` and start at a random price in \$50–\$300.

> Note: `TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))` copies the default dict. Handing out the shared module-level dict would let a per-ticker mutation leak into every ticker that used the fallback.

---

## 8. Simulator — `GBMSimulator`

### The math

Prices follow **Geometric Brownian Motion**, the model underlying Black-Scholes. Its closed-form step is:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

| Symbol | Meaning |
|---|---|
| `S(t)` | current price |
| `μ` | annualized drift (expected return) |
| `σ` | annualized volatility |
| `dt` | time step, as a fraction of a trading year |
| `Z` | standard normal draw, correlated across tickers |

Three properties make it the right choice here:

1. **Prices can never go negative** — the step is multiplicative and `exp()` is strictly positive. No clamping, no floor checks.
2. **Returns are lognormal**, matching how real equities actually behave.
3. **The `−σ²/2` term** is the Itô correction. Without it, the *expected price* would drift up faster than `μ` because `E[exp(X)] > exp(E[X])`. With it, `E[S(t)] = S(0)·exp(μt)` exactly — which is what "5% annual drift" should mean.

**Choosing `dt`.** A trading year is 252 days × 6.5 hours × 3600 seconds = 5,896,800 seconds. A 500ms tick is therefore:

```
dt = 0.5 / 5_896_800 ≈ 8.48e-8
```

Sanity check with AAPL (σ = 0.22, S = \$190): the per-tick standard deviation is `S·σ·√dt = 190 × 0.22 × 2.91e-4 ≈ $0.012`. Roughly a cent per tick — small enough to look like a live quote, large enough that the 2-decimal rounding doesn't flatten it to nothing. Over a 6.5-hour session those accumulate to the correct daily range of about `190 × 0.22 / √252 ≈ $2.63`.

### Correlated moves via Cholesky decomposition

Independent random draws would make the watchlist look wrong — every ticker jittering on its own while real sectors move together. The fix is standard: build a correlation matrix `C`, take its Cholesky factor `L` (the lower-triangular matrix with `L·Lᵀ = C`), and multiply independent normals by it.

```
Z_correlated = L @ Z_independent
```

The result has covariance `L·I·Lᵀ = C` — exactly the correlations we asked for, with each component still marginally N(0,1). Cholesky also *validates* the matrix: it raises `LinAlgError` on a non-positive-definite one, so an incoherent correlation config fails loudly at ticker-add time rather than producing quiet nonsense.

The matrix is rebuilt whenever the ticker set changes. That is O(n²) to build plus O(n³) to factor, but n is a watchlist — under 50 — and it happens only on add/remove, never in the tick loop.

### Random shock events

Each ticker, each tick, has a ~0.1% chance of a 2–5% jump in either direction. At 2 ticks/sec across 10 tickers that is an event roughly every 50 seconds somewhere on the board — frequent enough to keep the terminal dramatic, rare enough per ticker (~once per 8 minutes) that it reads as news rather than noise.

### Implementation

```python
# backend/app/market/simulator.py  (GBMSimulator)
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

Two subtleties worth preserving if this is ever refactored:

- **`_add_ticker_internal` exists to batch initialization.** The constructor adds n tickers then factors *once*; the public `add_ticker` factors on every call. Without the split, constructing with 10 tickers would run 10 Cholesky decompositions.
- **`_cholesky is None` for n ≤ 1 is correct, not a bug.** A 1×1 correlation matrix is `[[1.0]]`, whose factor is the identity — the branch skips a pointless matrix multiply, and `z_independent` is already the right answer.

### Example

```python
>>> sim = GBMSimulator(tickers=["AAPL", "TSLA", "JPM"])
>>> sim.step()
{'AAPL': 190.01, 'TSLA': 249.98, 'JPM': 195.0}
>>> sim.step()
{'AAPL': 190.0, 'TSLA': 250.03, 'JPM': 195.01}
>>> sim.add_ticker("PYPL")          # not in SEED_PRICES → random $50-300 start
>>> sim.get_tickers()
['AAPL', 'TSLA', 'JPM', 'PYPL']
>>> sim.remove_ticker("JPM")
>>> sim.get_price("JPM") is None
True
```

For deterministic tests, seed both RNGs — the simulator uses NumPy for the normal draws and stdlib `random` for shock events:

```python
import random
import numpy as np

np.random.seed(42)
random.seed(42)
sim = GBMSimulator(tickers=["AAPL"])
assert sim.step() == sim_replay_expected  # reproducible
```

---
## 9. Simulator — `SimulatorDataSource`

The `MarketDataSource` implementation: an async loop around `GBMSimulator.step()`.

```python
# backend/app/market/simulator.py  (SimulatorDataSource)

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

### Key behaviors

- **Seeding the cache in `start()`** means the very first SSE poll — potentially milliseconds after startup — already has all 10 tickers at their seed prices. Without it the watchlist would render empty for up to 500ms on first paint.
- **`add_ticker` seeds too.** A ticker added mid-session gets a price in the cache synchronously, so the `POST /api/watchlist` response can include it and the row renders populated instead of blank-until-next-tick.
- **The `try` is inside the `while`, and `await asyncio.sleep()` is outside it.** A raised exception logs and the loop continues on the next tick; and because the sleep sits after the `try/except`, no failure path can spin the loop hot. A one-off numerical error must not silently kill price streaming for the rest of the session.
- **`stop()` awaits the cancelled task** and swallows `CancelledError`. Without the await, `stop()` could return while the task is still mid-tick and writing to the cache.
- **The task is named** (`simulator-loop`) purely so it is identifiable in `asyncio.all_tasks()` output when debugging a hung shutdown.
- **`get_tickers()` delegates to the simulator's public method** rather than reaching into `_tickers`, keeping the simulator's internals genuinely private.

---

## 10. Massive API — `MassiveDataSource`

Used when `MASSIVE_API_KEY` is set. Same interface, real prices.

### The API

- **Package:** `massive` (`uv add massive`) — the renamed Polygon.io client. Base URL `https://api.massive.com`; the legacy `api.polygon.io` still resolves.
- **Auth:** `RESTClient(api_key=...)`, which sends `Authorization: Bearer <key>`.
- **Endpoint:** `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`

The snapshot endpoint is the whole reason this design is viable on the free tier: it returns **every requested ticker in a single HTTP call**. Ten watchlist tickers cost one request, not ten.

| Tier | Limit | Poll interval |
|---|---|---|
| Free | 5 req/min | 15s (default) |
| Paid | effectively unlimited | 2–5s |

Response shape per ticker (abridged):

```json
{
  "ticker": "AAPL",
  "day": {
    "open": 129.61, "high": 130.15, "low": 125.07, "close": 125.07,
    "volume": 111237700, "previous_close": 129.61,
    "change": -4.54, "change_percent": -3.50
  },
  "last_trade": {
    "price": 125.07, "size": 100, "exchange": "XNYS",
    "timestamp": 1675190399000
  },
  "last_quote": { "bid_price": 125.06, "ask_price": 125.08, "spread": 0.02 },
  "prev_daily_bar": { "...": "previous day OHLCV" }
}
```

We consume exactly two fields: **`last_trade.price`** and **`last_trade.timestamp`** (Unix *milliseconds* — divide by 1000). `day.previous_close` and `day.change_percent` are available and are what a true daily-change display would need; see [§20](#20-open-design-items).

### Implementation

```python
# backend/app/market/massive_client.py
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

### Design points

- **`asyncio.to_thread` is mandatory, not stylistic.** `RESTClient` is synchronous and does blocking I/O. Calling it directly on the event loop would freeze *every* SSE connection for the duration of the HTTP round trip — a visible stall in the UI on every poll. Offloading to a worker thread is also precisely why `PriceCache` uses a `threading.Lock`.
- **`_fetch_snapshots` is a separate method** so the thread boundary wraps one small, synchronous, easily-mocked unit. Tests patch this method and never touch the network.
- **Two-level error handling.** The inner `except (AttributeError, TypeError)` catches a *single* malformed snapshot — a ticker with a null `last_trade` during a halt, say — and skips just that ticker, so nine good prices still land. The outer `except Exception` catches whole-poll failures (auth, rate limit, DNS) and returns quietly; the loop retries on the next interval. The cache keeps serving the last known good prices throughout, so the UI degrades to "stale" rather than "empty".
- **Immediate first poll in `start()`.** Without it, a 15s interval means 15 seconds of blank watchlist on boot. Note this makes `start()` slow (one network round trip) and able to surface an auth failure early — where it is logged, not raised, so a bad key never blocks app startup.
- **Tickers are normalized to uppercase** on add/remove, since the API is case-sensitive and user input from the trade bar or the LLM is not reliably capitalized.
- **`add_ticker` takes effect on the next poll**, up to 15s later. This asymmetry with the simulator (which seeds instantly) is inherent to polling — the frontend should tolerate a watchlist row with no price yet.
- **The `massive` import is at module level.** It is a declared core dependency in `pyproject.toml`, so it is always installed; earlier lazy-import-inside-method versions only made mocking harder in tests.

---

## 11. Factory

Selects the implementation from the environment. This is the only place in the codebase that reads `MASSIVE_API_KEY`.

```python
# backend/app/market/factory.py
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

`.strip()` matters: `.env` files routinely contain `MASSIVE_API_KEY=` or a key with trailing whitespace. A present-but-empty variable must mean "simulator", not "start a REST client with an empty key and fail every poll with 401".

The factory returns an **unstarted** source. Construction is cheap and synchronous; `start()` opens the network client and spawns the background task. Separating them lets the caller construct during app setup and start inside the lifespan handler.

```python
# Simulator (no key)
>>> os.environ.pop("MASSIVE_API_KEY", None)
>>> type(create_market_data_source(PriceCache())).__name__
'SimulatorDataSource'

# Massive (key present)
>>> os.environ["MASSIVE_API_KEY"] = "abc123"
>>> type(create_market_data_source(PriceCache())).__name__
'MassiveDataSource'

# Empty / whitespace-only key → simulator
>>> os.environ["MASSIVE_API_KEY"] = "   "
>>> type(create_market_data_source(PriceCache())).__name__
'SimulatorDataSource'
```

---

## 12. SSE Streaming Endpoint

`GET /api/stream/prices` — the browser's `EventSource` connects here and receives every tracked price roughly twice a second.

```python
# backend/app/market/stream.py
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

The first frame sets the client's reconnect backoff:

```
retry: 1000

```

Then each update is one `data:` line holding a JSON object keyed by ticker:

```
data: {"AAPL":{"ticker":"AAPL","price":190.52,"previous_price":190.48,"timestamp":1700000000.5,"change":0.04,"change_percent":0.021,"direction":"up"},"GOOGL":{...}}

```

Every SSE frame must end with a **blank line** — hence `\n\n`. Without it the browser buffers the frame and dispatches nothing.

**Why one object rather than per-ticker events?** A single 10-ticker frame is one `onmessage` callback and one React state update per tick. Ten separate events would mean ten callbacks and, without batching, potentially ten re-renders — for data that always arrives together anyway.

Client side:

```js
const es = new EventSource("/api/stream/prices");

es.onmessage = (e) => {
  const prices = JSON.parse(e.data);   // { AAPL: {price, direction, ...}, ... }
  applyPrices(prices);                 // flash green/up, red/down; append to sparkline buffer
};

es.onerror = () => setStatus("reconnecting");  // EventSource retries on its own
es.onopen  = () => setStatus("connected");
```

### Behavior notes

- **Version gating** (§5) suppresses identical payloads. Under Massive's 15s polling the stream sends ~1 frame per 15s instead of 30 duplicates.
- **`request.is_disconnected()` each iteration** ends the generator when a tab closes. Without it, a closed tab leaves a generator looping and serializing forever.
- **`X-Accel-Buffering: no`** disables nginx response buffering. Behind a proxy without it, SSE frames get held in a buffer and arrive in bursts — the stream appears frozen, then jumps.
- **`CancelledError` is caught and logged, not swallowed silently or re-raised noisily** — server shutdown cancels in-flight generators, and that is a normal event, not an error.
- **The `retry: 1000` directive plus `EventSource`'s built-in reconnection** is the entire reconnect story. No client-side retry logic to write; the header's connection-status dot just tracks `onopen`/`onerror`.

> **Known footgun:** `router` is a module-level `APIRouter`, and `create_stream_router()` registers `/prices` on it via closure. Calling the factory twice in one process (as tests might) double-registers the route. It is called once at startup in production. If tests need repeated calls, move `router = APIRouter(...)` inside the factory.

---
## 13. FastAPI Lifecycle Integration

This section is the contract for the Backend/API agent — the market package is complete, but `app/main.py` is not yet written. Wire it up like this.

```python
# backend/app/main.py
"""FinAlly FastAPI application."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.market import PriceCache, create_market_data_source, create_stream_router

logger = logging.getLogger(__name__)

DEFAULT_TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    init_database()                                  # lazy schema creation + seed
    tickers = get_watchlist_tickers() or DEFAULT_TICKERS

    cache = PriceCache()
    source = create_market_data_source(cache)        # reads MASSIVE_API_KEY
    await source.start(tickers)

    app.state.price_cache = cache
    app.state.market_source = source

    logger.info("Market data started: %s", source.get_tickers())

    yield

    # --- Shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Mount the SSE router — must happen after the cache exists
app.include_router(create_stream_router(app.state.price_cache))
```

> If `app.state` is not yet populated at import time in your arrangement, construct the cache at module scope instead and assign it into `app.state` inside `lifespan`. The cache is a plain object with no async setup, so this is safe.

Accessing market data from other routes:

```python
def get_price_cache(request: Request) -> PriceCache:
    """FastAPI dependency: the shared price cache."""
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    """FastAPI dependency: the active market data source."""
    return request.app.state.market_source


@app.get("/api/health")
async def health(cache: PriceCache = Depends(get_price_cache)) -> dict:
    return {
        "status": "ok",
        "tickers_cached": len(cache),
        "market_data": "live",
    }
```

### Ordering requirements

1. **Database before market data** — the initial ticker list comes from the `watchlist` table, which must exist and be seeded first.
2. **`start()` before serving traffic.** The lifespan handler awaits it, so the first request already sees a warm cache. With Massive this costs one HTTP round trip at boot; it is worth it.
3. **`stop()` on shutdown**, so the background task is cancelled rather than left to die with the loop and log a spurious "Task was destroyed but it is pending!".
4. **Both the cache and the source live on `app.state`** — one instance per process, reachable from any handler, never a module-level global.

---

## 14. Watchlist Coordination

The watchlist lives in two places that must not drift: the `watchlist` **table** (durable) and the data source's **active ticker set** (in-memory). The API layer owns keeping them in sync.

### Adding a ticker

```python
@app.post("/api/watchlist")
async def add_to_watchlist(
    body: AddTickerRequest,
    cache: PriceCache = Depends(get_price_cache),
    source: MarketDataSource = Depends(get_market_source),
) -> dict:
    ticker = body.ticker.upper().strip()

    if not ticker.isalpha() or len(ticker) > 5:
        raise HTTPException(400, detail=f"Invalid ticker: {ticker}")

    db_add_watchlist_ticker(ticker)     # INSERT OR IGNORE (UNIQUE user_id, ticker)
    await source.add_ticker(ticker)     # begins producing prices

    return {
        "ticker": ticker,
        "price": cache.get_price(ticker),   # populated instantly (sim), None until next poll (Massive)
    }
```

Order matters: **database first, source second.** If the DB write fails, the source never starts producing a price for a ticker that isn't persisted. The reverse order could leave a ticker streaming that vanishes on restart.

### Removing a ticker

```python
@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
) -> dict:
    ticker = ticker.upper().strip()

    db_remove_watchlist_ticker(ticker)
    await source.remove_ticker(ticker)  # also clears it from the PriceCache

    return {"ticker": ticker, "removed": True}
```

### Edge case: removing a ticker you still hold

Removing `AAPL` from the watchlist while holding 10 shares of it would drop its price from the cache — and portfolio valuation would then mark that position at nothing.

**Rule: never stop tracking a ticker with an open position.** The API layer enforces it; the market layer has no concept of positions and must not gain one.

```python
positions = db_get_positions()
if any(p.ticker == ticker and p.quantity > 0 for p in positions):
    # Remove from the watchlist display, but keep the price feed alive
    db_remove_watchlist_ticker(ticker)
    # deliberately NOT: await source.remove_ticker(ticker)
    return {"ticker": ticker, "removed": True, "note": "still tracked (open position)"}
```

The union of *watchlist tickers* and *position tickers* is the true set the source should track. On startup, seed it from both tables:

```python
tickers = sorted(set(db_get_watchlist_tickers()) | set(db_get_position_tickers()))
await source.start(tickers or DEFAULT_TICKERS)
```

---

## 15. Consuming Prices Downstream

Every consumer follows the same shape: read the cache, handle `None`.

### Trade execution — the fill price

```python
def execute_trade(ticker: str, quantity: float, side: str, cache: PriceCache) -> Trade:
    price = cache.get_price(ticker)
    if price is None:
        raise HTTPException(503, detail=f"No price available for {ticker}; try again shortly")

    if side == "buy":
        cost = price * quantity
        if cost > get_cash_balance():
            raise HTTPException(400, detail="Insufficient cash")
        ...
```

Because the cache rounds on write, `price` here is exactly the number the user saw on screen — the fill reconciles with the display, and `price * quantity` is the cost with no hidden sub-cent drift.

### Portfolio valuation — mark to market

```python
def value_portfolio(cache: PriceCache) -> dict:
    prices = cache.get_all()                # one consistent snapshot, taken under the lock
    positions = db_get_positions()

    total = get_cash_balance()
    rows = []
    for p in positions:
        update = prices.get(p.ticker)
        current = update.price if update else p.avg_cost   # fall back to cost basis
        market_value = current * p.quantity
        rows.append({
            "ticker": p.ticker,
            "quantity": p.quantity,
            "avg_cost": p.avg_cost,
            "current_price": current,
            "unrealized_pnl": (current - p.avg_cost) * p.quantity,
            "pnl_percent": (current - p.avg_cost) / p.avg_cost * 100 if p.avg_cost else 0.0,
        })
        total += market_value

    return {"cash": get_cash_balance(), "positions": rows, "total_value": total}
```

Take **one** `get_all()` snapshot and value the whole portfolio from it. Calling `get_price()` per position would let prices tick between positions, so the totals wouldn't correspond to any single instant.

Falling back to `avg_cost` on a cache miss reports \$0 unrealized P&L for that position rather than a crash or a wild number — the honest answer when the current price is unknown.

### Portfolio snapshots — the P&L chart

A background task writes `portfolio_snapshots` every 30 seconds (and after each trade), reading from the same cache:

```python
async def snapshot_loop(cache: PriceCache, interval: float = 30.0) -> None:
    while True:
        try:
            db_insert_snapshot(total_value=value_portfolio(cache)["total_value"])
        except Exception:
            logger.exception("Portfolio snapshot failed")
        await asyncio.sleep(interval)
```

### LLM chat context

The chat handler builds its portfolio context from `value_portfolio(cache)` and the watchlist prices from `cache.get_all()`, so the model sees the same numbers as the screen at the moment of the request.

---

## 16. Error Handling & Edge Cases

| Situation | Behavior | Where handled |
|---|---|---|
| Empty watchlist at startup | `start([])` is valid; `step()` returns `{}`, SSE sends nothing until a ticker is added | `GBMSimulator.step`, `_poll_once` |
| Cache miss during a trade | 503 with a retry hint — never fill at a guessed price | Trade route (§15) |
| Cache miss during valuation | Fall back to `avg_cost` → \$0 unrealized for that row | `value_portfolio` (§15) |
| Invalid `MASSIVE_API_KEY` | 401 logged each poll; cache stays empty; app runs, UI shows no prices | `_poll_once` outer except |
| Rate limit (429) | Poll fails, logged, retried next interval; last good prices keep serving | `_poll_once` outer except |
| Malformed/partial snapshot | That one ticker is skipped with a warning; others still update | `_poll_once` inner except |
| Network drop mid-poll | Same as any poll failure — logged, retried, stale prices served | `_poll_once` outer except |
| Numerical error in a GBM step | Logged with traceback; loop continues next tick | `_run_loop` except |
| Client closes an SSE tab | `is_disconnected()` ends the generator | `_generate_events` |
| Server shutdown with clients attached | Generators get `CancelledError`, log and exit; `stop()` cancels producer tasks | `_generate_events`, `stop()` |
| Unknown ticker added to simulator | Random \$50–300 start, `DEFAULT_PARAMS`, 0.3 correlation | `_add_ticker_internal` |
| Unknown ticker sent to Massive | Absent from the snapshot response; simply never appears in the cache | API behavior |
| Ticker removed while held | Watchlist row goes; price feed stays alive | Watchlist route (§14) |
| Two writers racing on the cache | `threading.Lock` on every read and write | `PriceCache` |

**The governing principle for background loops: log and continue, never propagate.** An unhandled exception in `_run_loop` or `_poll_loop` kills the task, and a dead producer means a permanently frozen UI with no error anywhere the user can see. A logged exception costs one tick.

### Precision

Prices are rounded to 2 decimals on cache write, so the underlying GBM float keeps full precision across steps while every consumer sees clean money values. Rounding the *simulator's internal* price each step would accumulate rounding drift into the price path itself.

---

## 17. Testing Strategy

73 tests across 6 modules in `backend/tests/market/`; overall coverage 84%.

| Module | Focus |
|---|---|
| `test_models.py` | `change`/`change_percent`/`direction` derivation, zero-division, `to_dict()` keys, immutability |
| `test_cache.py` | update/get/get_all/remove, previous-price chaining, version bumps, rounding, `__len__`/`__contains__` |
| `test_simulator.py` | GBM step math, positivity, Cholesky rebuild, add/remove, seed prices, shock events |
| `test_simulator_source.py` | Async lifecycle: start seeds cache, loop writes, stop cancels cleanly |
| `test_massive.py` | Mocked polling, timestamp conversion, malformed-snapshot skip, task cancellation |
| `test_factory.py` | Env-var selection, empty/whitespace key handling |

Run with:

```bash
cd backend
uv run pytest                                   # all tests
uv run pytest --cov=app --cov-report=term-missing
uv run ruff check .
```

`asyncio_mode = "auto"` is set in `pyproject.toml`, so `async def test_*` functions need no `@pytest.mark.asyncio`.

### Testing the model and cache

```python
def test_direction_and_change():
    u = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert u.direction == "up"
    assert u.change == 1.0
    assert u.change_percent == pytest.approx(0.5263, abs=1e-4)


def test_zero_previous_price_does_not_divide_by_zero():
    assert PriceUpdate(ticker="X", price=10.0, previous_price=0.0).change_percent == 0.0


def test_first_update_is_flat():
    cache = PriceCache()
    u = cache.update("AAPL", 190.0)
    assert u.previous_price == 190.0 and u.direction == "flat"


def test_previous_price_chains_across_updates():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    u = cache.update("AAPL", 191.0)
    assert u.previous_price == 190.0 and u.direction == "up"


def test_version_increments_on_every_write():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    cache.update("AAPL", 190.0)          # same price still counts as a write
    assert cache.version == v0 + 2


def test_remove_clears_ticker():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert "AAPL" not in cache and cache.get("AAPL") is None
```

### Testing the simulator

```python
def test_prices_stay_positive_over_many_steps():
    sim = GBMSimulator(tickers=["AAPL", "TSLA"])
    for _ in range(1000):
        for price in sim.step().values():
            assert price > 0


def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL", "MSFT"])
    assert set(sim.step()) == {"AAPL", "GOOGL", "MSFT"}


def test_moves_are_small_per_tick():
    """One tick should move a $190 stock by cents, not dollars."""
    sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)  # no shocks
    start = sim.get_price("AAPL")
    sim.step()
    assert abs(sim.get_price("AAPL") - start) < 0.5


def test_cholesky_builds_for_full_default_watchlist():
    """The 10-ticker correlation matrix must be positive definite."""
    sim = GBMSimulator(tickers=list(SEED_PRICES))
    assert sim._cholesky.shape == (10, 10)
    assert sim.step()                       # would raise if the factor were invalid


def test_single_ticker_has_no_cholesky():
    assert GBMSimulator(tickers=["AAPL"])._cholesky is None


def test_add_and_remove_rebuild_correlation():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    sim.add_ticker("TSLA")
    assert sim._cholesky.shape == (3, 3)
    sim.remove_ticker("GOOGL")
    assert sim._cholesky.shape == (2, 2)
    assert "GOOGL" not in sim.get_tickers()


def test_unknown_ticker_gets_default_params():
    sim = GBMSimulator(tickers=["ZZZZ"])
    assert 50.0 <= sim.get_price("ZZZZ") <= 300.0


def test_shock_event_moves_price_sharply():
    sim = GBMSimulator(tickers=["AAPL"], event_probability=1.0)  # always shock
    start = sim.get_price("AAPL")
    sim.step()
    assert abs(sim.get_price("AAPL") / start - 1) >= 0.02
```

### Testing the async source

```python
async def test_start_seeds_cache_immediately():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL", "GOOGL"])
    try:
        assert cache.get_price("AAPL") == 190.00   # seed price, before any tick
    finally:
        await source.stop()


async def test_loop_writes_updates():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
    await source.start(["AAPL"])
    try:
        v0 = cache.version
        await asyncio.sleep(0.05)
        assert cache.version > v0
    finally:
        await source.stop()


async def test_stop_is_idempotent_and_cancels_task():
    source = SimulatorDataSource(price_cache=PriceCache())
    await source.start(["AAPL"])
    await source.stop()
    await source.stop()                      # must not raise
    assert source._task is None


async def test_remove_ticker_clears_cache():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL", "GOOGL"])
    try:
        await source.remove_ticker("GOOGL")
        assert "GOOGL" not in cache
        assert source.get_tickers() == ["AAPL"]
    finally:
        await source.stop()
```

### Testing the Massive client without a network

Patch `_fetch_snapshots` — the single synchronous seam — and assert on what lands in the cache.

```python
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def make_snapshot(ticker: str, price: float, ts_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        last_trade=SimpleNamespace(price=price, timestamp=ts_ms),
    )


async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()             # start() would build a real client
    source._tickers = ["AAPL"]
    source._fetch_snapshots = MagicMock(
        return_value=[make_snapshot("AAPL", 190.5, 1675190399000)]
    )

    await source._poll_once()

    assert cache.get_price("AAPL") == 190.5


async def test_timestamps_convert_ms_to_seconds():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._fetch_snapshots = MagicMock(
        return_value=[make_snapshot("AAPL", 190.5, 1675190399000)]
    )

    await source._poll_once()

    assert cache.get("AAPL").timestamp == 1675190399.0


async def test_malformed_snapshot_is_skipped_but_others_land():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL", "GOOGL"]
    source._fetch_snapshots = MagicMock(return_value=[
        SimpleNamespace(ticker="AAPL", last_trade=None),          # malformed
        make_snapshot("GOOGL", 175.0, 1675190399000),             # fine
    ])

    await source._poll_once()                 # must not raise

    assert cache.get_price("AAPL") is None
    assert cache.get_price("GOOGL") == 175.0


async def test_poll_failure_does_not_kill_the_loop():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test", price_cache=cache)
    source._client = MagicMock()
    source._tickers = ["AAPL"]
    source._fetch_snapshots = MagicMock(side_effect=RuntimeError("429 rate limited"))

    await source._poll_once()                 # swallowed and logged

    assert len(cache) == 0


@patch("app.market.massive_client.RESTClient")
async def test_stop_cancels_poller(mock_client):
    source = MassiveDataSource(api_key="test", price_cache=PriceCache())
    source._fetch_snapshots = MagicMock(return_value=[])
    await source.start(["AAPL"])
    await source.stop()
    assert source._task is None and source._client is None
```

### Testing the factory

```python
def test_no_key_selects_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)


def test_key_selects_massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "abc123")
    assert isinstance(create_market_data_source(PriceCache()), MassiveDataSource)


def test_whitespace_key_selects_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "   ")
    assert isinstance(create_market_data_source(PriceCache()), SimulatorDataSource)
```

### Gaps worth closing

- **SSE integration test.** `stream.py` sits at ~31% coverage. A test using `httpx.AsyncClient` against the ASGI app that reads two frames and asserts the `retry:` directive and the JSON shape would cover the primary cache consumer. (Move the `APIRouter` inside `create_stream_router` first — see §12.)
- **Concurrent cache writes.** The lock is right by inspection; a test with several threads hammering `update()` while a reader loops `get_all()` would prove it.

---

## 18. Configuration Reference

### Environment variables

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | unset | Set and non-empty → Massive REST poller. Unset/empty/whitespace → GBM simulator. |

### Tunable constructor parameters

| Parameter | Location | Default | Notes |
|---|---|---|---|
| `update_interval` | `SimulatorDataSource` | `0.5` s | Tick rate; matches the SSE cadence |
| `event_probability` | `SimulatorDataSource`, `GBMSimulator` | `0.001` | Per ticker per tick shock chance |
| `dt` | `GBMSimulator` | `~8.48e-8` | 500ms as a fraction of a trading year; change with `update_interval` |
| `poll_interval` | `MassiveDataSource` | `15.0` s | 15s for free tier; 2–5s for paid |
| `interval` | `_generate_events` | `0.5` s | SSE emit cadence |

### Dependencies (`backend/pyproject.toml`)

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",          # Cholesky decomposition + normal draws
    "massive>=1.0.0",        # Polygon.io REST client
    "rich>=13.0.0",          # terminal demo
]

[tool.hatch.build.targets.wheel]
packages = ["app"]           # required — uv sync fails without it
```

### Tuning notes

- Raising `update_interval` **without** raising `dt` proportionally makes prices move more slowly in wall-clock terms — the two encode the same physical time step and must change together: `dt = update_interval / 5_896_800`.
- Raise a ticker's `sigma` for more visible movement; `mu` shifts the long-run trend and is nearly invisible over a single session.
- `event_probability` above ~0.01 makes shocks constant and the price path stops looking like a market.

---

## 19. Demo Harness

`backend/market_data_demo.py` renders a live Rich dashboard — all 10 default tickers with unicode sparklines, direction arrows and an event log — driven by the real `SimulatorDataSource` and `PriceCache`.

```bash
cd backend
uv run market_data_demo.py     # runs 60s, or Ctrl+C
```

It is the quickest way to eyeball tuning changes (volatility, shock frequency, tick rate) without the frontend, and it doubles as an end-to-end check that the source→cache path works.

---

## 20. Open Design Items

Carried forward for the agents building on top of this layer.

### 20.1 Daily change % vs tick change % — needs a decision

`PLAN.md` §10 requires the watchlist to show **"daily change %"**. What `PriceUpdate.change_percent` actually reports is the change since the *previous tick* — which is the right input for the flash animation and the wrong number for that column. As shipped, a watchlist bound directly to `change_percent` would display a near-zero percentage flickering around 0.00%.

Recommended fix — add a session baseline to the cache and carry both figures:

```python
class PriceCache:
    def __init__(self) -> None:
        ...
        self._session_open: dict[str, float] = {}   # first price seen this session

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ...
            self._session_open.setdefault(ticker, round(price, 2))
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                session_open=self._session_open[ticker],   # new field
                timestamp=ts,
            )
```

with `PriceUpdate.day_change_percent` derived from `session_open`, serialized in `to_dict()` alongside the existing tick fields. Under Massive, `session_open` should instead be populated from `day.previous_close`, which the snapshot response already returns and the poller currently discards — giving a true daily change rather than a since-page-load one.

This is additive: `change`, `direction` and the flash behavior are untouched.

### 20.2 `PriceCache.version` is read without the lock

```python
@property
def version(self) -> int:
    return self._version      # no lock
```

Safe on CPython — an `int` read is atomic under the GIL — and the SSE loop only needs "did this change?". On a free-threaded build (PEP 703) it becomes a genuine race. One-line fix: wrap in `with self._lock:`. Left as-is deliberately, since it is on the 500ms-per-client hot path.

### 20.3 Module-level router in `stream.py`

Described in §12 — `create_stream_router()` is not safe to call twice. Move the `APIRouter` construction inside the factory when the SSE integration test lands.

### 20.4 Historical bars

The cache holds only the latest price, so the main chart area starts empty and fills in from the SSE stream. If pre-loaded history is wanted later, Massive's `list_aggs()` supplies OHLCV bars and the simulator would need a synthetic back-fill to match. Out of scope for the current build.

### 20.5 Multiple SSE clients

Every connected client runs its own `_generate_events` loop reading the same cache — correct but O(clients) serializations of identical data. Fine for single-user local use. For many clients, serialize once per version bump and fan the string out to subscribers.

---

## Appendix — Quick Reference

```python
from app.market import PriceCache, create_market_data_source, create_stream_router

# Startup
cache = PriceCache()
source = create_market_data_source(cache)         # env-driven: simulator or Massive
await source.start(["AAPL", "GOOGL", "MSFT"])
app.include_router(create_stream_router(cache))   # GET /api/stream/prices

# Read (any thread, any handler)
cache.get("AAPL")        # PriceUpdate | None
cache.get_price("AAPL")  # float | None
cache.get_all()          # dict[str, PriceUpdate] — one consistent snapshot
len(cache)               # tickers tracked
"AAPL" in cache          # bool

# Watchlist changes
await source.add_ticker("TSLA")
await source.remove_ticker("GOOGL")               # also clears the cache entry
source.get_tickers()                              # list[str]

# Shutdown
await source.stop()                               # idempotent
```
