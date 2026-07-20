# Market Data Backend — Design Document

**Status:** Reflects the actual, implemented code in `backend/app/market/` as of this writing (verified against source, not a draft). This supersedes the sketches in `planning/archive/MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, and `MASSIVE_API.md`, and is more current than `planning/archive/MARKET_DATA_DESIGN.md` (which predates the post-review fixes below). For a one-page overview see `planning/MARKET_DATA_SUMMARY.md`.

Everything here lives under `backend/app/market/` (8 modules, ~770 lines) and is covered by 73 tests in `backend/tests/market/` (see §12).

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
11. [Public Package API — `__init__.py`](#11-public-package-api--__init__py)
12. [Testing Strategy](#12-testing-strategy)
13. [Integration Points for the Rest of the Backend](#13-integration-points-for-the-rest-of-the-backend)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Configuration Reference](#15-configuration-reference)

---

## 1. Architecture Overview

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, single instance per process)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation          (not yet built)
        └──→ Trade execution              (not yet built)
```

Both data sources implement the same `MarketDataSource` ABC (§5). Neither returns prices to a caller directly — each pushes updates into a shared `PriceCache` on its own schedule (simulator: ~500ms, Massive: ~15s). Every downstream consumer reads exclusively from `PriceCache`, so it is entirely agnostic to which source is active. This is the "strategy pattern" referenced in `PLAN.md` §6.

**Why push into a cache instead of pull-on-demand?** It decouples timing. The simulator ticks at 500ms; Massive polls at 2–15s depending on tier. The SSE layer (§10) always reads the cache at its own fixed cadence regardless of how fast the underlying source updates — it doesn't need to know or care.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports the public API (§11)
      models.py                # PriceUpdate — immutable frozen dataclass
      cache.py                 # PriceCache — thread-safe in-memory store
      interface.py              # MarketDataSource ABC
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
      simulator.py              # GBMSimulator (math engine) + SimulatorDataSource (async wrapper)
      massive_client.py         # MassiveDataSource — REST polling client
      factory.py                # create_market_data_source() — env-based selection
      stream.py                 # create_stream_router() — FastAPI SSE endpoint factory
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
  market_data_demo.py           # Rich-terminal live demo (`uv run market_data_demo.py`)
```

Each file has a single responsibility; `__init__.py` re-exports so the rest of the backend imports from `app.market` without reaching into submodules.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the **only** data structure that leaves the market data layer. SSE streaming, portfolio valuation, and trade execution all work exclusively with this type.

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

**Design decisions:**
- `frozen=True` — value objects, safe to share across async tasks without copying.
- `slots=True` — memory optimization; many of these are created per second.
- `change`, `change_percent`, `direction` are **computed properties**, never stored fields — they can never drift out of sync with `price`/`previous_price`.
- `to_dict()` is the single serialization point, shared by the SSE endpoint and (eventually) REST responses.

---

## 4. Price Cache — `cache.py`

The central hub. Exactly one `PriceCache` instance exists per running app (created in the FastAPI lifespan, §13). Data sources write to it; everything else reads from it.

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

The SSE loop (§10) polls the cache every ~500ms. Without a version counter it would re-serialize and re-send all prices even when nothing changed (Massive only updates every 15s — 29 out of every 30 SSE ticks would otherwise be wasted, identical payloads). The counter lets the SSE loop skip sends when nothing is new:

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
- `MassiveDataSource._poll_once()` runs the synchronous Massive client via `asyncio.to_thread()` — a real OS thread. `asyncio.Lock` would not protect against a concurrent writer there.
- `threading.Lock` works correctly from both sync threads and the async event loop, so one lock type covers every caller.

`version` is read outside the lock as a plain attribute access. On CPython (GIL), reading a single `int` is atomic, so this is safe in practice; it would need to move under the lock only if the project ever ran on a no-GIL build.

---

## 5. Abstract Interface — `interface.py`

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

Both `SimulatorDataSource` and `MassiveDataSource` implement this ABC in full, so the rest of the backend (and tests) can depend on `MarketDataSource` alone and never need to `isinstance`-check which one is active.

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond stdlib types. Consumed by `simulator.py`.

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

Tickers not present in `SEED_PRICES`/`TICKER_PARAMS` (i.e., anything a user or the AI adds beyond the default 10) get a **random seed price between $50–$300** and `DEFAULT_PARAMS` (`sigma=0.25, mu=0.05`) — see `GBMSimulator._add_ticker_internal` in §7. This is the simulator's answer to `PLAN.md`'s open question about unrecognized tickers: they are accepted and simulated with generic parameters rather than rejected.

---

## 7. GBM Simulator — `simulator.py`

Two classes: `GBMSimulator` (pure math engine, stateful) and `SimulatorDataSource` (the `MarketDataSource` implementation wrapping it in an async loop).

### 7.1 GBMSimulator — the math engine

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
            mu = params["mu"]
            sigma = params["sigma"]

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
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Determine correlation between two tickers based on sector grouping.

        Correlation structure:
          - Same tech sector:    0.6
          - Same finance sector: 0.5
          - TSLA with anything:  0.3 (it does its own thing)
          - Cross-sector:        0.3
          - Unknown tickers:     0.3
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

### 7.2 SimulatorDataSource — async wrapper

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

**Key behaviors:**
- **Immediate seeding** — `start()` populates the cache with seed prices *before* the loop begins, so the SSE endpoint has data on its very first tick with no blank-screen delay. The same happens in `add_ticker()`.
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`, for clean shutdown during FastAPI lifespan teardown. Idempotent: calling it twice is safe.
- **Exception resilience** — `_run_loop` catches exceptions per-step (`logger.exception`) so a single bad tick can't kill the whole feed.
- **`get_tickers()` is public on `GBMSimulator`** — `SimulatorDataSource.get_tickers()` calls it rather than reaching into a private `_tickers` list, keeping the class boundary clean.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST API snapshot endpoint on a configurable interval. The synchronous Massive SDK call runs via `asyncio.to_thread()` to avoid blocking the event loop.

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
            "Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval,
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
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
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

### Massive SDK reference (used above)

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")  # or reads MASSIVE_API_KEY from env if omitted

# One call returns snapshots for every requested ticker — critical for
# staying within the free tier's 5 req/min rate limit.
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    snap.ticker             # "AAPL"
    snap.last_trade.price   # 190.50
    snap.last_trade.timestamp  # Unix ms
    snap.day.change_percent    # day change %, if needed later
```

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** (bad key) | Logged as error. Poller keeps running; user fixes `.env` and restarts. |
| **429 Rate limited** | Logged as error. Retries automatically on the next `poll_interval`. |
| **Network timeout** | Logged as error. Retries automatically on the next cycle. |
| **Malformed snapshot for one ticker** | That ticker is skipped with a warning; other tickers in the same batch still update. |
| **All tickers fail** | Cache retains last-known prices — SSE keeps streaming stale-but-present data, which is preferable to a blank screen. |

### Why `massive` is imported at module level (not lazily)

Unlike the interim design in `planning/archive/MARKET_DATA_DESIGN.md`, the current code imports `RESTClient`/`SnapshotMarketType` at the top of `massive_client.py`, and `factory.py` imports both `MassiveDataSource` and `SimulatorDataSource` at module level too. This was a deliberate fix from code review: `massive` is declared a **core** dependency in `pyproject.toml` (not optional), so `uv sync` always installs it — there is no simulator-only install path to protect with a lazy import, and lazy imports only complicated test mocking (`patch("...RESTClient")` needs the name to exist at module scope to patch cleanly).

---

## 9. Factory — `factory.py`

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

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g. ["AAPL", "GOOGL", ...] loaded from SQLite watchlist
```

---

## 10. SSE Streaming Endpoint — `stream.py`

A FastAPI route holding open a long-lived HTTP connection, pushing price updates as `text/event-stream`.

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
    yield "retry: 1000\n\n"  # Tell the client to retry after 1s if the connection drops

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

### Wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

### Frontend consumption

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // { "AAPL": { ticker, price, previous_price, change, change_percent, direction, timestamp }, ... }
};
```

`EventSource` has built-in automatic reconnection; the `retry: 1000\n\n` directive tells the browser to wait 1s before reconnecting after a drop, matching `PLAN.md`'s connection-status indicator behavior (yellow while reconnecting).

### Why poll-and-push instead of event-driven?

Polling the cache on a fixed interval (rather than the data source notifying the SSE layer directly) produces predictable, evenly-spaced updates. The frontend accumulates these into sparkline charts client-side, and even spacing matters for a visually clean chart — an event-driven push would produce irregular gaps whenever Massive's poll interval and the simulator's tick rate diverge.

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

Downstream code should always `from app.market import ...` and never reach into `app.market.cache`, `app.market.simulator`, etc. directly.

---

## 12. Testing Strategy

**73 tests, all passing** across 6 modules in `backend/tests/market/`. Run with:

```bash
cd backend
uv run pytest tests/market/ -v --cov=app.market
```

| Module | Tests | Coverage | Notes |
|--------|-------|----------|-------|
| `test_models.py` | 11 | 100% | `PriceUpdate` properties, serialization, edge cases (zero previous price) |
| `test_cache.py` | 13 | 100% | Update/get/remove, direction computation, version counter, concurrent-shape assumptions |
| `test_simulator.py` | 17 | 98% | GBM math sanity (positivity, seed match), add/remove ticker, Cholesky rebuild, duplicate/nonexistent handling |
| `test_simulator_source.py` | 10 | (integration) | `start()` seeds cache immediately, prices evolve over time, clean/idempotent `stop()`, dynamic add/remove |
| `test_factory.py` | 7 | 100% | Env var present/absent/empty/whitespace → correct source class |
| `test_massive.py` | 13 | 56% (expected) | Poll updates cache, malformed snapshot skipped without crashing, API errors don't propagate |

Overall: **84% coverage**. `massive_client.py` sits at 56% because the real network-calling methods are mocked rather than exercised; `stream.py` is not separately measured here since it requires a running ASGI server to test meaningfully (would need `httpx.AsyncClient` against the FastAPI app — a good candidate for an E2E test in `test/` rather than a backend unit test).

### Representative test patterns

**GBM math invariants** (`test_simulator.py`):
```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_cholesky_rebuilds_on_add(self):
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None  # Only 1 ticker, no correlation matrix
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None
```

**Cache version semantics** (`test_cache.py`):
```python
def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

**Massive resilience** (`test_massive.py`) — mocking `_fetch_snapshots` directly (not the SDK) avoids depending on network or a live API key:
```python
async def test_malformed_snapshot_skipped(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "BAD"]

    good_snap = _make_snapshot("AAPL", 190.50, 1707580800000)
    bad_snap = MagicMock()
    bad_snap.ticker = "BAD"
    bad_snap.last_trade = None  # Triggers AttributeError, must not crash the poll

    with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
    assert cache.get_price("BAD") is None
```

**Async lifecycle** (`test_simulator_source.py`):
```python
async def test_start_populates_cache(self):
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL", "GOOGL"])
    assert cache.get("AAPL") is not None  # Seeded before the loop's first tick
    await source.stop()
```

---

## 13. Integration Points for the Rest of the Backend

The market data layer is complete; the rest of `backend/app/` (routes, DB, chat) has **not** been built yet per `CLAUDE.md`. This section is forward-looking guidance for whoever builds `app/main.py` and the portfolio/watchlist routers, so they wire into the existing market layer correctly rather than reinventing it.

### 13.1 FastAPI lifespan

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # from SQLite — see PLAN.md §7
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield

    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
```

### 13.2 Dependency injection for other routes

```python
from fastapi import Depends, HTTPException

def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source


@router.post("/portfolio/trade")
async def execute_trade(trade: TradeRequest, price_cache: PriceCache = Depends(get_price_cache)):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}. Try again shortly.")
    # ... execute trade at current_price, per PLAN.md §7/§9 ...


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

    # Keep pricing an open position even after it leaves the watchlist —
    # otherwise portfolio valuation for that ticker goes stale/unpriceable.
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

### 13.3 Rules for whoever builds the portfolio/watchlist/chat layers

- **Never import `app.market.cache`, `app.market.simulator`, etc. directly.** Always `from app.market import PriceCache, MarketDataSource, ...` (§11).
- **A ticker with an open position must stay tracked** even if removed from the watchlist (§13.2 above) — otherwise its price freezes and P&L can't be computed. This resolves the open question in `PLAN.md` §14.
- **A price-cache miss on trade execution is a 400, not a 500** — it means the ticker was just added and the source hasn't produced a price yet (only realistically possible with `MassiveDataSource`; the simulator seeds synchronously in `add_ticker()`).
- **The LLM's trade/watchlist actions (`PLAN.md` §9) go through the exact same `source.add_ticker()` / `source.remove_ticker()` / cache-read path as manual actions** — there is no separate code path for AI-initiated changes.

---

## 14. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| **Empty watchlist at startup** | `start([])` — simulator produces no prices, Massive poller skips its API call. SSE sends empty `data: {}` events. First `add_ticker()` call starts producing data for that ticker immediately. |
| **Trade on a ticker with no cached price** | `price_cache.get_price(ticker)` returns `None` → caller should raise `HTTPException(400, ...)`. The simulator avoids this by seeding synchronously; Massive may have a brief gap right after `add_ticker()` until the next poll. |
| **`MASSIVE_API_KEY` set but invalid** | First poll gets a 401, logged as an error; the poller keeps retrying every `poll_interval`. SSE keeps streaming (connection is fine) but with no data. Fixed by correcting `.env` and restarting the process — there's no in-process key hot-reload. |
| **Massive rate-limited (429) or network failure** | Logged, no re-raise; retried automatically on the next scheduled poll. Cache retains last-known prices in the meantime — stale-but-present beats blank. |
| **Malformed snapshot for one ticker** | That ticker is skipped with a `logger.warning`; the rest of the batch still updates. |
| **Thread contention on `PriceCache`** | `threading.Lock`'s critical section is a dict lookup + assignment — negligible at 10 tickers / 2 updates/sec. Would need a `ReadWriteLock` only at a scale (hundreds of tickers, many concurrent SSE readers) this project doesn't reach. |
| **GBM numerical stability** | Prices are `round()`ed to 2 decimals on every cache write; the multiplicative `exp(...)` formulation guarantees positivity regardless of how small `dt` is. |
| **Unrecognized ticker added to simulator** | Gets a random seed price in `[$50, $300]` and `DEFAULT_PARAMS` (`sigma=0.25, mu=0.05`) — accepted, not rejected (see §6). |

---

## 15. Configuration Reference

| Parameter | Location | Default | Description |
|-----------|----------|---------|--------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set (non-empty after `.strip()`), `create_market_data_source` returns `MassiveDataSource`; otherwise `SimulatorDataSource`. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` seconds | Time between `GBMSimulator.step()` calls. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` seconds | Time between Massive API polls (matches free-tier 5 req/min). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a 2–5% shock event per ticker per tick. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step, fraction of a trading year for a 500ms tick. |
| SSE push interval | `_generate_events()` | `0.5` seconds | Cadence at which the SSE loop checks `PriceCache.version`. |
| SSE retry directive | `_generate_events()` | `1000` ms | `retry:` value sent to the browser for `EventSource` auto-reconnect. |

---

## Demo

A Rich-terminal live dashboard exists at `backend/market_data_demo.py`:

```bash
cd backend
uv run market_data_demo.py
```

Displays all 10 default tickers with sparklines, color-coded direction arrows, and an event log for notable price moves. Runs for 60 seconds or until `Ctrl+C` — useful for manually sanity-checking simulator behavior without booting the full FastAPI app.
