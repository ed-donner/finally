# Market Data Backend — Design

**Status: implemented.** This document describes the market data subsystem as it actually exists in the codebase today, at `backend/app/market/` (9 modules, 84% test coverage, 73 tests). It supersedes `planning/archive/MARKET_DATA_DESIGN.md`, which described the pre-review design — several details there (lazy `massive` imports, missing `pyproject.toml` build config, private-attribute access, a mistyped generator return type) were fixed during code review and are reflected here as built. See `planning/MARKET_DATA_SUMMARY.md` for the short version and `planning/archive/MARKET_DATA_REVIEW.md` for the review that produced these fixes.

This doc is a reference for anyone integrating against the module (wiring up the FastAPI app, portfolio/trade routes, watchlist routes) — not a spec to reimplement.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy)
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters--seed_pricespy)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy)
9. [Factory — `factory.py`](#9-factory--factorypy)
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint--streampy)
11. [Package Exports — `__init__.py`](#11-package-exports--__init__py)
12. [Integrating This Module Into the FastAPI App](#12-integrating-this-module-into-the-fastapi-app)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing](#14-testing)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, single instance)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Portfolio valuation (not yet wired — see §12)
        └──→ Trade execution (not yet wired — see §12)
```

Both data sources implement the same `MarketDataSource` ABC (strategy pattern). Neither returns prices to its caller — each pushes `PriceUpdate`s into a shared `PriceCache` on its own schedule (simulator: every 500ms; Massive: every 15s by default). Everything downstream — SSE streaming, trade execution, portfolio valuation — reads from the cache and is completely agnostic to which source is active. This decouples timing: the simulator and the Massive poller run on very different cadences, but consumers always read at their own pace from one place.

**What's built vs. what's still needed:** `backend/app/market/` is complete, tested, and self-contained. There is currently **no `app/main.py`** and no FastAPI app instantiated anywhere in the repo — `backend/app/` contains only `__init__.py` and the `market/` package. Watchlist, portfolio, and trade routers don't exist yet. §12 below specifies exactly how those pieces should wire against this module when they're built, matching the real function signatures in the code (not aspirational ones).

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
      seed_prices.py            # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py               # GBMSimulator + SimulatorDataSource
      massive_client.py          # MassiveDataSource
      factory.py                 # create_market_data_source()
      stream.py                  # SSE endpoint (FastAPI router factory)
  market_data_demo.py          # Rich-terminal live demo of the simulator (dev tool, not app code)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
      # no test_stream.py — SSE generator needs a running ASGI server; untested directly
```

Each file has a single responsibility. `app.market.__init__` re-exports the public surface so the rest of the backend imports `from app.market import PriceCache, create_market_data_source, ...` without reaching into submodules.

---

## 3. Data Model — `models.py`

`PriceUpdate` is the only data structure that leaves the market data layer.

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

- `frozen=True`: safe to share across async tasks without copying.
- `slots=True`: memory optimization — many of these are created per second.
- `change` / `change_percent` / `direction` are computed properties, never stored fields, so they can't drift out of sync with `price`/`previous_price`.
- `to_dict()` is the single serialization point, used by both the SSE endpoint and any future REST responses.

---

## 4. Price Cache — `cache.py`

The central hub: data sources write to it, everything else reads from it.

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
        self._version: int = 0  # Bumped on every update() call

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
            return dict(self._prices)  # shallow copy

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

**Why a version counter.** The SSE loop polls the cache every ~500ms. Without a version counter it would re-serialize and resend every ticker on every tick, even when the Massive source hasn't produced anything new in 15s. The counter lets the SSE loop skip sends when nothing changed:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

**Why `threading.Lock`, not `asyncio.Lock`.** The Massive client's synchronous REST call runs inside `asyncio.to_thread(...)`, which executes in a real OS thread — an `asyncio.Lock` would not protect against a write from that thread. `threading.Lock` works correctly from both a background thread and the async event loop, which is the mixed environment this cache actually lives in.

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
        """Stop the background task and release resources. Safe to call more
        than once — after stop(), the source will not write to the cache again.
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
        """Return the current list of actively tracked tickers (sync — no I/O)."""
```

`get_tickers()` is declared here and implemented by both concrete sources — it's a plain sync method reaching into whatever in-memory list each source maintains, not a network call.

**Why the source writes to the cache instead of returning prices from `start()`.** This push model decouples timing between producer and consumers: the simulator ticks at 500ms, Massive polls at 15s, but the SSE layer always reads from the cache at its own fixed cadence. SSE code never needs to know which source is active or what its native update interval is.

---

## 6. Seed Prices & Ticker Parameters — `seed_prices.py`

Constants only — no logic, no imports beyond stdlib. Shared by the simulator (initial prices + GBM parameters) and conceptually reusable as fallback prices elsewhere.

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

# sigma: annualized volatility, mu: annualized drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers not in the list above (e.g. dynamically added via watchlist)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups used by GBMSimulator's Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6       # Tech stocks move together
INTRA_FINANCE_CORR = 0.5    # Finance stocks move together
CROSS_GROUP_CORR = 0.3      # Cross-sector, and unknown/unclassified tickers
TSLA_CORR = 0.3             # TSLA does its own thing regardless of counterpart
```

Note there is deliberately **no** separate `DEFAULT_CORR` constant — an earlier draft had one, but it was numerically identical to `CROSS_GROUP_CORR` and never actually referenced by the fallback branch, so it was removed as dead/misleading during review. Both "cross-sector" and "unknown ticker" cases use `CROSS_GROUP_CORR`.

---

## 7. GBM Simulator — `simulator.py`

Two classes: `GBMSimulator` (pure math engine, stateful) and `SimulatorDataSource` (the `MarketDataSource` implementation wrapping it in an async loop).

### 7.1 `GBMSimulator`

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

    S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

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

            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # ~0.1% chance per tick per ticker of a 2-5% shock, for visual drama
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
        """Public accessor — used by SimulatorDataSource.get_tickers()
        so it doesn't have to reach into a private attribute."""
        return list(self._tickers)

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2), n < 50 in practice — cheap even rebuilt on every add/remove."""
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

### 7.2 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator. Runs a background asyncio
    task that calls GBMSimulator.step() every `update_interval` seconds."""

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

Key behaviors: the cache is seeded with initial prices **before** the loop starts, so there's no blank-screen delay on the first SSE tick; `stop()` cancels and awaits the task, swallowing `CancelledError`, for clean shutdown during FastAPI lifespan teardown; `_run_loop` catches exceptions per-step so one bad tick can't kill the whole feed.

---

## 8. Massive API Client — `massive_client.py`

Polls the Massive (formerly Polygon.io) REST snapshot endpoint on an interval. The synchronous Massive client runs inside `asyncio.to_thread()` so it never blocks the event loop.

```python
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

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call.

    Rate limits:
      - Free tier: 5 req/min -> poll every 15s (default)
      - Paid tiers: higher limits -> poll every 2-15s depending on tier
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
        self._tickers = list(tickers)

        await self._poll_once()  # Immediate first poll so the cache has data right away

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
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return

        try:
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
            # Common causes: 401 bad key, 429 rate limit, network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call — runs inside asyncio.to_thread()."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

**Imports are top-level, not lazy.** `massive` is declared a core dependency in `pyproject.toml` (`massive>=1.0.0`), so `RESTClient`/`SnapshotMarketType` are imported at module scope like everything else. (An earlier draft deferred these imports into `start()` on the theory that `massive` should be optional for simulator-only use; that theory didn't hold up because `uv sync` installs the full lockfile regardless, and lazy imports made the module's own unit tests fragile to patch — see `planning/archive/MARKET_DATA_REVIEW.md` §3.2. If a future need arises to make `massive` a true optional dependency, reintroducing the lazy import inside `start()` plus `pyproject.toml` extras would be the way to do it — but that's not the current design.)

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error; poller keeps running (user might fix `.env` and restart the container). |
| **429 Rate Limited** | Logged as error; next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error; retries automatically on next cycle. |
| **Malformed snapshot** | That ticker is skipped with a warning; other tickers in the same poll still process. |
| **All tickers fail** | Cache retains last-known prices — SSE keeps streaming stale-but-present data rather than nothing. |

### Massive API reference used here

`get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])` returns one snapshot object per ticker in a single call — this is what keeps polling within the free tier's 5 req/min limit regardless of watchlist size. Each snapshot exposes (at minimum, for what this client uses):

```python
snap.ticker                    # "AAPL"
snap.last_trade.price          # 190.42
snap.last_trade.timestamp      # Unix milliseconds
```

Other endpoints exist (`get_snapshot_ticker` for single-ticker detail, `get_previous_close_agg`, `list_aggs` for historical bars, `get_last_trade`/`get_last_quote`) but are not currently called anywhere in this module — they're candidates if a future detail-chart or historical-data feature needs them, not part of the live-polling path.

---

## 9. Factory — `factory.py`

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real market data)
    - Otherwise -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

This is the only place in the module that reads `MASSIVE_API_KEY` — no other file inspects environment variables directly. Selection logic is a single string check: empty/whitespace/absent → simulator; anything else → Massive.

---

## 10. SSE Streaming Endpoint — `stream.py`

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
    """Factory that binds a PriceCache to the /prices route via closure,
    avoiding a module-level global. Call exactly once, at app startup."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
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
    yield "retry: 1000\n\n"  # browser EventSource reconnect delay, in ms

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

**Wire format** — each event is a JSON object keyed by ticker:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Client side:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);   // { "AAPL": {...}, "GOOGL": {...} }
};
```

**Why poll-and-push instead of an event-driven push from the cache.** The endpoint polls `price_cache.version` on a fixed interval rather than being notified by the writer. This is simpler (no pub/sub plumbing between the market module and the streaming layer) and produces evenly-spaced updates, which matters because the frontend accumulates these into sparklines — jittery spacing would look worse than a slightly coarser but regular cadence.

**Note on test coverage.** `stream.py` has no dedicated unit test (`tests/market/` has no `test_stream.py`) because exercising `_generate_events` meaningfully requires a running ASGI server (`httpx.AsyncClient` against the mounted app) rather than calling the generator directly. This is a known gap — see §14.

---

## 11. Package Exports — `__init__.py`

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

Everything outside `app/market/` should import from `app.market`, not from submodules — this keeps the internal file layout free to change without touching call sites.

---

## 12. Integrating This Module Into the FastAPI App

This is the part that **doesn't exist yet** in the codebase. `app/main.py` needs to be created; here is how it should use the market module, based on the real signatures above (not the aspirational sketch in the archived doc).

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

    initial_tickers = await load_watchlist_tickers()  # from SQLite, see PLAN.md §7
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

`create_stream_router()` should be called exactly once (at startup) — it registers a route on a module-level `APIRouter` inside `stream.py`, so calling it twice would double-register `/api/stream/prices`. That's fine for normal app startup (which only calls it once) but worth remembering if you ever write an integration test that spins up the app more than once in the same process; construct a fresh `TestClient`/`AsyncClient` against the single app instance rather than re-running the lifespan.

Routes elsewhere in the backend pull the cache and source via FastAPI dependency injection:

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
        raise HTTPException(400, f"Price not yet available for {trade.ticker}")
    # ... validate cash/shares, execute at current_price, record in SQLite ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)
    # ... return ticker + current price if the cache already has one ...


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

When the watchlist changes — via the REST endpoint or the LLM chat's `watchlist_changes` — the market data source must be told, or it will keep tracking (or fail to track) the wrong set of tickers.

**Adding a ticker:**
```
POST /api/watchlist {ticker: "PYPL"}
  -> INSERT INTO watchlist (SQLite)
  -> await source.add_ticker("PYPL")
       Simulator: seeds a price (from SEED_PRICES or a random $50-300 draw),
                  rebuilds the Cholesky correlation matrix
       Massive:   appended to the polled ticker list; price appears after
                  the next poll cycle (up to `poll_interval` seconds later)
  -> return success
```

**Removing a ticker:**
```
DELETE /api/watchlist/PYPL
  -> DELETE FROM watchlist (SQLite)
  -> await source.remove_ticker("PYPL")
       Simulator: dropped from GBMSimulator, Cholesky rebuilt, cache entry removed
       Massive:   dropped from polled list, cache entry removed
  -> return success
```

**Edge case — ticker still has an open position.** If the user removes a ticker from their watchlist while still holding shares, the data source should keep tracking it so portfolio valuation stays accurate (the position still needs a live price even if it's off the watchlist). The watchlist route is responsible for this check — the market module itself has no concept of "positions":

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # only stop tracking if nothing is held

    return {"status": "ok"}
```

---

## 14. Testing

**73 tests, all passing, 84% overall coverage.** Located in `backend/tests/market/`.

| Module | Coverage | What's covered |
|---|---|---|
| `models.py` | 100% | Construction; `change`/`change_percent`/`direction` for up/down/flat and the zero-previous-price guard; `to_dict()`; frozen-dataclass immutability. |
| `cache.py` | 100% | update+get; first-update-is-flat; direction on up/down moves; `remove()` (existing + no-op on missing); `get_all()` snapshot semantics; `version` increments; `get_price()`; `__len__`/`__contains__`; custom timestamp override; 2dp rounding. |
| `interface.py` | 100% | ABC contract (implicitly, via both concrete implementations). |
| `seed_prices.py` | 100% | Constant data, no branching logic to exercise beyond import. |
| `factory.py` | 100% | Simulator selected on missing/empty/whitespace-only key; Massive selected on a real key; correct constructor args passed through in each case. |
| `simulator.py` | 98% | `step()` returns all tickers and stays positive over 10k iterations; seed prices match `SEED_PRICES`; add/remove (including duplicate-add and remove-of-missing no-ops); unknown tickers get a random $50-300 seed; empty-ticker-list step returns `{}`; prices actually move after many steps; Cholesky is `None` for ≤1 ticker and non-`None` once a second ticker is added; pairwise correlation for tech/finance/TSLA/cross-sector combinations. Uncovered: the internal duplicate-guard branch in `_add_ticker_internal` and the exception-log line inside `_run_loop`. |
| `massive_client.py` | 56% (expected) | `_poll_once` with mocked `_fetch_snapshots`: happy path, malformed-snapshot skip (`AttributeError`/`TypeError` guard), whole-poll exception doesn't crash; ms→s timestamp conversion; `add_ticker`/`remove_ticker` including upper-casing and whitespace stripping; empty-ticker-list skips the poll; `stop()` idempotency and task cancellation; immediate first poll on `start()`. Coverage is capped because the real HTTP call path (`_fetch_snapshots` itself, unmocked) isn't exercised — that would require live network access or a fully faked `massive.RESTClient`. |
| `stream.py` | 31% (expected, no dedicated test file) | Only incidentally covered by import. Testing `_generate_events` properly needs an ASGI test client (`httpx.AsyncClient` against a mounted app) driving a real `StreamingResponse`, which doesn't exist yet — see the gap noted below. |

**Known gaps, if this module is revisited:**
- No SSE integration test. Once `app/main.py` exists, add one using `httpx.AsyncClient(app=app, base_url=...)` that connects to `/api/stream/prices`, reads a few chunks, and asserts on the parsed JSON — this is the natural place to also exercise `create_stream_router` end-to-end rather than just unit-testing pieces around it.
- No concurrent-writer test for `PriceCache` (multiple threads calling `update()` simultaneously) — the lock usage is correct by inspection but untested under contention.
- No test with the full 10-ticker default watchlist through `GBMSimulator` — existing tests use 1-2 tickers; a full-roster test would catch any correlation-matrix issue (e.g., non-positive-semi-definite construction) that only shows up at that size.

None of these block current functionality; they're deferred because they require infrastructure (a live ASGI app, thread-based stress testing) that other parts of the backend don't have yet either.

---

## 15. Error Handling & Edge Cases

**Startup with an empty watchlist.** If the database has no watchlist rows, `start([])` is called. Both sources handle this: the simulator's `step()` returns `{}` on zero tickers, the Massive poller's `_poll_once()` returns immediately when `self._tickers` is empty. The SSE endpoint sends empty payloads. Adding a ticker later starts tracking immediately via `add_ticker()`.

**Trading a ticker with no cached price.** Can happen right after adding a ticker to a Massive-backed watchlist, before the first poll completes (the simulator avoids this by seeding synchronously in `add_ticker()`). The trade route should treat a `None` from `price_cache.get_price()` as a 400, not attempt the trade:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Try again shortly.")
```

**Invalid Massive API key.** The first poll fails with 401; `_poll_once` logs the error and returns without raising, so the poller keeps retrying every `poll_interval` seconds indefinitely. The SSE connection itself stays healthy (it's independent of whether the cache has data), so the frontend's connection-status dot would show "connected" while prices stay blank — the fix is correcting `.env` and restarting the container, there's no in-app recovery path today.

**Thread safety under load.** `PriceCache` uses a plain mutex; the critical sections are all tiny (dict read/write). At the project's actual scale (≤ a few dozen tickers, updates every 500ms, a handful of concurrent SSE readers) contention is a non-issue. If this ever needed to scale to hundreds of tickers or many concurrent readers, a read-write lock would be the natural next step — not something to build preemptively here.

**Simulator numerical behavior.** GBM's multiplicative update (`price *= exp(...)`) means prices can never go negative or exactly zero starting from a positive seed. Output is rounded to 2 decimal places in `GBMSimulator.step()`, so floating-point drift never becomes user-visible.

---

## 16. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (unset) | Non-empty → `MassiveDataSource`; empty/unset → `SimulatorDataSource`. Read only in `factory.py`. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` sec | Time between `GBMSimulator.step()` calls. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` sec | Time between Massive REST polls (free-tier-safe default; lower it if the account is paid). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Per-ticker, per-tick chance of a 2-5% shock event. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step as a fraction of a trading year, derived from a 500ms tick. |
| SSE push interval | `_generate_events()` | `0.5` sec | How often the SSE loop checks `price_cache.version` and potentially sends. |
| SSE retry directive | `_generate_events()` | `1000` ms | `retry:` line sent to the browser; controls `EventSource` auto-reconnect delay. |

Dependencies (`backend/pyproject.toml`): `fastapi>=0.115.0`, `uvicorn[standard]>=0.32.0`, `numpy>=2.0.0`, `massive>=1.0.0`, `rich>=13.0.0` (used only by the standalone demo, not the app). Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`. Build config includes `[tool.hatch.build.targets.wheel] packages = ["app"]`, without which `uv sync` cannot determine what to ship — this was a review-blocking bug in an earlier draft and is now fixed.
