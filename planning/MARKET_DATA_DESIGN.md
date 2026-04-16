# Market Data Backend — Implementation Design

Complete implementation guide for the FinAlly market data subsystem. Everything in this document lives under `backend/app/market/`.

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Data Model — `models.py`](#2-data-model)
3. [Price Cache — `cache.py`](#3-price-cache)
4. [Abstract Interface — `interface.py`](#4-abstract-interface)
5. [Seed Data — `seed_data.py`](#5-seed-data)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client)
8. [Factory — `factory.py`](#8-factory)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. File Structure

```
backend/
  app/
    market/
      __init__.py        # Re-exports public API
      models.py          # PriceUpdate dataclass
      cache.py           # PriceCache (thread-safe in-memory store)
      interface.py       # MarketDataSource ABC
      seed_data.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
      simulator.py       # GBMSimulator + SimulatorDataSource
      massive_client.py  # MassiveDataSource (polygon-api-client)
      factory.py         # create_market_data_source()
      stream.py          # SSE FastAPI router
```

Each file has a single responsibility. `__init__.py` re-exports the public API so the rest of the backend imports from `app.market` without reaching into submodules.

---

## 2. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the **only** data structure that leaves the market data layer. Every downstream consumer — SSE streaming, portfolio valuation, trade execution, watchlist API — works with this type.

```python
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    prev_price: float       # Price from the previous update
    open_price: float       # Session-start seed price — set once, never overwritten
    timestamp: float        # Unix seconds
    direction: str          # "up", "down", or "flat"

    def to_sse_dict(self) -> dict:
        """Serialize to the SSE event wire format."""
        from datetime import datetime, timezone
        ts_iso = datetime.fromtimestamp(
            self.timestamp, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        return {
            "ticker": self.ticker,
            "price": self.price,
            "prev_price": self.prev_price,
            "open_price": self.open_price,
            "timestamp": ts_iso,
            "direction": self.direction,
        }
```

### Design decisions

- **`frozen=True`**: Immutable value objects — safe to share across async tasks without copying.
- **`slots=True`**: Minor memory optimization; many instances created per second.
- **`open_price`**: Set once when the ticker first enters the cache; never overwritten. This is the baseline for `(price - open_price) / open_price * 100` daily change % on the frontend.
- **`direction`**: Computed and stored (not a property) so it is consistent with `prev_price` at creation time.

---

## 3. Price Cache

**File: `backend/app/market/cache.py`**

The shared in-memory hub. Data sources write to it; SSE streaming, portfolio valuation, and trade execution read from it.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe cache of the latest price per ticker.

    Writers: SimulatorDataSource or MassiveDataSource (one active at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self) -> None:
        self._data: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Incremented on every write — used by SSE for change detection

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        open_price: float | None = None,
    ) -> PriceUpdate:
        """Record a new price. Returns the resulting PriceUpdate.

        open_price is only used on the *first* update for a ticker; ignored
        on subsequent calls. If not provided on first update, price is used.
        """
        with self._lock:
            ts = timestamp or time.time()
            existing = self._data.get(ticker)

            if existing:
                prev_price = existing.price
                effective_open = existing.open_price  # Never overwrite
            else:
                prev_price = price
                effective_open = open_price if open_price is not None else price

            if price > prev_price:
                direction = "up"
            elif price < prev_price:
                direction = "down"
            else:
                direction = "flat"

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                prev_price=round(prev_price, 2),
                open_price=round(effective_open, 2),
                timestamp=ts,
                direction=direction,
            )
            self._data[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._data.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: return just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all current prices."""
        with self._lock:
            return dict(self._data)

    def remove(self, ticker: str) -> None:
        """Remove a ticker — called when it leaves the watchlist."""
        with self._lock:
            self._data.pop(ticker, None)

    @property
    def version(self) -> int:
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._data
```

### Why `threading.Lock` not `asyncio.Lock`

The Massive client calls the synchronous `polygon` SDK inside `asyncio.to_thread()`, which runs in a real OS thread. `asyncio.Lock` only works within the async event loop — it doesn't protect against concurrent OS threads. `threading.Lock` works correctly from both sync threads and the async event loop.

### Version counter

The SSE loop polls the cache every ~500ms. The version counter lets it skip serialization when nothing has changed (important for the Massive poller, which only updates every 15s):

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        # serialize and yield
    await asyncio.sleep(0.5)
```

---

## 4. Abstract Interface

**File: `backend/app/market/interface.py`**

```python
from __future__ import annotations
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the source for prices — it reads
    from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates. Must be called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker and purge it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current active ticker list."""
```

### Push model rationale

The source writes to the cache rather than returning prices. This decouples timing: the simulator ticks at 500ms, Massive polls at 15s, but SSE always reads the cache at its own 500ms cadence. The SSE layer never needs to know which source is active.

---

## 5. Seed Data

**File: `backend/app/market/seed_data.py`**

Constants only — no logic, no imports.

```python
"""Seed prices and GBM parameters for the market simulator."""

# Realistic starting prices for the 10 default watchlist tickers
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
# sigma: annualized volatility   mu: annualized drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High vol, low drift
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High vol, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low vol (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Fallback for tickers not in TICKER_PARAMS (dynamically added tickers)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
DEFAULT_SEED_PRICE: float = 100.00  # Per PLAN.md: unknown tickers start at $100

# Sector groups for Cholesky correlation matrix
TECH_TICKERS: frozenset[str] = frozenset({"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"})
FINANCE_TICKERS: frozenset[str] = frozenset({"JPM", "V"})

INTRA_TECH_CORR: float = 0.60
INTRA_FINANCE_CORR: float = 0.50
CROSS_GROUP_CORR: float = 0.30
TSLA_CORR: float = 0.25       # TSLA does its own thing
DEFAULT_CORR: float = 0.30
```

---

## 6. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes: `GBMSimulator` (pure math engine) and `SimulatorDataSource` (async wrapper implementing `MarketDataSource`).

### GBM Math

At each time step a price evolves as:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

**Deriving `dt` for 500ms ticks:**
```
dt = 0.5s / (252 days/yr * 6.5 hr/day * 3600 s/hr)
   = 0.5 / 5,896,800 ≈ 8.48e-8
```

Correlated random draws use Cholesky decomposition of a correlation matrix:
```
Z_correlated = L @ Z_independent    where L = cholesky(C)
```

### 6.1 GBMSimulator

```python
from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_data import (
    CROSS_GROUP_CORR,
    DEFAULT_CORR,
    DEFAULT_PARAMS,
    DEFAULT_SEED_PRICE,
    FINANCE_TICKERS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TECH_TICKERS,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)

# 500ms as fraction of a trading year (252 days * 6.5h * 3600s)
_TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
_DT = 0.5 / _TRADING_SECONDS_PER_YEAR          # ~8.48e-8
_EVENT_PROB = 0.001                              # ~0.1% chance of shock per tick per ticker


class GBMSimulator:
    """Geometric Brownian Motion price simulator for a dynamic ticker set.

    Prices can never go negative (exp() is always positive). Correlated moves
    are generated via Cholesky decomposition of a sector-based correlation matrix.
    """

    def __init__(
        self,
        tickers: list[str],
        dt: float = _DT,
        event_probability: float = _EVENT_PROB,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_raw = np.random.standard_normal(n)
        z = (self._cholesky @ z_raw) if self._cholesky is not None else z_raw

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * float(z[i])
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event shock: ~1 event per 50s across 10 tickers at 2 ticks/s
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= (1 + shock)
                logger.debug("Random event: %s shock %.1f%%", ticker, shock * 100)

            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker. Rebuilds the Cholesky matrix."""
        if ticker in self._prices:
            return
        self._add(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the Cholesky matrix."""
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

    def _add(self, ticker: str) -> None:
        """Add without rebuilding Cholesky — for batch init."""
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, DEFAULT_SEED_PRICE)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Recompute Cholesky factor of the n×n correlation matrix. O(n²)."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = _pairwise_corr(self._tickers[i], self._tickers[j])
                corr[i, j] = rho
                corr[j, i] = rho

        self._cholesky = np.linalg.cholesky(corr)


def _pairwise_corr(t1: str, t2: str) -> float:
    """Sector-based pairwise correlation."""
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR
    if t1 in TECH_TICKERS and t2 in TECH_TICKERS:
        return INTRA_TECH_CORR
    if t1 in FINANCE_TICKERS and t2 in FINANCE_TICKERS:
        return INTRA_FINANCE_CORR
    return CROSS_GROUP_CORR
```

### 6.2 SimulatorDataSource

```python
UPDATE_INTERVAL = 0.5  # seconds


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator.

    Runs one asyncio background task that calls GBMSimulator.step() every
    UPDATE_INTERVAL seconds and writes results to the PriceCache.
    The SSE endpoint reads from the same cache — there is no second timer.
    """

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = UPDATE_INTERVAL,
        event_probability: float = _EVENT_PROB,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed cache immediately so SSE has data on its very first tick
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
            logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

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

### Key behaviors

| Behavior | Detail |
|----------|--------|
| Immediate seeding | `start()` populates the cache before the loop begins — no blank-screen delay |
| Prices never negative | `exp()` is always positive |
| Graceful shutdown | `stop()` cancels and awaits the task, catching `CancelledError` |
| Exception resilience | Loop catches per-step exceptions — a bad tick doesn't kill the feed |
| Cholesky rebuild | O(n²), negligible for n < 50 tickers |

---

## 7. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (Polygon.io) REST API snapshot endpoint. The synchronous `polygon` SDK runs in `asyncio.to_thread()` to avoid blocking the event loop.

**Package**: `polygon-api-client` (install with `uv add polygon-api-client`)

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

POLL_INTERVAL_FREE_TIER = 15.0   # 5 req/min free tier → poll every 15s


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = POLL_INTERVAL_FREE_TIER,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: Any = None

    async def start(self, tickers: list[str]) -> None:
        # Lazy import: only required when MASSIVE_API_KEY is set.
        # Students without an API key never need this package installed.
        from polygon import RESTClient
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)

        # Immediate first poll so the cache has data before the loop starts
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
            logger.info("Massive: added %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _poll_loop(self) -> None:
        """Sleep first, then poll. (First poll already happened in start().)"""
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
                    # Massive timestamps are Unix milliseconds → convert to seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    # Use day.open as open_price; fall back to prev_day.close pre-market
                    open_price = None
                    if snap.day and snap.day.open:
                        open_price = snap.day.open
                    elif snap.prev_day and snap.prev_day.close:
                        open_price = snap.prev_day.close
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=timestamp,
                        open_price=open_price,
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "?"), e)
            logger.debug("Massive poll: %d/%d tickers updated", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop retries on the next interval

    def _fetch_snapshots(self) -> list:
        """Synchronous SDK call. Runs in a thread via asyncio.to_thread()."""
        return self._client.get_snapshot_all("stocks", tickers=self._tickers)
```

### Error handling

| Scenario | Behavior |
|----------|----------|
| 401 Unauthorized | Logged as error; poller keeps running (fix `.env`, restart) |
| 429 Rate Limited | Logged as error; retries after `poll_interval` seconds |
| Network timeout | Logged as error; retries automatically next cycle |
| Malformed snapshot | Individual ticker skipped with warning; others still processed |
| All tickers fail | Cache retains last-known prices; SSE streams stale data |

### open_price handling

```
day.open set        → use it (normal trading hours)
day.open missing    → fall back to prev_day.close (pre-market)
both missing        → pass None; PriceCache sets open_price = first observed price
```

---

## 8. Factory

**File: `backend/app/market/factory.py`**

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select simulator or Massive based on MASSIVE_API_KEY env var.

    Returns an unstarted source. Caller must: await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        from .massive_client import MassiveDataSource
        logger.info("Market data: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        from .simulator import SimulatorDataSource
        logger.info("Market data: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

### Usage

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(["AAPL", "GOOGL", "MSFT", ...])  # tickers from DB watchlist
```

---

## 9. SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

Long-lived HTTP connection pushing price updates as `text/event-stream`. The SSE loop reads from the cache on its own 500ms cadence — decoupled from the data source's update rate.

```python
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory so the router has access to the PriceCache without globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint. Client connects with EventSource, receives all
        tracked ticker prices every ~500ms.

        Wire format per event:
            data: {"ticker":"AAPL","price":190.50,"prev_price":190.42,
                   "open_price":190.00,"timestamp":"2026-04-10T12:00:00.500Z",
                   "direction":"up"}
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
):
    """Yield one SSE event per ticker per interval. One event = one JSON object."""
    # Tell the browser to retry after 1 second on disconnect
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
                for update in prices.values():
                    payload = json.dumps(update.to_sse_dict())
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled: %s", client_ip)
```

### SSE wire format (one event per ticker)

```
data: {"ticker":"AAPL","price":190.50,"prev_price":190.42,"open_price":190.00,"timestamp":"2026-04-10T12:00:00.500Z","direction":"up"}

data: {"ticker":"GOOGL","price":175.12,"prev_price":175.08,"open_price":175.00,"timestamp":"2026-04-10T12:00:00.500Z","direction":"up"}

```

### Frontend consumption

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const update = JSON.parse(event.data);
    // update: { ticker, price, prev_price, open_price, timestamp, direction }
    // Flash the price cell, update sparkline, compute daily change %
    const dailyChange = (update.price - update.open_price) / update.open_price * 100;
};
eventSource.onerror = () => {
    // EventSource auto-reconnects after `retry` ms — no manual handling needed
};
```

---

## 10. FastAPI Lifecycle Integration

**In `backend/app/main.py`:**

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

    initial_tickers = await load_watchlist_tickers()  # reads default 10 from SQLite
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # App running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# FastAPI dependencies for injecting market state into route handlers
def get_price_cache() -> PriceCache:
    return app.state.price_cache

def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Using market state in other routes

```python
from fastapi import Depends

@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"No price available for {trade.ticker}. Try again shortly.")
    # ... execute at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.insert_watchlist(payload.ticker)
    await source.add_ticker(payload.ticker)


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist(ticker)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)
```

---

## 11. Watchlist Coordination

### Flow: Adding a ticker

```
POST /api/watchlist {"ticker": "PYPL"}
  → INSERT into watchlist table
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator ($100 seed), rebuilds Cholesky, seeds cache
      Massive:   appends to ticker list, included on next poll
  → Return watchlist entry with price (null if Massive hasn't polled yet)
```

### Flow: Removing a ticker

```
DELETE /api/watchlist/PYPL
  → DELETE from watchlist table
  → (check: does user hold PYPL?)
      If no position → await source.remove_ticker("PYPL")
                        simulator stops tracking it, cache entry purged
      If open position → keep tracking for portfolio valuation
  → Return success
```

### Edge case: open position

Removing a ticker from the watchlist while holding shares must not stop price tracking — portfolio valuation needs a current price for unrealized P&L. The route checks for an open position before calling `remove_ticker`.

---

## 12. Testing Strategy

### Unit tests: `GBMSimulator`

```python
# backend/tests/market/test_simulator.py
import pytest
from app.market.simulator import GBMSimulator
from app.market.seed_data import SEED_PRICES, DEFAULT_SEED_PRICE


class TestGBMSimulator:

    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_always_positive(self):
        sim = GBMSimulator(["AAPL"])
        for _ in range(10_000):
            assert sim.step()["AAPL"] > 0

    def test_initial_price_matches_seed(self):
        sim = GBMSimulator(["AAPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_unknown_ticker_uses_default_seed(self):
        sim = GBMSimulator(["ZZZZ"])
        assert sim.get_price("ZZZZ") == DEFAULT_SEED_PRICE

    def test_add_ticker(self):
        sim = GBMSimulator(["AAPL"])
        sim.add_ticker("TSLA")
        assert "TSLA" in sim.step()

    def test_remove_ticker(self):
        sim = GBMSimulator(["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_add_duplicate_is_noop(self):
        sim = GBMSimulator(["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim._tickers) == 1

    def test_remove_absent_is_noop(self):
        sim = GBMSimulator(["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_empty_step(self):
        sim = GBMSimulator([])
        assert sim.step() == {}

    def test_cholesky_none_for_single_ticker(self):
        sim = GBMSimulator(["AAPL"])
        assert sim._cholesky is None

    def test_cholesky_built_for_two_tickers(self):
        sim = GBMSimulator(["AAPL", "GOOGL"])
        assert sim._cholesky is not None

    def test_prices_drift_over_time(self):
        sim = GBMSimulator(["AAPL"])
        for _ in range(1000):
            sim.step()
        assert sim.get_price("AAPL") != SEED_PRICES["AAPL"]
```

### Unit tests: `PriceCache`

```python
# backend/tests/market/test_cache.py
from app.market.cache import PriceCache


class TestPriceCache:

    def test_update_and_get(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.ticker == "AAPL"
        assert update.price == 190.50
        assert cache.get("AAPL") == update

    def test_first_update_is_flat(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.50)
        assert update.direction == "flat"
        assert update.prev_price == 190.50
        assert update.open_price == 190.50

    def test_direction_up(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 191.00)
        assert update.direction == "up"
        assert update.prev_price == 190.00

    def test_direction_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        update = cache.update("AAPL", 189.00)
        assert update.direction == "down"

    def test_open_price_never_changes(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 195.00)
        cache.update("AAPL", 185.00)
        assert cache.get("AAPL").open_price == 190.00

    def test_open_price_from_first_call(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00, open_price=185.00)
        assert cache.get("AAPL").open_price == 185.00
        # Second update with different open_price is ignored
        cache.update("AAPL", 192.00, open_price=999.00)
        assert cache.get("AAPL").open_price == 185.00

    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_all(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        assert set(cache.get_all().keys()) == {"AAPL", "GOOGL"}

    def test_version_increments(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 190.00)
        assert cache.version == v0 + 1
        cache.update("AAPL", 191.00)
        assert cache.version == v0 + 2
```

### Integration tests: `SimulatorDataSource`

```python
# backend/tests/market/test_simulator_source.py
import asyncio
import pytest
from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:

    async def test_start_seeds_cache_immediately(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])
        # Cache populated before first loop tick
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        await source.start(["AAPL"])
        await source.stop()
        await source.stop()  # Should not raise

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
```

### Unit tests: `MassiveDataSource` (mocked)

```python
# backend/tests/market/test_massive.py
from unittest.mock import MagicMock, patch
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _snap(ticker: str, price: float, ts_ms: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ms
    snap.day.open = price * 0.99
    snap.prev_day.close = price * 0.98
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:

    async def test_poll_updates_cache(self):
        cache = PriceCache()
        source = MassiveDataSource("test-key", cache, poll_interval=999)
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", return_value=[
            _snap("AAPL", 190.50, 1707580800000),
            _snap("GOOGL", 175.25, 1707580800000),
        ]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        cache = PriceCache()
        source = MassiveDataSource("test-key", cache, poll_interval=999)
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()

        bad = MagicMock()
        bad.ticker = "BAD"
        bad.last_trade = None  # AttributeError when accessing .price

        with patch.object(source, "_fetch_snapshots", return_value=[
            _snap("AAPL", 190.50, 1707580800000), bad
        ]):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_raise(self):
        cache = PriceCache()
        source = MassiveDataSource("test-key", cache, poll_interval=999)
        source._tickers = ["AAPL"]
        source._client = MagicMock()

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None
```

---

## 13. Error Handling & Edge Cases

### Empty watchlist at startup

Both sources handle `start([])` gracefully: simulator produces no prices, Massive skips its API call. When a ticker is added later, tracking begins immediately.

### Price cache miss during trade

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"No price available for {ticker}. Try again shortly.")
```

The simulator seeds the cache in `add_ticker()` so this should be rare. The Massive client may have a brief gap after a ticker is added (until the next poll).

### Massive API key invalid

401 errors are logged; the poller keeps running. The SSE stream continues (sending empty data). Users see no prices until the key is corrected and the container restarted.

### Thread safety

`PriceCache` uses `threading.Lock`. Under normal load (10–50 tickers, 2 updates/sec), lock contention is negligible. The critical section is a single dict assignment.

### GBM numerical stability

- Prices use `exp()` — always positive, never zero or negative
- Prices are `round()`ed to 2 decimal places in `GBMSimulator.step()`
- The exponential formulation is numerically stable for the tiny `dt` values used

---

## 14. Configuration Summary

| Parameter | Where | Default | Description |
|-----------|-------|---------|-------------|
| `MASSIVE_API_KEY` | env var | `""` | Set to enable real market data; empty = simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5s` | Simulator tick rate |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0s` | Massive API poll rate |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Random shock chance per ticker per tick |
| `dt` | `GBMSimulator` | `~8.48e-8` | GBM time step (fraction of trading year) |
| SSE push interval | `_generate_events()` | `0.5s` | How often SSE sends to clients |
| SSE retry | `_generate_events()` | `1000ms` | Browser reconnect delay |

### Package `__init__.py`

```python
# backend/app/market/__init__.py

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
