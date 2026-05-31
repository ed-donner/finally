# Market Data Backend — Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified Python interface, the in-memory price cache, the GBM-based simulator, the Massive (Polygon.io) REST client, the SSE streaming endpoint, and FastAPI lifecycle integration.

All code in this document lives under `backend/app/market/`.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Layout](#2-file-layout)
3. [Data Model (`models.py`)](#3-data-model-modelspy)
4. [Price Cache (`cache.py`)](#4-price-cache-cachepy)
5. [Unified Interface (`interface.py`)](#5-unified-interface-interfacepy)
6. [Seed Prices & GBM Parameters (`seed_prices.py`)](#6-seed-prices--gbm-parameters-seed_pricespy)
7. [Simulator (`simulator.py`)](#7-simulator-simulatorpy)
8. [Massive API Client (`massive_client.py`)](#8-massive-api-client-massive_clientpy)
9. [Factory (`factory.py`)](#9-factory-factorypy)
10. [SSE Streaming Endpoint (`stream.py`)](#10-sse-streaming-endpoint-streampy)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist Coordination](#12-watchlist-coordination)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Summary](#15-configuration-summary)

---

## 1. Architecture Overview

The market data subsystem follows a **strategy pattern**: two interchangeable data sources implement the same abstract interface and write into a shared in-memory cache. Downstream consumers (SSE, trade execution, portfolio valuation) only ever touch the cache — they never know or care which data source is active.

```
                ┌──────────────────────────────────────┐
                │ create_market_data_source(cache)     │
                │   (reads MASSIVE_API_KEY env var)    │
                └────────────┬─────────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
  ┌────────────────────────┐    ┌────────────────────────┐
  │ SimulatorDataSource    │    │ MassiveDataSource      │
  │  (GBM, 500ms tick)     │    │  (REST poll, 15s tick) │
  └───────────┬────────────┘    └───────────┬────────────┘
              │  writes                     │  writes
              └──────────┬──────────────────┘
                         ▼
                  ┌──────────────┐
                  │ PriceCache   │  ← thread-safe, in-memory
                  │ (versioned)  │
                  └──────┬───────┘
                         │ reads
              ┌──────────┼──────────┐
              ▼          ▼          ▼
       SSE /api/    Trade        Portfolio
       stream/      execution    valuation
       prices
```

**Why this shape?**

| Decision | Rationale |
|---|---|
| Strategy pattern (ABC) | The simulator and Massive client can be swapped at startup with zero changes elsewhere. |
| Shared cache as the only contract | Decouples producer timing (500ms vs 15s) from consumer timing (SSE at 500ms). |
| Push-into-cache rather than return-prices | The data source owns its update cadence; consumers read a snapshot at their own rate. |
| In-memory cache (not Redis/etc.) | Single-container, single-process app. No distributed state needed. |
| `threading.Lock` (not `asyncio.Lock`) | The Massive client is synchronous and runs via `asyncio.to_thread()` in a real OS thread; an `asyncio.Lock` would not protect it. |

---

## 2. File Layout

```
backend/
  app/
    market/
      __init__.py             # Re-exports the public API
      models.py               # PriceUpdate dataclass
      cache.py                # PriceCache (thread-safe, in-memory)
      interface.py            # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, correlation groups
      simulator.py            # GBMSimulator + SimulatorDataSource
      massive_client.py       # MassiveDataSource (Polygon.io REST poller)
      factory.py              # create_market_data_source()
      stream.py               # SSE FastAPI router factory
```

Public surface (re-exported from `app/market/__init__.py`):

```python
"""Market data subsystem for FinAlly."""

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

The rest of the backend should import from `app.market`, never from submodules.

---

## 3. Data Model (`models.py`)

`PriceUpdate` is the **only** type that leaves the market data layer. Every downstream consumer works with this immutable value object.

```python
# backend/app/market/models.py
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
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

**Why these choices?**

- `frozen=True` — instances are shared across async tasks without copying.
- `slots=True` — minor memory win; many instances per second.
- Computed `change` / `direction` / `change_percent` — derived properties can never be inconsistent with `price` and `previous_price`.
- `to_dict()` — the single serialization point used by both SSE and REST responses.

---

## 4. Price Cache (`cache.py`)

The central data hub. Data sources write; SSE / portfolio / trade execution read. Must be thread-safe because the Massive client's synchronous call runs in a thread executor.

```python
# backend/app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Bumped on every update — used by SSE to skip no-op pushes

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the new PriceUpdate.

        On first update for a ticker, previous_price == price (direction='flat').
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
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
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)  # shallow copy

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Version counter: why?

The SSE loop runs at 500ms. The Massive poller only updates the cache every 15 seconds. Without a version counter, the SSE endpoint would re-serialize and re-send the same payload 30 times per Massive poll. The version counter lets the SSE loop skip pushes when nothing has changed:

```python
last_version = -1
while True:
    if cache.version != last_version:
        last_version = cache.version
        yield format_sse(cache.get_all())
    await asyncio.sleep(0.5)
```

---

## 5. Unified Interface (`interface.py`)

The contract that both data sources implement.

```python
# backend/app/market/interface.py
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
        Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

The interface deliberately has **no** `get_price()` method. Consumers go through the cache, not the source.

---

## 6. Seed Prices & GBM Parameters (`seed_prices.py`)

Constants only. No logic, no non-stdlib imports.

```python
# backend/app/market/seed_prices.py
"""Seed prices, per-ticker GBM parameters, and correlation groups."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  185.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     280.00,
    "NFLX":  600.00,
}

# Per-ticker GBM parameters
#   sigma — annualized volatility (higher = larger moves)
#   mu    — annualized drift (expected return)
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # high vol
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # high vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers added dynamically (not in TICKER_PARAMS)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector membership for the correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR    = 0.6   # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR   = 0.3   # between sectors / unknown
TSLA_CORR          = 0.3   # TSLA does its own thing
```

---

## 7. Simulator (`simulator.py`)

Two classes in one file:
- `GBMSimulator` — pure math engine. Stateful; advances prices one step at a time.
- `SimulatorDataSource` — `MarketDataSource` implementation wrapping the simulator in an async loop.

### 7.1 GBM Math Recap

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

With a 500ms tick and 252 trading days × 6.5 hours per day:

```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.5e-8
```

That tiny `dt` produces sub-cent moves per tick that accumulate naturally over time. Prices stay positive because `exp(...)` is always positive.

**Correlated moves** are achieved by Cholesky-decomposing a sector-based correlation matrix `C = L L^T` and computing `Z_correlated = L @ Z_independent`.

**Random shocks** add visual drama: every step, each ticker has a small (~0.1%) chance of a 2–5% pop or drop.

### 7.2 `GBMSimulator`

```python
# backend/app/market/simulator.py
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
    # 252 trading days * 6.5 hours * 3600 seconds = 5,896,800 trading seconds/year
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

    # ---- Public API ----

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        This is the hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        # n independent standard-normal draws, then correlate via Cholesky
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            # GBM step
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock: ~0.1% per tick per ticker → ~1 event every 50s with 10 tickers
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random shock on %s: %.1f%% %s",
                    ticker,
                    shock_magnitude * 100,
                    "up" if shock_sign > 0 else "down",
                )

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
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

    # ---- Internals ----

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (used for batch init)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Recompute the Cholesky factor of the correlation matrix.

        O(n^2) build + O(n^3) decomposition, but n < 50 in practice.
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
        """Sector-based pairwise correlation.

        Same tech sector   → 0.6
        Same finance sector → 0.5
        TSLA with anything → 0.3
        Cross-sector / unknown → 0.3
        """
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

### 7.3 `SimulatorDataSource`

Wraps the math engine in a cancellable background task that writes to the cache.

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

        # Seed the cache with starting prices so SSE has data on its first tick.
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
        if not self._sim:
            return
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
        """Step the simulator, write to cache, sleep, repeat."""
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")  # Keep running on transient errors
            await asyncio.sleep(self._interval)
```

**Key behaviors:**
- **Immediate seeding** — the cache has seed prices before the loop's first tick. The frontend never sees a blank screen.
- **Cooperative cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`. Clean FastAPI lifespan teardown.
- **Per-step exception isolation** — a bad tick logs and continues; the feed stays up.

---

## 8. Massive API Client (`massive_client.py`)

Polls the Massive (formerly Polygon.io) REST snapshot endpoint. The synchronous Massive client is wrapped in `asyncio.to_thread()` so it doesn't block the event loop.

### 8.1 What we call

Single endpoint covers everything for live polling:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT,...
```

A **single** API call returns all watched tickers — critical for staying under the free tier's 5 req/min limit. The Python SDK exposes this as:

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

### 8.2 `MassiveDataSource`

```python
# backend/app/market/massive_client.py
from __future__ import annotations

import asyncio
import logging
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls /v2/snapshot/locale/us/markets/stocks/tickers for all watched tickers
    in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min  → poll every 15s (default)
      - Paid tiers: higher    → poll every 2-5s
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
        self._client: Any = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = [t.upper().strip() for t in tickers]

        # Immediate first poll so the cache has data before the SSE endpoint starts
        await self._poll_once()

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval",
            len(self._tickers),
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
            logger.info("Massive: added %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # ---- Internals ----

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll cycle: fetch snapshots, write the latest trade price for each."""
        if not self._tickers or not self._client:
            return

        try:
            # Synchronous SDK call → push to thread executor
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
        except Exception as e:
            # Common failures: 401 (bad key), 429 (rate limit), 5xx, network.
            # Do not re-raise — retry on next interval.
            logger.error("Massive poll failed: %s", e)
            return

        processed = 0
        for snap in snapshots:
            try:
                price = snap.last_trade.price
                ts = snap.last_trade.timestamp / 1000.0  # ms → seconds
                self._cache.update(ticker=snap.ticker, price=price, timestamp=ts)
                processed += 1
            except (AttributeError, TypeError) as e:
                logger.warning(
                    "Skipping snapshot for %s: %s",
                    getattr(snap, "ticker", "???"),
                    e,
                )

        logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

    def _fetch_snapshots(self) -> list:
        """Synchronous SDK call. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 8.3 Error-handling philosophy

| Failure | Behavior |
|---|---|
| **401 Unauthorized** (bad key) | Error logged. Poller keeps running. User restarts with a corrected key. |
| **429 Rate limit** | Error logged. Next poll waits the configured `poll_interval`. |
| **Network timeout / 5xx** | Error logged. Retries on next cycle. |
| **Malformed individual snapshot** | That ticker is skipped with a warning. Others still processed. |
| **Whole poll fails** | Cache retains last-known prices. SSE keeps streaming stale data — better than nothing. |

The poller is intentionally resilient: it should never crash the app on a transient API hiccup.

### 8.4 Mapping Massive responses → `PriceCache.update()`

```
snap.ticker                  → ticker arg
snap.last_trade.price        → price arg
snap.last_trade.timestamp    → timestamp arg (after /1000 for ms → s)
```

`snap.day.previous_close`, `snap.day.change_percent`, OHLCV etc. are available on the snapshot but not used by the cache today. Future work could expose them as additional cache fields if the UI needs day-change calculations independent of session start.

---

## 9. Factory (`factory.py`)

Selects the data source from environment configuration. Both sources share the cache that the caller provides.

```python
# backend/app/market/factory.py
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source from environment.

      MASSIVE_API_KEY set and non-empty → MassiveDataSource (real data)
      otherwise                         → SimulatorDataSource (GBM)

    Returns an unstarted source; caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource

        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    from .simulator import SimulatorDataSource

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

Usage at startup:

```python
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])
```

---

## 10. SSE Streaming Endpoint (`stream.py`)

A long-lived HTTP connection that pushes price updates to the browser as `text/event-stream`. The browser's native `EventSource` handles reconnection.

```python
# backend/app/market/stream.py
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Build the SSE streaming router, closing over the shared PriceCache.

    Returning a fresh router per call avoids the latent footgun of registering
    routes twice on a module-level singleton during tests.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Wire format:
            retry: 1000

            data: {"AAPL": {"ticker":"AAPL","price":190.50,...}, "GOOGL": {...}}

        Client connects with:
            const es = new EventSource('/api/stream/prices');
            es.onmessage = (e) => { const prices = JSON.parse(e.data); ... };
        """
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if ever proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted price events on a fixed cadence.

    Uses PriceCache.version to skip emitting when nothing has changed.
    Stops cleanly when the client disconnects.
    """
    # Tell the browser to wait 1s before reconnecting on drop
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
                    payload = json.dumps(
                        {ticker: u.to_dict() for ticker, u in prices.items()}
                    )
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

**Wire-level example** (one event):

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

**Frontend consumption:**

```javascript
const es = new EventSource('/api/stream/prices');
es.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices is { "AAPL": { ticker, price, previous_price, timestamp, change, change_percent, direction }, ... }
  updateWatchlist(prices);
};
es.onerror = () => updateConnectionStatus('reconnecting');
es.onopen  = () => updateConnectionStatus('connected');
```

**Why poll-and-push instead of event-driven?** A fixed cadence produces evenly-spaced updates that the frontend can accumulate into sparkline charts. The version counter prevents the cost of redundant serialization when the underlying source (Massive) is slower than the SSE cadence.

---

## 11. FastAPI Lifecycle Integration

The market data system starts/stops with the FastAPI application via the `lifespan` context manager.

```python
# backend/app/main.py
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
    # --- STARTUP ---
    price_cache = PriceCache()
    source = create_market_data_source(price_cache)

    initial_tickers = await load_watchlist_tickers()  # reads SQLite watchlist
    await source.start(initial_tickers)

    app.state.price_cache = price_cache
    app.state.market_source = source

    app.include_router(create_stream_router(price_cache))

    yield  # app is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency-injection helpers for other routes
def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Using market data in other routes

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    cache: PriceCache = Depends(get_price_cache),
):
    price = cache.get_price(trade.ticker)
    if price is None:
        raise HTTPException(400, f"No price available yet for {trade.ticker}")
    # ... execute at `price` ...


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
    # Keep tracking if user still holds shares so portfolio valuation stays accurate
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
    return {"status": "ok"}
```

---

## 12. Watchlist Coordination

### Add flow

```
User / LLM  →  POST /api/watchlist {ticker: "PYPL"}
   1. INSERT INTO watchlist (user_id, ticker) VALUES ('default','PYPL')
   2. await source.add_ticker("PYPL")
        Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache
        Massive:   appends to ticker list; appears in next poll
   3. Return success (ticker + current price, if seeded)
```

### Remove flow

```
User / LLM  →  DELETE /api/watchlist/PYPL
   1. DELETE FROM watchlist WHERE ticker='PYPL'
   2. If no open position for PYPL:
        await source.remove_ticker("PYPL")
            Simulator: removes from sim, rebuilds Cholesky, removes from cache
            Massive:   removes from ticker list, removes from cache
   3. Return success
```

If a position is still open the ticker stays tracked so portfolio valuation continues to mark-to-market correctly.

---

## 13. Testing Strategy

All tests live under `backend/tests/market/`. Target ≥80% coverage on production code with `pytest-asyncio` and `pytest-cov`.

### 13.1 `test_cache.py` — PriceCache

```python
from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.50)
    assert update.direction == "flat"
    assert update.previous_price == 190.50


def test_direction_up():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00


def test_direction_down():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 189.00)
    assert update.direction == "down"


def test_version_increments_on_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    cache.update("GOOGL", 175.00)
    assert cache.version == v0 + 2


def test_get_price_convenience():
    cache = PriceCache()
    cache.update("AAPL", 190.50)
    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("NOPE") is None


def test_remove():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
```

### 13.2 `test_simulator.py` — GBMSimulator

```python
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


def test_step_returns_all_tickers():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    assert set(sim.step().keys()) == {"AAPL", "GOOGL"}


def test_prices_stay_positive():
    """GBM is multiplicative — prices can never go negative."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0


def test_initial_price_matches_seed():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]


def test_unknown_ticker_gets_random_price_in_range():
    sim = GBMSimulator(tickers=["ZZZZ"])
    p = sim.get_price("ZZZZ")
    assert 50.0 <= p <= 300.0


def test_add_and_remove_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("TSLA")
    assert "TSLA" in sim.step()
    sim.remove_ticker("AAPL")
    assert "AAPL" not in sim.step()


def test_cholesky_handles_full_default_watchlist():
    """All 10 default tickers must produce a valid (PSD) correlation matrix."""
    sim = GBMSimulator(tickers=list(SEED_PRICES.keys()))
    # If the matrix isn't positive semi-definite, cholesky raises LinAlgError
    assert sim._cholesky is not None
    result = sim.step()
    assert len(result) == 10
```

### 13.3 `test_simulator_source.py` — async wrapper

```python
import asyncio
import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
async def test_start_immediately_seeds_cache():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.5)
    await source.start(["AAPL", "GOOGL"])
    # Cache should be populated even before the first loop tick
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL"])
    await source.stop()
    await source.stop()  # must not raise


@pytest.mark.asyncio
async def test_add_remove_ticker_round_trip():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL"])

    await source.add_ticker("TSLA")
    assert "TSLA" in source.get_tickers()
    assert cache.get("TSLA") is not None  # seeded

    await source.remove_ticker("TSLA")
    assert "TSLA" not in source.get_tickers()
    assert cache.get("TSLA") is None

    await source.stop()
```

### 13.4 `test_massive.py` — mocked SDK

```python
from unittest.mock import MagicMock, patch

import pytest

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _snap(ticker: str, price: float, ts_ms: int) -> MagicMock:
    s = MagicMock()
    s.ticker = ticker
    s.last_trade.price = price
    s.last_trade.timestamp = ts_ms
    return s


@pytest.mark.asyncio
async def test_poll_updates_cache():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()  # bypass start()
    source._tickers = ["AAPL", "GOOGL"]

    fake = [
        _snap("AAPL", 190.50, 1707580800000),
        _snap("GOOGL", 175.25, 1707580800000),
    ]
    with patch.object(source, "_fetch_snapshots", return_value=fake):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("GOOGL") == 175.25


@pytest.mark.asyncio
async def test_malformed_snapshot_is_skipped():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()
    source._tickers = ["AAPL", "BAD"]

    good = _snap("AAPL", 190.50, 1707580800000)
    bad = MagicMock()
    bad.ticker = "BAD"
    bad.last_trade = None  # AttributeError when accessed

    with patch.object(source, "_fetch_snapshots", return_value=[good, bad]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("BAD") is None


@pytest.mark.asyncio
async def test_api_error_does_not_crash_the_poller():
    cache = PriceCache()
    source = MassiveDataSource(api_key="k", price_cache=cache, poll_interval=60.0)
    source._client = MagicMock()
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("boom")):
        await source._poll_once()  # must not raise

    assert cache.get_price("AAPL") is None
```

### 13.5 `test_factory.py`

```python
import os
from unittest.mock import patch

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.simulator import SimulatorDataSource
from app.market.massive_client import MassiveDataSource


def test_factory_returns_simulator_when_no_key():
    with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}, clear=False):
        source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)


def test_factory_returns_massive_when_key_set():
    with patch.dict(os.environ, {"MASSIVE_API_KEY": "abc"}, clear=False):
        source = create_market_data_source(PriceCache())
    assert isinstance(source, MassiveDataSource)


def test_factory_treats_whitespace_only_key_as_empty():
    with patch.dict(os.environ, {"MASSIVE_API_KEY": "   "}, clear=False):
        source = create_market_data_source(PriceCache())
    assert isinstance(source, SimulatorDataSource)
```

### 13.6 `test_stream.py` — SSE integration

```python
import asyncio
import json
import pytest
from httpx import AsyncClient, ASGITransport
from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import create_stream_router


@pytest.mark.asyncio
async def test_sse_emits_seeded_prices():
    cache = PriceCache()
    cache.update("AAPL", 190.50)

    app = FastAPI()
    app.include_router(create_stream_router(cache))

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        async with client.stream("GET", "/api/stream/prices") as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")

            # Read first data event
            async def first_data_line():
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        return line[len("data: ") :]
                return None

            payload = await asyncio.wait_for(first_data_line(), timeout=2.0)
            data = json.loads(payload)
            assert data["AAPL"]["price"] == 190.50
```

---

## 14. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Empty initial watchlist | Both sources start cleanly. Simulator's `step()` returns `{}`. Massive's poll is a no-op until tickers are added. SSE sends no `data:` events. |
| Trade attempted before first price arrives | `cache.get_price()` returns `None` → API returns HTTP 400 with a clear message. (Simulator avoids this by seeding the cache in `start()`/`add_ticker()`. Massive may have a brief gap.) |
| Bad `MASSIVE_API_KEY` | First poll fails with 401. Error logged. Poller keeps retrying. SSE streams empty data until the user corrects `.env` and restarts. |
| Rate-limit hit (429) | Logged; next poll waits for `poll_interval`. With default 15s on the free tier, the cache simply doesn't refresh that cycle. |
| Network blip during poll | Logged; retries automatically next cycle. Cache keeps last-known values; SSE keeps streaming. |
| Malformed single snapshot | That ticker is skipped with a warning. Others still processed. |
| Ticker added mid-session (simulator) | Seed price written immediately. Cholesky matrix rebuilt. Next `step()` includes it. |
| Ticker added mid-session (Massive) | Appended to list. First price arrives on the next poll cycle (≤ `poll_interval` seconds). |
| Watchlist removal with open position | Watchlist entry deleted; data source keeps tracking the ticker so portfolio valuation stays accurate. |
| Double `stop()` | Idempotent. Second call sees the task already done and returns. |
| Floating-point precision | All prices rounded to 2 decimals at write time. GBM's `exp(...)` keeps values strictly positive. |

---

## 15. Configuration Summary

All tunable parameters:

| Parameter | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env var | `""` | Empty/unset → simulator; non-empty → Massive client |
| `LLM_MOCK` | env var | `false` | (Unrelated to market data; used by chat layer) |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick rate |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Per-tick chance of a 2–5% shock |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | Fraction-of-trading-year per tick |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Free tier safe (5 req/min) |
| SSE push cadence | `_generate_events(interval=...)` | `0.5` s | Matches simulator tick |
| SSE retry directive | `_generate_events` | `1000` ms | EventSource auto-reconnect delay |
| Cache value rounding | `PriceCache.update` | 2 decimals | Cents granularity |

### Quick-start usage

```python
from app.market import PriceCache, create_market_data_source

# Startup
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA",
                    "NVDA", "META", "JPM", "V", "NFLX"])

# Read prices (consumer code)
update = cache.get("AAPL")          # PriceUpdate | None
price  = cache.get_price("AAPL")    # float | None
prices = cache.get_all()            # dict[str, PriceUpdate]

# Dynamic watchlist
await source.add_ticker("PYPL")
await source.remove_ticker("META")

# Shutdown
await source.stop()
```

This design covers every responsibility called out in `PLAN.md` §6 (Market Data) and §10 (SSE) while keeping each module small, testable, and independently swappable.
