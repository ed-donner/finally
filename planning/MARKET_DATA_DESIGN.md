# Market Data Backend — Detailed Design

Implementation-ready design for `backend/app/market/`: the unified data-source interface, the
thread-safe price cache, the GBM simulator, the Massive (Polygon.io) REST client, the SSE
streaming endpoint, and how the rest of the backend wires into all of it.

**Status of this document.** The market data subsystem was already built once (see
`planning/MARKET_DATA_SUMMARY.md`, 8 modules, 73 tests, 84% coverage) and the original design is
archived at `planning/archive/MARKET_DATA_DESIGN.md`. Since that build, `PLAN.md` §6 picked up
three requirements that the shipped code does not implement yet:

1. **`prev_close`** per ticker, so the watchlist can show a daily change % (`PLAN.md` §6, Decisions
   Log #1) — the cache today only knows the previous *tick*, not the previous close.
2. **Unknown-ticker fallback** must set `prev_close` equal to the generated seed price (§6).
3. **The tracked ticker set is watchlist ∪ open positions**, not the watchlist alone (§6, Decisions
   Log #6) — this is enforced by the (not-yet-built) watchlist API routes calling `add_ticker` /
   `remove_ticker` correctly, not by a change inside `app/market/` itself.

This document specifies the market data module **as it needs to exist to satisfy the current
`PLAN.md`** — the current code plus the `prev_close` extension. Section 15 lists the concrete diff
against what's on disk today. Everything not called out there already matches the shipped
implementation exactly.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint)
11. [FastAPI Lifecycle Integration](#11-fastapi-lifecycle-integration)
12. [Watchlist / Tracked-Ticker-Set Coordination](#12-watchlist--tracked-ticker-set-coordination)
13. [Testing Strategy](#13-testing-strategy)
14. [Error Handling & Edge Cases](#14-error-handling--edge-cases)
15. [Delta Against the Current Implementation](#15-delta-against-the-current-implementation)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Architecture

```
                         MarketDataSource (ABC)
                        /                      \
        SimulatorDataSource                MassiveDataSource
        (GBM, default,                     (Polygon.io REST poll,
         no API key needed)                 needs MASSIVE_API_KEY)
                        \                      /
                         v                    v
                          PriceCache (thread-safe, in-memory)
                                     |
                 -------------------+-------------------
                 |                  |                  |
        SSE /api/stream/prices   Portfolio valuation   Trade execution
        (this module)            (backend/app/portfolio, not yet built)
```

- **Strategy pattern.** Both data sources implement the same `MarketDataSource` ABC. Everything
  downstream — SSE streaming, portfolio valuation, trade execution — is source-agnostic; it only
  ever talks to `PriceCache`.
- **Push model.** Data sources write into the cache on their own schedule (simulator: 500ms,
  Massive: 15s). Readers never call the data source directly for a price.
- **Single point of truth.** `PriceCache` decouples producers from consumers, so a future
  multi-reader scenario (e.g. multiple SSE clients, portfolio valuation, the LLM's portfolio
  context) needs no changes to this layer.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                               #   create_market_data_source, create_stream_router
      models.py                # PriceUpdate dataclass
      cache.py                 # PriceCache (thread-safe in-memory store)
      interface.py              # MarketDataSource ABC
      seed_prices.py            # SEED_PRICES, PREV_CLOSE, TICKER_PARAMS, DEFAULT_PARAMS,
                               #   CORRELATION_GROUPS
      simulator.py               # GBMSimulator + SimulatorDataSource
      massive_client.py          # MassiveDataSource
      factory.py                  # create_market_data_source()
      stream.py                   # SSE endpoint (FastAPI router)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Each file has a single responsibility; `__init__.py` re-exports the public surface so the rest of
the backend imports from `app.market` without reaching into submodules.

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer. Every downstream
consumer — SSE streaming, portfolio valuation, trade execution, the LLM's portfolio context — works
exclusively with this type.

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
    prev_close: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    # --- Tick-over-tick change (drives the green/red price flash) ---

    @property
    def change(self) -> float:
        """Absolute price change from the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous tick."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat', based on the tick-over-tick change."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    # --- Daily change (drives the watchlist's Change % column) ---

    @property
    def change_from_close(self) -> float:
        """Absolute price change from the prior day's close."""
        return round(self.price - self.prev_close, 4)

    @property
    def change_percent_from_close(self) -> float:
        """Percentage change from the prior day's close."""
        if self.prev_close == 0:
            return 0.0
        return round((self.price - self.prev_close) / self.prev_close * 100, 4)

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "prev_close": self.prev_close,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "change_from_close": self.change_from_close,
            "change_percent_from_close": self.change_percent_from_close,
        }
```

### Design decisions

- **`frozen=True, slots=True`**: price updates are immutable value objects, safe to share across
  async tasks without copying, and cheap to create many times per second.
- **Two independent baselines, both computed properties**: `previous_price` (last tick) drives the
  transient green/red flash; `prev_close` (prior session's close) drives the daily change % shown
  in the watchlist. Keeping both as stored fields with derived properties means neither `direction`
  nor `change_percent_from_close` can ever drift out of sync with the prices they're computed from.
- **`prev_close` is required, not optional.** Every write path (simulator seed, simulator
  fallback-ticker seed, Massive poll) has a definite value for it — see §6 and §8 — so there's no
  `None`-handling burden on every reader. A ticker with no known previous close simply isn't in the
  cache yet.
- **`to_dict()`** stays the single serialization point, used by both the SSE endpoint and any future
  REST response that embeds a price.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The price cache is the central data hub. Data sources write to it; SSE streaming, portfolio
valuation, and trade execution read from it. It must be thread-safe because the Massive client's
synchronous call runs via `asyncio.to_thread`, which executes in a real OS thread.

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

    def update(
        self,
        ticker: str,
        price: float,
        prev_close: float | None = None,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        `prev_close` is optional on a call: once a ticker has an entry, later
        calls (e.g. every simulator tick) can omit it and the existing value
        is carried forward, since a session's close doesn't change tick to
        tick. It is required on the *first* write for a ticker — see
        SimulatorDataSource.start()/add_ticker() and MassiveDataSource._poll_once(),
        both of which always pass it.
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            resolved_prev_close = prev_close
            if resolved_prev_close is None:
                resolved_prev_close = prev.prev_close if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                prev_close=round(resolved_prev_close, 2),
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
        """Remove a ticker from the cache (e.g., when it leaves the tracked set)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE streaming loop polls the cache every ~500ms. Without a version counter it would serialize
and re-send every price on every tick even when nothing changed (e.g. under Massive, which only
updates every 15s). The counter lets the SSE loop skip a send when nothing is new:

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
- The Massive client's synchronous `get_snapshot_all()` runs inside `asyncio.to_thread()`, a real OS
  thread — `asyncio.Lock` provides no protection there.
- `threading.Lock` works correctly from both a sync thread and the async event loop.

---

## 5. Abstract Interface

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

This interface is unchanged from the shipped implementation, and deliberately stays unaware of
*why* a ticker is tracked. §12 explains why "tracked = watchlist ∪ open positions" is enforced one
layer up, by the callers of `add_ticker` / `remove_ticker`, not inside this module.

### Why the source writes to the cache instead of returning prices

This push model decouples timing. The simulator ticks at 500ms, Massive polls at 15s, but SSE
always reads from the cache at its own 500ms cadence — it doesn't need to know which data source is
active or what its update interval is.

---

## 6. Seed Prices & Ticker Parameters

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib. Adds `PREV_CLOSE`, a baseline a few percent
away from `SEED_PRICES` so the watchlist opens with a realistic mix of gainers and losers (`PLAN.md`
§6) rather than every ticker showing 0.00% on first paint.

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

# Prior session's close for each ticker — the baseline for daily change %.
# Deliberately a few percent off SEED_PRICES so the watchlist opens with a
# realistic spread of gainers and losers instead of a flat 0.00% row.
PREV_CLOSE: dict[str, float] = {
    "AAPL": 187.50,   # +1.33% today
    "GOOGL": 177.20,  # -1.24% today
    "MSFT": 415.00,   # +1.20% today
    "AMZN": 188.40,   # -1.82% today
    "TSLA": 241.30,   # +3.60% today
    "NVDA": 812.00,   # -1.48% today
    "META": 493.00,   # +1.42% today
    "JPM": 193.10,    # +0.98% today
    "V": 282.50,      # -0.88% today
    "NFLX": 589.00,   # +1.87% today
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

`PREV_CLOSE` has no entry for a ticker the user adds dynamically — that's intentional. A fallback
ticker's `prev_close` is set equal to its generated seed price (§7), per `PLAN.md` §6: *"A fallback
ticker gets `prev_close` equal to its generated seed price."* This yields exactly a 0.00% daily
change on first paint for a ticker with no real history, which is the honest answer, not a
guess.

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine, advances prices one step at a time) and
`SimulatorDataSource` (the `MarketDataSource` implementation wrapping it in an async loop and
writing to the `PriceCache`).

### 7.1 GBMSimulator — the math engine

Unchanged from the shipped implementation — `prev_close` is a cache-layer concern (a fixed daily
baseline), not something the per-tick random walk needs to know about.

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
    PREV_CLOSE,
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
        self._prev_close: dict[str, float] = {}
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
        del self._prev_close[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(ticker)

    def get_prev_close(self, ticker: str) -> float | None:
        """Prior session's close for a ticker, or None if not tracked."""
        return self._prev_close.get(ticker)

    def get_tickers(self) -> list[str]:
        """Return the list of currently tracked tickers."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        seed = SEED_PRICES.get(ticker)
        if seed is None:
            # Unknown ticker: fallback seed in $50-$500, and per PLAN.md §6 its
            # prev_close equals that same seed (0.00% change on first paint —
            # there is no real history to invent a baseline from).
            seed = random.uniform(50.0, 500.0)
            self._prev_close[ticker] = seed
        else:
            self._prev_close[ticker] = PREV_CLOSE.get(ticker, seed)
        self._prices[ticker] = seed
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

**What changed from the shipped `simulator.py`:** the `_prev_close: dict[str, float]` dict, its
population in `_add_ticker_internal` (with the unknown-ticker fallback rule), its cleanup in
`remove_ticker`, and the new `get_prev_close()` accessor. `step()` itself is untouched — a daily
close doesn't move tick to tick.

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
        # Seed the cache with initial price + prev_close so SSE has data
        # (and a correct daily change %) immediately.
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            prev_close = self._sim.get_prev_close(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price, prev_close=prev_close)
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
            prev_close = self._sim.get_prev_close(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price, prev_close=prev_close)
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
                        # prev_close omitted: it's fixed for the session, so
                        # PriceCache.update() carries the existing value forward.
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**What changed from the shipped `SimulatorDataSource`:** `start()` and `add_ticker()` now also pull
`get_prev_close()` and pass it into `cache.update()`. `_run_loop()` is unchanged — see the
`PriceCache.update()` fallback in §4 that carries `prev_close` forward on ticks that don't supply
one.

### Key behaviors

- **Immediate seeding**: the cache is populated with seed price *and* `prev_close` before the loop
  begins, so the SSE endpoint's very first tick already has a correct price and a correct daily
  change %.
- **Graceful cancellation**: `stop()` cancels the task and awaits it, catching `CancelledError` —
  clean shutdown during FastAPI lifespan teardown.
- **Exception resilience**: the loop catches exceptions per-step so one bad tick doesn't kill the
  feed.

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (Polygon.io-compatible) REST snapshot endpoint on a configurable interval. The
synchronous client runs in `asyncio.to_thread()` to avoid blocking the event loop.

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
      - Paid tiers: higher limits → poll every 2-15s
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

        await self._poll_once()  # immediate first poll: cache has data right away

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval
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
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms → s

                    # Previous session's close, for the daily change %.
                    # `prev_day` is the Polygon/Massive snapshot's OHLC block
                    # for the prior trading session; if it's ever absent (a
                    # brand-new listing, an odd API response) fall back to the
                    # tick price itself so change_percent_from_close reads
                    # 0.00% instead of crashing the whole poll.
                    prev_close = getattr(getattr(snap, "prev_day", None), "close", None)
                    if prev_close is None:
                        prev_close = price

                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        prev_close=prev_close,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e
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

**What changed from the shipped `massive_client.py`:** `_poll_once()` now reads `snap.prev_day.close`
(guarded with `getattr` in case a response is missing that block) and passes it as `prev_close` into
`cache.update()`. Everything else — the lazy-vs-eager import question was already resolved in the
shipped code by making `massive` a core dependency (see §15.4) — is unchanged.

> **Note on the field name.** `massive` (the Polygon.io-compatible client this project depends on,
> pinned `>=1.0.0`, `2.2.0` resolved in `uv.lock`) was not installed in the environment this document
> was written in, so `snap.prev_day.close` is inferred from Polygon.io's public snapshot schema
> (`TickerSnapshot.prev_day` is the prior session's OHLC bar) rather than confirmed against the
> installed package. Confirm the exact attribute name against `massive`'s actual `TickerSnapshot`
> model before merging — the `getattr(..., None)` guard means a wrong name degrades to "0.00% daily
> change," not a crash, but it should still be corrected.

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error. Poller keeps running (user might fix `.env` and restart). |
| **429 Rate Limited** | Logged as error. Next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error. Retries automatically on next cycle. |
| **Malformed snapshot** | Individual ticker skipped with a warning; others still processed. |
| **Missing `prev_day`** | `prev_close` falls back to the tick price (0.00% daily change), not a crash. |
| **All tickers fail** | Cache retains last-known prices. SSE keeps streaming stale data (better than none). |

---

## 9. Factory

**File: `backend/app/market/factory.py`**

Unchanged from the shipped implementation.

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

### Usage at app startup

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g. the watchlist loaded from SQLite
```

---

## 10. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

Unchanged from the shipped implementation — it already serializes whatever `PriceUpdate.to_dict()`
returns, so adding `prev_close` / `change_from_close` / `change_percent_from_close` to the model in
§3 flows through automatically with no edits here.

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

### SSE wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"prev_close":187.50,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up","change_from_close":3.00,"change_percent_from_close":1.6}, "GOOGL":{...}}

```

Client-side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);
  // prices["AAPL"].change_percent_from_close  → watchlist "Change %" column
  // prices["AAPL"].direction                  → green/red price flash
};
```

### Why poll-and-push instead of event-driven?

The SSE endpoint polls the cache on a fixed interval rather than being notified by the data source.
Simpler, and it produces regularly-spaced updates, which matters because the frontend accumulates
them into sparklines and the main chart client-side (`PLAN.md` §10) — irregular spacing there would
look wrong.

---

## 11. FastAPI Lifecycle Integration

The market data system starts and stops with the FastAPI app via the `lifespan` context manager.

**In `backend/app/main.py`** (not yet built — this is the shape it needs):

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router
from app.market.interface import MarketDataSource


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage startup and shutdown of background services."""

    # --- STARTUP ---

    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    # Tracked set = watchlist ∪ open positions (PLAN.md §6) — computed once
    # here from the database, which the (not-yet-built) watchlist/portfolio
    # modules own. See §12 for how it stays correct after startup.
    initial_tickers = await load_tracked_tickers()  # reads watchlist + positions from SQLite
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

### Accessing market data from other routes

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
        raise HTTPException(400, f"No price available for {trade.ticker}")
    # ... validate against cash/position and execute at current_price (PLAN.md §8) ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into the watchlist table ...
    await source.add_ticker(payload.ticker)  # a DB write alone does not start pricing it
    # ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from the watchlist table ...
    # Only stop tracking if there's no open position — see §12.
    # ...
```

---

## 12. Watchlist / Tracked-Ticker-Set Coordination

`PLAN.md` §6 defines the tracked set as **the watchlist plus every ticker with a non-zero
position** — not the watchlist alone, because a user can de-watchlist a ticker they still hold, and
the portfolio can't be valued without a live price for it. This logic belongs to the watchlist and
portfolio routes (not yet built), not to `app/market/` — the market module only ever does what it's
told via `add_ticker` / `remove_ticker`. This section documents the contract those routes must
satisfy.

### Flow: adding a ticker (manual or via LLM chat)

```
POST /api/watchlist {ticker: "PYPL"}
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator (fallback seed + prev_close, §7), rebuilds
                 Cholesky, seeds cache immediately
      Massive:   appends to the poll ticker list, appears on the next poll (≤15s)
  → 200 {ticker, price if already cached}
```

A database write alone does **not** start pricing the symbol — `source.add_ticker()` must be
called in the same request, or the ticker sits in the watchlist table with no price and every
SSE frame and trade attempt for it fails.

### Flow: removing a ticker

```
DELETE /api/watchlist/PYPL
  → DELETE FROM watchlist (SQLite)
  → position = await db.get_position("PYPL")
  → if position is None:
        await source.remove_ticker("PYPL")   # stops pricing, drops from cache
    else:
        # ticker stays tracked and priced; it just stops appearing in the
        # watchlist panel. Portfolio valuation for the open position still
        # needs a live price.
  → 200 {}
```

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

### Flow: opening a position in a ticker that isn't on the watchlist

Buying a ticker not currently tracked (§8's trade validation requires a cached price first, so in
practice the trade route or the chat tool-call handler must call `add_ticker` before or as part of
executing the buy):

```python
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
    source: MarketDataSource = Depends(get_market_source),
):
    if trade.ticker not in price_cache:
        await source.add_ticker(trade.ticker)
        # Simulator seeds synchronously — price is available immediately after.
        # Massive does not; the trade route should recheck the cache and
        # reject with a clear "price not yet available, try again shortly"
        # rather than blocking on a poll (see §14.2).
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}")
    # ... proceed with trade validation and execution ...
```

The ticker then joins the tracked set with no separate watchlist entry required.

---

## 13. Testing Strategy

The shipped suite (73 tests, 84% coverage — see `MARKET_DATA_SUMMARY.md`) already covers
`models.py`, `cache.py`, `interface.py`, `seed_prices.py`, `simulator.py`, `factory.py`, and
`massive_client.py` (mocked) at or near 100%. Extending it for `prev_close` means adding cases to
the existing files rather than new ones.

### 13.1 `test_models.py` — additions

```python
def test_change_from_close_positive():
    update = PriceUpdate(ticker="AAPL", price=192.00, previous_price=191.50, prev_close=190.00)
    assert update.change_from_close == 2.00
    assert round(update.change_percent_from_close, 2) == 1.05

def test_change_from_close_zero_prev_close_is_safe():
    update = PriceUpdate(ticker="ZZZZ", price=100.0, previous_price=100.0, prev_close=0.0)
    assert update.change_percent_from_close == 0.0  # no division by zero

def test_to_dict_includes_close_baseline_fields():
    update = PriceUpdate(ticker="AAPL", price=192.00, previous_price=191.50, prev_close=190.00)
    d = update.to_dict()
    assert d["prev_close"] == 190.00
    assert "change_from_close" in d
    assert "change_percent_from_close" in d
```

### 13.2 `test_cache.py` — additions

```python
def test_update_requires_prev_close_on_first_write():
    cache = PriceCache()
    update = cache.update("AAPL", 190.00, prev_close=187.50)
    assert update.prev_close == 187.50

def test_prev_close_carries_forward_when_omitted():
    cache = PriceCache()
    cache.update("AAPL", 190.00, prev_close=187.50)
    update = cache.update("AAPL", 191.00)  # simulator tick, no prev_close passed
    assert update.prev_close == 187.50

def test_missing_prev_close_on_first_write_falls_back_to_price():
    cache = PriceCache()
    update = cache.update("ZZZZ", 120.00)  # no prev_close supplied at all
    assert update.prev_close == 120.00
```

### 13.3 `test_simulator.py` — additions

```python
def test_known_ticker_prev_close_from_table():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_prev_close("AAPL") == PREV_CLOSE["AAPL"]

def test_unknown_ticker_prev_close_equals_seed():
    sim = GBMSimulator(tickers=["ZZZZ"])
    assert sim.get_prev_close("ZZZZ") == sim.get_price("ZZZZ")

def test_prev_close_removed_with_ticker():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.remove_ticker("AAPL")
    assert sim.get_prev_close("AAPL") is None
```

### 13.4 `test_simulator_source.py` — additions

```python
async def test_start_seeds_cache_with_prev_close():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
    await source.start(["AAPL"])
    assert cache.get("AAPL").prev_close == PREV_CLOSE["AAPL"]
    await source.stop()

async def test_prev_close_stable_across_ticks():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
    await source.start(["AAPL"])
    initial_prev_close = cache.get("AAPL").prev_close
    await asyncio.sleep(0.3)
    assert cache.get("AAPL").prev_close == initial_prev_close  # never drifts
    await source.stop()
```

### 13.5 `test_massive.py` — additions

```python
def _make_snapshot(ticker, price, timestamp_ms, prev_close=None):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    if prev_close is not None:
        snap.prev_day.close = prev_close
    else:
        snap.prev_day = None
    return snap

async def test_poll_captures_prev_close():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    snap = _make_snapshot("AAPL", 190.50, 1707580800000, prev_close=187.50)
    with patch.object(source, "_fetch_snapshots", return_value=[snap]):
        await source._poll_once()
    assert cache.get("AAPL").prev_close == 187.50

async def test_missing_prev_day_falls_back_to_price():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    snap = _make_snapshot("AAPL", 190.50, 1707580800000, prev_close=None)
    with patch.object(source, "_fetch_snapshots", return_value=[snap]):
        await source._poll_once()
    assert cache.get("AAPL").prev_close == 190.50
```

### 13.6 What stays out of scope here

Per `PLAN.md` §12, watchlist idempotency, the 30-ticker cap, and "removing a watchlist ticker with
an open position keeps it priced" are backend API-route tests (against the not-yet-built watchlist
endpoints), not market-module tests — `app/market/` only needs to prove `add_ticker`/`remove_ticker`
behave correctly in isolation, which the existing suite already does.

---

## 14. Error Handling & Edge Cases

### 14.1 Startup: empty watchlist

`start([])` — both sources handle it gracefully: the simulator produces no prices, the Massive
poller skips its API call, and the SSE endpoint sends nothing until a ticker is added.

### 14.2 Price cache miss during a trade

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this in practice by seeding synchronously in `add_ticker()`. Massive has an
inherent gap (up to `poll_interval` seconds) between adding a ticker and its first price — the 400
with a clear message is the correct response, not a blocking wait.

### 14.3 Massive API key invalid

First poll fails with 401; the poller logs and keeps retrying. SSE keeps streaming (connected, just
empty for the affected tickers). Fix is to correct `MASSIVE_API_KEY` and restart.

### 14.4 Massive snapshot missing `prev_day`

Handled by the `getattr(..., None)` fallback in §8 — `prev_close` degrades to the tick price
(0.00% daily change shown) rather than raising and dropping the whole ticker from that poll cycle.

### 14.5 Thread safety under load

`PriceCache` uses a plain mutex; the critical section is a dict lookup and assignment. Negligible
contention at the project's scale (≤30 tickers, one writer). A `ReadWriteLock` would only matter at
a scale this project doesn't target.

### 14.6 Simulator precision

Prices are `round()`ed to 2 decimals in `GBMSimulator.step()`; the exponential formulation is
numerically stable and always positive, so GBM can never produce a negative or zero price.

---

## 15. Delta Against the Current Implementation

Everything in §3–§10 that isn't called out below is byte-for-byte what's already on disk in
`backend/app/market/`. To bring the shipped code up to this design:

| # | File | Change |
|---|------|--------|
| 1 | `models.py` | Add `prev_close: float` field to `PriceUpdate`; add `change_from_close` / `change_percent_from_close` properties; include both in `to_dict()`. |
| 2 | `cache.py` | Add `prev_close: float \| None = None` parameter to `update()`; carry the existing value forward when omitted, or fall back to `price` on a ticker's first write with none supplied. |
| 3 | `seed_prices.py` | Add the `PREV_CLOSE: dict[str, float]` table (§6). |
| 4 | `simulator.py` (`GBMSimulator`) | Add `_prev_close` dict; populate it in `_add_ticker_internal` (known ticker → `PREV_CLOSE` table; unknown → equal to the generated seed); delete the entry in `remove_ticker`; add `get_prev_close()`. |
| 5 | `simulator.py` (`SimulatorDataSource`) | `start()` and `add_ticker()` pass `prev_close=self._sim.get_prev_close(ticker)` into `cache.update()`. |
| 6 | `massive_client.py` | `_poll_once()` reads `snap.prev_day.close` (guarded, falling back to the tick price) and passes it as `prev_close`. **Verify the exact attribute name against the installed `massive` package** — see the note in §8. |
| 7 | `tests/market/*` | Add the cases in §13. |

`interface.py`, `factory.py`, and `stream.py` need **no changes** — the interface doesn't mention
prices at all, the factory only picks a class, and the SSE endpoint just serializes whatever
`to_dict()` returns.

Also worth folding in from the existing review (`planning/archive/MARKET_DATA_REVIEW.md`), since
they're touching some of the same files:

- §3.5 there: `SimulatorDataSource.get_tickers()` in the archived design reached into
  `self._sim._tickers` (private). The current shipped code already fixed this with a public
  `GBMSimulator.get_tickers()` (confirmed in `simulator.py:140-142` on disk) — §7.1 above reflects
  the fixed version; no further action needed.
- §3.4 there (the `version` property not being read under the lock) is fixed in §4 above — it now
  acquires `self._lock`.

---

## 16. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use the simulator. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (seconds) | Time between simulator ticks. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (seconds) | Time between Massive API polls (free tier: 5 req/min). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick. |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step (fraction of a trading year). |
| SSE push interval | `_generate_events()` | `0.5` (seconds) | Time between SSE pushes to the client. |
| SSE retry directive | `_generate_events()` | `1000` (ms) | Browser `EventSource` reconnection delay. |
| `PREV_CLOSE` spread | `seed_prices.py` | ±1-4% of seed | Static per-ticker baseline for the daily change % on first paint. |

### Package `__init__.py`

Unchanged from the shipped implementation.

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
