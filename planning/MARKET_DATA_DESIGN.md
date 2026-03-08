# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration.

Everything lives under `backend/app/market/`.

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
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Architecture Overview

```
┌──────────────────────────────────────────────────────────────────┐
│                       FastAPI Application                        │
│                                                                  │
│  ┌──────────────────┐          ┌──────────────────────────────┐  │
│  │  MarketDataSource │          │         PriceCache           │  │
│  │       (ABC)       │─writes──▶│  (thread-safe, in-memory)    │  │
│  │                   │          │                              │  │
│  │  ┌─────────────┐ │          │  • update(ticker, price)     │  │
│  │  │  Simulator   │ │          │  • get(ticker) → PriceUpdate │  │
│  │  │ DataSource   │ │          │  • get_all() → dict          │  │
│  │  │  (default)   │ │          │  • get_price(ticker) → float │  │
│  │  └─────────────┘ │          │  • version → int (monotonic) │  │
│  │  ┌─────────────┐ │          └──────────┬───────────────────┘  │
│  │  │  Massive     │ │                     │                     │
│  │  │ DataSource   │ │                     │ reads                │
│  │  │  (optional)  │ │                     ▼                     │
│  │  └─────────────┘ │          ┌──────────────────────────────┐  │
│  └──────────────────┘          │  SSE Stream → Frontend       │  │
│                                │  GET /api/stream/prices      │  │
│  create_market_data_source()   │                              │  │
│  reads MASSIVE_API_KEY env     │  Portfolio Valuation          │  │
│  to select implementation      │  Trade Execution              │  │
│                                └──────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
```

### Design Principles

- **Strategy pattern**: Both data sources implement `MarketDataSource` ABC. All downstream code is source-agnostic.
- **PriceCache as single point of truth**: Producers (simulator or Massive poller) write; consumers (SSE, portfolio, trades) read. No direct coupling between producers and consumers.
- **Push model**: Data sources run background tasks that push updates into the cache on their own schedule. Nobody polls the data source directly.
- **SSE over WebSockets**: One-way server→client push is all we need. Simpler, universal browser support via `EventSource`, built-in reconnection.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports public API
      models.py               # PriceUpdate frozen dataclass
      cache.py                # PriceCache (thread-safe in-memory store)
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, correlation config
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py              # create_market_data_source() selector
      stream.py               # SSE endpoint factory (FastAPI router)
```

### Public API (`__init__.py`)

```python
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

Downstream code only needs:

```python
from app.market import PriceCache, PriceUpdate, create_market_data_source, create_stream_router
```

---

## 3. Data Model — `models.py`

A single immutable dataclass represents every price snapshot flowing through the system.

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

### Design Decisions

| Choice | Rationale |
|--------|-----------|
| `frozen=True` | Immutable — safe to pass across async boundaries, no accidental mutation |
| `slots=True` | Lower memory, faster attribute access (many instances created per second) |
| Computed properties | `change`, `change_percent`, `direction` are derived; storing them would risk inconsistency |
| `to_dict()` | Explicit serialization rather than relying on `dataclasses.asdict()` — includes computed properties |

### Usage Example

```python
update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.25)
print(update.direction)       # "up"
print(update.change)          # 1.25
print(update.change_percent)  # 0.6571
print(update.to_dict())       # {"ticker": "AAPL", "price": 191.50, ...}
```

---

## 4. Price Cache — `cache.py`

The central hub of the market data system. One writer (the active data source) and multiple readers (SSE, portfolio, trade execution).

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

### Key Properties

- **Thread safety**: `threading.Lock` protects all reads and writes. Required because the Massive client runs synchronous REST calls in `asyncio.to_thread()`, meaning cache writes can come from a different OS thread.
- **Version counter**: Monotonically increasing integer bumped on every `update()`. The SSE endpoint uses this for change detection — if `version` hasn't changed since the last push, skip sending a duplicate event.
- **Memory bounded**: Only stores the latest `PriceUpdate` per ticker, so memory is O(n) where n = number of tickers. No history accumulation.
- **First update semantics**: When a ticker appears for the first time, `previous_price = price`, so `direction = "flat"` and `change = 0`. This prevents a false spike on first data.

### Usage Examples

```python
cache = PriceCache()

# Write (from data source)
update = cache.update("AAPL", 191.50)
print(update.direction)  # "flat" (first update)

update = cache.update("AAPL", 192.00)
print(update.direction)  # "up"

# Read (from SSE, portfolio, trade execution)
aapl = cache.get("AAPL")          # PriceUpdate or None
price = cache.get_price("AAPL")   # 192.0 or None
all_prices = cache.get_all()      # {"AAPL": PriceUpdate, ...}

# Dynamic watchlist
cache.remove("AAPL")
print("AAPL" in cache)  # False
```

---

## 5. Abstract Interface — `interface.py`

The contract that both data sources implement. Downstream code never needs to know whether prices come from a simulator or a real API.

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

### Interface Contract

| Method | Behavior | Idempotent? |
|--------|----------|-------------|
| `start(tickers)` | Starts background task, seeds cache immediately | No — call once |
| `stop()` | Cancels background task, releases resources | Yes — safe to call multiple times |
| `add_ticker(ticker)` | Adds to active set; appears on next update cycle | Yes — no-op if present |
| `remove_ticker(ticker)` | Removes from active set AND from PriceCache | Yes — no-op if not present |
| `get_tickers()` | Returns current active ticker list (copy) | N/A — read-only |

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Configuration constants used by the GBM simulator. Separated from `simulator.py` for clarity and testability.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
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
    "V": {"sigma": 0.17, "mu": 0.04},    # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

### Parameter Rationale

| Ticker | Sigma | Character |
|--------|-------|-----------|
| TSLA | 0.50 | Highest vol — big swings, visually dramatic |
| NVDA | 0.40 | High vol, high drift (0.08) — AI boom momentum |
| NFLX | 0.35 | Moderately volatile entertainment stock |
| META | 0.30 | Mid-range tech |
| AMZN | 0.28 | Slightly more volatile large-cap |
| GOOGL | 0.25 | Typical large-cap tech |
| AAPL | 0.22 | Stable mega-cap |
| MSFT | 0.20 | Most stable tech giant |
| JPM | 0.18 | Low vol bank stock |
| V | 0.17 | Lowest vol — steady payments company |

### Correlation Matrix (Visual)

```
         AAPL GOOGL MSFT AMZN TSLA NVDA META JPM   V  NFLX
AAPL     1.0  0.6  0.6  0.6  0.3  0.6  0.6  0.3  0.3  0.6
GOOGL    0.6  1.0  0.6  0.6  0.3  0.6  0.6  0.3  0.3  0.6
MSFT     0.6  0.6  1.0  0.6  0.3  0.6  0.6  0.3  0.3  0.6
AMZN     0.6  0.6  0.6  1.0  0.3  0.6  0.6  0.3  0.3  0.6
TSLA     0.3  0.3  0.3  0.3  1.0  0.3  0.3  0.3  0.3  0.3
NVDA     0.6  0.6  0.6  0.6  0.3  1.0  0.6  0.3  0.3  0.6
META     0.6  0.6  0.6  0.6  0.3  0.6  1.0  0.3  0.3  0.6
JPM      0.3  0.3  0.3  0.3  0.3  0.3  0.3  1.0  0.5  0.3
V        0.3  0.3  0.3  0.3  0.3  0.3  0.3  0.5  1.0  0.3
NFLX     0.6  0.6  0.6  0.6  0.3  0.6  0.6  0.3  0.3  1.0
```

This matrix is positive semi-definite (all eigenvalues > 0), so Cholesky decomposition always succeeds.

### Dynamic Tickers

When a user adds a ticker not in `SEED_PRICES` (e.g., `"PYPL"`):
- **Seed price**: random between $50–$300
- **GBM params**: `DEFAULT_PARAMS` (sigma=0.25, mu=0.05)
- **Correlation**: `CROSS_GROUP_CORR` (0.3) with all existing tickers

---

## 7. GBM Simulator — `simulator.py`

Two classes: `GBMSimulator` (pure math engine) and `SimulatorDataSource` (async wrapper implementing `MarketDataSource`).

### 7.1 GBMSimulator — The Math Engine

```python
"""GBM-based market simulator."""

import math
import random
import numpy as np

class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where Z is a correlated standard normal (via Cholesky decomposition).
    """

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
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

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms.
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
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Rebuilds the correlation matrix."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the correlation matrix."""
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
        """Add without rebuilding Cholesky (batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky decomposition of the correlation matrix."""
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
```

### GBM Math Breakdown

For a 500ms time step:

```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48 × 10⁻⁸

For AAPL (sigma=0.22, mu=0.05):
  drift     = (0.05 - 0.5 * 0.22²) * 8.48e-8 ≈ 2.0e-9  (negligible per tick)
  diffusion = 0.22 * sqrt(8.48e-8) * Z ≈ 6.4e-5 * Z

  So per tick, AAPL moves ~0.01% per standard deviation
  → $190 * 6.4e-5 ≈ $0.012 per 1-sigma move

For TSLA (sigma=0.50, mu=0.03):
  diffusion = 0.50 * sqrt(8.48e-8) * Z ≈ 1.46e-4 * Z
  → $250 * 1.46e-4 ≈ $0.036 per 1-sigma move (3x AAPL — visibly more volatile)
```

### Random Events

- Probability: 0.1% per tick per ticker
- At 2 ticks/second with 10 tickers → ~1 event every 50 seconds
- Magnitude: 2–5% shock (up or down, equally likely)
- Effect: AAPL gets a $3.80–$9.50 jump; NVDA gets a $16–$40 jump
- Purpose: Visual drama and dashboard activity

### Cholesky Decomposition

Given a correlation matrix C, the Cholesky decomposition produces a lower-triangular matrix L where L × Lᵀ = C. To generate correlated random draws:

```
Z_independent  = [Z₁, Z₂, ..., Zₙ]   (independent standard normals)
Z_correlated   = L × Z_independent      (correlated standard normals)
```

The Cholesky is rebuilt when tickers are added or removed. The operation is O(n³) but n < 50, so it's instantaneous.

### 7.2 SimulatorDataSource — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task that calls GBMSimulator.step() every
    500ms and writes results to the PriceCache.
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

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

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

### Key Behaviors

- **Immediate seeding**: On `start()`, the cache is populated with seed prices before the background task begins. This ensures the SSE endpoint has data from the first poll.
- **Graceful error handling**: `_run_loop` catches and logs exceptions but never crashes. The simulation continues on the next tick.
- **Clean shutdown**: `stop()` cancels the task and awaits `CancelledError`.
- **Instant add**: When `add_ticker()` is called, the ticker's seed price is immediately written to the cache (no waiting for the next step).

---

## 8. Massive API Client — `massive_client.py`

REST polling client for real market data via the Massive (Polygon.io) API.

```python
"""Massive (Polygon.io) API client for real market data."""

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

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

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
            # The Massive RESTClient is synchronous — run in a thread
            # to avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → seconds
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

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Massive API Details

**Endpoint used**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`

This returns all requested tickers in a **single API call** — critical for staying within the free tier's 5 requests/minute limit.

**Response fields extracted per ticker**:

```json
{
  "ticker": "AAPL",
  "last_trade": {
    "price": 191.50,
    "timestamp": 1675190399000
  },
  "day": {
    "open": 190.00,
    "high": 192.00,
    "low": 189.50,
    "close": 191.50,
    "change_percent": 0.79
  }
}
```

We extract `last_trade.price` and `last_trade.timestamp` (converted from milliseconds to seconds).

### Rate Limit Strategy

| Tier | Limit | Poll Interval |
|------|-------|---------------|
| Free | 5 req/min | 15 seconds (4 req/min, under limit) |
| Paid | Unlimited | 2–5 seconds (configurable) |

The `poll_interval` parameter defaults to 15.0 seconds. Users on paid tiers can lower this:

```python
MassiveDataSource(api_key=key, price_cache=cache, poll_interval=3.0)
```

### Thread Safety

The `massive` Python client is synchronous. We use `asyncio.to_thread(self._fetch_snapshots)` to run it in a thread pool without blocking the event loop. This is why `PriceCache` uses a `threading.Lock` rather than `asyncio.Lock`.

### Error Handling

| Error | Behavior |
|-------|----------|
| 401 Unauthorized | Logged, poll skipped, retried next interval |
| 429 Rate Limited | Logged, poll skipped, retried next interval |
| Network error | Logged, poll skipped, retried next interval |
| Malformed snapshot | Individual ticker skipped, others processed |
| Empty ticker list | Poll skipped entirely |

The poller never crashes — it logs errors and continues. The worst case is stale prices for one poll interval.

---

## 9. Factory — `factory.py`

Selects the implementation at startup based on environment variables.

```python
"""Factory for creating market data sources."""

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

### Selection Logic

```
MASSIVE_API_KEY=""           → SimulatorDataSource (default)
MASSIVE_API_KEY not set      → SimulatorDataSource (default)
MASSIVE_API_KEY="sk-abc123"  → MassiveDataSource (real data)
```

The factory returns an **unstarted** source. The caller is responsible for `await source.start(tickers)`.

---

## 10. SSE Streaming Endpoint — `stream.py`

The bridge between the price cache and the frontend. Pushes updates to the browser via Server-Sent Events.

```python
"""SSE streaming endpoint for live price updates."""

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
    """Create the SSE streaming router with a reference to the price cache."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint: GET /api/stream/prices

        Streams all ticker prices every ~500ms as JSON events.
        Client connects with EventSource and receives:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Async generator yielding SSE-formatted price events."""
    # Tell browser to retry after 1 second on disconnect
    yield "retry: 1000\n\n"

    last_version = -1

    try:
        while True:
            if await request.is_disconnected():
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
        pass
```

### SSE Protocol

The SSE event format follows the W3C spec:

```
retry: 1000\n\n                          ← Sent once on connect
data: {"AAPL": {...}, "GOOGL": {...}}\n\n  ← Sent every ~500ms when data changes
data: {"AAPL": {...}, "GOOGL": {...}}\n\n
...
```

### Version-Based Change Detection

The generator tracks `price_cache.version`. If the version hasn't changed since the last push, no event is sent. This prevents sending redundant identical payloads:

- **Simulator** (500ms updates): Events flow ~every 500ms (every tick changes the version)
- **Massive** (15s polling): Events flow ~every 15s (version only bumps when a poll completes)
- **Between polls**: The SSE loop checks every 500ms but doesn't send since version is unchanged

### SSE Event Payload

Each event contains all tracked tickers as a JSON object:

```json
{
  "AAPL": {
    "ticker": "AAPL",
    "price": 191.50,
    "previous_price": 191.45,
    "timestamp": 1709913600.0,
    "change": 0.05,
    "change_percent": 0.0261,
    "direction": "up"
  },
  "GOOGL": {
    "ticker": "GOOGL",
    "price": 175.20,
    "previous_price": 175.35,
    "timestamp": 1709913600.0,
    "change": -0.15,
    "change_percent": -0.0855,
    "direction": "down"
  }
}
```

### Client-Side Connection

```javascript
const source = new EventSource('/api/stream/prices');

source.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices is { "AAPL": {...}, "GOOGL": {...}, ... }
  Object.values(prices).forEach(update => {
    updateTicker(update.ticker, update.price, update.direction);
  });
};

source.onerror = () => {
  // EventSource auto-reconnects using the retry: 1000 directive
  console.log('SSE connection lost, reconnecting...');
};
```

### Response Headers

| Header | Value | Purpose |
|--------|-------|---------|
| `Content-Type` | `text/event-stream` | SSE protocol requirement |
| `Cache-Control` | `no-cache` | Prevent caching of streaming response |
| `Connection` | `keep-alive` | Maintain long-lived connection |
| `X-Accel-Buffering` | `no` | Prevent nginx from buffering SSE events |

---

## 11. FastAPI Lifecycle Integration

The market data subsystem integrates with FastAPI's lifespan context manager.

```python
# backend/app/main.py (relevant excerpt)

from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle: start market data on startup, stop on shutdown."""

    # 1. Create the price cache (shared state)
    price_cache = PriceCache()

    # 2. Create the data source (simulator or Massive, based on env)
    market_source = create_market_data_source(price_cache)

    # 3. Load initial watchlist from database
    initial_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                       "NVDA", "META", "JPM", "V", "NFLX"]
    # In production: initial_tickers = await db.get_watchlist_tickers("default")

    # 4. Start the data source (begins background task)
    await market_source.start(initial_tickers)

    # 5. Store references in app.state for access by endpoints
    app.state.price_cache = price_cache
    app.state.market_source = market_source

    yield  # App is running

    # 6. Shutdown: stop the data source
    await market_source.stop()


app = FastAPI(lifespan=lifespan)

# Register the SSE streaming router
# (must be done after app creation but the cache reference is injected at startup)
```

### Startup Flow

```
1. PriceCache() created                         → empty cache
2. create_market_data_source(cache) called       → SimulatorDataSource or MassiveDataSource
3. Load watchlist from DB                        → ["AAPL", "GOOGL", ...]
4. await source.start(tickers)                   → cache seeded with initial prices
                                                  → background task starts
5. app.state populated                           → endpoints can access cache & source
6. App starts serving requests                   → SSE endpoint reads from cache
```

### Shutdown Flow

```
1. await source.stop()                           → background task cancelled
                                                  → resources released
2. PriceCache and source are garbage collected   → clean exit
```

---

## 12. Watchlist Coordination

When the user adds or removes a ticker from their watchlist, the backend must update both the database and the market data source.

### Adding a Ticker

```python
# In the watchlist API endpoint (POST /api/watchlist)
async def add_to_watchlist(ticker: str, request: Request):
    market_source = request.app.state.market_source

    # 1. Add to database
    await db.add_watchlist_ticker("default", ticker)

    # 2. Add to market data source (starts tracking immediately)
    await market_source.add_ticker(ticker)

    # The SSE stream will include the new ticker on the next cycle
```

### Removing a Ticker

```python
# In the watchlist API endpoint (DELETE /api/watchlist/{ticker})
async def remove_from_watchlist(ticker: str, request: Request):
    market_source = request.app.state.market_source

    # 1. Remove from database
    await db.remove_watchlist_ticker("default", ticker)

    # 2. Remove from market data source (also removes from PriceCache)
    await market_source.remove_ticker(ticker)

    # The SSE stream will exclude the ticker on the next cycle
```

### Behavior Differences by Source

| Action | SimulatorDataSource | MassiveDataSource |
|--------|-------------------|-------------------|
| `add_ticker("PYPL")` | Immediately creates GBM state, seeds cache with random price, rebuilds Cholesky | Adds to poll list; data appears on next API poll (~15s) |
| `remove_ticker("AAPL")` | Removes GBM state, removes from cache, rebuilds Cholesky | Removes from poll list, removes from cache |

---

## 13. Testing Strategy

### Unit Tests — Models

```python
# tests/market/test_models.py

def test_price_update_direction_up():
    update = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    assert update.direction == "up"
    assert update.change == 1.0
    assert update.change_percent > 0

def test_price_update_direction_flat():
    update = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
    assert update.direction == "flat"
    assert update.change == 0.0

def test_price_update_immutable():
    update = PriceUpdate(ticker="AAPL", price=190.0, previous_price=190.0)
    with pytest.raises(FrozenInstanceError):
        update.price = 200.0

def test_to_dict_includes_computed():
    update = PriceUpdate(ticker="AAPL", price=191.0, previous_price=190.0)
    d = update.to_dict()
    assert "direction" in d
    assert "change" in d
    assert "change_percent" in d
```

### Unit Tests — Cache

```python
# tests/market/test_cache.py

def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.0)
    assert update.direction == "flat"
    assert update.previous_price == 190.0

def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.0)
    assert cache.version == v0 + 1

def test_remove_clears_ticker():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert "AAPL" not in cache
```

### Unit Tests — Simulator

```python
# tests/market/test_simulator.py

def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    prices = sim.step()
    assert set(prices.keys()) == {"AAPL", "GOOGL"}

def test_prices_stay_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"], dt=1e-4)  # Larger dt for bigger moves
    for _ in range(10000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_add_remove_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("GOOGL")
    assert "GOOGL" in sim.get_tickers()
    sim.remove_ticker("GOOGL")
    assert "GOOGL" not in sim.get_tickers()

def test_cholesky_builds_for_full_default_set():
    """Ensure the correlation matrix works for all 10 default tickers."""
    sim = GBMSimulator(tickers=list(SEED_PRICES.keys()))
    prices = sim.step()
    assert len(prices) == 10
```

### Integration Tests — SimulatorDataSource

```python
# tests/market/test_simulator_source.py

@pytest.mark.asyncio
async def test_start_seeds_cache():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL", "GOOGL"])
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()

@pytest.mark.asyncio
async def test_add_ticker_seeds_immediately():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache)
    await source.start(["AAPL"])
    await source.add_ticker("GOOGL")
    assert cache.get("GOOGL") is not None
    await source.stop()
```

### Unit Tests — Factory

```python
# tests/market/test_factory.py

def test_no_key_returns_simulator(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_with_key_returns_massive(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)

def test_empty_key_returns_simulator(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "  ")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

### Test Coverage Summary

| Module | Target | Notes |
|--------|--------|-------|
| `models.py` | 100% | All properties and edge cases |
| `cache.py` | 100% | All methods including `__len__`, `__contains__` |
| `interface.py` | 100% | ABC — covered by implementations |
| `seed_prices.py` | 100% | Constants — covered by simulator tests |
| `simulator.py` | 98% | Exception log in `_run_loop` hard to trigger |
| `massive_client.py` | ~56% | Real API methods mocked; expected |
| `factory.py` | 100% | All branches covered |
| `stream.py` | ~31% | SSE generator requires ASGI test client |
| **Overall** | **~84%** | |

---

## 14. Error Handling & Edge Cases

### Simulator Edge Cases

| Scenario | Handling |
|----------|----------|
| Zero tickers | `step()` returns `{}` |
| Single ticker | Cholesky not used (set to None); univariate GBM |
| Unknown ticker added | Random seed price ($50–$300), default GBM params |
| `step()` exception | Caught in `_run_loop`, logged, loop continues |
| Double `add_ticker` | No-op (checked in `_add_ticker_internal`) |
| Double `remove_ticker` | No-op (checked in `remove_ticker`) |

### Massive Edge Cases

| Scenario | Handling |
|----------|----------|
| Invalid API key | 401 error logged, poll skipped, retried next interval |
| Rate limited | 429 error logged, poll skipped, retried next interval |
| Network failure | Exception logged, poll skipped, retried next interval |
| Malformed snapshot | Individual ticker skipped with warning, others processed |
| Empty ticker list | `_poll_once` returns immediately |
| Market closed | `last_trade.price` reflects last traded price (including after-hours) |
| `_client` is None | `_poll_once` returns immediately |

### SSE Edge Cases

| Scenario | Handling |
|----------|----------|
| Client disconnects | Detected via `request.is_disconnected()`, loop exits cleanly |
| Task cancelled | `CancelledError` caught, loop exits cleanly |
| Empty cache | No event sent (waits for data) |
| No version change | No event sent (avoids redundant payloads) |

### Cache Edge Cases

| Scenario | Handling |
|----------|----------|
| First update for ticker | `previous_price = price`, `direction = "flat"` |
| Remove unknown ticker | `pop(ticker, None)` — no-op |
| Concurrent access | `threading.Lock` ensures serialized access |
| `get()` unknown ticker | Returns `None` |
| `get_price()` unknown ticker | Returns `None` |

---

## 15. Configuration Summary

### Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `MASSIVE_API_KEY` | (empty) | Empty/unset → Simulator; non-empty → Massive API |

### Tunable Constants

| Constant | Location | Default | Description |
|----------|----------|---------|-------------|
| `DEFAULT_DT` | `simulator.py` | 8.48e-8 | Time step as fraction of trading year |
| `event_probability` | `GBMSimulator.__init__` | 0.001 | Random shock probability per tick per ticker |
| `update_interval` | `SimulatorDataSource.__init__` | 0.5s | How often the simulator steps |
| `poll_interval` | `MassiveDataSource.__init__` | 15.0s | How often to poll Massive API |
| SSE `interval` | `_generate_events` | 0.5s | How often the SSE loop checks for changes |
| SSE `retry` | `_generate_events` | 1000ms | Browser reconnection delay |

### Dependencies

```toml
# backend/pyproject.toml
[project]
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",
    "massive>=1.0.0",
    "rich>=13.0.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.24.0",
    "pytest-cov>=6.0.0",
    "ruff>=0.8.0",
]
```

---

## Appendix: Data Flow Diagram

```
┌──────────────┐     500ms      ┌──────────────┐     500ms      ┌──────────────┐
│  GBMSimulator │────step()────▶│  PriceCache   │◀───get_all()───│  SSE Stream  │
│  (10 tickers) │   dict[str,   │  (Lock-based) │   dict[str,    │  (per client)│
│               │    float]     │               │  PriceUpdate]  │              │
└──────────────┘               │  .version ────┤               │  "data: {}"  │
                                │   (monotonic)  │  version-based │  ──────────▶ │
     OR                        │               │  skip if same  │   Browser    │
                                │               │               │  EventSource │
┌──────────────┐     15s       │               │               └──────────────┘
│ Massive API   │───poll()────▶│               │
│ (REST client) │  to_thread   │               │◀───get_price()──  Trade Exec
│               │              │               │◀───get_all()────  Portfolio
└──────────────┘               └──────────────┘
```
