# Market Data Backend — Design

**Status:** Implemented in `backend/app/market/`. This document describes the system as built — architecture, interfaces, and code for each module — so downstream agents (Backend, Frontend, LLM integration) can integrate against it without re-reading source.

See also: `planning/MARKET_DATA_SUMMARY.md` (short overview), `planning/archive/` (original design/review docs from the build phase).

---

## 1. Goals & Constraints

- One abstract interface, two interchangeable implementations: an in-process **GBM simulator** (default, zero config) and a **Massive (Polygon.io) REST poller** (real data, opt-in via `MASSIVE_API_KEY`).
- Downstream code (SSE stream, portfolio valuation, trade execution) never branches on which source is active — it only ever reads from a shared `PriceCache`.
- Cheap to run: single background asyncio task, no external processes, no message broker.
- Ticker set is dynamic — the watchlist can grow/shrink at runtime (`add_ticker` / `remove_ticker`) without restarting the source.

## 2. Architecture

```
                     ┌─────────────────────────────┐
                     │      MarketDataSource (ABC)   │
                     │  start / stop / add_ticker /   │
                     │  remove_ticker / get_tickers   │
                     └───────────┬─────────────────┘
                                 │
              ┌──────────────────┴──────────────────┐
              │                                     │
   ┌─────────────────────┐              ┌─────────────────────────┐
   │  SimulatorDataSource  │              │   MassiveDataSource      │
   │  (GBMSimulator inside) │              │   (Polygon.io REST poll) │
   └──────────┬───────────┘              └────────────┬────────────┘
              │  writes                                 │  writes
              └──────────────────┬──────────────────────┘
                                 ▼
                          ┌─────────────┐
                          │  PriceCache  │  thread-safe, in-memory
                          │ (ticker→last)│  version counter
                          └──────┬──────┘
                                 │ reads
             ┌───────────────────┼───────────────────┐
             ▼                   ▼                   ▼
   SSE /api/stream/prices   Portfolio valuation   Trade execution
```

`create_market_data_source(cache)` is the single entry point: it inspects `MASSIVE_API_KEY` and returns the right implementation, already wired to the cache but **not started**.

### Module map

```
backend/app/market/
├── __init__.py        # Public exports
├── models.py           # PriceUpdate (immutable dataclass)
├── interface.py        # MarketDataSource ABC
├── cache.py            # PriceCache (thread-safe store)
├── seed_prices.py      # Seed prices, per-ticker GBM params, correlation groups
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource (Polygon.io REST poller)
├── factory.py           # create_market_data_source()
└── stream.py            # create_stream_router() — SSE endpoint factory
```

---

## 3. Data Model — `models.py`

`PriceUpdate` is a frozen, slotted dataclass — one instance per ticker per tick. Immutability means a reference handed to an SSE consumer can never be mutated out from under it.

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

`direction` is what the frontend uses to decide whether to flash a row green or red. `to_dict()` is the exact JSON shape sent over SSE and is the contract the frontend's `EventSource` handler parses against.

---

## 4. Unified Interface — `interface.py`

The strategy-pattern contract both sources implement. Everything downstream depends on this abstraction, never on a concrete class.

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
        """Begin producing price updates. Starts a background task. Call once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

Four lifecycle methods is deliberately the whole surface. Anything price-related goes through the cache, not the source, so a caller can't accidentally couple business logic to "is this the simulator or the real API."

---

## 5. Shared Price Cache — `cache.py`

Single writer at a time (whichever `MarketDataSource` is active), many readers (SSE stream, portfolio valuation, trade execution, the LLM's portfolio-context builder). A plain `threading.Lock` is sufficient — the simulator writes from an asyncio task in the same event loop thread, and FastAPI's request handlers run there too; the lock only matters if you ever add a second writer thread (e.g. the Massive client's `asyncio.to_thread` snapshot fetch does its work off-thread but still writes through this same cache method on the event loop).

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
        """Record a new price. Auto-computes direction/change vs. the prior entry.

        First update for a ticker: previous_price == price (direction='flat').
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
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices — a shallow copy, safe to iterate."""
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
        """Monotonic counter. SSE uses this to skip redundant frames."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Why a version counter instead of dirty-flags per ticker:** the SSE endpoint always broadcasts the *entire* price snapshot (not per-ticker diffs), because the frontend keeps its own sparkline history keyed by ticker and just wants "give me everything you have." A single monotonically increasing int lets the stream loop skip re-serializing/re-sending when nothing changed since the last tick, without tracking per-ticker dirty state.

---

## 6. Simulator — `simulator.py`

### 6.1 Seed data — `seed_prices.py`

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
    "V": 280.00, "NFLX": 600.00,
}

# sigma: annualized volatility: mu: annualized drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM":  {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":    {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}  # for dynamically added tickers

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR = 0.3     # Between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing
```

A ticker added at runtime that isn't in `TICKER_PARAMS` gets `DEFAULT_PARAMS` and a random seed price in `$50–$300` — see `_add_ticker_internal` below.

### 6.2 GBM math

Prices evolve under Geometric Brownian Motion:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

where `μ` is annualized drift, `σ` is annualized volatility, `dt` is the tick size expressed as a fraction of a trading year, and `Z` is a (correlated) standard normal draw. With 500ms ticks over a 252-day/6.5-hour trading year, `dt ≈ 8.48e-8` — tiny enough that each tick is a sub-cent move, which accumulates into realistic-looking intraday drift over minutes of runtime.

Cross-ticker correlation is applied by drawing independent normals and left-multiplying by the **Cholesky decomposition** of a correlation matrix built from `CORRELATION_GROUPS`: same-sector pairs get high correlation (tech 0.6, finance 0.5), everything else (including TSLA, which is deliberately carved out to "do its own thing") gets 0.3.

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
    INTRA_FINANCE_CORR, INTRA_TECH_CORR, SEED_PRICES, TICKER_PARAMS, TSLA_CORR,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8 (one 500ms tick)

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
        """Advance all tickers by one tick. Hot path — called every 500ms."""
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

            # ~0.1% chance per tick per ticker of a 2-5% shock (for visual drama)
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign

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

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n²) rebuild on add/remove; fine since n stays well under 50."""
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

### 6.3 `SimulatorDataSource` — wiring the simulator to the cache

```python
class SimulatorDataSource(MarketDataSource):
    """Runs GBMSimulator.step() every `update_interval` seconds via an asyncio task."""

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
        # Seed the cache immediately so SSE has data before the first tick
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

Note the `try/except Exception` inside `_run_loop`: a single bad tick (e.g. a NaN from a pathological `sigma`) logs and moves on rather than silently killing the background task and freezing every price on the board.

---

## 7. Real Data — Massive (Polygon.io) — `massive_client.py`

REST polling, not a WebSocket — deliberately simpler, works on every Polygon tier including the free one, and needs no persistent outbound connection management.

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
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call.

    Rate limits:
      - Free tier: 5 req/min -> poll every 15s (default)
      - Paid tiers: higher limits -> poll every 2-5s
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
        await self._poll_once()  # immediate poll so cache isn't empty
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval)

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
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            # RESTClient is synchronous -> offload to a thread so the event loop isn't blocked
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> s
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise: the next scheduled poll retries. Common failures:
            # 401 (bad key), 429 (rate limit), transient network errors.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

**Design notes:**
- One API call covers the whole watchlist (`get_snapshot_all(tickers=[...])`) rather than one call per ticker — critical on the free tier's 5-calls/minute budget.
- A poll failure (bad key, rate limit, transient network blip) is swallowed and logged; the cache simply keeps serving its last-known prices until the next successful poll. The frontend never sees a hard error from a transient Polygon outage.
- Malformed individual snapshots (missing `last_trade`, etc.) are skipped per-ticker rather than aborting the whole poll.
- `add_ticker`/`remove_ticker` only mutate the tracked list; the new ticker's price appears on the *next* scheduled poll (up to `poll_interval` seconds later) rather than triggering an immediate extra API call, to stay within rate limits.

---

## 8. Factory — `factory.py`

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
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource; otherwise -> SimulatorDataSource.

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

This is the *only* place in the codebase that branches on which implementation is active. Everything else — the SSE stream, portfolio code, LLM tool calls — depends solely on `MarketDataSource` / `PriceCache`.

---

## 9. SSE Streaming — `stream.py`

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
    """Factory pattern injects the PriceCache without module-level globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # browser auto-reconnect delay if the connection drops

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
                    payload = json.dumps({t: u.to_dict() for t, u in prices.items()})
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

Wire format (one SSE frame, all tracked tickers each time the cache version changes):

```
data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.48, "timestamp": 1780012345.123, "change": 0.04, "change_percent": 0.021, "direction": "up"}, "GOOGL": {...}, ...}

```

Frontend side (per `PLAN.md` §10):

```javascript
const es = new EventSource("/api/stream/prices");
es.onmessage = (event) => {
  const prices = JSON.parse(event.data); // { AAPL: {...}, GOOGL: {...}, ... }
  for (const [ticker, update] of Object.entries(prices)) {
    updateWatchlistRow(ticker, update); // flash green/red per `update.direction`
    appendSparklinePoint(ticker, update.price, update.timestamp);
  }
};
```

`request.is_disconnected()` is polled once per loop iteration (every `interval` seconds) rather than relying purely on exceptions, so a dropped client is detected within one tick and doesn't leave an orphaned generator running.

---

## 10. Public Package Surface — `__init__.py`

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

All downstream code imports from `app.market`, never from the submodules directly.

---

## 11. Integration — FastAPI App Wiring

This is how the rest of the backend (not yet built) is expected to wire the market data subsystem into the FastAPI app's lifespan, and how other subsystems read from it.

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router
from app.db import get_watchlist  # returns list[str] from the watchlist table

price_cache = PriceCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    source = create_market_data_source(price_cache)
    tickers = get_watchlist()  # seeded defaults on a fresh DB: AAPL, GOOGL, MSFT, ...
    await source.start(tickers)
    app.state.market_source = source
    yield
    await source.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

```python
# app/api/watchlist.py — add/remove endpoints keep the DB and the live source in sync
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/watchlist")


@router.post("")
async def add_ticker(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    db_add_watchlist_ticker(ticker)                       # persist
    await request.app.state.market_source.add_ticker(ticker)  # start streaming it
    return {"ticker": ticker}


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    db_remove_watchlist_ticker(ticker)
    await request.app.state.market_source.remove_ticker(ticker)
    return {"ticker": ticker}
```

```python
# app/api/portfolio.py — trade execution and valuation read the current price straight from the cache
def execute_trade(ticker: str, quantity: float, side: str, price_cache: PriceCache):
    price = price_cache.get_price(ticker)
    if price is None:
        raise ValueError(f"No live price available for {ticker}")
    # ... validate cash/shares, write to `positions` + `trades` tables at `price` ...
```

The LLM chat flow (§9 of `PLAN.md`) builds its portfolio context the same way — `price_cache.get_all()` for current watchlist prices, `price_cache.get_price(ticker)` per position — before constructing the prompt.

---

## 12. Configuration

| Env var | Effect |
|---|---|
| `MASSIVE_API_KEY` | Unset/empty (default) → simulator. Set → `MassiveDataSource`, polling every 15s (free tier default). |
| — | Simulator has no env config; tune via `SimulatorDataSource(update_interval=..., event_probability=...)` constructor args if ever needed. |

No other environment variables affect this subsystem. `LLM_MOCK` (chat mock mode) is unrelated and handled entirely in the LLM integration layer.

---

## 13. Testing Strategy

73 tests in `backend/tests/market/`, 84% overall coverage. Approach per module:

| Module | What's tested |
|---|---|
| `test_models.py` | `change`/`change_percent`/`direction` arithmetic, zero-previous-price edge case, `to_dict()` shape |
| `test_cache.py` | update/get/get_all/remove, version bump on every write, first-update flat direction, thread-safety of concurrent updates |
| `test_simulator.py` | GBM math (drift/diffusion formula), Cholesky correlation matrix construction, add/remove ticker rebuilds correlation, random event shock bounds |
| `test_simulator_source.py` | `SimulatorDataSource` lifecycle — start seeds the cache immediately, stop cancels the task cleanly, add/remove ticker propagates to cache |
| `test_factory.py` | env var present/absent/empty-string branching, returns correct concrete type |
| `test_massive.py` | snapshot parsing, ms→s timestamp conversion, malformed-snapshot skip-and-continue, poll failure doesn't crash the loop |

`stream.py` is intentionally lower-coverage in unit tests (31%) since exercising a real `StreamingResponse` needs a running ASGI server — that path is instead covered by the Playwright E2E "SSE resilience: disconnect and verify reconnection" scenario in `test/` per `PLAN.md` §12.

Run locally:

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app
```

---

## 14. Demo

`backend/market_data_demo.py` — a standalone Rich-based terminal dashboard exercising the simulator end-to-end (all 10 default tickers, sparklines, color-coded direction arrows, an event log for shock moves) without needing the rest of the backend running:

```bash
cd backend
uv run market_data_demo.py   # runs 60s or until Ctrl+C
```

Useful as a quick sanity check after touching `simulator.py` or `seed_prices.py`.
