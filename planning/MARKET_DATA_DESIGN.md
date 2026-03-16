# Market Data Backend — Detailed Implementation Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive API client, SSE streaming endpoint, and FastAPI lifecycle integration with complete code snippets.

Everything lives under `backend/app/market/` (8 modules, ~500 lines).

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure & Module Map](#2-file-structure--module-map)
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
┌────────────────────────────────────────────────────────────────┐
│                    Market Data Subsystem                       │
│                                                                │
│  MarketDataSource (ABC)                                        │
│  ├── SimulatorDataSource  →  GBM simulator (default)           │
│  └── MassiveDataSource    →  Polygon.io REST poller            │
│          │                                                     │
│          ▼                                                     │
│     PriceCache (thread-safe, in-memory, versioned)             │
│          │                                                     │
│          ├──→ SSE stream endpoint (GET /api/stream/prices)     │
│          ├──→ Portfolio valuation (GET /api/portfolio)          │
│          ├──→ Trade execution (POST /api/portfolio/trade)       │
│          └──→ Watchlist enrichment (GET /api/watchlist)         │
└────────────────────────────────────────────────────────────────┘
```

### Design Principles

- **Strategy pattern**: Both data sources implement the same ABC. All downstream code is source-agnostic.
- **PriceCache as single point of truth**: Producers write, consumers read. No direct coupling.
- **Environment-driven selection**: `MASSIVE_API_KEY` present → real data. Absent → simulator.
- **Dynamic watchlist**: Tickers can be added/removed at runtime without restart.
- **Thread-safe reads**: Multiple async consumers can read the cache concurrently.

### Data Flow

```
                     ┌──────────────┐
                     │  Environment │
                     │  Variables   │
                     └──────┬───────┘
                            │ MASSIVE_API_KEY?
                            ▼
                     ┌──────────────┐
                     │   Factory    │
                     │  factory.py  │
                     └──────┬───────┘
                   ┌────────┴────────┐
                   ▼                 ▼
          ┌─────────────┐   ┌──────────────┐
          │  Simulator   │   │   Massive    │
          │ (every 0.5s) │   │ (every 15s)  │
          └──────┬───────┘   └──────┬───────┘
                 │                   │
                 └────────┬──────────┘
                          ▼
                   ┌──────────────┐
                   │  PriceCache  │  ← version counter increments
                   └──────┬───────┘
                          │
              ┌───────────┼───────────┐
              ▼           ▼           ▼
         SSE Stream   Portfolio   Trade Exec
         (stream.py)  Valuation   Pricing
```

---

## 2. File Structure & Module Map

```
backend/app/market/
├── __init__.py          # Public API: 5 exports
├── models.py            # PriceUpdate dataclass (immutable, frozen)
├── interface.py         # MarketDataSource ABC (5 abstract methods)
├── cache.py             # PriceCache (thread-safe, Lock-based, versioned)
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS
├── simulator.py         # GBMSimulator (math) + SimulatorDataSource (async)
├── massive_client.py    # MassiveDataSource (REST polling via asyncio.to_thread)
├── factory.py           # create_market_data_source() (env-driven selection)
└── stream.py            # create_stream_router() (SSE endpoint factory)
```

### Public API (`__init__.py`)

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

Every downstream module imports from `app.market` — never from submodules directly:

```python
from app.market import PriceCache, PriceUpdate, create_market_data_source
```

---

## 3. Data Model — `models.py`

`PriceUpdate` is the **only** data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution — works exclusively with this type.

### Full Implementation

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

### Design Decisions

| Decision | Rationale |
|----------|-----------|
| `frozen=True` | Immutability prevents accidental mutation after cache insertion |
| `slots=True` | Memory-efficient; faster attribute access (no `__dict__`) |
| Computed properties (not stored fields) | `change`, `change_percent`, `direction` always derived from `price` and `previous_price` — no stale data |
| `to_dict()` method | Single serialization point for SSE and REST API responses |
| `previous_price == 0` guard | Prevents division-by-zero in `change_percent` |

### Usage Examples

```python
# Creating directly (rarely needed — PriceCache.update() creates these)
update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.25)
print(update.direction)       # "up"
print(update.change)          # 1.25
print(update.change_percent)  # 0.6569

# Serialization for SSE/JSON
import json
json.dumps(update.to_dict())
# {"ticker":"AAPL","price":191.5,"previous_price":190.25,"timestamp":1710507600.1,
#  "change":1.25,"change_percent":0.6569,"direction":"up"}
```

---

## 4. Price Cache — `cache.py`

The `PriceCache` is the **single point of truth** for current prices. It decouples data producers (simulator or Massive) from consumers (SSE, portfolio, trades).

### Full Implementation

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

### Key Behaviors

- **Auto-computes `previous_price`**: On first update for a ticker, `previous_price == price` (direction is "flat"). Subsequent updates use the cached price as `previous_price`.
- **Prices rounded to 2 decimals on write**: Prevents floating-point noise from accumulating in the simulator.
- **Version counter**: Monotonically increasing integer, incremented on every `update()` call. The SSE endpoint uses this for efficient change detection — only push when version changes.
- **Thread-safe with `Lock`**: Required because the Massive client runs API calls via `asyncio.to_thread()`, which means cache writes can happen from a different thread.

### Usage by Downstream Consumers

```python
# Trade execution — get current price to fill a market order
price = cache.get_price("AAPL")
if price is None:
    raise ValueError("No price available for AAPL")
# Execute trade at this price...

# Portfolio valuation — get all prices to compute total value
all_prices = cache.get_all()
total_value = cash_balance
for position in positions:
    current = all_prices.get(position.ticker)
    if current:
        total_value += position.quantity * current.price

# Watchlist enrichment — return watchlist entries with live prices
update = cache.get("AAPL")
if update:
    enriched = {
        "ticker": "AAPL",
        "price": update.price,
        "change": update.change,
        "change_percent": update.change_percent,
        "direction": update.direction,
    }

# SSE change detection — only send when cache has new data
if cache.version != last_seen_version:
    # New data available — push to client
    last_seen_version = cache.version
```

---

## 5. Abstract Interface — `interface.py`

The `MarketDataSource` ABC defines the contract that both the simulator and Massive client implement. Downstream code never depends on a concrete implementation.

### Full Implementation

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

### Method Contract Summary

| Method | When Called | Side Effects |
|--------|-----------|--------------|
| `start(tickers)` | App startup (once) | Creates background task, seeds cache |
| `stop()` | App shutdown | Cancels background task, idempotent |
| `add_ticker(t)` | User adds to watchlist | Ticker appears in next update cycle |
| `remove_ticker(t)` | User removes from watchlist | Stops tracking + removes from cache |
| `get_tickers()` | Informational | None — read-only |

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Configuration data for the GBM simulator. Separated into its own module for clarity and testability.

### Full Implementation

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
    "AAPL": {"sigma": 0.22, "mu": 0.05},   # Moderate
    "GOOGL": {"sigma": 0.25, "mu": 0.05},  # Moderate
    "MSFT": {"sigma": 0.20, "mu": 0.05},   # Steady
    "AMZN": {"sigma": 0.28, "mu": 0.05},   # Slightly volatile
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # Very volatile, low drift
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # Volatile, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},   # Moderate-high
    "JPM": {"sigma": 0.18, "mu": 0.04},    # Stable (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # Stable (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},   # Volatile
}

# Default parameters for dynamically added tickers
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing (despite being in tech set)
```

### Behavior for Unknown Tickers

When a ticker is dynamically added that isn't in `SEED_PRICES`:
- **Seed price**: Random value between $50–$300
- **GBM parameters**: `sigma=0.25, mu=0.05` (moderate)
- **Correlation**: 0.3 with all other tickers (cross-group default)

---

## 7. GBM Simulator — `simulator.py`

Two classes in one file: the synchronous math engine (`GBMSimulator`) and the async lifecycle wrapper (`SimulatorDataSource`).

### Mathematical Model

Each price step follows discrete Geometric Brownian Motion:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

| Symbol | Meaning | Value |
|--------|---------|-------|
| `S(t)` | Current price | Per-ticker |
| `μ` | Annualized drift | 0.03–0.08 |
| `σ` | Annualized volatility | 0.17–0.50 |
| `dt` | Time step (fraction of trading year) | ~8.48e-8 |
| `Z` | Correlated standard normal | Via Cholesky |

**Time scaling**: `dt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` — 500ms expressed as a fraction of a trading year. This tiny dt produces sub-cent moves per tick that accumulate naturally.

### Correlated Moves via Cholesky Decomposition

```
1. Build n×n correlation matrix C where C[i][j] = pairwise_correlation(i, j)
2. Cholesky decompose: L = cholesky(C)  (lower triangular, L × Lᵀ = C)
3. Each tick: z_independent = N(0,1)^n  →  z_correlated = L @ z_independent
4. Use z_correlated[i] as Z in GBM for ticker i
```

The Cholesky matrix is **rebuilt** whenever tickers are added or removed (infrequent user-initiated operation).

### GBMSimulator — Full Implementation

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
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS,
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES,
    TICKER_PARAMS, TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

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
        """Advance all tickers by one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

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

            # Random shock event: ~0.1% chance per tick per ticker
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker, shock_magnitude * 100,
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
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
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
                corr[i, j] = rho
                corr[j, i] = rho
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

### Correlation Matrix Example (10 default tickers)

```
         AAPL  GOOGL  MSFT  AMZN  TSLA  NVDA  META  JPM    V   NFLX
AAPL      1.0   0.6   0.6   0.6   0.3   0.6   0.6   0.3  0.3   0.6
GOOGL     0.6   1.0   0.6   0.6   0.3   0.6   0.6   0.3  0.3   0.6
MSFT      0.6   0.6   1.0   0.6   0.3   0.6   0.6   0.3  0.3   0.6
AMZN      0.6   0.6   0.6   1.0   0.3   0.6   0.6   0.3  0.3   0.6
TSLA      0.3   0.3   0.3   0.3   1.0   0.3   0.3   0.3  0.3   0.3
NVDA      0.6   0.6   0.6   0.6   0.3   1.0   0.6   0.3  0.3   0.6
META      0.6   0.6   0.6   0.6   0.3   0.6   1.0   0.3  0.3   0.6
JPM       0.3   0.3   0.3   0.3   0.3   0.3   0.3   1.0  0.5   0.3
V         0.3   0.3   0.3   0.3   0.3   0.3   0.3   0.5  1.0   0.3
NFLX      0.6   0.6   0.6   0.6   0.3   0.6   0.6   0.3  0.3   1.0
```

### Random Shock Events

- **Probability**: 0.1% per tick per ticker (`event_probability = 0.001`)
- **Magnitude**: 2–5% of current price (uniformly random)
- **Direction**: 50/50 up or down
- **Expected frequency**: With 10 tickers at 2 ticks/second → ~1 event every 50 seconds
- Applied **after** the GBM step so they compound on top of normal movement

### SimulatorDataSource — Full Implementation

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

### Expected Price Behavior (5-minute demo session, ~600 ticks)

| Ticker | σ | Price | Expected Range | Character |
|--------|---|-------|---------------|-----------|
| JPM | 0.18 | $195 | ±$0.20–0.50 | Gentle drift |
| V | 0.17 | $280 | ±$0.25–0.60 | Gentle drift |
| AAPL | 0.22 | $190 | ±$0.30–0.80 | Steady |
| MSFT | 0.20 | $420 | ±$0.50–1.20 | Steady (high price amplifies) |
| TSLA | 0.50 | $250 | ±$1.00–3.00 | Wild swings |
| NVDA | 0.40 | $800 | ±$2.00–5.00 | Large moves |

Plus 6–12 random shock events adding 2–5% sudden jumps for visual excitement.

---

## 8. Massive API Client — `massive_client.py`

REST polling client for the Massive (formerly Polygon.io) API, providing real market data when a `MASSIVE_API_KEY` is configured.

### Full Implementation

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

        # Immediate first poll so cache has data right away
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
            # The Massive RESTClient is synchronous — run in a thread
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps: milliseconds → seconds
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
                        getattr(snap, "ticker", "???"), e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop will retry on the next interval.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### API Response Mapping

The Massive `get_snapshot_all()` call returns snapshot objects. We extract:

| SDK Path | Description | Used For |
|----------|-------------|----------|
| `snap.ticker` | Ticker symbol | Cache key |
| `snap.last_trade.price` | Latest trade price | `PriceCache.update(price=)` |
| `snap.last_trade.timestamp` | Trade timestamp (ms) | `PriceCache.update(timestamp=)` — divided by 1000 |

### Behavioral Differences: Simulator vs. Massive

| Behavior | Simulator | Massive |
|----------|-----------|---------|
| Update frequency | Every 0.5s | Every 15s (free tier) |
| First data available | Immediately (seed prices) | After first successful poll |
| Unknown tickers | Random seed price $50–$300 | No data until market recognizes it |
| Market hours | Always active (24/7) | Data stale outside trading hours |
| Network required | No | Yes |
| Add ticker latency | Instant (next step) | Next poll cycle (up to 15s) |
| Error recovery | Log + continue | Log + retry next interval |

### Error Handling Strategy

```
Poll fails (any exception)
  → Log error
  → Don't re-raise
  → Loop continues
  → Retry on next interval (15s)

Malformed snapshot (missing last_trade, etc.)
  → Log warning with ticker name
  → Skip that ticker
  → Process remaining snapshots normally
```

This ensures the poller never crashes — it degrades gracefully on API errors, rate limits, network issues, or malformed responses.

---

## 9. Factory — `factory.py`

Single decision point: which data source to use. Made once at startup. No runtime switching.

### Full Implementation

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

### Selection Logic

```
MASSIVE_API_KEY=""         → SimulatorDataSource
MASSIVE_API_KEY not set    → SimulatorDataSource
MASSIVE_API_KEY="sk-..."   → MassiveDataSource
```

The returned source is **unstarted**. The caller must:
```python
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", ...])  # Begin producing prices
```

---

## 10. SSE Streaming Endpoint — `stream.py`

The SSE (Server-Sent Events) endpoint reads from `PriceCache` and pushes updates to connected browser clients. It is the bridge between the backend price producers and the frontend.

### Full Implementation

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
    """Create the SSE streaming router with a reference to the price cache."""

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
    """Async generator that yields SSE-formatted price events."""
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

### SSE Protocol Details

**First event** (client configuration):
```
retry: 1000\n\n
```
This tells the browser's `EventSource` to auto-reconnect after 1 second if the connection drops.

**Subsequent events** (price data):
```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.25,"timestamp":1710507600.1,"change":0.25,"change_percent":0.1316,"direction":"up"},"GOOGL":{...}}\n\n
```

**Change detection**: The endpoint polls `price_cache.version` every 500ms. Only sends data when the version has changed since the last push. This avoids redundant payloads when the Massive poller updates less frequently (every 15s).

### Frontend Client Usage

```typescript
const source = new EventSource("/api/stream/prices");

source.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  // Update watchlist, sparklines, portfolio values...
  for (const [ticker, update] of Object.entries(prices)) {
    updateTickerPrice(ticker, update);
  }
};

source.onerror = () => {
  // EventSource automatically reconnects (retry: 1000 directive)
  showReconnectingIndicator();
};
```

---

## 11. FastAPI Lifecycle Integration

How the market data subsystem plugs into the FastAPI application.

### Startup & Shutdown

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan handler — manages market data lifecycle."""

    # --- Startup ---
    cache = PriceCache()
    source = create_market_data_source(cache)

    # Default watchlist tickers (loaded from DB in production)
    default_tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                       "NVDA", "META", "JPM", "V", "NFLX"]
    await source.start(default_tickers)

    # Store references for dependency injection
    app.state.price_cache = cache
    app.state.market_source = source

    yield  # App is running

    # --- Shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

# Mount SSE router (must happen after app creation)
# Note: create_stream_router is called during module setup, but the
# route handler closures capture price_cache from app.state at request time
stream_router = create_stream_router(app.state.price_cache)
app.include_router(stream_router)
```

### Dependency Injection Pattern

Other endpoints access the cache and source via `app.state`:

```python
from fastapi import Request

@app.get("/api/portfolio")
async def get_portfolio(request: Request):
    cache: PriceCache = request.app.state.price_cache
    # Use cache.get_price("AAPL") for valuations...

@app.post("/api/portfolio/trade")
async def execute_trade(request: Request, trade: TradeRequest):
    cache: PriceCache = request.app.state.price_cache
    price = cache.get_price(trade.ticker)
    if price is None:
        raise HTTPException(400, f"No price available for {trade.ticker}")
    # Execute at current price...

@app.post("/api/watchlist")
async def add_to_watchlist(request: Request, body: WatchlistAdd):
    source: MarketDataSource = request.app.state.market_source
    await source.add_ticker(body.ticker)
    # Also insert into DB...

@app.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(request: Request, ticker: str):
    source: MarketDataSource = request.app.state.market_source
    await source.remove_ticker(ticker)
    # Also delete from DB...
```

---

## 12. Watchlist Coordination

The watchlist is stored in SQLite for persistence and mirrored in the market data source for live price updates. Operations must keep both in sync.

### Add Ticker Flow

```
User action: Add "PYPL" to watchlist
  1. Validate ticker format (1-5 uppercase alpha chars)
  2. INSERT into watchlist table (DB)
  3. await source.add_ticker("PYPL")
     ├── Simulator: immediately seeds cache, included in next step()
     └── Massive: appended to ticker list, fetched on next poll cycle
  4. Return enriched watchlist entry (with price from cache, if available)
```

### Remove Ticker Flow

```
User action: Remove "NFLX" from watchlist
  1. DELETE from watchlist table (DB)
  2. await source.remove_ticker("NFLX")
     ├── Simulator: removed from GBMSimulator + cache.remove()
     └── Massive: removed from ticker list + cache.remove()
  3. Return success
```

### On App Startup

```
1. Load watchlist tickers from DB
2. Pass to source.start(tickers)
   → Simulator seeds cache immediately
   → Massive does immediate first poll
3. SSE endpoint starts streaming from cache
4. Frontend connects and receives first data within ~500ms
```

---

## 13. Testing Strategy

### Test Structure

```
backend/tests/market/
├── test_models.py              # 11 tests — PriceUpdate creation, properties, serialization
├── test_cache.py               # 13 tests — CRUD, version tracking, thread safety
├── test_simulator.py           # 17 tests — GBM math, Cholesky, shock events
├── test_simulator_source.py    # 10 tests — async lifecycle, cache integration
├── test_factory.py             # 7 tests  — env-driven selection
└── test_massive.py             # 13 tests — polling, error handling, snapshot parsing
```

**73 tests total, 84% coverage.**

### Example: Testing PriceUpdate

```python
def test_price_update_up_direction():
    update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.25)
    assert update.direction == "up"
    assert update.change == 1.25
    assert update.change_percent == pytest.approx(0.6569, abs=0.0001)

def test_price_update_flat_on_same_price():
    update = PriceUpdate(ticker="AAPL", price=190.00, previous_price=190.00)
    assert update.direction == "flat"
    assert update.change == 0.0

def test_price_update_to_dict():
    update = PriceUpdate(ticker="AAPL", price=191.50, previous_price=190.25, timestamp=1000.0)
    d = update.to_dict()
    assert d["ticker"] == "AAPL"
    assert d["price"] == 191.50
    assert d["direction"] == "up"
```

### Example: Testing PriceCache

```python
def test_cache_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.00)
    assert update.direction == "flat"
    assert update.previous_price == 190.00

def test_cache_version_increments():
    cache = PriceCache()
    assert cache.version == 0
    cache.update("AAPL", 190.00)
    assert cache.version == 1
    cache.update("AAPL", 191.00)
    assert cache.version == 2

def test_cache_remove():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
```

### Example: Testing GBMSimulator

```python
def test_gbm_prices_always_positive():
    """GBM guarantees positive prices (exp() never returns negative)."""
    sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_gbm_correlated_moves():
    """Tech stocks should correlate more than cross-sector pairs."""
    sim = GBMSimulator(tickers=["AAPL", "MSFT", "JPM"], event_probability=0.0)
    aapl_moves, msft_moves, jpm_moves = [], [], []
    prev = {t: sim.get_price(t) for t in ["AAPL", "MSFT", "JPM"]}

    for _ in range(5000):
        prices = sim.step()
        aapl_moves.append(prices["AAPL"] - prev["AAPL"])
        msft_moves.append(prices["MSFT"] - prev["MSFT"])
        jpm_moves.append(prices["JPM"] - prev["JPM"])
        prev = prices

    tech_corr = np.corrcoef(aapl_moves, msft_moves)[0, 1]
    cross_corr = np.corrcoef(aapl_moves, jpm_moves)[0, 1]
    assert tech_corr > cross_corr  # Tech stocks correlate more

def test_gbm_shock_events_occur():
    """With high event probability, shocks should produce large moves."""
    sim = GBMSimulator(tickers=["AAPL"], event_probability=1.0)  # Always shock
    prices = sim.step()
    change_pct = abs(prices["AAPL"] - 190.00) / 190.00
    assert change_pct > 0.01  # At least 1% move from shock
```

### Example: Testing SimulatorDataSource (async)

```python
@pytest.mark.asyncio
async def test_simulator_seeds_cache_on_start():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])

    assert cache.get_price("AAPL") is not None
    assert cache.get_price("GOOGL") is not None
    await source.stop()

@pytest.mark.asyncio
async def test_simulator_add_ticker_runtime():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL"])

    await source.add_ticker("TSLA")
    assert cache.get_price("TSLA") is not None
    assert "TSLA" in source.get_tickers()
    await source.stop()
```

### Example: Testing Factory

```python
def test_factory_returns_simulator_by_default(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)

def test_factory_returns_massive_with_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key-123")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, MassiveDataSource)

def test_factory_ignores_empty_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "  ")
    cache = PriceCache()
    source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

### Running Tests

```bash
cd backend
uv run --extra dev pytest -v                     # All tests
uv run --extra dev pytest tests/market/ -v       # Market tests only
uv run --extra dev pytest --cov=app.market       # With coverage
uv run --extra dev ruff check app/market/ tests/ # Lint
```

---

## 14. Error Handling & Edge Cases

### Simulator Error Handling

| Scenario | Handling |
|----------|----------|
| `step()` raises exception | Caught in `_run_loop`, logged, loop continues |
| Task cancelled (shutdown) | `CancelledError` breaks the loop cleanly |
| Empty ticker list | `step()` returns `{}`, no error |
| Duplicate `add_ticker()` | No-op (guard in `_add_ticker_internal`) |
| `remove_ticker()` for unknown ticker | No-op (guard check) |

### Massive Error Handling

| Scenario | Handling |
|----------|----------|
| API call fails (network, 5xx) | Logged, retry on next poll interval |
| 401/403 (bad API key) | Logged, retry on next interval (won't self-heal) |
| 429 (rate limit) | Logged, retry on next interval (natural backoff) |
| Malformed snapshot (missing `last_trade`) | Warning logged, that ticker skipped, others processed |
| `asyncio.to_thread()` exception | Caught in `_poll_once`, logged, loop continues |

### PriceCache Edge Cases

| Scenario | Handling |
|----------|----------|
| `get()` for unknown ticker | Returns `None` |
| `get_price()` for unknown ticker | Returns `None` |
| `remove()` for unknown ticker | No-op (`dict.pop(key, None)`) |
| First `update()` for a ticker | `previous_price = price` (direction = "flat") |
| `previous_price == 0` | `change_percent` returns `0.0` (division guard) |

### SSE Edge Cases

| Scenario | Handling |
|----------|----------|
| Client disconnects | Detected via `request.is_disconnected()`, generator exits |
| No prices in cache | No `data:` event sent (empty dict check) |
| Cache unchanged since last push | Skipped (version check) |
| Stream cancelled by server | `CancelledError` caught, logged cleanly |

---

## 15. Configuration Summary

### Environment Variables

| Variable | Default | Effect |
|----------|---------|--------|
| `MASSIVE_API_KEY` | _(empty)_ | Empty/unset → simulator; set → Massive REST API |

### Tunable Constants

| Constant | Location | Default | Description |
|----------|----------|---------|-------------|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | How often the simulator steps |
| `event_probability` | `GBMSimulator.__init__` | `0.001` (0.1%) | Shock event probability per tick per ticker |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | How often to poll the Massive API |
| `DEFAULT_DT` | `GBMSimulator` class | `~8.48e-8` | Time step as fraction of trading year |
| `interval` | `_generate_events()` | `0.5` (seconds) | SSE push cadence |
| `SEED_PRICES` | `seed_prices.py` | 10 tickers | Starting prices for known tickers |
| `TICKER_PARAMS` | `seed_prices.py` | Per-ticker σ, μ | GBM volatility and drift |
| `DEFAULT_PARAMS` | `seed_prices.py` | `σ=0.25, μ=0.05` | For dynamically added tickers |
| `INTRA_TECH_CORR` | `seed_prices.py` | `0.6` | Correlation within tech sector |
| `INTRA_FINANCE_CORR` | `seed_prices.py` | `0.5` | Correlation within finance sector |
| `CROSS_GROUP_CORR` | `seed_prices.py` | `0.3` | Cross-sector correlation |
| `TSLA_CORR` | `seed_prices.py` | `0.3` | TSLA's correlation with everything |

### Dependencies

| Package | Required For | Notes |
|---------|-------------|-------|
| `numpy` | GBM simulator | Random normals, Cholesky decomposition |
| `fastapi` | SSE endpoint | `APIRouter`, `StreamingResponse`, `Request` |
| `massive` | Massive client | Only needed when `MASSIVE_API_KEY` is set |
| `asyncio` (stdlib) | Background tasks | Task scheduling, `to_thread()` |
| `threading` (stdlib) | PriceCache | `Lock` for thread safety |
