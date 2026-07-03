# Market Data Backend — Design Document

**Status:** Implemented and tested. This document describes the system as built in `backend/app/market/` (73 passing tests, 84% coverage). See `planning/MARKET_DATA_SUMMARY.md` for the short version and `planning/archive/` for the original design-phase notes and code review that fed into this implementation.

This document is the reference for how the market data subsystem works: the unified interface both data sources implement, the GBM simulator, the Massive (Polygon.io) REST client, the shared price cache, the SSE streaming endpoint, and how the rest of the backend (not yet built) should integrate with it.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Unified Interface — `interface.py`](#5-unified-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [Simulator — `simulator.py`](#7-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [Package Exports — `__init__.py`](#11-package-exports)
12. [FastAPI Integration (for downstream agents)](#12-fastapi-integration)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing](#14-testing)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Reference](#16-configuration-reference)

---

## 1. Architecture Overview

The subsystem is a **strategy pattern**: one abstract interface, two interchangeable implementations, one shared cache that decouples producers from consumers.

```
                    MarketDataSource (ABC)
                    ┌────────────┴────────────┐
                    │                         │
          SimulatorDataSource         MassiveDataSource
          (GBM math engine)           (Polygon.io REST poller)
          default, no API key         used when MASSIVE_API_KEY is set
                    │                         │
                    └────────────┬────────────┘
                                 ▼
                          PriceCache
                    (thread-safe, in-memory,
                     versioned for change detection)
                                 │
                ┌────────────────┼────────────────┐
                ▼                ▼                ▼
    SSE /api/stream/prices   Trade execution   Portfolio valuation
```

Neither the simulator nor the Massive client returns prices to a caller. Both **push** updates into the `PriceCache` on their own schedule (500ms for the simulator, 15s for Massive's free tier). Every consumer — SSE streaming, trade execution, portfolio valuation — reads the latest snapshot from the cache and is completely unaware of which source is active.

This buys two things:
- **Source-agnostic downstream code.** Swapping simulator ↔ Massive is an environment variable, not a code change.
- **Decoupled timing.** The SSE loop always pushes at a steady ~500ms cadence to the frontend for clean sparkline rendering, regardless of how often the underlying source actually updates.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               #             create_market_data_source, create_stream_router
      models.py                # PriceUpdate — immutable price snapshot
      cache.py                 # PriceCache — thread-safe in-memory store
      interface.py              # MarketDataSource — ABC
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py              # GBMSimulator (math) + SimulatorDataSource (async wrapper)
      massive_client.py         # MassiveDataSource — Polygon.io REST poller
      factory.py                # create_market_data_source() — env-driven selection
      stream.py                 # create_stream_router() — SSE FastAPI route factory
  tests/
    market/
      test_models.py            # 11 tests
      test_cache.py              # 13 tests
      test_simulator.py          # 19 tests
      test_simulator_source.py   # 10 tests
      test_factory.py            # 7 tests
      test_massive.py            # 13 tests
  market_data_demo.py           # Rich terminal live-dashboard demo (`uv run market_data_demo.py`)
```

Each module has one job. `app.market.__init__` is the only import path the rest of the backend should use — nothing outside `app/market/` reaches into a submodule directly.

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only object type that leaves the market data layer. SSE serialization, portfolio valuation, and trade execution all consume it.

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

**Design rationale**

- `frozen=True, slots=True` — value objects created many times per second; immutability makes them safe to hand across async tasks without copying, and slots trims per-instance memory.
- `change`, `change_percent`, `direction` are **computed properties**, never stored — they can never drift out of sync with `price`/`previous_price`.
- `to_dict()` is the single serialization boundary used by the SSE endpoint (and, later, any REST endpoint that echoes a price).

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The cache is the single point of truth: exactly one data source writes to it at a time, and any number of readers (SSE clients, trade validation, portfolio math) read from it concurrently.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
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
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

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

**Why a version counter?** The SSE loop polls the cache every ~500ms. Without a counter it would re-serialize and re-send every ticker on every tick, even when Massive hasn't polled in 15 seconds. The counter lets the SSE loop skip a send when nothing changed:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock` and not `asyncio.Lock`?** The Massive client's synchronous `get_snapshot_all()` call runs inside `asyncio.to_thread()` — a real OS thread, which `asyncio.Lock` cannot protect against. `threading.Lock` is safe from both a background thread and the async event loop, and the critical sections here are dict operations — cheap enough that contention is a non-issue at this scale (≤50 tickers, sub-millisecond lock hold time).

---

## 5. Unified Interface

**File: `backend/app/market/interface.py`**

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
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

This is the entire contract. Both implementations below satisfy it identically from the caller's point of view — the only difference is what happens inside `start()`/`_run_loop()`.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Pure constants, no logic, no imports beyond stdlib types. Shared by the simulator (initial prices + GBM parameters) and reusable as sensible fallbacks anywhere else that needs a plausible starting price.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

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

Tickers added dynamically via the watchlist that aren't in `SEED_PRICES`/`TICKER_PARAMS` fall back to a random seed price in `$50–$300` and `DEFAULT_PARAMS`, handled in `simulator.py` (see below).

> Naming note: an earlier draft also defined a separate `DEFAULT_CORR` constant. It was removed during code review because it was numerically identical to and functionally redundant with `CROSS_GROUP_CORR` — both applied to the same fallback case, so keeping only one avoids confusion about which applies when.

---

## 7. Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math, no asyncio) and `SimulatorDataSource` (the `MarketDataSource` implementation wrapping it in an async loop).

### 7.1 The Math

Geometric Brownian Motion — the same lognormal model underlying Black-Scholes — guarantees prices stay positive and produces realistic-looking continuous paths:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` — current price
- `mu` — annualized drift (expected return)
- `sigma` — annualized volatility
- `dt` — time step as a fraction of a trading year
- `Z` — a (correlated) standard normal draw

For 500ms ticks over a 252-day, 6.5-hour trading year:

```
dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8
```

This tiny `dt` produces sub-cent per-tick moves that accumulate into realistic-looking intraday ranges over minutes of runtime.

**Correlated moves.** Real stocks don't move independently. A Cholesky decomposition of a sector-correlation matrix turns independent normal draws into correlated ones: given correlation matrix `C`, compute `L = cholesky(C)`, then `Z_correlated = L @ Z_independent`. Tech stocks correlate at 0.6, finance at 0.5, everything else (including TSLA, deliberately excluded from the tech cluster) at 0.3.

**Random events.** Each ticker has a ~0.1% chance per tick of a sudden 2–5% shock, for visual drama. At 10 tickers and 2 ticks/sec, expect a shock roughly every 50 seconds.

### 7.2 `GBMSimulator`

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
    """Geometric Brownian Motion simulator for correlated stock prices."""

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

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms. Keep it fast.
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
        """Return the list of currently tracked tickers (public — no reaching
        into private state from SimulatorDataSource)."""
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2), called on every add/remove. Fine for n < 50 tickers."""
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
        # Seed the cache immediately so SSE has data on its very first tick
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

**Key behaviors**

- **Immediate seeding.** The cache is populated with seed prices *before* the loop starts, so the SSE endpoint has data on the very first poll — no blank-screen delay on page load.
- **Graceful cancellation.** `stop()` cancels the task and awaits it, swallowing `CancelledError`, for clean shutdown under FastAPI's lifespan teardown.
- **Per-step exception isolation.** `_run_loop` catches exceptions around a single `step()` call so one bad tick can't kill the whole feed — it logs and continues on the next interval.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Massive (formerly Polygon.io) is polled — not streamed — via its REST snapshot endpoint, since polling works uniformly across all pricing tiers including free.

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

        await self._poll_once()  # Immediate first poll so cache has data right away

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

    async def _poll_loop(self) -> None:
        """First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return

        try:
            # Massive's RESTClient is synchronous — offload to a thread so it
            # doesn't block the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> s
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e,
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — retried on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 8.1 Massive API reference (as used here)

- **Package**: `massive` (declared as a core dependency in `pyproject.toml`; `RESTClient` and `SnapshotMarketType` are imported at module level, not lazily — see §8.2)
- **Auth**: `RESTClient(api_key=...)`; the client sets the `Authorization: Bearer <key>` header
- **Endpoint used**: `GET /v2/snapshot/locale/us/markets/stocks/tickers` via `client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])` — one call fetches every watched ticker, which is what keeps this within the free tier's 5 req/min limit
- **Fields extracted**: `last_trade.price` (current price) and `last_trade.timestamp` (Unix **milliseconds**, converted to seconds before writing to the cache)
- **Rate limits**: free tier 5 req/min → 15s poll interval default; paid tiers support much higher rates → drop `poll_interval` to 2–5s

Other endpoints (`get_snapshot_ticker`, `get_previous_close_agg`, `list_aggs`, `get_last_trade`/`get_last_quote`) exist in the `massive` SDK and are documented for reference in `planning/archive/MASSIVE_API.md`, but the current implementation only needs `get_snapshot_all` — a single batched call per poll is sufficient for the watchlist-driven UI.

### 8.2 Dependency strategy: core, not lazy

An earlier design draft imported `massive` lazily inside `start()` so students without a Massive API key wouldn't need the package installed. In the shipped implementation, `massive` is a **core dependency** (`pyproject.toml`: `"massive>=1.0.0"`) and `massive_client.py` imports `RESTClient`/`SnapshotMarketType` at module level. This was a deliberate correction made during code review: lazy imports made the mocked test suite (`patch("app.market.massive_client.RESTClient")`) fragile, since `patch` needs the name to exist at module scope to replace it. Since `uv sync` always installs the full dependency set for every student regardless of whether they have a Massive key, there was no real benefit to the lazy import — only test friction. `factory.py` still ensures `MassiveDataSource` is only *instantiated* (and its background task only *started*) when `MASSIVE_API_KEY` is actually set.

### 8.3 Error handling philosophy

| Error | Behavior |
|---|---|
| **401 Unauthorized** (bad key) | Logged as error; poller keeps running and retries every interval. |
| **429 Rate limited** | Logged as error; retried on the next scheduled poll. |
| **Network timeout** | Logged as error; retried automatically. |
| **Malformed snapshot for one ticker** | That ticker is skipped with a warning; other tickers in the same batch are still processed. |
| **All tickers fail** | Cache retains last-known prices — stale data beats no data; SSE keeps streaming what it has. |

---

## 9. Factory

**File: `backend/app/market/factory.py`**

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

This is the only place in the codebase that branches on `MASSIVE_API_KEY`. Everything else — SSE streaming, trade execution, portfolio valuation, the chat/LLM tool layer — talks to `MarketDataSource`/`PriceCache` and never checks which concrete implementation is active.

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

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

    Factory pattern — injects the PriceCache without module-level globals.
    """

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events shaped:

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
    """Yield SSE-formatted price events. Sends all prices every `interval`
    seconds, skipping ticks where nothing changed. Stops on client disconnect.
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

**Wire format** — one JSON object keyed by ticker per event:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

**Frontend consumption**:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
};
```

**Why poll-and-push instead of event-driven?** The endpoint polls the cache on a fixed interval rather than being notified by the data source directly. This gives evenly-spaced updates regardless of source (simulator ticks at 500ms, Massive polls at 15s) — important because the frontend accumulates these into sparklines on the client, and even spacing keeps that rendering clean.

---

## 11. Package Exports

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

Everything outside `app/market/` should import from `app.market`, never `app.market.simulator` or `app.market.massive_client` directly.

---

## 12. FastAPI Integration

The rest of the backend (`app/main.py`, portfolio/trade/watchlist/chat routes, the SQLite layer) has not been built yet. This section is the intended integration contract for whichever agent builds it.

### 12.1 App startup/shutdown via `lifespan`

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

    initial_tickers = await load_watchlist_tickers()  # reads app.state's SQLite connection
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### 12.2 Using it from other routes (dependency injection)

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
    # ... validate cash/shares, write trade + position rows, execute at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)
    # ... return ticker + current price if available ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from watchlist table ...
    await source.remove_ticker(ticker)
```

---

## 13. Watchlist Coordination

The market data source must stay in sync with whatever tickers the watchlist (and any open positions) actually need.

**Adding a ticker** (manual or via LLM chat):

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
        Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
        Massive:   appends to polled ticker list, appears on next poll cycle
  → respond with ticker + current price (if already available)
```

**Removing a ticker:**

```
DELETE /api/watchlist/PYPL
  → DELETE FROM watchlist (SQLite)
  → await source.remove_ticker("PYPL")
        Simulator: removes from GBMSimulator, rebuilds Cholesky, drops from cache
        Massive:   drops from polled ticker list, drops from cache
  → respond 200
```

**Edge case — ticker still held as a position.** If the user removes a ticker from the watchlist but still owns shares, the data source must keep tracking it so portfolio valuation stays accurate. The watchlist route, not the market data layer, is responsible for this check:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 14. Testing

**73 tests, all passing, 84% overall coverage.**

| Module | Tests | Coverage | What's exercised |
|---|---|---|---|
| `test_models.py` | 11 | 100% | `PriceUpdate` computed properties, `to_dict()`, edge cases (zero previous price) |
| `test_cache.py` | 13 | 100% | update/get/get_all/remove, direction computation, version counter |
| `test_simulator.py` | 19 | 98% | GBM math (positivity, drift over many steps), add/remove ticker, Cholesky rebuild, correlation lookup, full 10-ticker default set |
| `test_simulator_source.py` | 10 | (integration) | async start/stop lifecycle, immediate cache seeding, add/remove while running, double-stop safety |
| `test_factory.py` | 7 | 100% | env var branching (`MASSIVE_API_KEY` set/unset/whitespace) |
| `test_massive.py` | 13 | 56% (expected) | `_poll_once` success/partial-failure/total-failure paths, mocked snapshots, timestamp ms→s conversion |

`massive_client.py` coverage tops out at 56% because the real network-calling paths (`RESTClient(...)`, actual HTTP) are intentionally not exercised in unit tests — everything below `_fetch_snapshots` is mocked. `stream.py` is the one module without dedicated tests (SSE requires a running ASGI server); it's covered indirectly through manual/E2E testing once the frontend exists.

### Representative test patterns

**GBM math invariants:**

```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0
```

**Cache version semantics:**

```python
def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

**Massive polling with mocked snapshots (no real API calls):**

```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


async def test_poll_updates_cache(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]

    with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
```

**Simulator lifecycle (async):**

```python
async def test_start_populates_cache(self):
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])

    assert cache.get("AAPL") is not None  # Seeded before the loop's first tick
    assert cache.get("GOOGL") is not None

    await source.stop()
```

### Manual verification: the Rich terminal demo

`backend/market_data_demo.py` runs a live dashboard (all default tickers, sparklines, color-coded direction arrows, event log for shocks) for 60 seconds or until Ctrl+C — the fastest way to visually sanity-check the simulator or a live Massive connection without the frontend:

```bash
cd backend
uv run market_data_demo.py
```

---

## 15. Error Handling & Edge Cases

**Empty watchlist at startup.** If the database has no watchlist rows, `start([])` is a no-op for both sources — the simulator produces nothing, Massive skips its poll. SSE sends nothing until a ticker is added, at which point `add_ticker()` makes it appear immediately.

**Trading a ticker with no cached price.** Can happen for a split second right after adding a ticker to a Massive-backed watchlist (before the first poll lands). Route-level handling:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this entirely by seeding the cache synchronously inside `add_ticker()`. Massive has an unavoidable gap until the next poll; a clear 400 is the right response rather than blocking the request on a poll.

**Invalid Massive API key.** First poll returns 401; the poller logs and keeps retrying every `poll_interval`. The SSE connection itself stays "connected" (green indicator) even though no data is flowing — the fix is a `.env` correction and restart, not something the running process can self-heal.

**Thread safety under load.** `PriceCache`'s `threading.Lock` is a plain mutex; at this project's scale (≤50 tickers, a handful of SSE connections) lock contention is negligible — the critical sections are single dict operations. A read/write lock would be the next step if this were ever a bottleneck, but that's out of scope here.

**Floating-point precision.** GBM's exponential formulation (`exp(drift + diffusion)`) is numerically stable and always positive; prices are rounded to 2 decimals on write (`PriceCache.update`), so no precision artifacts reach consumers.

---

## 16. Configuration Reference

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (unset) | If set and non-empty → Massive API; otherwise → simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5`s | Time between simulator ticks |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, fraction of a trading year |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0`s | Time between Massive API polls (free-tier default) |
| SSE push interval | `stream._generate_events` | `0.5`s | Cadence of cache polling on the SSE side |
| SSE retry directive | `stream._generate_events` | `1000`ms | Browser `EventSource` reconnect delay |

**Package build note:** `pyproject.toml` requires `[tool.hatch.build.targets.wheel] packages = ["app"]` for `uv sync`/Docker builds to succeed — this was a gap caught and fixed during code review (see `planning/archive/MARKET_DATA_REVIEW.md` §3.1) and is already present in the current `backend/pyproject.toml`.
