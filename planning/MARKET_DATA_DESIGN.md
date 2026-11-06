# Market Data Backend: Detailed Design (v2)

This is the implementation-ready design for the FinAlly market data subsystem. It covers the unified interface, the shared price cache, the GBM simulator, the Massive (Polygon.io) REST poller, SSE streaming, FastAPI wiring and tests. Every module is given in full, so an agent can implement or re-implement `backend/app/market/` from this document alone.

**Relationship to existing docs**

| Doc | Role |
|---|---|
| `planning/PLAN.md` §6 | The product requirements this design satisfies |
| `planning/MARKET_DATA_SUMMARY.md` | Status of the code already in `backend/app/market/` (v1) |
| `planning/archive/*.md` | Original v1 research and design. **Superseded by this document.** |

This v2 keeps the v1 architecture and public API, which are sound and already used by `backend/CLAUDE.md`. It fixes defects found while checking v1 against the real `massive` client and adds what the rest of the platform needs (daily change %, ticker validation, tracking watchlist ∪ positions). §15 lists every delta from the current code.

> **Critical v1 bug.** `massive_client.py` reads `snap.last_trade.timestamp`, but the `massive` package's `LastTrade` model has no `timestamp` attribute. The field is `sip_timestamp`, in **nanoseconds**. Each snapshot raises `AttributeError`, gets logged and skipped, so **real-data mode never produces a price**. The v1 tests build snapshots with `MagicMock`, which invents any attribute, so they pass anyway. v2 fixes the parsing (§8) and tests with real `TickerSnapshot.from_dict(...)` objects (§13.4).

---

## Table of Contents

1. [Requirements Recap](#1-requirements-recap)
2. [Architecture & Data Flow](#2-architecture--data-flow)
3. [File Layout](#3-file-layout)
4. [Data Model: `models.py`](#4-data-model-modelspy)
5. [Price Cache: `cache.py`](#5-price-cache-cachepy)
6. [Unified Interface: `interface.py`](#6-unified-interface-interfacepy)
7. [Simulator: `seed_prices.py` + `simulator.py`](#7-simulator)
8. [Massive API Client: `massive_client.py`](#8-massive-api-client-massive_clientpy)
9. [Factory: `factory.py`](#9-factory-factorypy)
10. [SSE Streaming: `stream.py`](#10-sse-streaming-streampy)
11. [FastAPI Integration](#11-fastapi-integration)
12. [Coordinating with Watchlist, Trades & Chat](#12-coordinating-with-watchlist-trades--chat)
13. [Testing](#13-testing)
14. [Error Handling & Configuration Reference](#14-error-handling--configuration-reference)
15. [Delta from v1 / Implementation Checklist](#15-delta-from-v1--implementation-checklist)

---

## 1. Requirements Recap

From `PLAN.md` §6 and §10:

- **Two implementations, one interface.** The simulator is the default. Massive REST polling is used when `MASSIVE_API_KEY` is non-empty.
- **Simulator:** GBM, ~500 ms ticks, correlated moves (tech moves together), occasional 2–5% "events", realistic seed prices, runs in-process.
- **Massive:** REST polling, not WebSocket. It fetches the union of watched tickers in one call. Free tier polls every 15 s, paid tiers every 2–15 s.
- **Shared price cache:** latest price, previous price and timestamp per ticker. One writer (the active source), many readers.
- **SSE:** `GET /api/stream/prices` pushes every ticker roughly every 500 ms. Each item carries ticker, price, previous price, timestamp and direction.
- **Frontend needs** (§10): live price with up/down flash, **daily change %** and sparklines accumulated from SSE. The trade, portfolio and chat code needs a synchronous "current price of X".

---

## 2. Architecture & Data Flow

```
                 ┌──────────────────────── MarketDataSource (ABC) ───────────────────────┐
                 │                                                                        │
   MASSIVE_API_KEY empty                                                 MASSIVE_API_KEY set
                 │                                                                        │
     SimulatorDataSource                                                   MassiveDataSource
     └─ GBMSimulator.step() every 0.5 s                     poll get_snapshot_all() every 15 s
                 │   (asyncio task)                          (asyncio task + to_thread for HTTP)
                 └───────────────────────────┬────────────────────────────────────────┘
                                             │ cache.update(ticker, price, ts, reference_price)
                                             ▼
                                ┌──────────────────────────┐
                                │ PriceCache (thread-safe) │  latest PriceUpdate per ticker
                                │  version counter         │  + monotonic version
                                └────────────┬─────────────┘
               ┌─────────────────────────────┼────────────────────────────┐
               ▼                             ▼                            ▼
   SSE /api/stream/prices          trade execution / portfolio      watchlist GET, chat context
   (pushes when version changes)   cache.get_price("AAPL")          cache.get_all()
```

**Push model.** Sources write to the cache on their own schedule. Consumers read the cache on theirs and never call a source for a price. The SSE layer doesn't know whether ticks arrive every 0.5 s (simulator) or every 15 s (Massive).

**Control path.** Watchlist and position changes call `source.add_ticker()` / `source.remove_ticker()` (§12).

**Concurrency model.** Everything runs on the single uvicorn event loop, except the blocking Massive HTTP call, which runs in a worker thread via `asyncio.to_thread`. `PriceCache` uses a `threading.Lock` so it is safe from both.

---

## 3. File Layout

```
backend/
  app/
    market/
      __init__.py         # Public re-exports
      models.py           # PriceUpdate, normalize_ticker()
      cache.py            # PriceCache
      interface.py        # MarketDataSource ABC
      seed_prices.py      # Seed prices, GBM params, correlation groups (constants only)
      simulator.py        # GBMSimulator (math) + SimulatorDataSource (async wrapper)
      massive_client.py   # extract_quote() + MassiveDataSource
      factory.py          # create_market_data_source()
      stream.py           # create_stream_router(), generate_price_events()
    main.py               # create_app(): wiring + lifespan (owned by the backend agent)
    deps.py               # get_price_cache / get_market_source dependencies
  tests/market/
    test_models.py  test_cache.py  test_simulator.py  test_simulator_source.py
    test_massive.py  test_factory.py  test_stream.py
```

### `__init__.py`

```python
# backend/app/market/__init__.py
"""Market data subsystem for FinAlly.

Public API (import from `app.market`, never from submodules):
    PriceUpdate, normalize_ticker, PriceCache, MarketDataSource,
    create_market_data_source, create_stream_router
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate, normalize_ticker
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "normalize_ticker",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
]
```

---

## 4. Data Model: `models.py`

`PriceUpdate` is the only type that leaves the market data layer. v2 adds:

- **`reference_price`** is the baseline for **daily change %**, which the watchlist panel needs. For Massive it is the previous session's close (`prev_day.close`), the standard convention. For the simulator it is the price when the ticker started being tracked (its "session open").
- **`normalize_ticker()`** is the one place ticker strings are cleaned and validated. v1 upper-cased in the Massive source but not in the simulator, so `"aapl"` and `"AAPL"` could become two tickers.

```python
# backend/app/market/models.py
"""Data models for market data."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Literal

Direction = Literal["up", "down", "flat"]

# 1-10 chars, starts with a letter; allows class shares like "BRK.B" and "BF-B".
_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def normalize_ticker(raw: str) -> str:
    """Upper-case and validate a ticker symbol. Raises ValueError if invalid.

    Every entry point that accepts a ticker from the outside world (REST body,
    URL path, LLM output) must pass it through this function first.
    """
    ticker = (raw or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError(f"Invalid ticker symbol: {raw!r}")
    return ticker


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of one ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds
    reference_price: float | None = None  # Prev close (Massive) / session open (simulator)

    # --- Tick-over-tick (drives the green/red flash) ---

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> Direction:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    # --- Daily (drives the "daily change %" column) ---

    @property
    def day_change(self) -> float | None:
        if not self.reference_price:
            return None
        return round(self.price - self.reference_price, 4)

    @property
    def day_change_percent(self) -> float | None:
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 4)

    def to_dict(self) -> dict:
        """Single serialization point for SSE and REST responses."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "reference_price": self.reference_price,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
        }
```

Design notes:

- `frozen=True` makes updates value objects that are safe to share across tasks and threads without copying. `slots=True` saves memory, since we create ~20 of these per second.
- Derived fields are properties, so `direction` can never disagree with the prices.
- New fields have defaults, so v1 call sites keep working (`PriceUpdate("AAPL", 1.0, 1.0)`).

Example:

```python
>>> u = PriceUpdate("AAPL", price=191.20, previous_price=191.05, timestamp=1762400000.0, reference_price=190.00)
>>> u.direction, u.change, u.day_change_percent
('up', 0.15, 0.6316)
>>> normalize_ticker(" brk.b ")
'BRK.B'
>>> normalize_ticker("DROP TABLE")
ValueError: Invalid ticker symbol: 'DROP TABLE'
```

---

## 5. Price Cache: `cache.py`

The cache is the hub between producers and consumers. v2 changes from v1:

1. **`reference_price` is sticky.** If a writer doesn't pass one, the previous value is kept. On the very first write it defaults to the first price. The simulator never has to track it; it just works.
2. **`remove()` bumps `version`.** The SSE payload is a full snapshot, so a removal is a change. Clients see it on the next push instead of up to 15 s later in Massive mode.
3. **`version` is read under the lock.** This is consistent and safe on free-threaded Python.
4. **`timestamp is None` check** instead of `timestamp or time.time()`, which treated a legitimate `0.0` as missing.
5. **Input guard:** non-finite or non-positive prices are rejected. A bad API row can't put `NaN` or `0` into portfolio math.

```python
# backend/app/market/cache.py
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import math
import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Latest PriceUpdate per ticker, plus a monotonic version counter.

    Writers: exactly one active MarketDataSource.
    Readers: SSE stream, trade execution, portfolio valuation, watchlist API, chat.

    Uses threading.Lock (not asyncio.Lock) because the Massive poller's results
    are produced in a worker thread and readers live on the event loop.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        reference_price: float | None = None,
    ) -> PriceUpdate:
        """Record a new price and return the resulting PriceUpdate.

        - previous_price is the last cached price (== price on first write -> 'flat').
        - reference_price is sticky: omitted -> keep the previous one (or the
          first price ever seen for this ticker).
        """
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid price for {ticker}: {price!r}")

        with self._lock:
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if reference_price is None:
                reference_price = prev.reference_price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=time.time() if timestamp is None else timestamp,
                reference_price=round(reference_price, 2),
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
        """Shallow copy; safe to iterate while writers continue."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
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

    def __contains__(self, ticker: object) -> bool:
        with self._lock:
            return ticker in self._prices
```

Example:

```python
cache = PriceCache()
cache.update("AAPL", 190.00)            # first write: flat, reference_price=190.00
u = cache.update("AAPL", 190.37)        # up, change=0.37, day_change_percent=0.1947
cache.update("AAPL", 189.90, reference_price=189.50)  # Massive passes prev close explicitly
cache.get_price("AAPL")                 # 189.9
cache.version                           # 3
```

---

## 6. Unified Interface: `interface.py`

The interface is the same as v1, with the contract written down so both implementations (and test fakes) behave identically.

```python
# backend/app/market/interface.py
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push PriceUpdates into a shared PriceCache on their own
    schedule; consumers read the cache, never the source.

    Contract (both implementations MUST honour it):
      * Tickers are normalized with `normalize_ticker()`; invalid ones raise ValueError.
      * start() is called once; a second call raises RuntimeError.
      * add_ticker()/remove_ticker() before start() raise RuntimeError.
      * add_ticker() is idempotent; remove_ticker() of an unknown ticker is a no-op.
      * remove_ticker() also removes the ticker from the PriceCache, and no
        in-flight update may re-insert it afterwards.
      * stop() is idempotent; after it returns, the source never writes again.
      * Background loops never die on a single failure; they log and continue.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL"])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a ticker."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and drop it from the cache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers, in insertion order."""
```

---

## 7. Simulator

### 7.1 The math

Each tick, for every ticker:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

- `dt = 0.5 s / (252 days × 6.5 h × 3600 s) = 0.5 / 5,896,800 ≈ 8.48e-8` of a trading year. One wall-clock second of simulation equals one second of real trading-hours time, so volatility looks realistic.
- **Per-tick size check:** AAPL has σ = 0.22, so σ√dt ≈ 0.22 × 2.91e-4 ≈ 6.4e-5. At $190 that is a one-standard-deviation move of about **1.2¢ per tick**. Over a 6.5 h day (46,800 ticks) the 1-sd range is about 0.22/√252 ≈ 1.4%. That is realistic, so the flashes are frequent but not wild.
- **Correlation:** draw `z ~ N(0, I)`, then `Z = L·z` where `L = cholesky(C)`. `C` is built from sector groups: tech 0.6, finance 0.5, everything else 0.3, and TSLA 0.3 with everyone.
- **Events:** each ticker has probability p = 0.001 per tick of a ±2–5% jump. With 10 tickers at 2 ticks/s that is 20 trials/s, so about **one event every 50 s** somewhere on the board.
- GBM is multiplicative, so prices stay strictly positive. Prices are rounded to cents only at output. Internal state keeps full precision, so rounding never accumulates.

### 7.2 `seed_prices.py`

v2 adds a few common extra tickers so that what the LLM or user typically adds (AMD, PYPL, …) gets a sensible price and sector instead of a random $50–$300. The correlation lookup is now generic over groups, so adding a sector means editing data, not code.

```python
# backend/app/market/seed_prices.py
"""Seed prices and per-ticker parameters for the market simulator (constants only)."""

# Realistic starting prices. The first 10 are the default watchlist.
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
    # Common additions (so dynamically-added tickers look realistic)
    "AMD": 160.00,
    "INTC": 35.00,
    "ORCL": 125.00,
    "CRM": 270.00,
    "PYPL": 65.00,
    "MA": 460.00,
    "BAC": 38.00,
    "GS": 450.00,
    "DIS": 110.00,
    "KO": 62.00,
}

# Per-ticker GBM parameters: sigma = annualized volatility, mu = annualized drift.
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
    "AMD": {"sigma": 0.45, "mu": 0.06},
    "INTC": {"sigma": 0.35, "mu": 0.02},
    "PYPL": {"sigma": 0.35, "mu": 0.03},
    "KO": {"sigma": 0.14, "mu": 0.03},
}

# Parameters for tickers not listed above.
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Range for the random starting price of unknown tickers.
UNKNOWN_SEED_RANGE: tuple[float, float] = (50.0, 300.0)

# Sector groups -> intra-group correlation.
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX", "AMD", "INTC", "ORCL", "CRM"},
    "finance": {"JPM", "V", "MA", "BAC", "GS", "PYPL"},
}
INTRA_GROUP_CORR: dict[str, float] = {"tech": 0.6, "finance": 0.5}

CROSS_GROUP_CORR = 0.3  # Different sectors, or any unknown ticker

# Tickers that "do their own thing": fixed low correlation with everything.
INDEPENDENT_TICKERS: set[str] = {"TSLA"}
INDEPENDENT_CORR = 0.3
```

> **Positive-definiteness:** the matrix is block-equicorrelated (within-block ρ ∈ {0.5, 0.6}, between-block 0.3, unit diagonal). Every block value is ≥ the cross value and < 1, so the matrix is positive definite for any number of tickers and Cholesky always succeeds. `_rebuild_cholesky` still falls back to uncorrelated draws if someone later sets invalid constants.

### 7.3 `simulator.py`

v2 changes from v1: an optional `seed` for deterministic tests and demos (it uses private RNGs, so it never touches global `random` / `np.random` state), the generic correlation lookup, a Cholesky fallback, ticker normalization, and start/early-call guards.

```python
# backend/app/market/simulator.py
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .models import normalize_ticker
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INDEPENDENT_CORR,
    INDEPENDENT_TICKERS,
    INTRA_GROUP_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    UNKNOWN_SEED_RANGE,
)

logger = logging.getLogger(__name__)

TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_UPDATE_INTERVAL = 0.5  # seconds


class GBMSimulator:
    """Correlated Geometric Brownian Motion over a dynamic set of tickers.

    Pure and synchronous: no asyncio, no cache. Easy to unit test.
    """

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_UPDATE_INTERVAL / TRADING_SECONDS_PER_YEAR,
        event_probability: float = 0.001,
        event_range: tuple[float, float] = (0.02, 0.05),
        seed: int | None = None,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._event_range = event_range
        self._rng = np.random.default_rng(seed)
        self._py_rng = random.Random(seed)

        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price rounded to cents}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z = self._rng.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma * sigma) * self._dt
            diffusion = sigma * self._sqrt_dt * z[i]
            price = self._prices[ticker] * math.exp(drift + diffusion)

            if self._py_rng.random() < self._event_prob:
                shock = self._py_rng.uniform(*self._event_range) * self._py_rng.choice((-1, 1))
                price *= 1 + shock
                logger.debug("Simulated event on %s: %+.1f%%", ticker, shock * 100)

            self._prices[ticker] = price
            result[ticker] = round(price, 2)
        return result

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        price = self._prices.get(ticker)
        return None if price is None else round(price, 2)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _add_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker) or self._py_rng.uniform(*UNKNOWN_SEED_RANGE)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2) rebuild on add/remove; n is small (< 50)."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho
        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            logger.error("Correlation matrix not positive definite; using independent moves")
            self._cholesky = None


def _group_of(ticker: str) -> str | None:
    for name, members in CORRELATION_GROUPS.items():
        if ticker in members:
            return name
    return None


def pairwise_correlation(t1: str, t2: str) -> float:
    """Sector-based correlation between two distinct tickers."""
    if t1 in INDEPENDENT_TICKERS or t2 in INDEPENDENT_TICKERS:
        return INDEPENDENT_CORR
    g1, g2 = _group_of(t1), _group_of(t2)
    if g1 is not None and g1 == g2:
        return INTRA_GROUP_CORR[g1]
    return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource that steps a GBMSimulator every `update_interval` seconds."""

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = DEFAULT_UPDATE_INTERVAL,
        event_probability: float = 0.001,
        seed: int | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._seed = seed
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        if self._sim is not None:
            raise RuntimeError("SimulatorDataSource already started")
        normalized = list(dict.fromkeys(normalize_ticker(t) for t in tickers))
        self._sim = GBMSimulator(
            tickers=normalized,
            dt=self._interval / TRADING_SECONDS_PER_YEAR,
            event_probability=self._event_prob,
            seed=self._seed,
        )
        # Seed the cache now so the first SSE push and first trade have prices.
        # This first write also fixes each ticker's reference_price (session open).
        for ticker in normalized:
            self._cache.update(ticker, self._sim.get_price(ticker))
        self._task = asyncio.create_task(self._run_loop(), name="market-simulator")
        logger.info("Simulator started with %d tickers", len(normalized))

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        sim = self._require_started()
        ticker = normalize_ticker(ticker)
        if ticker in sim.get_tickers():
            return
        sim.add_ticker(ticker)
        self._cache.update(ticker, sim.get_price(ticker))  # price available immediately
        logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        sim = self._require_started()
        ticker = normalize_ticker(ticker)
        sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    def _require_started(self) -> GBMSimulator:
        if self._sim is None:
            raise RuntimeError("SimulatorDataSource.start() has not been called")
        return self._sim

    async def _run_loop(self) -> None:
        # step() and the cache writes run with no await in between, so an
        # add/remove (also on the event loop) can never interleave with a tick.
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker, price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Example (standalone, for the demo or a notebook):

```python
sim = GBMSimulator(["AAPL", "NVDA", "JPM"], seed=42)
for _ in range(3):
    print(sim.step())
# {'AAPL': 190.0, 'NVDA': 800.03, 'JPM': 195.0}  (exact values depend on seed)
```

---

## 8. Massive API Client: `massive_client.py`

### 8.1 What we call

One request per poll covers every tracked ticker:

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...
Authorization: Bearer <MASSIVE_API_KEY>
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=key, retries=0)
snaps = client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=["AAPL", "MSFT"])
```

### 8.2 Actual response model (verified against the installed `massive` package)

The archived `MASSIVE_API.md` used several wrong attribute names. These are the real ones:

| JSON key | Python attribute | Notes |
|---|---|---|
| `ticker` | `snap.ticker` | |
| `lastTrade.p` | `snap.last_trade.price` | **Primary price** |
| `lastTrade.t` | `snap.last_trade.sip_timestamp` | **Nanoseconds.** There is no `.timestamp`. |
| `min.c` | `snap.min.close` | Latest minute bar close (fallback price) |
| `day.c` | `snap.day.close` | Today's running close. `0` before the session's first trade. |
| `prevDay.c` | `snap.prev_day.close` | **Previous close → `reference_price`**. There is no `day.previous_close`. |
| `todaysChangePerc` | `snap.todays_change_percent` | There is no `day.change_percent`. We compute our own from `reference_price`. |
| `updated` | `snap.updated` | Nanoseconds; fallback timestamp |

Any of `last_trade`, `min`, `day` and `prev_day` can be `None`. Snapshot data is cleared at midnight ET and repopulates from about 4 am ET. Unknown or delisted tickers are simply **absent** from the result list.

A sample payload, used in the tests:

```json
{
  "ticker": "AAPL",
  "todaysChange": 1.2, "todaysChangePerc": 0.63,
  "updated": 1762435800123456789,
  "day":     {"o": 189.1, "h": 191.0, "l": 188.7, "c": 190.4, "v": 51234567},
  "prevDay": {"o": 188.0, "h": 189.9, "l": 187.5, "c": 189.2, "v": 48000000},
  "min":     {"o": 190.3, "h": 190.5, "l": 190.2, "c": 190.45, "v": 120000},
  "lastTrade": {"p": 190.5, "s": 100, "t": 1762435800123456789, "x": 4},
  "lastQuote": {"p": 190.49, "P": 190.51, "t": 1762435800100000000}
}
```

### 8.3 Rate limits and retry policy

- The free tier allows 5 req/min, so the default `poll_interval = 15 s` (4 req/min) leaves headroom.
- The client's urllib3 adapter **retries 429/5xx internally (3× by default)**. On the free tier each hidden retry spends quota and makes the 429 worse. We build the client with `retries=0` and handle failures in our loop with **exponential backoff**: interval × 2ⁿ, capped at 120 s, reset after the first success.
- Errors arrive as `massive.exceptions.BadResponse` (non-200, body in the message), `AuthError` (missing key), or urllib3/socket errors. All are caught the same way.

### 8.4 Code

```python
# backend/app/market/massive_client.py
"""Massive (formerly Polygon.io) REST poller for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .models import normalize_ticker

logger = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = 15.0  # free tier: 5 req/min
MAX_BACKOFF = 120.0


def _positive(value: Any) -> float | None:
    """Return value as float if it's a usable price, else None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def epoch_to_seconds(value: Any) -> float | None:
    """Convert a Unix timestamp in s/ms/us/ns (detected by magnitude) to seconds."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    if value > 1e17:
        return value / 1e9  # nanoseconds (Massive snapshot default)
    if value > 1e14:
        return value / 1e6  # microseconds
    if value > 1e11:
        return value / 1e3  # milliseconds
    return float(value)


def extract_quote(snap: Any) -> tuple[float, float, float | None] | None:
    """Pull (price, timestamp_seconds, reference_price) out of a TickerSnapshot.

    Price fallback chain: last trade -> latest minute close -> day close -> prev close.
    Returns None if no usable price exists.
    """
    last_trade = getattr(snap, "last_trade", None)
    minute = getattr(snap, "min", None)
    day = getattr(snap, "day", None)
    prev_day = getattr(snap, "prev_day", None)

    reference = _positive(getattr(prev_day, "close", None))
    price = (
        _positive(getattr(last_trade, "price", None))
        or _positive(getattr(minute, "close", None))
        or _positive(getattr(day, "close", None))
        or reference
    )
    if price is None:
        return None

    ts = (
        epoch_to_seconds(getattr(last_trade, "sip_timestamp", None))
        or epoch_to_seconds(getattr(snap, "updated", None))
        or time.time()
    )
    return price, ts, reference


class MassiveDataSource(MarketDataSource):
    """Polls the Massive snapshot endpoint for all tracked tickers in one call."""

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_backoff: float = MAX_BACKOFF,
        client: Any | None = None,  # inject a fake in tests
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._max_backoff = max(max_backoff, poll_interval)
        self._client = client
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._started = False
        self._failures = 0
        self._warned_missing: set[str] = set()

    # --- MarketDataSource ---

    async def start(self, tickers: list[str]) -> None:
        if self._started:
            raise RuntimeError("MassiveDataSource already started")
        self._started = True
        if self._client is None:
            # retries=0: our loop owns retry/backoff so hidden retries don't burn quota.
            self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))

        await self._poll_once()  # populate the cache before the first SSE push
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers every %.1fs", len(self._tickers), self._interval
        )

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        self._require_started()
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (priced on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._require_started()
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._warned_missing.discard(ticker)
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    @property
    def current_interval(self) -> float:
        """Poll interval including any active backoff."""
        if self._failures == 0:
            return self._interval
        return min(self._interval * 2**self._failures, self._max_backoff)

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("MassiveDataSource.start() has not been called")

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self.current_interval)
            await self._poll_once()

    async def _poll_once(self) -> bool:
        """One poll cycle. Returns True on success. Never raises (except cancellation)."""
        requested = tuple(self._tickers)  # immutable copy handed to the worker thread
        if not requested or self._client is None:
            return True

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, requested)
        except Exception as exc:  # BadResponse (401/403/429/5xx), network errors, ...
            self._failures += 1
            logger.warning(
                "Massive poll failed (%d in a row), next try in %.0fs: %s",
                self._failures,
                self.current_interval,
                exc,
            )
            return False
        self._failures = 0

        # Re-read the live set: a ticker removed while the request was in flight
        # must not be written back into the cache.
        active = set(self._tickers)
        seen: set[str] = set()
        for snap in snapshots or []:
            ticker = (getattr(snap, "ticker", None) or "").upper()
            if ticker not in active:
                continue
            quote = extract_quote(snap)
            if quote is None:
                logger.warning("Massive: no usable price for %s", ticker)
                continue
            price, ts, reference = quote
            self._cache.update(ticker, price, timestamp=ts, reference_price=reference)
            seen.add(ticker)

        missing = (set(requested) & active) - seen - self._warned_missing
        if missing:
            # Usually an invalid/delisted symbol. Warn once per ticker.
            logger.warning("Massive returned no data for: %s", ", ".join(sorted(missing)))
            self._warned_missing |= missing
        return True

    def _fetch_snapshots(self, tickers: tuple[str, ...]) -> list:
        """Blocking HTTP call; runs in a worker thread via asyncio.to_thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=list(tickers),
        )
```

### 8.5 Behavior notes

- **Market closed:** `last_trade.price` is the last (possibly after-hours) trade. Every poll writes the same price, so `direction` reads `"flat"` and the frontend shows no flash. That is the correct behavior.
- **New ticker latency:** a ticker added mid-cycle is priced at the next poll, at most `poll_interval` later. Consumers must handle `cache.get(t) is None` (§12).
- **Invalid key:** every poll fails with 401 and backs off to one try every 120 s. The cache stays empty, SSE pushes nothing, and the log has a clear warning. The fix is to correct `.env` and restart.

---

## 9. Factory: `factory.py`

```python
# backend/app/market/factory.py
"""Select the market data source from environment variables."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import DEFAULT_POLL_INTERVAL, MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def _float_env(env: Mapping[str, str], name: str, default: float, minimum: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(float(raw), minimum)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def create_market_data_source(
    price_cache: PriceCache, env: Mapping[str, str] | None = None
) -> MarketDataSource:
    """Return an unstarted source.

    - MASSIVE_API_KEY non-empty -> MassiveDataSource (MASSIVE_POLL_INTERVAL, default 15s)
    - otherwise                 -> SimulatorDataSource
    """
    env = os.environ if env is None else env
    api_key = env.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        interval = _float_env(env, "MASSIVE_POLL_INTERVAL", DEFAULT_POLL_INTERVAL, minimum=1.0)
        logger.info("Market data source: Massive API (poll every %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

`MASSIVE_POLL_INTERVAL` is a new **optional** variable. Add it to `.env.example` and to PLAN §5 as "Optional: seconds between Massive polls (default 15; paid tiers can use 2–5)". The `env` parameter lets tests avoid `monkeypatch`.

---

## 10. SSE Streaming: `stream.py`

### 10.1 Wire format

One unnamed event per push. `data` is a JSON object keyed by ticker and holds the **full current snapshot**, so it is authoritative: a ticker missing from it is no longer tracked.

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.37,"previous_price":190.25,"timestamp":1762435800.5,"change":0.12,"change_percent":0.0631,"direction":"up","reference_price":190.0,"day_change":0.37,"day_change_percent":0.1947},"GOOGL":{...}}

: keepalive

```

- `retry: 1000` tells `EventSource` to reconnect 1 s after a drop.
- Pushes happen only when `cache.version` changed. That is ~every 500 ms with the simulator and ~every 15 s with Massive.
- The `: keepalive` comment is sent after 15 s without data. It keeps proxies and load balancers (App Runner, Render) from closing an idle stream in Massive mode. `EventSource` ignores comments.

### 10.2 Code

v2 builds the `APIRouter` **inside** the factory. v1 used a module-level router, so calling the factory twice (for example, in tests) registered `/prices` twice. The generator is a public function so it can be unit-tested without a server.

```python
# backend/app/market/stream.py
"""SSE endpoint for live price updates."""

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

PUSH_INTERVAL = 0.5  # seconds between cache checks
HEARTBEAT_INTERVAL = 15.0  # seconds of silence before a keepalive comment
RETRY_MS = 1000  # EventSource reconnect delay


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Build a fresh router exposing GET /api/stream/prices."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            generate_price_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # disable proxy buffering
            },
        )

    return router


def format_sse(data: object) -> str:
    return f"data: {json.dumps(data, separators=(',', ':'))}\n\n"


async def generate_price_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = PUSH_INTERVAL,
    heartbeat: float = HEARTBEAT_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects."""
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    yield f"retry: {RETRY_MS}\n\n"

    last_version = -1
    last_sent = time.monotonic()
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                snapshot = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield format_sse(snapshot)  # may be {} if every ticker was removed
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    finally:
        logger.info("SSE client disconnected: %s", client)
```

### 10.3 Frontend consumption (reference for the frontend agent)

```ts
// frontend: lib/usePriceStream.ts (sketch)
export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;          // unix seconds
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
  reference_price: number | null;
  day_change: number | null;
  day_change_percent: number | null;
}
export type ConnectionStatus = "connected" | "reconnecting" | "disconnected";

export function connectPrices(
  onPrices: (prices: Record<string, PriceUpdate>) => void,
  onStatus: (s: ConnectionStatus) => void,
): () => void {
  const es = new EventSource("/api/stream/prices");
  es.onopen = () => onStatus("connected");
  es.onmessage = (e) => onPrices(JSON.parse(e.data));
  es.onerror = () =>
    onStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
  return () => es.close();
}
// Sparklines: append {time: timestamp, value: price} per ticker on each message,
// but only when timestamp advanced. In Massive mode the same tick can be resent.
// Flash: when direction !== "flat", add .flash-up / .flash-down, remove after ~500ms.
```

---

## 11. FastAPI Integration

### 11.1 `main.py`

Use an app factory: the cache and source are created once, the routers are included at build time (not inside `lifespan`), and the static mount goes **last** so `/api/*` routes always win.

```python
# backend/app/main.py (market-data-relevant parts)
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"  # Next.js export copied here


def create_app() -> FastAPI:
    price_cache = PriceCache()
    source = create_market_data_source(price_cache)  # reads env; Docker passes --env-file

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from app.db import get_tracked_tickers  # watchlist ∪ open positions (see §12)

        await source.start(await get_tracked_tickers())
        try:
            yield
        finally:
            await source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.state.price_cache = price_cache
    app.state.market_source = source

    app.include_router(create_stream_router(price_cache))
    # app.include_router(portfolio_router); app.include_router(watchlist_router); ...

    if STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
```

> For local runs outside Docker, load the project-root `.env` before `create_app()` runs, for example with `python-dotenv`'s `load_dotenv(Path(__file__).parents[2] / ".env")`. Otherwise `MASSIVE_API_KEY` is not visible to the factory.

### 11.2 `deps.py`

Route handlers get market data through dependencies. They never import the cache from `main`, which avoids circular imports and lets tests swap in fakes with `app.dependency_overrides`.

```python
# backend/app/deps.py
from fastapi import Request

from app.market import MarketDataSource, PriceCache


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

---

## 12. Coordinating with Watchlist, Trades & Chat

### 12.1 What should be tracked

**tracked = watchlist ∪ tickers with an open position.** If a user removes a ticker from the watchlist but still holds it, the source must keep pricing it, or portfolio valuation goes stale.

```python
# backend/app/db/... (owned by the backend agent)
async def get_tracked_tickers(user_id: str = "default") -> list[str]:
    rows = await db.fetch_all(
        """
        SELECT ticker FROM watchlist WHERE user_id = :u
        UNION
        SELECT ticker FROM positions WHERE user_id = :u AND quantity > 0
        """,
        {"u": user_id},
    )
    return [r["ticker"] for r in rows]
```

### 12.2 Shared helpers

Put these in the service layer so the REST routes and the LLM chat flow use the same logic.

```python
# backend/app/services/market_tracking.py
from app.market import MarketDataSource, PriceCache, normalize_ticker


async def ensure_tracked(source: MarketDataSource, ticker: str) -> str:
    ticker = normalize_ticker(ticker)   # ValueError -> 400 at the route layer
    await source.add_ticker(ticker)     # idempotent
    return ticker


async def release_if_unused(source: MarketDataSource, ticker: str, *, in_watchlist: bool,
                            position_qty: float) -> None:
    """Stop pricing a ticker only when nothing needs it any more."""
    if not in_watchlist and position_qty <= 0:
        await source.remove_ticker(ticker)


def require_price(cache: PriceCache, ticker: str) -> float:
    price = cache.get_price(ticker)
    if price is None:
        raise LookupError(f"Price not yet available for {ticker}. Try again in a few seconds.")
    return price
```

### 12.3 Route usage

```python
# Watchlist
@router.post("/api/watchlist", status_code=201)
async def add_watchlist(body: WatchlistAdd, source=Depends(get_market_source),
                        cache=Depends(get_price_cache)):
    try:
        ticker = normalize_ticker(body.ticker)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await db.insert_watchlist(ticker)                      # UNIQUE(user_id, ticker) -> 409 on dup
    await ensure_tracked(source, ticker)
    update = cache.get(ticker)                             # None in Massive mode until next poll
    return {"ticker": ticker, "price": update.to_dict() if update else None}


@router.delete("/api/watchlist/{ticker}", status_code=204)
async def remove_watchlist(ticker: str, source=Depends(get_market_source)):
    ticker = normalize_ticker(ticker)
    await db.delete_watchlist(ticker)
    pos = await db.get_position(ticker)
    await release_if_unused(source, ticker, in_watchlist=False,
                            position_qty=pos.quantity if pos else 0)


@router.get("/api/watchlist")
async def list_watchlist(cache=Depends(get_price_cache)):
    tickers = await db.list_watchlist()
    return [{"ticker": t, "price": (u.to_dict() if (u := cache.get(t)) else None)} for t in tickers]


# Trade: the fill price always comes from the cache
@router.post("/api/portfolio/trade")
async def trade(body: TradeRequest, source=Depends(get_market_source),
                cache=Depends(get_price_cache)):
    ticker = await ensure_tracked(source, body.ticker)    # LLM may trade an unwatched ticker
    try:
        price = require_price(cache, ticker)               # instant in sim; may 400 in Massive
    except LookupError as e:
        raise HTTPException(400, str(e))
    result = await portfolio.execute(ticker, body.side, body.quantity, price)
    if body.side == "sell" and result.remaining_qty == 0 and not await db.in_watchlist(ticker):
        await source.remove_ticker(ticker)                 # fully closed, not watched
    return result
```

### 12.4 Chat context

The LLM prompt builder reads `cache.get_all()` once per request, a consistent snapshot:

```python
prices = cache.get_all()
watch_lines = [
    f"{t}: ${u.price:.2f} ({u.day_change_percent:+.2f}% today)"
    if u.day_change_percent is not None else f"{t}: ${u.price:.2f}"
    for t, u in prices.items()
]
```

LLM-issued `watchlist_changes` and `trades` go through the same `ensure_tracked` / `require_price` helpers, so validation is identical to manual actions.

---

## 13. Testing

Test config is already in `pyproject.toml`: `asyncio_mode = "auto"`, so async tests need no decorator.

### 13.1 Models & cache

```python
# backend/tests/market/test_cache.py
import math
import threading

import pytest

from app.market import PriceCache, PriceUpdate, normalize_ticker


def test_first_update_is_flat_and_sets_reference():
    u = PriceCache().update("AAPL", 190.0)
    assert u.direction == "flat" and u.previous_price == 190.0 and u.reference_price == 190.0
    assert u.day_change_percent == 0.0


def test_direction_and_sticky_reference():
    c = PriceCache()
    c.update("AAPL", 190.0)
    u = c.update("AAPL", 191.9)
    assert u.direction == "up" and u.change == 1.9
    assert u.reference_price == 190.0 and u.day_change_percent == 1.0


def test_explicit_reference_overrides():
    c = PriceCache()
    u = c.update("AAPL", 190.0, reference_price=200.0)
    assert u.day_change_percent == -5.0


def test_zero_timestamp_is_respected():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


@pytest.mark.parametrize("bad", [0, -1, math.nan, math.inf, None])
def test_rejects_bad_prices(bad):
    with pytest.raises(ValueError):
        PriceCache().update("AAPL", bad)


def test_remove_bumps_version_only_if_present():
    c = PriceCache()
    c.update("AAPL", 1.0)
    v = c.version
    c.remove("AAPL")
    assert c.version == v + 1
    c.remove("AAPL")
    assert c.version == v + 1


def test_concurrent_writers():
    c = PriceCache()

    def work(n):
        for i in range(1000):
            c.update(f"T{n}", 100 + i)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.version == 8000 and len(c) == 8


def test_to_dict_shape():
    d = PriceUpdate("AAPL", 191.2, 191.05, 1.0, 190.0).to_dict()
    assert set(d) == {"ticker", "price", "previous_price", "timestamp", "change",
                      "change_percent", "direction", "reference_price", "day_change",
                      "day_change_percent"}


@pytest.mark.parametrize("raw,expected", [("aapl", "AAPL"), (" brk.b ", "BRK.B"), ("BF-B", "BF-B")])
def test_normalize_ticker_ok(raw, expected):
    assert normalize_ticker(raw) == expected


@pytest.mark.parametrize("raw", ["", "1ABC", "TOOLONGTICKER", "A B", "AAPL;"])
def test_normalize_ticker_rejects(raw):
    with pytest.raises(ValueError):
        normalize_ticker(raw)
```

### 13.2 Simulator math

```python
# backend/tests/market/test_simulator.py
import numpy as np

from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator, pairwise_correlation

DEFAULT = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


def test_seeded_runs_are_deterministic():
    a, b = GBMSimulator(DEFAULT, seed=7), GBMSimulator(DEFAULT, seed=7)
    assert [a.step() for _ in range(50)] == [b.step() for _ in range(50)]


def test_initial_prices_are_seeds():
    sim = GBMSimulator(DEFAULT)
    assert all(sim.get_price(t) == SEED_PRICES[t] for t in DEFAULT)


def test_prices_stay_positive_even_with_many_events():
    sim = GBMSimulator(["TSLA"], event_probability=0.5, seed=1)
    for _ in range(5000):
        assert sim.step()["TSLA"] > 0


def test_cholesky_exists_for_full_default_set():
    sim = GBMSimulator(DEFAULT)
    assert sim._cholesky is not None and sim._cholesky.shape == (10, 10)


def test_correlation_rules():
    assert pairwise_correlation("AAPL", "MSFT") == 0.6
    assert pairwise_correlation("JPM", "V") == 0.5
    assert pairwise_correlation("AAPL", "JPM") == 0.3
    assert pairwise_correlation("TSLA", "AAPL") == 0.3
    assert pairwise_correlation("ZZZZ", "AAPL") == 0.3


def test_empirical_correlation_tech_higher_than_cross():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0, seed=3)
    rows = [sim.step() for _ in range(20001)]
    p = np.array([[r[t] for t in ("AAPL", "MSFT", "JPM")] for r in rows])
    # cents rounding adds noise to tiny ticks, so compare relative ordering only
    rets = np.diff(np.log(p), axis=0)
    c = np.corrcoef(rets.T)
    assert c[0, 1] > c[0, 2]


def test_per_tick_move_is_small():
    sim = GBMSimulator(["AAPL"], event_probability=0, seed=0)
    prev = sim.get_price("AAPL")
    for _ in range(1000):
        p = sim.step()["AAPL"]
        assert abs(p / prev - 1) < 0.002  # < 0.2% per tick without events
        prev = p


def test_add_remove_and_unknown_ticker():
    sim = GBMSimulator(["AAPL"], seed=0)
    sim.add_ticker("ZZZZ")
    assert 50 <= sim.get_price("ZZZZ") <= 300
    sim.add_ticker("ZZZZ")
    assert sim.get_tickers() == ["AAPL", "ZZZZ"]
    sim.remove_ticker("AAPL")
    sim.remove_ticker("NOPE")
    assert list(sim.step()) == ["ZZZZ"]
```

### 13.3 Simulator data source

```python
# backend/tests/market/test_simulator_source.py
import asyncio

import pytest

from app.market import PriceCache
from app.market.simulator import SimulatorDataSource


async def test_start_seeds_cache_and_ticks():
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=0.01, seed=1)
    await src.start(["aapl", "GOOGL", "AAPL"])  # normalized + de-duplicated
    assert src.get_tickers() == ["AAPL", "GOOGL"]
    v0 = cache.version
    await asyncio.sleep(0.1)
    assert cache.version > v0
    await src.stop()
    await src.stop()  # idempotent
    v1 = cache.version
    await asyncio.sleep(0.05)
    assert cache.version == v1  # no writes after stop


async def test_add_is_priced_immediately_and_remove_clears_cache():
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=10)
    await src.start([])
    await src.add_ticker("pypl")
    assert cache.get_price("PYPL") == 65.0
    await src.remove_ticker("PYPL")
    assert "PYPL" not in cache and src.get_tickers() == []
    await src.stop()


async def test_guards():
    src = SimulatorDataSource(PriceCache())
    with pytest.raises(RuntimeError):
        await src.add_ticker("AAPL")
    await src.start(["AAPL"])
    with pytest.raises(RuntimeError):
        await src.start(["AAPL"])
    with pytest.raises(ValueError):
        await src.add_ticker("not a ticker")
    await src.stop()
```

### 13.4 Massive (with real model objects, no network)

Building snapshots with `TickerSnapshot.from_dict` means a wrong attribute name fails the test instead of being silently invented by `MagicMock`.

```python
# backend/tests/market/test_massive.py
import asyncio

import pytest
from massive.rest.models import TickerSnapshot

from app.market import PriceCache
from app.market.massive_client import MassiveDataSource, epoch_to_seconds, extract_quote

NS = 1762435800123456789


def snap(ticker, price=190.5, prev_close=189.2, **overrides):
    d = {"ticker": ticker, "updated": NS, "prevDay": {"c": prev_close},
         "day": {"c": price}, "min": {"c": price},
         "lastTrade": {"p": price, "s": 100, "t": NS}}
    d.update(overrides)
    return TickerSnapshot.from_dict(d)


class FakeClient:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result or [], error, []

    def get_snapshot_all(self, market_type, tickers):
        self.calls.append(list(tickers))
        if self.error:
            raise self.error
        return self.result


def test_extract_quote_from_real_model():
    price, ts, ref = extract_quote(snap("AAPL"))
    assert price == 190.5 and ref == 189.2
    assert ts == pytest.approx(NS / 1e9)


def test_extract_quote_fallbacks():
    s = snap("AAPL", lastTrade={}, min={"c": 0}, day={"c": 0})  # pre-market: only prev close
    assert extract_quote(s)[0] == 189.2
    assert extract_quote(TickerSnapshot.from_dict({"ticker": "X"})) is None


@pytest.mark.parametrize("v,expected", [(NS, NS / 1e9), (1762435800123, 1762435800.123),
                                        (1762435800, 1762435800.0), (None, None), (0, None)])
def test_epoch_to_seconds(v, expected):
    assert epoch_to_seconds(v) == (pytest.approx(expected) if expected else None)


async def test_start_polls_immediately_and_sets_reference():
    cache = PriceCache()
    client = FakeClient([snap("AAPL"), snap("GOOGL", 175.0, 170.0)])
    src = MassiveDataSource("k", cache, poll_interval=60, client=client)
    await src.start(["aapl", "GOOGL"])
    assert client.calls == [["AAPL", "GOOGL"]]
    u = cache.get("GOOGL")
    assert u.price == 175.0 and u.reference_price == 170.0
    assert u.day_change_percent == pytest.approx(2.9412)
    await src.stop()


async def test_failure_backs_off_then_resets():
    cache = PriceCache()
    client = FakeClient(error=RuntimeError("429"))
    src = MassiveDataSource("k", cache, poll_interval=10, max_backoff=60, client=client)
    await src.start(["AAPL"])
    assert src.current_interval == 20
    await src._poll_once()
    await src._poll_once()
    assert src.current_interval == 60  # capped
    client.error, client.result = None, [snap("AAPL")]
    assert await src._poll_once() is True
    assert src.current_interval == 10 and cache.get_price("AAPL") == 190.5
    await src.stop()


async def test_removed_during_inflight_poll_is_not_reinserted():
    cache = PriceCache()
    gate = asyncio.Event()

    class SlowClient(FakeClient):
        def get_snapshot_all(self, market_type, tickers):
            asyncio.run_coroutine_threadsafe(gate.wait(), loop).result()
            return [snap("AAPL"), snap("MSFT")]

    loop = asyncio.get_running_loop()
    src = MassiveDataSource("k", cache, poll_interval=60, client=FakeClient())
    await src.start(["AAPL", "MSFT"])
    src._client = SlowClient()
    poll = asyncio.create_task(src._poll_once())
    await asyncio.sleep(0.01)
    await src.remove_ticker("MSFT")
    gate.set()
    await poll
    assert "MSFT" not in cache and "AAPL" in cache
    await src.stop()


async def test_unknown_and_unrequested_tickers_ignored():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60,
                            client=FakeClient([snap("AAPL"), snap("EXTRA")]))
    await src.start(["AAPL", "FAKEX"])
    assert "EXTRA" not in cache and "FAKEX" not in cache and "AAPL" in cache
    await src.stop()
```

### 13.5 Factory & SSE

```python
# backend/tests/market/test_factory.py
from app.market import PriceCache, create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


def test_factory_selection():
    c = PriceCache()
    assert isinstance(create_market_data_source(c, env={}), SimulatorDataSource)
    assert isinstance(create_market_data_source(c, env={"MASSIVE_API_KEY": "  "}), SimulatorDataSource)
    src = create_market_data_source(c, env={"MASSIVE_API_KEY": "k", "MASSIVE_POLL_INTERVAL": "5"})
    assert isinstance(src, MassiveDataSource) and src.current_interval == 5.0
    bad = create_market_data_source(c, env={"MASSIVE_API_KEY": "k", "MASSIVE_POLL_INTERVAL": "x"})
    assert bad.current_interval == 15.0
```

```python
# backend/tests/market/test_stream.py
import json

from fastapi import FastAPI

from app.market import PriceCache, create_stream_router
from app.market.stream import generate_price_events


class FakeRequest:
    """Minimal stand-in: disconnects after `polls` checks."""

    def __init__(self, polls):
        self.polls, self.client = polls, None

    async def is_disconnected(self):
        self.polls -= 1
        return self.polls < 0


async def collect(cache, polls, **kw):
    return [f async for f in generate_price_events(cache, FakeRequest(polls), interval=0, **kw)]


async def test_retry_then_snapshot_then_nothing_if_unchanged():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    frames = await collect(cache, polls=3, heartbeat=999)
    assert frames[0] == "retry: 1000\n\n"
    data = [f for f in frames if f.startswith("data:")]
    assert len(data) == 1  # version unchanged afterwards -> no resend
    assert json.loads(data[0][5:])["AAPL"]["price"] == 190.0


async def test_heartbeat_when_idle():
    frames = await collect(PriceCache(), polls=3, heartbeat=0)
    assert ": keepalive\n\n" in frames


def test_router_factory_is_reentrant():
    c = PriceCache()
    app = FastAPI()
    app.include_router(create_stream_router(c))
    app.include_router(create_stream_router(c))  # v1 registered the route twice on one router
    assert [r.path for r in create_stream_router(c).routes] == ["/api/stream/prices"]
```

### 13.6 Manual / E2E checks

- `uv run market_data_demo.py` shows the live terminal dashboard (simulator).
- `curl -N localhost:8000/api/stream/prices` shows `retry:` then a `data:` frame about every 0.5 s.
- With a real key: `MASSIVE_API_KEY=... uv run python -c "..."`. Call `MassiveDataSource._poll_once()` once and print `cache.get_all()`. This is the only test that catches API-shape drift, so run it before releases.
- Playwright (in `test/`) covers "prices are streaming" and "SSE reconnects" (PLAN §12).

---

## 14. Error Handling & Configuration Reference

### 14.1 Failure modes

| Situation | Behavior |
|---|---|
| Simulator `step()` raises | Logged with traceback; loop continues next tick |
| Massive 401 / 403 | Warning logged; backoff to 120 s; cache stays as-is (empty if never succeeded) |
| Massive 429 | No hidden urllib3 retries (`retries=0`); exponential backoff 30 → 60 → 120 s; resets on success |
| Network error / timeout | Same as 429 |
| Snapshot without a usable price | That ticker skipped with a warning; others still update |
| Requested ticker absent from response | Warned once per ticker (likely invalid symbol); watchlist shows `price: null` |
| Ticker removed while a poll is in flight | Result for it discarded; it is not re-inserted into the cache |
| Trade before a ticker has a price | `require_price` → HTTP 400 "Price not yet available…" (Massive mode only) |
| Invalid ticker string | `normalize_ticker` → `ValueError` → HTTP 400 |
| Empty watchlist at startup | Sources idle; SSE sends `{}` once; adding a ticker resumes normally |
| SSE client disconnects | Generator exits on the next check (≤ 0.5 s) and logs it |
| Idle SSE (Massive between polls) | `: keepalive` comment every 15 s |

### 14.2 Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | empty | Non-empty → Massive; else simulator |
| `MASSIVE_POLL_INTERVAL` | env (new, optional) | `15` | Seconds, min 1. Paid tiers: 2–5 |
| `update_interval` | `SimulatorDataSource` | `0.5` s | `dt` is derived from it |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | `0.001` | Per ticker per tick |
| `event_range` | `GBMSimulator` | `(0.02, 0.05)` | Shock size |
| `seed` | `SimulatorDataSource` / `GBMSimulator` | `None` | Set for deterministic tests/demos |
| `max_backoff` | `MassiveDataSource` | `120` s | Backoff cap |
| `PUSH_INTERVAL` | `stream.py` | `0.5` s | SSE cache check cadence |
| `HEARTBEAT_INTERVAL` | `stream.py` | `15` s | Keepalive after silence |
| `RETRY_MS` | `stream.py` | `1000` | EventSource reconnect delay |

---

## 15. Delta from v1 / Implementation Checklist

The existing code in `backend/app/market/` is v1, with 73 passing tests. To bring it to this design:

| # | File | Change | Why |
|---|---|---|---|
| 1 | `massive_client.py` | Parse with `extract_quote()`: `last_trade.sip_timestamp` (ns), fallback chain, `prev_day.close` as reference | **Bug:** v1 reads the non-existent `last_trade.timestamp`, so real-data mode never prices anything |
| 2 | `massive_client.py` | `RESTClient(retries=0)` + exponential backoff (`current_interval`) | Hidden retries burn free-tier quota; v1 hammers at a fixed interval |
| 3 | `massive_client.py` | Pass a tuple copy of tickers to the thread; drop results for tickers removed mid-poll | Data race / removed ticker resurrected in the cache |
| 4 | `massive_client.py` | Injectable `client`; warn once for tickers missing from the response | Testability; invalid-symbol visibility |
| 5 | `tests/market/test_massive.py` | Use `TickerSnapshot.from_dict` and a `FakeClient` instead of `MagicMock` | `MagicMock` hid bug #1 |
| 6 | `models.py` | Add `reference_price`, `day_change`, `day_change_percent`; add `normalize_ticker()` | Watchlist "daily change %"; consistent tickers |
| 7 | `cache.py` | Sticky `reference_price`; `remove()` bumps version; locked `version`; `timestamp is None`; reject bad prices | Correctness and SSE freshness |
| 8 | `simulator.py` / `seed_prices.py` | `seed` param + private RNGs; generic group correlation; extra seed tickers; Cholesky fallback; normalization and guards | Determinism, extensibility, robustness |
| 9 | `stream.py` | Router created inside the factory; public `generate_price_events`; keepalive comments; push `{}` when empty | Fixes double registration; testable; proxy-safe |
| 10 | `factory.py` | `env` parameter; optional `MASSIVE_POLL_INTERVAL` | Testability; paid-tier tuning |
| 11 | `__init__.py`, `backend/CLAUDE.md` | Export `normalize_ticker`; document new fields | Keep the developer guide accurate |
| 12 | `.env.example`, `PLAN.md` §5 | Document `MASSIVE_POLL_INTERVAL` | New optional variable |
| 13 | New `tests/market/test_stream.py` | SSE generator + router tests | v1 `stream.py` had 31% coverage |

**Definition of done:** `uv run --extra dev pytest` and `uv run --extra dev ruff check app/ tests/` both pass, `market_data_demo.py` still runs, and one manual poll with a real `MASSIVE_API_KEY` prints prices for all 10 default tickers.
