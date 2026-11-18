# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified Python API, the in-memory price cache, the GBM simulator, the Massive (Polygon.io) REST client, the SSE stream, and how the rest of the backend wires them together.

This document is the **single implementation reference**. It consolidates and supersedes `archive/MARKET_DATA_DESIGN.md`, and it folds in the open items from `MARKET_INTERFACE.md` §8 and `REVIEW.md` (session-change fields, the Massive `sip_timestamp` bug, the price fallback chain, the free-tier EOD fallback, the stream-router footgun). Background material stays where it is:

| Topic | Reference |
|---|---|
| Product contract (SSE shape, trade rules, tracked tickers) | `PLAN.md` §6, §8, §9 |
| Interface rationale | `MARKET_INTERFACE.md` |
| Simulator maths and tuning | `MARKET_SIMULATOR.md` |
| Massive endpoints, plans, SDK attribute names | `MASSIVE_API.md` |

Everything below lives in `backend/app/market/` unless a path says otherwise. Code is written for Python 3.12+, FastAPI, numpy 2, and `massive` 2.2.0 (the version in `uv.lock`). SDK attribute names were checked against the installed package source.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [File layout and public API](#2-file-layout-and-public-api)
3. [`PriceUpdate` — models.py](#3-priceupdate--modelspy)
4. [`PriceCache` — cache.py](#4-pricecache--cachepy)
5. [`MarketDataSource` — interface.py](#5-marketdatasource--interfacepy)
6. [Seed data — seed_prices.py](#6-seed-data--seed_pricespy)
7. [GBM simulator — simulator.py](#7-gbm-simulator--simulatorpy)
8. [Massive client — massive_client.py](#8-massive-client--massive_clientpy)
9. [Factory — factory.py](#9-factory--factorypy)
10. [SSE stream — stream.py](#10-sse-stream--streampy)
11. [App integration: lifespan, dependencies, tracked tickers](#11-app-integration)
12. [Consumer examples](#12-consumer-examples)
13. [Testing](#13-testing)
14. [Edge cases and failure modes](#14-edge-cases-and-failure-modes)
15. [Configuration](#15-configuration)
16. [Implementation checklist (current code → this design)](#16-implementation-checklist)

---

## 1. Architecture

```
                   create_market_data_source(cache)        ← reads MASSIVE_API_KEY
                                 │
                ┌────────────────┴─────────────────┐
     MASSIVE_API_KEY empty                MASSIVE_API_KEY set
                ▼                                   ▼
     SimulatorDataSource                   MassiveDataSource
     GBMSimulator.step() every 0.5 s       REST poll every 15 s
     (correlated GBM + shock events)       snapshot mode ──403──► EOD mode (grouped daily)
                │ cache.update()                    │ cache.update()
                └──────────────┬────────────────────┘
                               ▼
                 PriceCache  (threading.Lock, version counter,
                              derives previous_price / direction / session_open)
                               │ get / get_price / get_all / version
     ┌──────────────┬──────────┴───────┬────────────────┬──────────────────┐
 SSE /api/stream   GET /api/portfolio   POST trade       GET /api/watchlist   chat context
 (batch per change) (valuation)         (fill price)     (price columns)      (LLM prompt)
```

Three rules hold the design together:

1. **Producers push, consumers pull.** A source writes into `PriceCache` on its own schedule. No consumer ever asks a source for a price.
2. **One interface, two strategies.** `MarketDataSource` is an ABC with five methods. The factory picks the strategy once at startup; nothing downstream knows which one runs.
3. **The cache owns all derived fields.** `previous_price`, `change`, `change_percent`, `direction`, `session_open` and `session_change_percent` are computed in one place, identically for both sources. A source only supplies `(ticker, price, timestamp)`.

The market layer knows nothing about the database. The rule "track watchlist ∪ open positions" (PLAN §6) lives in a small app-layer service (§11.3) that calls `add_ticker` / `remove_ticker`.

---

## 2. File layout and public API

```
backend/app/
├── market/
│   ├── __init__.py         # re-exports the public API below
│   ├── models.py           # PriceUpdate
│   ├── cache.py            # PriceCache
│   ├── interface.py        # MarketDataSource (ABC)
│   ├── seed_prices.py      # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
│   ├── simulator.py        # GBMSimulator (pure engine) + SimulatorDataSource
│   ├── massive_client.py   # MassiveDataSource (snapshot + EOD modes)
│   ├── factory.py          # create_market_data_source()
│   └── stream.py           # create_stream_router() → GET /api/stream/prices
├── services/
│   └── market_tracking.py  # tracked_tickers / ensure_tracked / maybe_untrack (app layer)
├── deps.py                 # get_price_cache / get_market_source FastAPI dependencies
└── main.py                 # create_app() + lifespan
```

```python
# app/market/__init__.py
"""Market data subsystem for FinAlly. Import from here, not from submodules."""

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

| Name | Kind | Used by |
|---|---|---|
| `PriceUpdate` | frozen dataclass | everything that reads prices |
| `PriceCache` | class | the one object consumers read |
| `MarketDataSource` | ABC | lifespan, tracking service |
| `create_market_data_source(cache)` | function | lifespan |
| `create_stream_router(cache)` | function | `create_app()` |

---

## 3. `PriceUpdate` — models.py

The only data type that leaves the market layer. Its `to_dict()` is exactly the per-ticker object in the SSE payload (PLAN §6) and the source of the price columns in `GET /api/watchlist`.

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of one ticker's latest price plus derived fields."""

    ticker: str
    price: float                       # rounded to 2 dp by the cache
    previous_price: float              # price at the previous cache write (== price on first write)
    timestamp: float = field(default_factory=time.time)  # Unix seconds (float)
    session_open: float | None = None  # first price recorded since server start; None → price

    def __post_init__(self) -> None:
        # Direct construction (tests, ad-hoc use) may omit session_open.
        # frozen + slots still allows object.__setattr__ during init.
        if self.session_open is None:
            object.__setattr__(self, "session_open", self.price)

    @property
    def change(self) -> float:
        """Absolute change vs the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percent change vs the previous tick."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up' | 'down' | 'flat' vs the previous tick (drives the flash animation)."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def session_change_percent(self) -> float:
        """Percent change since session open — the watchlist's "Chg %" column."""
        if not self.session_open:
            return 0.0
        return round((self.price - self.session_open) / self.session_open * 100, 4)

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE. Key order matches PLAN §6."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "session_open": self.session_open,
            "session_change_percent": self.session_change_percent,
        }
```

**Example** (the PLAN §6 sample, reproduced exactly):

```python
>>> u = PriceUpdate("AAPL", 190.52, 190.41, timestamp=1731753600.12, session_open=190.0)
>>> u.to_dict()
{'ticker': 'AAPL', 'price': 190.52, 'previous_price': 190.41, 'timestamp': 1731753600.12,
 'change': 0.11, 'change_percent': 0.0578, 'direction': 'up',
 'session_open': 190.0, 'session_change_percent': 0.2737}
```

Design notes:
- `frozen=True, slots=True`: readers on other threads can never see a half-written object, and each instance is small (the cache holds ≤ ~40).
- Derived values are properties, not stored fields, so they can never disagree with `price`.
- `session_open` is placed last with a default so the existing positional constructor calls in tests keep working.

---

## 4. `PriceCache` — cache.py

The single source of truth for "current price". Thread-safe because the Massive source's HTTP call runs in a worker thread (writes themselves happen back on the event loop, but the lock makes the cache safe regardless of who writes).

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Latest PriceUpdate per ticker.

    Writers: exactly one MarketDataSource (simulator or Massive).
    Readers: SSE stream, portfolio valuation, trade execution, watchlist, chat context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # bumped on every mutation (update AND remove)

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price and return the resulting PriceUpdate.

        First write for a ticker: previous_price = price (direction 'flat') and
        session_open = price. Later writes carry session_open forward unchanged.
        """
        price = round(float(price), 2)
        ts = timestamp if timestamp is not None else time.time()  # `or` would drop ts == 0.0
        with self._lock:
            prev = self._prices.get(ticker)
            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=prev.price if prev else price,
                timestamp=ts,
                session_open=prev.session_open if prev else price,
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
        """Shallow copy — safe to iterate while the source keeps writing."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Forget a ticker entirely (session_open included).

        Bumps the version so the SSE stream pushes a batch without it.
        """
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Example lifecycle:**

```python
cache = PriceCache()
cache.update("AAPL", 190.00)   # previous=190.00, direction=flat, session_open=190.00
cache.update("AAPL", 190.37)   # previous=190.00, direction=up,   session_change_percent=0.1947
cache.update("AAPL", 190.12)   # previous=190.37, direction=down, session_open still 190.00
cache.remove("AAPL")           # gone; version bumped
cache.update("AAPL", 191.00)   # fresh entry: session_open=191.00
```

Design notes:
- **Version counter, not timestamps**, drives SSE change detection: an int comparison per SSE loop iteration, no diffing.
- **`threading.Lock`, not `asyncio.Lock`**: works from both the event loop and worker threads; critical sections are a dict lookup and an insert, so contention is irrelevant.
- **Rounding happens here**, once, so both sources produce identical 2-dp prices. Sources keep full precision internally.
- **`remove()` bumps the version** (new). Without it, removing a ticker would not trigger an SSE event and the client would keep showing it until the next price tick.
- **`session_open` resets on remove → re-add.** Simple and acceptable; the alternative (a separate never-cleared dict) leaks entries for every ticker ever touched.

---

## 5. `MarketDataSource` — interface.py

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data producers.

    A source pushes prices into the shared PriceCache on its own schedule.
    Nobody calls a source to *get* a price — everybody reads the cache.

        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL"])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices for `tickers` and spawn the background task.

        Call exactly once. Should populate the cache before returning where the
        source can (simulator: always; Massive: best-effort first poll).
        Must not raise on data-provider errors — log and keep going.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task. Idempotent. No cache writes afterwards."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a ticker. Idempotent.

        Simulator: price is in the cache when this returns.
        Massive: price appears on the next successful poll.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the cache. Idempotent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""
```

Behaviour both implementations must honour (enforced by the contract tests in §13.6):

| Behaviour | Simulator | Massive |
|---|---|---|
| Cache populated when `start()` returns | ✅ seed prices | ✅ if the first poll succeeds; empty otherwise |
| `add_ticker()` → price available | immediately | next poll (≤ `poll_interval`; immediately in EOD mode if the bar is already fetched) |
| `remove_ticker()` clears the cache | ✅ | ✅ (and late poll results for it are discarded) |
| Error inside the loop | logged, loop continues | logged (deduplicated), loop continues, last prices kept |
| Ticker normalisation | `.upper().strip()` | `.upper().strip()` |
| `stop()` twice | safe | safe |
| Thread of cache writes | event loop | event loop (only the HTTP call runs in `asyncio.to_thread`) |

Callers must treat "no price yet" as a normal state — hence `400 "No price available for XYZ yet"` for trades (PLAN §9).

---

## 6. Seed data — seed_prices.py

Plain constants so they are easy to tune and to import in tests. Unchanged from the current code.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# sigma = annualised volatility, mu = annualised drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},
    "NVDA": {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},
    "V": {"sigma": 0.17, "mu": 0.04},
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}  # tickers added at runtime
UNKNOWN_PRICE_RANGE: tuple[float, float] = (50.0, 300.0)        # start price for unseeded tickers

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3   # cross-sector and unknown tickers
TSLA_CORR = 0.3          # TSLA "does its own thing" despite being tech
```

To make a new ticker realistic (e.g. `PYPL`), add it to `SEED_PRICES`, `TICKER_PARAMS` and a group — no code change.

---

## 7. GBM simulator — simulator.py

Two classes: a **pure, synchronous engine** (`GBMSimulator`, no asyncio, no cache — trivially unit-testable) and an **async adapter** (`SimulatorDataSource`) that implements the interface.

### 7.1 Maths recap (details in `MARKET_SIMULATOR.md`)

```
S(t+Δt) = S(t) · exp((μ − σ²/2)·Δt + σ·√Δt·Z)        Z ~ N(0,1), correlated via Cholesky
Δt      = 0.5 s / (252 · 6.5 · 3600 s) ≈ 8.48e-8       (real-time speed)
shock   : with p = 0.001 per ticker per tick, S *= 1 ± U(0.02, 0.05)
```

Correlation: build an n×n matrix from the pairwise rules, factor once with `np.linalg.cholesky` whenever the ticker set changes, then each tick `z = L @ standard_normal(n)`.

### 7.2 Engine

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
    UNKNOWN_PRICE_RANGE,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated geometric Brownian motion over a dynamic set of tickers."""

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600      # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR      # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._tickers: list[str] = []                    # order == Cholesky row order
        self._prices: dict[str, float] = {}              # full precision, never rounded
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for t in tickers:
            self._add_ticker_internal(t)
        self._rebuild_cholesky()                         # once, not per ticker

    # --- public ---------------------------------------------------------

    def step(self) -> dict[str, float]:
        """Advance every ticker by Δt. Returns {ticker: price rounded to 2 dp}. Hot path."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        out: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]
            self._prices[ticker] *= math.exp(
                (mu - 0.5 * sigma * sigma) * self._dt + sigma * self._sqrt_dt * z[i]
            )
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice((-1, 1))
                self._prices[ticker] *= 1 + shock
                logger.debug("Shock event on %s: %+.1f%%", ticker, shock * 100)
            out[ticker] = round(self._prices[ticker], 2)
        return out

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

    # --- internals ------------------------------------------------------

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker) or random.uniform(*UNKNOWN_PRICE_RANGE)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho
        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Can't happen with the current constants (block structure is PD), but a
            # future tweak to the groups must never crash the price loop.
            logger.warning("Correlation matrix not positive definite; using independent draws")
            self._cholesky = None

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

### 7.3 Async source

```python
class SimulatorDataSource(MarketDataSource):
    """Runs GBMSimulator.step() every `update_interval` seconds and writes to the cache."""

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
        normalized = list(dict.fromkeys(t.upper().strip() for t in tickers))  # dedupe, keep order
        self._sim = GBMSimulator(normalized, event_probability=self._event_prob)
        # Seed the cache so SSE / trades have prices before the first tick.
        # The seed is the first write → it becomes session_open (PLAN §6).
        for t in normalized:
            self._cache.update(t, self._sim.get_price(t))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(normalized))

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
        ticker = ticker.upper().strip()
        if self._sim is None or ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        # Write immediately so the ticker is tradable right away (PLAN §9).
        self._cache.update(ticker, self._sim.get_price(ticker))
        logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if self._sim is not None:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim is not None:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker, price)
            except Exception:
                logger.exception("Simulator step failed")   # never let the loop die
            await asyncio.sleep(self._interval)
```

Concurrency: `_run_loop`, `add_ticker` and `remove_ticker` all run on the same event loop, and `step()` has no `await`, so they never interleave mid-step. No lock is needed inside the engine.

**Example — one tick's worth of cache writes:**

```python
cache = PriceCache()
src = SimulatorDataSource(cache)
await src.start(["AAPL", "TSLA"])
cache.get("AAPL").to_dict()
# {'ticker': 'AAPL', 'price': 190.0, 'previous_price': 190.0, 'direction': 'flat',
#  'session_open': 190.0, 'session_change_percent': 0.0, ...}
await asyncio.sleep(0.6)
cache.get("AAPL").to_dict()
# {'ticker': 'AAPL', 'price': 190.01, 'previous_price': 190.0, 'direction': 'up',
#  'session_open': 190.0, 'session_change_percent': 0.0053, ...}
await src.add_ticker("pypl")          # normalised to PYPL, priced in [50, 300] immediately
assert cache.get_price("PYPL") is not None
await src.stop()
```

---

## 8. Massive client — massive_client.py

Polls the Massive (Polygon.io) REST API with **one request per cycle for all tracked tickers** and writes the results into the cache. Two modes:

| Mode | Endpoint | Plans | Default interval |
|---|---|---|---|
| `snapshot` (initial) | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=…` (`get_snapshot_all`) | Starter and up (15-min delayed or real-time) | 15 s |
| `eod` (fallback) | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` (`get_grouped_daily_aggs`) | all, including free Basic | 300 s |

The source starts in `snapshot` mode. The first `BadResponse` whose body contains `NOT_AUTHORIZED` (free plan, no snapshot entitlement) permanently switches it to `eod` for the rest of the process.

### 8.1 Key facts from `MASSIVE_API.md` that the code depends on

- `RESTClient` is **synchronous** (urllib3) → every call goes through `asyncio.to_thread`.
- The SDK already retries 429/5xx three times with backoff → **no retry loop of our own**.
- Non-200 → `massive.exceptions.BadResponse(<response body text>)`. 403 body: `{"status":"NOT_AUTHORIZED", ...}`. 401 body: `{"status":"ERROR", "error":"Unknown API Key", ...}`.
- `TickerSnapshot` fields: `ticker, day, prev_day, min, last_trade, last_quote, todays_change, todays_change_percent, updated`. `LastTrade` has **`sip_timestamp` (ns)** — there is **no `.timestamp`** (the bug in the current code).
- Snapshot `updated` / `last_trade.sip_timestamp` are **nanoseconds**; `min.timestamp` and all aggregate `timestamp`s are **milliseconds**.
- Unknown tickers are silently omitted from snapshot responses.
- After the daily reset (~3:30 AM ET) `day.close` can be `0` → price fallback chain `last_trade.price → min.close → day.close → prev_day.close`.
- Grouped daily returns an empty list for weekends, holidays and dates without a finished bar → walk back day by day.

### 8.2 Implementation

```python
"""Massive (Polygon.io) REST poller for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

SNAPSHOT_MODE = "snapshot"
EOD_MODE = "eod"
EOD_MAX_LOOKBACK_DAYS = 5   # covers a long weekend + holiday


class Quote(NamedTuple):
    ticker: str
    price: float
    timestamp: float  # Unix seconds


def _first_positive(*values: float | None) -> float | None:
    for v in values:
        if v is not None and v > 0:
            return float(v)
    return None


def quote_from_snapshot(snap: Any) -> Quote | None:
    """Extract (ticker, price, ts) from a massive TickerSnapshot. None if unusable."""
    if not snap.ticker:
        return None
    lt = snap.last_trade
    price = _first_positive(
        lt.price if lt else None,
        snap.min.close if snap.min else None,
        snap.day.close if snap.day else None,
        snap.prev_day.close if snap.prev_day else None,
    )
    if price is None:
        return None
    ns = (lt.sip_timestamp if lt else None) or snap.updated
    ts = ns / 1e9 if ns else time.time()
    return Quote(snap.ticker, price, ts)


def _is_not_authorized(err: BadResponse) -> bool:
    return "NOT_AUTHORIZED" in str(err)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive REST API."""

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        eod_poll_interval: float = 300.0,
        client: RESTClient | None = None,   # injectable for tests
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._eod_interval = eod_poll_interval
        self._client = client
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._mode = SNAPSHOT_MODE
        # EOD mode keeps the whole market's latest closes (~10k small entries) so a
        # ticker added later can be priced without spending another request.
        self._eod_quotes: dict[str, Quote] = {}
        self._eod_fetched_at = 0.0
        self._last_error: str | None = None

    # --- interface ------------------------------------------------------

    async def start(self, tickers: list[str]) -> None:
        if self._client is None:
            self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(t.upper().strip() for t in tickers))
        await self._poll_once()   # best effort: cache has data on success, empty otherwise
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, mode=%s", len(self._tickers), self._mode)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        # In EOD mode we may already hold its close — publish it right away.
        if (q := self._eod_quotes.get(ticker)) is not None:
            self._cache.update(q.ticker, q.price, q.timestamp)
        logger.info("Massive: added %s (mode=%s)", ticker, self._mode)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- polling --------------------------------------------------------

    @property
    def current_interval(self) -> float:
        return self._interval if self._mode == SNAPSHOT_MODE else self._eod_interval

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.current_interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One cycle: fetch, then write only still-tracked tickers. Never raises."""
        if not self._tickers or self._client is None:
            return
        tickers = list(self._tickers)          # snapshot: the worker thread must not see mutations
        try:
            quotes = await self._fetch(tickers)
        except Exception as e:                 # network, 401, 429-after-retries, parse errors
            self._log_failure(e)
            return

        tracked = set(self._tickers)           # a remove_ticker() may have run during the await
        written = 0
        for q in quotes:
            if q.ticker in tracked:
                self._cache.update(q.ticker, q.price, q.timestamp)
                written += 1
        if self._last_error is not None:
            logger.info("Massive poll recovered")
            self._last_error = None
        logger.debug("Massive poll (%s): %d/%d tickers", self._mode, written, len(tickers))

    async def _fetch(self, tickers: list[str]) -> list[Quote]:
        if self._mode == SNAPSHOT_MODE:
            try:
                return await asyncio.to_thread(self._fetch_snapshot_quotes, tickers)
            except BadResponse as e:
                if not _is_not_authorized(e):
                    raise
                logger.warning(
                    "Massive plan has no snapshot access; switching to end-of-day prices "
                    "(polling every %.0fs)", self._eod_interval,
                )
                self._mode = EOD_MODE
        return await self._fetch_eod(tickers)

    def _fetch_snapshot_quotes(self, tickers: list[str]) -> list[Quote]:
        """Runs in a worker thread."""
        snaps = self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=tickers)
        quotes: list[Quote] = []
        for snap in snaps:
            try:
                q = quote_from_snapshot(snap)
            except (AttributeError, TypeError, ValueError) as e:
                logger.warning("Skipping malformed snapshot for %s: %s",
                               getattr(snap, "ticker", "?"), e)
                continue
            if q is not None:
                quotes.append(q)
        return quotes

    async def _fetch_eod(self, tickers: list[str]) -> list[Quote]:
        # Refresh the whole-market close table at most once per EOD interval.
        if not self._eod_quotes or time.monotonic() - self._eod_fetched_at >= self._eod_interval:
            self._eod_quotes = await asyncio.to_thread(self._fetch_latest_grouped_daily)
            self._eod_fetched_at = time.monotonic()
        return [self._eod_quotes[t] for t in tickers if t in self._eod_quotes]

    def _fetch_latest_grouped_daily(self) -> dict[str, Quote]:
        """Runs in a worker thread. Walks back from yesterday (UTC) to the last trading day."""
        day = datetime.now(UTC).date() - timedelta(days=1)
        for _ in range(EOD_MAX_LOOKBACK_DAYS):
            bars = self._client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
            if bars:
                return {
                    b.ticker: Quote(b.ticker, float(b.close),
                                    b.timestamp / 1e3 if b.timestamp else time.time())
                    for b in bars
                    if b.ticker and b.close
                }
            day -= timedelta(days=1)
        logger.warning("Massive: no grouped-daily data in the last %d days", EOD_MAX_LOOKBACK_DAYS)
        return {}

    def _log_failure(self, e: Exception) -> None:
        msg = f"{type(e).__name__}: {e}"
        if msg != self._last_error:            # don't spam the same error every 15 s
            logger.error("Massive poll failed: %s", msg)
            self._last_error = msg
        else:
            logger.debug("Massive poll failed again: %s", msg)
```

Notes:
- **Rate budget.** Snapshot mode makes 1 request per 15 s = 4/min, inside the free 5/min (and paid tiers can lower the interval to 2–5 s). EOD mode costs up to 5 requests on a refresh (one per lookback day), once every 5 minutes, so it also stays within 5/min. Note the switch cycle itself: the failed snapshot call plus up to 5 grouped-daily calls can hit 6 requests in a few seconds once at startup; the SDK's internal 429 retry absorbs that, and the next cycle is 5 minutes away.
- **Why `date.today()` is not used:** the container runs in UTC and has no tz database in the slim image; UTC "yesterday" is a safe starting point because the walk-back handles weekends/holidays and today's bar is not available on free plans anyway.
- **The `tracked` filter** closes a race: if `remove_ticker("X")` runs while a poll is awaiting the HTTP thread, the result for X is dropped instead of resurrecting it in the cache.
- **Unchanged-price polls still call `cache.update()`**, so the version bumps and SSE emits a batch with `direction: "flat"`. That is honest for delayed/EOD data. The frontend's flash only fires on up/down.
- **`client=` injection** lets tests pass a fake client without patching module globals.

### 8.3 Example: raw response → cache

```python
from massive.rest.models import TickerSnapshot

snap = TickerSnapshot.from_dict({
    "ticker": "AAPL",
    "updated": 1731753600123456789,
    "day": {"c": 0},                                   # just after the daily reset
    "prevDay": {"c": 189.00},
    "min": {"c": 190.40, "t": 1731753540000},
    "lastTrade": {"p": 190.52, "t": 1731753600123456789},
})
quote_from_snapshot(snap)
# Quote(ticker='AAPL', price=190.52, timestamp=1731753600.1234567)

snap = TickerSnapshot.from_dict({"ticker": "AAPL", "day": {"c": 0}, "prevDay": {"c": 189.0}})
quote_from_snapshot(snap)
# Quote(ticker='AAPL', price=189.0, timestamp=<now>)   ← prev_day fallback, no timestamps
```

### 8.4 Quick manual check against the real API

```bash
cd backend
MASSIVE_API_KEY=... uv run python -c "
import asyncio, logging
from app.market import PriceCache
from app.market.massive_client import MassiveDataSource
logging.basicConfig(level=logging.INFO)
async def main():
    c = PriceCache(); s = MassiveDataSource(api_key=__import__('os').environ['MASSIVE_API_KEY'], price_cache=c)
    await s.start(['AAPL', 'MSFT', 'NVDA'])
    print(s._mode, {t: u.price for t, u in c.get_all().items()})
    await s.stop()
asyncio.run(main())
"
```

---

## 9. Factory — factory.py

```python
"""Selects the market data source from the environment."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-blank → Massive; otherwise → simulator.

    Returns an UNSTARTED source; the caller awaits source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = _float_env("MASSIVE_POLL_INTERVAL", 15.0)
        logger.info("Market data source: Massive API (poll every %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(2.0, float(raw))   # never hammer the API
    except ValueError:
        logger.warning("Ignoring invalid %s=%r", name, raw)
        return default
```

`MASSIVE_POLL_INTERVAL` is an **optional addition** not yet listed in PLAN §5 (default 15 s keeps the free tier safe; paid users can set 2–5). If the team prefers to keep §5 minimal, drop `_float_env` and hardcode 15 s — nothing else depends on it.

---

## 10. SSE stream — stream.py

`GET /api/stream/prices`. Sends `retry: 1000`, then checks the cache version every 500 ms and emits **one batch with every tracked ticker** whenever it changed (PLAN §6). A comment line is sent every 15 s of silence so proxies don't close an idle stream (relevant in Massive EOD mode, where prices change every 5 minutes).

```python
"""SSE streaming endpoint for live price updates."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(
    price_cache: PriceCache,
    interval: float = 0.5,
    heartbeat: float = 15.0,
) -> APIRouter:
    """Build a fresh router per call (a module-level router would register /prices twice)."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request, interval, heartbeat),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",   # disable proxy buffering (nginx, some PaaS)
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    heartbeat: float = 15.0,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    last_version = -1
    last_sent = time.monotonic()
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                batch = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(batch)}\n\n"   # may be {} if nothing is tracked
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": keep-alive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("SSE client disconnected: %s", client)
```

**Wire format example** (what `curl -N localhost:8000/api/stream/prices` prints):

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.01, "previous_price": 190.0, "timestamp": 1731753600.62, "change": 0.01, "change_percent": 0.0053, "direction": "up", "session_open": 190.0, "session_change_percent": 0.0053}, "GOOGL": {...}, ...}

data: {"AAPL": {...}, "GOOGL": {...}, ...}

: keep-alive

```

Frontend side (for reference — the frontend agent owns this):

```ts
const es = new EventSource("/api/stream/prices");
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CONNECTING ? "reconnecting" : "disconnected");
es.onmessage = (e) => {
  const batch: Record<string, PriceUpdate> = JSON.parse(e.data);
  setLatest(batch);            // replace the latest-price map (removed tickers disappear)
  appendHistory(batch, 500);   // per-ticker ring buffers for sparklines / main chart
};
```

Design notes:
- **Poll-and-push** (check the version every 500 ms) instead of per-client queues: the simulator ticks at the same rate anyway, every client gets a consistent snapshot, and there's no fan-out bookkeeping. A slow client only delays its own generator.
- Emitting `{}` when the last ticker is removed is deliberate: since each event *replaces* the client's map, the UI clears correctly.
- `request.is_disconnected()` plus Starlette cancelling the generator on disconnect means no leaked loops.

---

## 11. App integration

### 11.1 `app/main.py` — lifespan

The cache is created in `create_app()` (the stream router needs it at build time); the source is created and started in the lifespan, after the database is initialised (PLAN §7: watchlist and positions are available before the market source starts).

```python
"""FastAPI application factory."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.db import init_db                          # PLAN §7 (DB agent)
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.services.market_tracking import tracked_tickers
from app.services.snapshots import snapshot_loop    # portfolio snapshots every 30 s (PLAN §7)

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app() -> FastAPI:
    cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = init_db()                                         # create tables + seed
        source = create_market_data_source(cache)
        await source.start(tracked_tickers(db))                # watchlist ∪ positions
        app.state.db = db
        app.state.market_source = source
        snapshots = asyncio.create_task(snapshot_loop(db, cache), name="portfolio-snapshots")
        try:
            yield
        finally:
            snapshots.cancel()
            await source.stop()
            db.close()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.state.price_cache = cache

    app.include_router(create_stream_router(cache))
    # app.include_router(portfolio_router); app.include_router(watchlist_router); ...

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok"}

    if STATIC_DIR.is_dir():                                    # absent in backend-only dev
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
```

### 11.2 `app/deps.py` — dependencies

```python
from fastapi import Request

from app.market import MarketDataSource, PriceCache


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

### 11.3 `app/services/market_tracking.py` — watchlist ∪ positions

The single place that owns PLAN §6's tracked-ticker rule. Every route and the chat executor go through it; nothing else calls `add_ticker`/`remove_ticker`.

```python
"""Keeps the market data source tracking exactly watchlist ∪ open positions."""

from __future__ import annotations

import sqlite3

from app.market import MarketDataSource

DEFAULT_USER = "default"


def tracked_tickers(conn: sqlite3.Connection, user_id: str = DEFAULT_USER) -> list[str]:
    rows = conn.execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? "
        "UNION SELECT ticker FROM positions WHERE user_id = ? ORDER BY ticker",
        (user_id, user_id),
    ).fetchall()
    return [r[0] for r in rows]


def is_tracked_in_db(conn: sqlite3.Connection, ticker: str, user_id: str = DEFAULT_USER) -> bool:
    row = conn.execute(
        "SELECT 1 FROM watchlist WHERE user_id = ? AND ticker = ? "
        "UNION SELECT 1 FROM positions WHERE user_id = ? AND ticker = ? LIMIT 1",
        (user_id, ticker, user_id, ticker),
    ).fetchone()
    return row is not None


async def ensure_tracked(source: MarketDataSource, ticker: str) -> None:
    """After inserting a watchlist row (manual, chat, or implicit-on-trade)."""
    await source.add_ticker(ticker)          # idempotent


async def maybe_untrack(
    conn: sqlite3.Connection, source: MarketDataSource, ticker: str, user_id: str = DEFAULT_USER
) -> None:
    """After deleting a watchlist row, or after a sell that deleted the position row."""
    if not is_tracked_in_db(conn, ticker, user_id):
        await source.remove_ticker(ticker)   # also clears it from the cache
```

| Event | Call sequence |
|---|---|
| Startup | `source.start(tracked_tickers(db))` |
| `POST /api/watchlist` / chat `add` | insert row → commit → `ensure_tracked` |
| Trade in a ticker not on the watchlist | insert watchlist row → `ensure_tracked` → validate & execute trade (PLAN §9) |
| `DELETE /api/watchlist/{t}` / chat `remove` | delete row → commit → `maybe_untrack` |
| Sell that deletes the position row | commit trade txn → `maybe_untrack` |

Always call `maybe_untrack` **after** the commit, so the check sees the new state.

---

## 12. Consumer examples

```python
from fastapi import APIRouter, Depends, HTTPException
from app.deps import get_price_cache
from app.market import PriceCache

# --- Trade fill price (PLAN §9) ---
def fill_price(cache: PriceCache, ticker: str) -> float:
    price = cache.get_price(ticker)
    if price is None:
        raise HTTPException(400, f"No price available for {ticker} yet")
    return price

# --- Portfolio valuation (PLAN §8: fall back to avg_cost when uncached) ---
def position_row(cache: PriceCache, ticker: str, qty: float, avg_cost: float) -> dict:
    current = cache.get_price(ticker) or avg_cost
    value = qty * current
    pnl = qty * (current - avg_cost)
    return {
        "ticker": ticker, "quantity": qty, "avg_cost": avg_cost, "current_price": current,
        "market_value": round(value, 2), "unrealized_pnl": round(pnl, 2),
        "unrealized_pnl_percent": round((current - avg_cost) / avg_cost * 100, 4) if avg_cost else 0.0,
    }

# --- Watchlist row (PLAN §8: price fields null if not cached yet) ---
def watchlist_row(cache: PriceCache, ticker: str, added_at: str) -> dict:
    u = cache.get(ticker)
    return {
        "ticker": ticker,
        "price": u.price if u else None,
        "previous_price": u.previous_price if u else None,
        "session_change_percent": u.session_change_percent if u else None,
        "added_at": added_at,
    }

# --- LLM portfolio context (PLAN §10) ---
def watchlist_context(cache: PriceCache, tickers: list[str]) -> str:
    lines = []
    for t in tickers:
        u = cache.get(t)
        lines.append(f"{t}: ${u.price:.2f} ({u.session_change_percent:+.2f}% today)" if u
                     else f"{t}: no price yet")
    return "\n".join(lines)
```

---

## 13. Testing

All tests live in `backend/tests/market/` (pytest, `asyncio_mode = "auto"`). **No test makes a network call.** Massive tests build **real SDK model objects** with `TickerSnapshot.from_dict(...)` / `GroupedDailyAgg.from_dict(...)` instead of `MagicMock` — a permissive mock is exactly how the `last_trade.timestamp` bug slipped through.

### 13.1 Models

```python
from app.market.models import PriceUpdate

def test_to_dict_matches_plan_example():
    u = PriceUpdate("AAPL", 190.52, 190.41, timestamp=1731753600.12, session_open=190.0)
    d = u.to_dict()
    assert d["change"] == 0.11 and d["change_percent"] == 0.0578 and d["direction"] == "up"
    assert d["session_open"] == 190.0 and d["session_change_percent"] == 0.2737
    assert list(d) == ["ticker", "price", "previous_price", "timestamp", "change",
                       "change_percent", "direction", "session_open", "session_change_percent"]

def test_session_open_defaults_to_price():
    u = PriceUpdate("X", 10.0, 9.0)
    assert u.session_open == 10.0 and u.session_change_percent == 0.0
```

### 13.2 Cache

```python
from app.market.cache import PriceCache

def test_first_write_sets_session_open_and_flat():
    c = PriceCache()
    u = c.update("AAPL", 190.0)
    assert u.direction == "flat" and u.session_open == 190.0

def test_session_open_carries_forward():
    c = PriceCache()
    c.update("AAPL", 190.0); c.update("AAPL", 191.0)
    u = c.update("AAPL", 189.5)
    assert u.session_open == 190.0 and u.previous_price == 191.0 and u.direction == "down"

def test_remove_resets_session_and_bumps_version():
    c = PriceCache()
    c.update("AAPL", 190.0)
    v = c.version
    c.remove("AAPL")
    assert c.version == v + 1 and "AAPL" not in c
    assert c.update("AAPL", 200.0).session_open == 200.0

def test_remove_unknown_does_not_bump_version():
    c = PriceCache(); v = c.version
    c.remove("NOPE")
    assert c.version == v

def test_zero_timestamp_is_kept():
    assert PriceCache().update("A", 1.0, timestamp=0.0).timestamp == 0.0
```

### 13.3 Simulator engine

```python
import numpy as np, random
from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES

def test_prices_stay_positive_and_finite():
    np.random.seed(1); random.seed(1)
    sim = GBMSimulator(list(SEED_PRICES))
    for _ in range(10_000):
        prices = sim.step()
    assert all(p > 0 and np.isfinite(p) for p in prices.values())

def test_full_default_set_cholesky_ok():
    sim = GBMSimulator(list(SEED_PRICES))
    assert sim._cholesky.shape == (10, 10)

def test_correlation_tech_vs_tsla():
    np.random.seed(7); random.seed(7)
    sim = GBMSimulator(["AAPL", "MSFT", "TSLA"], dt=1 / 252, event_probability=0)
    prev = {t: sim.get_price(t) for t in sim.get_tickers()}
    rets = {t: [] for t in prev}
    for _ in range(20_000):
        cur = sim.step()
        for t in cur:
            rets[t].append(np.log(sim.get_price(t) / prev[t])); prev[t] = sim.get_price(t)
    c = np.corrcoef([rets["AAPL"], rets["MSFT"], rets["TSLA"]])
    assert abs(c[0, 1] - 0.6) < 0.05 and abs(c[0, 2] - 0.3) < 0.05

def test_unknown_ticker_price_range():
    sim = GBMSimulator([]); sim.add_ticker("ZZZZ")
    assert 50 <= sim.get_price("ZZZZ") <= 300
```

### 13.4 Massive (SDK models, fake client)

```python
import pytest
from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import EOD_MODE, MassiveDataSource, quote_from_snapshot


def snap(ticker, last=None, ns=None, minute=None, day=None, prev=None):
    d = {"ticker": ticker}
    if last is not None: d["lastTrade"] = {"p": last, "t": ns}
    if minute is not None: d["min"] = {"c": minute}
    if day is not None: d["day"] = {"c": day}
    if prev is not None: d["prevDay"] = {"c": prev}
    return TickerSnapshot.from_dict(d)


class FakeClient:
    def __init__(self, snapshots=None, snapshot_error=None, grouped=None):
        self.snapshots, self.snapshot_error = snapshots or [], snapshot_error
        self.grouped = grouped or {}          # {"YYYY-MM-DD": [GroupedDailyAgg, ...]}
        self.calls = []

    def get_snapshot_all(self, market_type, tickers=None, **kw):
        self.calls.append(("snapshot", tuple(tickers)))
        if self.snapshot_error:
            raise self.snapshot_error
        return [s for s in self.snapshots if s.ticker in tickers]

    def get_grouped_daily_aggs(self, date, adjusted=None, **kw):
        self.calls.append(("grouped", date))
        return self.grouped.get(date, [])


def test_quote_uses_sip_timestamp_ns():
    q = quote_from_snapshot(snap("AAPL", last=190.52, ns=1_731_753_600_000_000_000))
    assert q.price == 190.52 and q.timestamp == 1_731_753_600.0

@pytest.mark.parametrize("kwargs,expected", [
    ({"minute": 190.4, "day": 0, "prev": 189.0}, 190.4),
    ({"day": 190.1, "prev": 189.0}, 190.1),
    ({"day": 0, "prev": 189.0}, 189.0),
])
def test_price_fallback_chain(kwargs, expected):
    assert quote_from_snapshot(snap("AAPL", **kwargs)).price == expected

def test_no_price_returns_none():
    assert quote_from_snapshot(snap("AAPL", day=0)) is None

async def test_start_populates_cache():
    cache = PriceCache()
    client = FakeClient(snapshots=[snap("AAPL", last=190.5, ns=1), snap("MSFT", last=420.0, ns=1)])
    src = MassiveDataSource("k", cache, poll_interval=60, client=client)
    await src.start(["aapl", "MSFT"])
    assert cache.get_price("AAPL") == 190.5 and cache.get_price("MSFT") == 420.0
    await src.stop(); await src.stop()

async def test_not_authorized_switches_to_eod(monkeypatch):
    from app.market import massive_client as mc
    cache = PriceCache()
    bars = [GroupedDailyAgg.from_dict({"T": "AAPL", "c": 189.0, "t": 1731704400000}),
            GroupedDailyAgg.from_dict({"T": "PYPL", "c": 70.0, "t": 1731704400000})]
    yesterday = (mc.datetime.now(mc.UTC).date() - mc.timedelta(days=1)).isoformat()
    client = FakeClient(
        snapshot_error=BadResponse('{"status":"NOT_AUTHORIZED","message":"upgrade"}'),
        grouped={yesterday: bars},
    )
    src = MassiveDataSource("k", cache, poll_interval=60, client=client)
    await src.start(["AAPL"])
    assert src._mode == EOD_MODE and cache.get_price("AAPL") == 189.0
    await src.add_ticker("PYPL")                     # already in the EOD table → immediate
    assert cache.get_price("PYPL") == 70.0
    await src.stop()

async def test_other_errors_keep_snapshot_mode_and_cache():
    cache = PriceCache(); cache.update("AAPL", 190.0)
    client = FakeClient(snapshot_error=BadResponse('{"status":"ERROR","error":"Unknown API Key"}'))
    src = MassiveDataSource("k", cache, client=client)
    src._tickers = ["AAPL"]
    await src._poll_once()
    assert src._mode == "snapshot" and cache.get_price("AAPL") == 190.0

async def test_removed_during_poll_is_not_resurrected(monkeypatch):
    cache = PriceCache()
    src = MassiveDataSource("k", cache, client=FakeClient(snapshots=[snap("AAPL", last=1.0, ns=1)]))
    src._tickers = ["AAPL"]
    async def fetch(tickers):
        await src.remove_ticker("AAPL")              # simulate removal mid-flight
        return [__import__("app.market.massive_client", fromlist=["Quote"]).Quote("AAPL", 1.0, 1.0)]
    monkeypatch.setattr(src, "_fetch", fetch)
    await src._poll_once()
    assert "AAPL" not in cache
```

### 13.5 SSE generator

```python
import json
from app.market.cache import PriceCache
from app.market.stream import _generate_events

class FakeRequest:
    client = None
    def __init__(self, polls): self._polls = polls
    async def is_disconnected(self):
        self._polls -= 1
        return self._polls < 0

async def test_stream_emits_retry_then_batch_then_stops():
    cache = PriceCache(); cache.update("AAPL", 190.0); cache.update("MSFT", 420.0)
    events = [e async for e in _generate_events(cache, FakeRequest(polls=2), interval=0.01)]
    assert events[0] == "retry: 1000\n\n"
    batch = json.loads(events[1].removeprefix("data: "))
    assert set(batch) == {"AAPL", "MSFT"} and "session_change_percent" in batch["AAPL"]
    assert len(events) == 2                           # unchanged version → no second batch

async def test_stream_heartbeat_when_idle():
    cache = PriceCache(); cache.update("AAPL", 1.0)
    events = [e async for e in _generate_events(cache, FakeRequest(polls=3), interval=0.01, heartbeat=0)]
    assert ": keep-alive\n\n" in events

def test_router_factory_is_idempotent():
    from app.market.stream import create_stream_router
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert len(r1.routes) == len(r2.routes) == 1
```

An end-to-end check with the real app: `httpx.AsyncClient(transport=ASGITransport(app))` streaming `/api/stream/prices`, read the first `data:` line, then close. Keep it to one test; the generator tests above cover the logic.

### 13.6 Contract tests (both sources)

```python
import pytest
from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource

def make_massive(cache):
    snaps = [snap(t, last=100.0, ns=1) for t in ("AAPL", "MSFT", "TSLA")]
    return MassiveDataSource("k", cache, poll_interval=60, client=FakeClient(snapshots=snaps))

@pytest.fixture(params=["simulator", "massive"])
def source_and_cache(request):
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=60) if request.param == "simulator" \
        else make_massive(cache)
    return src, cache

async def test_contract(source_and_cache):
    src, cache = source_and_cache
    await src.start(["AAPL", "MSFT"])
    assert set(src.get_tickers()) == {"AAPL", "MSFT"} and "AAPL" in cache
    await src.add_ticker("tsla"); await src.add_ticker("TSLA")
    assert src.get_tickers().count("TSLA") == 1
    await src.remove_ticker("MSFT"); await src.remove_ticker("MSFT")
    assert "MSFT" not in src.get_tickers() and "MSFT" not in cache
    await src.stop(); await src.stop()
```

### 13.7 Tracking service

With an in-memory SQLite DB using the PLAN §7 schema and a fake source recording calls: removing a watched ticker that has an open position must **not** call `remove_ticker`; removing one without a position must. Selling the full position of a non-watchlist ticker must call `remove_ticker`.

---

## 14. Edge cases and failure modes

| Situation | Behaviour |
|---|---|
| Empty tracked set at startup | Both sources start fine; simulator `step()` returns `{}`; Massive skips polls; SSE sends `{}` once (version 0 ≠ −1) |
| Ticker added, not yet priced (Massive) | `cache.get` → `None`; watchlist shows `null` prices; trade → `400 "No price available for X yet"` |
| Unknown/invalid symbol in Massive mode | Snapshot silently omits it → stays unpriced forever; user can remove it. (Validation of the symbol format is the route's job: `^[A-Z][A-Z.]{0,5}$`) |
| Bad API key | One ERROR log line, then DEBUG repeats; cache stays empty; app keeps running. Fix: correct the key or unset it to use the simulator |
| Free plan | First poll gets `NOT_AUTHORIZED` → EOD mode; prices = last trading day's close; `direction` always `flat` |
| Rate limited (429) | SDK retries 3×, then `BadResponse` → cycle skipped, last prices kept |
| Network outage | Same as above; logs "Massive poll recovered" when it comes back |
| Weekend / holiday (EOD) | Walk back up to 5 days |
| Simulator exception in `step()` | Logged with traceback; loop continues next interval |
| `remove_ticker` during an in-flight Massive poll | Result discarded by the `tracked` filter |
| Shutdown while a Massive HTTP call is running | Task cancelled; the worker thread finishes in the background, its result is never written |
| Client disconnects from SSE | Generator exits via `is_disconnected()` / cancellation; logged |
| Restart | `session_open` resets to the first price after start (by design, PLAN §6) |
| Long run of the simulator | Prices are full-precision internally, rounded only on output; no rounding drift |

---

## 15. Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env / `.env` | empty | empty or whitespace → simulator |
| `MASSIVE_POLL_INTERVAL` | env (optional, §9) | 15 s | clamped to ≥ 2 s; keep ≥ 12 s on the free tier |
| EOD poll interval | `MassiveDataSource(eod_poll_interval=)` | 300 s | used after the free-tier fallback |
| Simulator tick | `SimulatorDataSource(update_interval=)` | 0.5 s | |
| Shock probability | `SimulatorDataSource(event_probability=)` | 0.001 | ≈ one event / 50 s across 10 tickers |
| Simulator Δt | `GBMSimulator(dt=)` | 0.5 s of a trading year | multiply for a livelier demo |
| SSE check interval | `create_stream_router(interval=)` | 0.5 s | |
| SSE heartbeat | `create_stream_router(heartbeat=)` | 15 s | |
| SSE client retry | hard-coded `retry: 1000` | 1 s | |

Dependencies (already in `pyproject.toml`): `fastapi`, `uvicorn[standard]`, `numpy`, `massive`. Dev: `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, plus `httpx` if the ASGI SSE test in §13.5 is added.

---

## 16. Implementation checklist

Current code (`backend/app/market/`, 73 passing tests) already implements most of this design. Remaining work, in suggested order:

| # | Change | File(s) | Why |
|---|---|---|---|
| 1 | **Fix Massive parsing**: use `quote_from_snapshot` (`last_trade.sip_timestamp` in ns, fallback chain `last_trade.price → min.close → day.close → prev_day.close`) | `massive_client.py` | Today every snapshot raises `AttributeError` on `.timestamp` and is skipped → Massive mode never fills the cache (REVIEW P1) |
| 2 | Rewrite Massive tests with real SDK models and `FakeClient` (§13.4) | `tests/market/test_massive.py` | `MagicMock` hid bug #1 |
| 3 | Add `session_open` + `session_change_percent` | `models.py`, `cache.py`, tests | PLAN §6 SSE contract (REVIEW P2) |
| 4 | `remove()` bumps `version`; `version` read under lock; `timestamp is not None` instead of `or` | `cache.py` | SSE reflects removals; correctness nits |
| 5 | Free-tier EOD fallback (`NOT_AUTHORIZED` → grouped daily, 300 s, whole-market close table) | `massive_client.py` | Free keys can't call snapshots at all |
| 6 | Poll robustness: copy tickers for the thread, `tracked` filter, deduplicated error logging, `client=` injection | `massive_client.py` | Race on remove; log noise; testability |
| 7 | Build `APIRouter` inside `create_stream_router`; heartbeat; emit `{}` batches | `stream.py` | Double-registration footgun; idle proxies; clear-on-remove |
| 8 | Simulator: normalise/dedupe tickers, `add_ticker` idempotent write, Cholesky `LinAlgError` guard, `UNKNOWN_PRICE_RANGE` constant | `simulator.py`, `seed_prices.py` | Contract consistency |
| 9 | Optional `MASSIVE_POLL_INTERVAL` | `factory.py` (+ PLAN §5 / `.env.example` if adopted) | Paid tiers |
| 10 | Contract tests over both sources, SSE generator tests | `tests/market/` | §13.5–13.6 |
| 11 | `market_tracking.py`, `deps.py`, lifespan in `main.py` | `app/services/`, `app/` | Built with the DB/portfolio/watchlist routes |
| 12 | Update `MARKET_DATA_SUMMARY.md` and `backend/CLAUDE.md` to the new `PriceUpdate` shape | docs | Keep agent docs in sync |
