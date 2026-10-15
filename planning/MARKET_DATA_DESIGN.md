# Market Data Backend — Detailed Design

**Status:** Current design (2026-10-15). Supersedes `planning/archive/MARKET_DATA_DESIGN.md`.
**Scope:** everything under `backend/app/market/`, plus how the rest of the backend (lifespan, watchlist, portfolio, chat) consumes it.
**Contract source:** `planning/PLAN.md` Sections 6 and 8. If this document and PLAN.md disagree, PLAN.md wins until this document's proposals (Section 13) are written back into it.

The market data subsystem already exists, has 73 passing tests, and is summarized in `MARKET_DATA_SUMMARY.md`. This document does three things:

1. Gives the complete, implementation-ready design for the unified API, the GBM simulator and the Massive poller, with full code for each module.
2. Corrects defects found while checking the existing code against the real `massive` package and the PLAN.md contracts (Section 4). One of them is critical: **Massive mode currently never puts a price in the cache.**
3. Adds what the PLAN.md contracts need and the code lacks: ticker normalization, the `day_change_percent` baseline, and integration with the FastAPI lifespan, watchlist, and trade endpoints.

All code below was run against `massive` as pinned in `backend/uv.lock`, together with the existing test suite and the new tests in Section 12.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [Module Layout](#3-module-layout)
4. [Corrections to the Current Implementation](#4-corrections-to-the-current-implementation)
5. [Unified API](#5-unified-api)
6. [GBM Simulator](#6-gbm-simulator)
7. [Massive API Poller](#7-massive-api-poller)
8. [Factory and Configuration](#8-factory-and-configuration)
9. [SSE Streaming Endpoint](#9-sse-streaming-endpoint)
10. [Application Integration](#10-application-integration)
11. [Optional Extension: Server-Side Price History](#11-optional-extension-server-side-price-history)
12. [Testing](#12-testing)
13. [Proposed Resolutions for PLAN.md Open Items](#13-proposed-resolutions-for-planmd-open-items)
14. [Implementation Checklist](#14-implementation-checklist)

---

## 1. Requirements

Traced from PLAN.md:

| # | Requirement | Source | Met by |
|---|---|---|---|
| R1 | Two sources, one interface; downstream code is source-agnostic | §6 | `MarketDataSource` ABC + `PriceCache` |
| R2 | Simulator by default; Massive when `MASSIVE_API_KEY` is non-empty | §5, §6 | `create_market_data_source()` |
| R3 | Simulator: GBM, ~500 ms ticks, correlated moves, random 2–5 % events, realistic seeds | §6 | `GBMSimulator` |
| R4 | Massive: REST polling of all watched tickers in one call, configurable interval (15 s free tier) | §6 | `MassiveDataSource` |
| R5 | Shared in-memory cache: latest price, previous price, timestamp per ticker | §6 | `PriceCache` |
| R6 | `GET /api/stream/prices`: `retry: 1000`, then one unnamed event ~every 500 ms when anything changed, keyed by ticker | §8 | `create_stream_router()` |
| R7 | Tickers trimmed, uppercased, matching `^[A-Z][A-Z0-9.]{0,9}$` | §8 conventions | `normalize_ticker()` |
| R8 | Simulator prices a newly added ticker immediately; Massive prices it on the next poll | §8 trade rules | `add_ticker()` in each source |
| R9 | Trade fill price = cached price at execution; no price → `price_unavailable` (409) | §8 | `PriceCache.get()` |
| R10 | `GET /api/watchlist` carries `price`, `previous_price`, `direction`, `day_change_percent` (null when unpriced) | §8 | `PriceUpdate` + `watchlist_entry()` |
| R11 | Removing a watched ticker removes it from the data source | §8 | `remove_ticker()` |
| R12 | Market data starts with the app; the database is ready first | §13.2 item 8 | Lifespan, Section 10.1 |

---

## 2. Architecture

```
                         ┌──────────────────────────────┐
  MASSIVE_API_KEY ──────▶│  create_market_data_source() │
                         └──────────────┬───────────────┘
                     empty / unset      │      set
                 ┌──────────────────────┴───────────────────┐
                 ▼                                          ▼
     ┌───────────────────────┐                ┌───────────────────────────┐
     │  SimulatorDataSource  │                │    MassiveDataSource      │
     │  asyncio task, 500 ms │                │  asyncio task, 15 s poll  │
     │  GBMSimulator.step()  │                │  to_thread(get_snapshot)  │
     └───────────┬───────────┘                └─────────────┬─────────────┘
                 │ update(ticker, price, ts, reference)     │
                 └──────────────────────┬───────────────────┘
                                        ▼
                        ┌───────────────────────────────┐
                        │  PriceCache (threading.Lock)  │
                        │  ticker → PriceUpdate         │
                        │  version counter              │
                        └───┬───────────┬───────────┬───┘
                            │           │           │
               SSE /api/stream/prices   │     trade execution
                                 portfolio / watchlist / chat context
```

### Principles

- **Push into a cache, never pull from a source.** Sources write to `PriceCache` on their own schedule; every consumer reads the cache. Consumers never know which source is active or how often it updates.
- **`PriceUpdate` is the only type that leaves the package.** It is immutable, and its derived fields (`change`, `direction`, `day_change_percent`) are computed properties, so they can never disagree with the prices.
- **One event loop, one worker thread.** The simulator runs entirely on the event loop. The Massive client is synchronous (`urllib3`), so each poll runs in `asyncio.to_thread`. The cache uses `threading.Lock` because the worker thread writes to it.
- **Sources own their ticker set; the database owns the watchlist.** The API layer updates the database first, then tells the source (Section 10.3). On startup the source is seeded from the database.

### Threading model

| Code | Runs on | Touches |
|---|---|---|
| `SimulatorDataSource._run_loop` | event loop | `GBMSimulator` state, `PriceCache` |
| `MassiveDataSource._poll_loop` | event loop | `_tickers`, `PriceCache` |
| `MassiveDataSource._fetch_snapshots` | worker thread | a **copy** of `_tickers`, HTTP only |
| `add_ticker` / `remove_ticker` | event loop (route handlers) | `_tickers`, `GBMSimulator`, `PriceCache` |
| SSE generators | event loop, one per client | `PriceCache` (read) |

The source methods never `await` between checking and mutating their ticker list, so no `asyncio.Lock` is needed. The only cross-thread data is the cache, which is locked, and the ticker list given to the worker, which is a copy.

---

## 3. Module Layout

```
backend/app/market/
  __init__.py          # Public API re-exports
  models.py            # PriceUpdate                       (changed: reference_price, day_change_percent)
  tickers.py           # normalize_ticker, InvalidTickerError (new)
  cache.py             # PriceCache                         (changed: baseline, remove bumps version)
  interface.py         # MarketDataSource ABC               (docstrings only)
  seed_prices.py       # Seeds, GBM params, correlations    (unchanged)
  simulator.py         # GBMSimulator + SimulatorDataSource (changed: normalization, idempotency)
  massive_client.py    # MassiveDataSource                  (rewritten parsing; fixes, backoff)
  factory.py           # create_market_data_source          (changed: MASSIVE_POLL_INTERVAL)
  stream.py            # SSE router factory                 (changed: per-call router, keepalive)
```

Every other backend module imports from `app.market` only, never from the submodules.

---

## 4. Corrections to the Current Implementation

Found by running the current code against real `massive` model objects (`TickerSnapshot.from_dict`) instead of `MagicMock`, and by checking it against PLAN.md §8.

| # | Severity | Defect | Effect | Fix (section) |
|---|---|---|---|---|
| C1 | **Critical** | `massive_client.py` reads `snap.last_trade.timestamp`. The real `LastTrade` model has no `timestamp`; the field is `sip_timestamp`, in **nanoseconds**. | `AttributeError` for every snapshot. Each one is logged as "Skipping" and **no price ever reaches the cache in Massive mode**. The tests pass only because `MagicMock` invents the attribute. | Rewritten `parse_snapshot()` (7.3) |
| C2 | High | No day-change baseline. `day_change_percent` is in the `GET /api/watchlist` contract but nothing computes it. (`MASSIVE_API.md` refers to `day.previous_close` and `day.change_percent`, which do not exist on the stock snapshot model. The real fields are `prev_day.close` and `todays_change_percent`.) | The watchlist cannot show daily change. | `reference_price` on `PriceUpdate` (5.1, 5.3, 7.3) |
| C3 | Medium | `PriceCache.remove()` does not bump `version`. | SSE clients do not learn about a removal until the next price change. That is up to 15 s in Massive mode, and never if no other ticker changes. | 5.3 |
| C4 | Medium | A poll in flight while `remove_ticker()` runs writes the removed ticker back into the cache. | A "ghost" ticker reappears in the stream after removal. | Membership check after the fetch (7.4) |
| C5 | Medium | `_fetch_snapshots` passes `self._tickers` itself to the worker thread. | `add_ticker()` on the event loop can mutate the list while the thread joins it. | Pass a copy (7.4) |
| C6 | Medium | Tickers are not normalized in `SimulatorDataSource`, and nowhere against the §8 format rule. | `"aapl"` and `"AAPL"` become two simulated tickers. | `normalize_ticker()` (5.2) |
| C7 | Low | `start()` awaits the first Massive poll with no bound (10 s connect + 10 s read × 3 retries). | App startup can hang for most of a minute when the API is unreachable. | `wait_for(..., 10 s)` (7.4) |
| C8 | Low | No backoff on repeated failures. | On a 429 the free tier keeps hitting the limit every 15 s. | Exponential backoff to 120 s (7.4) |
| C9 | Low | `stream.py` registers the route on a module-level `router`. | Calling `create_stream_router()` twice (e.g. in tests) registers `/prices` twice. | Router built inside the factory (9) |
| C10 | Low | No traffic on an idle stream. | Proxies can close a long-idle stream. An empty watchlist, or Massive mode while polls are failing, sends nothing at all. | `: keepalive` comment every 15 s (9) |
| C11 | Low | `SimulatorDataSource.add_ticker()` on a ticker that is already tracked re-seeds the cache. | A spurious "flat" tick and version bump. | Membership guard (6.5) |

C1 and C2 also make `planning/archive/MASSIVE_API.md` misleading. Section 7.2 is the corrected reference.

---

## 5. Unified API

### 5.1 `models.py` — `PriceUpdate`

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
    reference_price: float | None = None  # Baseline for day_change_percent

    @property
    def change(self) -> float:
        """Absolute price change from the previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def day_change_percent(self) -> float | None:
        """Percentage change against the day baseline, or None if there is none."""
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 2)

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
            "day_change_percent": self.day_change_percent,
        }
```

Semantics:

| Field | Meaning | Example |
|---|---|---|
| `price` | Latest price, rounded to cents | `191.24` |
| `previous_price` | Price at the previous update of this ticker (equal to `price` on the first update) | `191.10` |
| `change`, `change_percent`, `direction` | Against `previous_price`, i.e. **tick-to-tick**, as PLAN.md §8 specifies for SSE | `0.14`, `0.0733`, `"up"` |
| `reference_price` | Day baseline: previous close (Massive) or the first price seen since server start (simulator) | `190.00` |
| `day_change_percent` | Against `reference_price`, two decimals. Used by `GET /api/watchlist` and the SSE payload | `0.65` |

`reference_price` has a default, so every existing `PriceUpdate(...)` call and test still works. `to_dict()` gains one key, `day_change_percent`. This is an additive change to the SSE payload (Section 13, item 12).

### 5.2 `tickers.py` — normalization (new)

One rule for the whole backend. The API layer calls it to produce the `invalid_ticker` error. The sources call it again so they can never hold an un-normalized ticker, whoever calls them.

```python
"""Ticker normalization shared by the API layer and the data sources."""

from __future__ import annotations

import re

TICKER_PATTERN = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")


class InvalidTickerError(ValueError):
    """Raised when a ticker fails the format rule. Maps to `invalid_ticker` (400)."""

    def __init__(self, raw: str) -> None:
        super().__init__(f"'{raw}' is not a valid ticker symbol.")
        self.raw = raw


def normalize_ticker(raw: str) -> str:
    """Trim and uppercase a ticker, then validate it against TICKER_PATTERN."""
    ticker = raw.strip().upper()
    if not TICKER_PATTERN.fullmatch(ticker):
        raise InvalidTickerError(raw)
    return ticker
```

```python
>>> normalize_ticker("  brk.b ")
'BRK.B'
>>> normalize_ticker("AB-C")
InvalidTickerError: 'AB-C' is not a valid ticker symbol.
```

### 5.3 `cache.py` — `PriceCache`

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
        self._version: int = 0  # Bumped on every update and every removal

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        reference_price: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        reference_price is the day-change baseline. When omitted, the previous
        baseline is kept; the first update for a ticker uses its own price.
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if reference_price is None:
                reference_price = prev.reference_price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
                reference_price=round(reference_price, 2),
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
        """Remove a ticker. Bumps the version so SSE clients see the removal."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter, for SSE change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

Baseline behaviour, worked through:

```python
cache = PriceCache()
cache.update("AAPL", 100.00)                          # first update: reference = 100.00
cache.update("AAPL", 101.00).day_change_percent       # 1.0   (reference kept)
cache.update("AAPL", 99.00, reference_price=98.00)    # Massive supplies prev close
cache.get("AAPL").day_change_percent                  # 1.02
cache.get("AAPL").change                              # -2.0  (tick-to-tick, vs 101.00)
```

**Why a version counter?** The SSE loop checks the cache every 500 ms. If the version has not moved since the last check, it skips serialization and sends nothing. In Massive mode this means one event per poll instead of 30 identical ones.

**Why `threading.Lock`?** The Massive worker thread is a real OS thread; an `asyncio.Lock` would not protect against it. Critical sections are a dict lookup plus an assignment, so contention is negligible at this scale (tens of tickers, a handful of SSE clients).

### 5.4 `interface.py` — `MarketDataSource`

The method set is unchanged. The docstrings now state the normalization and pricing guarantees that the API layer relies on.

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never asks the source for prices; it reads the cache.

    All ticker arguments are normalized with normalize_ticker(); an invalid
    ticker raises InvalidTickerError.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts the background task. A second call while running is a no-op.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Safe to call more than once.

        After stop() returns, the source never writes to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        Simulator: the ticker is in the cache when this returns.
        Massive: the ticker is priced on the next successful poll.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set and from the PriceCache.

        No-op if not present. No later write re-adds it to the cache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the currently tracked tickers, in insertion order."""
```

### 5.5 `__init__.py` — public API

```python
"""Market data subsystem for FinAlly."""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router
from .tickers import InvalidTickerError, normalize_ticker

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
    "InvalidTickerError",
    "normalize_ticker",
]
```

---

## 6. GBM Simulator

### 6.1 Model

Each tick, every ticker evolves by geometric Brownian motion:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

| Symbol | Meaning | Value |
|---|---|---|
| `μ` | Annualized drift | 0.03–0.08 per ticker |
| `σ` | Annualized volatility | 0.17 (V) to 0.50 (TSLA) |
| `dt` | Tick length as a fraction of a trading year: `0.5 / (252 × 6.5 × 3600)` | `8.48e-8` |
| `Z` | Correlated standard normal (6.3) | |

`exp()` is always positive, so prices can never reach zero or go negative. The `−σ²/2` term makes the expected price grow at `μ` (the Itô correction).

### 6.2 What it looks like on screen

The simulator runs on wall-clock time, 24/7, treating every real second as a trading second.

| Ticker | σ | Typical move per tick (1 s.d.) | Typical move per hour of wall time |
|---|---|---|---|
| V | 0.17 | 0.005 % ≈ $0.014 on $280 | 0.42 % |
| AAPL | 0.22 | 0.006 % ≈ $0.012 on $190 | 0.54 % |
| NVDA | 0.40 | 0.012 % ≈ $0.093 on $800 | 0.99 % |
| TSLA | 0.50 | 0.015 % ≈ $0.036 on $250 | 1.24 % |

Per-tick s.d. = `σ·√dt = σ × 2.91e-4`; per-hour s.d. = `σ·√(3600/5,896,800) = σ × 0.0247`.

Because prices are rounded to cents, a low-volatility ticker sometimes shows a "flat" tick. This is expected.

**Random events.** Each tick, each ticker has probability `p = 0.001` of a shock of ±2–5 %. At 2 ticks/s with 10 tickers, that is 0.02 events/s, or **about one every 50 seconds** somewhere on the board. That is frequent enough for drama without wrecking the random walk.

### 6.3 Correlation

`Z = L · z`, where `z` is a vector of independent N(0,1) draws and `L` is the Cholesky factor of the correlation matrix `C` (`C = L·Lᵀ`).

| Pair | ρ |
|---|---|
| Both in tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Both in finance (JPM, V) | 0.5 |
| Anything with TSLA | 0.3 |
| Cross-sector, or any unknown ticker | 0.3 |

**Cholesky always succeeds for this structure**, whatever tickers are added. `C` decomposes as `0.3·J + 0.3·B_tech + 0.2·B_fin + D`. Here `J` is the all-ones matrix and `B_tech`/`B_fin` are all-ones blocks on their groups (all positive semi-definite). `D` is diagonal with entries 0.4 (tech), 0.5 (finance) or 0.7 (others), all strictly positive. The sum is therefore positive definite. TSLA is deliberately excluded from the tech block even though it is listed in the tech group; this preserves the argument. Anyone who edits the coefficients should keep `base + group bonus < 1` for each group.

Rebuilding `L` is O(n³) via numpy, but runs only on add or remove. For n < 50 it takes microseconds.

### 6.4 `seed_prices.py` (unchanged)

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

A ticker not in `SEED_PRICES` (for example `PYPL` or `ZZZZ`) starts at a uniform random price between $50 and $300 and uses `DEFAULT_PARAMS`. Section 13, item 11 discusses whether that is acceptable.

### 6.5 `simulator.py`

`GBMSimulator` is the pure, synchronous math engine. `SimulatorDataSource` wraps it in an asyncio task and writes to the cache. Compared with the current file, the changes are in `SimulatorDataSource` only: normalization, the idempotent `start()`, and the `add_ticker()` membership guard (C6, C11).

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
from .tickers import normalize_ticker

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8, one 500ms tick

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
        """Advance all tickers by one time step. Returns {ticker: new_price}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~one every 50s across 10 tickers at 2 ticks/sec
            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock
                logger.debug("Random event on %s: %+.1f%%", ticker, shock * 100)

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
        """Add a ticker without rebuilding Cholesky (for batch initialization)."""
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
                corr[i, j] = corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        if t1 == "TSLA" or t2 == "TSLA":  # In the tech set, but behaves independently
            return TSLA_CORR
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Calls GBMSimulator.step() every `update_interval` seconds and writes the
    results to the PriceCache.
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
        if self._task is not None:
            return
        tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache so SSE has data immediately. The seed becomes each
        # ticker's day baseline (first price seen since server start).
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
        ticker = normalize_ticker(ticker)
        if self._sim and ticker not in self._sim.get_tickers():
            self._sim.add_ticker(ticker)
            # Price it immediately so a trade right after the add can fill
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        """Step the simulation, write to the cache, sleep. Never dies on one bad step."""
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Why the simulator is safe without locks:** `step()`, `add_ticker()` and `remove_ticker()` all run on the event loop and contain no `await`, so they can never interleave. A ticker removed between two steps is simply absent from the next `step()` result, so it can never be written back to the cache.

**Example session:**

```python
cache = PriceCache()
sim = SimulatorDataSource(cache)
await sim.start(["AAPL", "TSLA"])
cache.get("AAPL")
# PriceUpdate(ticker='AAPL', price=190.0, previous_price=190.0, timestamp=1791100800.0,
#             reference_price=190.0)                      -> direction 'flat', day change 0.0
await sim.add_ticker("pypl")      # normalized to PYPL, random seed in [50, 300]
cache.get_price("PYPL")           # e.g. 137.42 (available immediately)
await asyncio.sleep(1)
cache.get("AAPL").to_dict()
# {'ticker': 'AAPL', 'price': 190.03, 'previous_price': 190.01, 'timestamp': 1791100801.0,
#  'change': 0.02, 'change_percent': 0.0105, 'direction': 'up', 'day_change_percent': 0.02}
await sim.stop()
```

---

## 7. Massive API Poller

### 7.1 Endpoint and budget

One request per poll covers every watched ticker:

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
Authorization: Bearer <MASSIVE_API_KEY>
```

The Python client sends this request through `RESTClient.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])`, which joins the list into the comma-separated parameter.

| Plan | Limit | Interval | Requests/min |
|---|---|---|---|
| Free | 5 req/min | 15 s (default) | 4 |
| Paid | effectively unlimited | 2–5 s (`MASSIVE_POLL_INTERVAL`) | 12–30 |

The free tier's 5 req/min leaves a margin of one request per minute. Nothing else in the backend may call Massive while the poller runs on the free tier.

> **Plan coverage:** snapshot endpoints are not included in every Massive plan. When the key's plan lacks them, each poll fails with a `BadResponse` whose body carries the API's `NOT_AUTHORIZED` status, and the poller logs it (7.5). Check this with a real key before relying on Massive mode.

### 7.2 Response shape (verified against the `massive` package)

Raw JSON for one ticker (inside the top-level `"tickers"` array):

```json
{
  "ticker": "AAPL",
  "todaysChange": 1.24,
  "todaysChangePerc": 0.65,
  "updated": 1791100800123456789,
  "day":     {"o": 190.10, "h": 192.00, "l": 189.50, "c": 191.20, "v": 41000000, "vw": 190.80},
  "prevDay": {"o": 188.00, "h": 191.00, "l": 187.20, "c": 190.00, "v": 52000000, "vw": 189.50},
  "min":     {"o": 191.10, "h": 191.30, "l": 191.00, "c": 191.20, "v": 120000, "t": 1791100740000},
  "lastTrade": {"p": 191.24, "s": 100, "x": 4, "t": 1791100800123456789, "i": "52983525"},
  "lastQuote": {"p": 191.23, "P": 191.25, "s": 2, "S": 3, "t": 1791100800120000000}
}
```

How `TickerSnapshot.from_dict` exposes it, and what we use:

| Python attribute | Raw key | Unit | Used for |
|---|---|---|---|
| `snap.ticker` | `ticker` | — | cache key |
| `snap.last_trade.price` | `lastTrade.p` | USD | **price** (first choice) |
| `snap.last_trade.sip_timestamp` | `lastTrade.t` | **nanoseconds** | timestamp (first choice) |
| `snap.min.close` | `min.c` | USD | price fallback 1 |
| `snap.day.close` | `day.c` | USD | price fallback 2 |
| `snap.prev_day.close` | `prevDay.c` | USD | **day baseline**; price fallback 3 |
| `snap.updated` | `updated` | nanoseconds | timestamp fallback |
| `snap.todays_change_percent` | `todaysChangePerc` | % | not used (we derive it from `prev_day.close` so both sources compute it the same way) |

There is **no** `last_trade.timestamp`, `day.previous_close`, or `day.change_percent` on the stock snapshot model. All three appear in `archive/MASSIVE_API.md`, and the first of them causes C1.

Every sub-object is `Optional`. `last_trade` is missing for some plans and tickers. Snapshot data is cleared at 12 am ET and refills from about 4 am ET, so in the early morning `day` can be empty or zero. This is why the price falls back through several fields.

### 7.3 Parsing — pure functions

The parsing is kept out of the class so it can be tested against real `TickerSnapshot` objects without any mocks.

```python
class ParsedSnapshot(NamedTuple):
    ticker: str
    price: float
    timestamp: float | None  # Unix seconds; None -> cache uses time.time()
    reference_price: float | None  # Previous close; None -> cache keeps its baseline


def _positive(value: Any) -> float | None:
    """Return value as a float if it is a positive number, else None."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None


def to_unix_seconds(ts: Any) -> float | None:
    """Convert a Massive timestamp to Unix seconds.

    Snapshot trade timestamps are nanoseconds; aggregate bars use milliseconds.
    The unit is inferred from magnitude so either is handled.
    """
    ts = _positive(ts)
    if ts is None:
        return None
    if ts > 1e17:
        return ts / 1e9  # nanoseconds
    if ts > 1e14:
        return ts / 1e6  # microseconds
    if ts > 1e11:
        return ts / 1e3  # milliseconds
    return ts


def parse_snapshot(snap: Any) -> ParsedSnapshot | None:
    """Extract price, timestamp and day baseline from a massive TickerSnapshot.

    Price falls back from the last trade to the latest minute bar, today's bar,
    and finally the previous day's close (snapshots are cleared at midnight ET).
    Returns None when the snapshot carries no usable price.
    """
    ticker = getattr(snap, "ticker", None)
    if not ticker:
        return None

    last_trade = getattr(snap, "last_trade", None)
    minute = getattr(snap, "min", None)
    day = getattr(snap, "day", None)
    prev_day = getattr(snap, "prev_day", None)

    price = (
        _positive(getattr(last_trade, "price", None))
        or _positive(getattr(minute, "close", None))
        or _positive(getattr(day, "close", None))
        or _positive(getattr(prev_day, "close", None))
    )
    if price is None:
        return None

    timestamp = to_unix_seconds(getattr(last_trade, "sip_timestamp", None)) or to_unix_seconds(
        getattr(snap, "updated", None)
    )
    reference_price = _positive(getattr(prev_day, "close", None))
    return ParsedSnapshot(ticker.upper(), price, timestamp, reference_price)
```

Unit inference works because a current Unix time is about 1.8 × 10⁹ s, 1.8 × 10¹² ms, 1.8 × 10¹⁵ µs, or 1.8 × 10¹⁸ ns. The thresholds sit between those orders of magnitude and stay valid for centuries.

```python
>>> parse_snapshot(TickerSnapshot.from_dict(RAW_AAPL))   # the JSON in 7.2
ParsedSnapshot(ticker='AAPL', price=191.24, timestamp=1791100800.1234567, reference_price=190.0)
>>> parse_snapshot(TickerSnapshot.from_dict({"ticker": "AAPL", "prevDay": {"c": 190.0}, "day": {"c": 0}}))
ParsedSnapshot(ticker='AAPL', price=190.0, timestamp=None, reference_price=190.0)
>>> parse_snapshot(TickerSnapshot.from_dict({"ticker": "ZZZZ"}))
None
```

### 7.4 `massive_client.py`

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, NamedTuple

from massive import RESTClient
from massive.exceptions import AuthError, BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .tickers import normalize_ticker

logger = logging.getLogger(__name__)

# ParsedSnapshot, _positive, to_unix_seconds, parse_snapshot: see 7.3


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls the multi-ticker snapshot endpoint for all watched tickers in a
    single API call, then writes results to the PriceCache.
    """

    MAX_BACKOFF = 120.0  # Seconds; cap for the delay after repeated failures
    STARTUP_POLL_TIMEOUT = 10.0  # Seconds; start() never blocks app startup longer

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
        self._failures = 0  # Consecutive failed polls, drives backoff
        self._unresolved: set[str] = set()  # Requested but absent from the last response

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            return
        self._client = RESTClient(api_key=self._api_key, connect_timeout=5.0, read_timeout=10.0)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))

        # Immediate first poll so the cache has data right away, bounded so a
        # slow or unreachable API cannot hold up application startup.
        try:
            await asyncio.wait_for(self._poll_once(), timeout=self.STARTUP_POLL_TIMEOUT)
        except TimeoutError:
            logger.warning("Massive: first poll timed out; continuing in the background")

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
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (priced on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._unresolved.discard(ticker)
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    def _next_delay(self) -> float:
        """Poll interval, doubled per consecutive failure up to MAX_BACKOFF."""
        if self._failures == 0:
            return self._interval
        return min(self._interval * 2 ** min(self._failures, 4), self.MAX_BACKOFF)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._next_delay())
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        tickers = list(self._tickers)  # Copy: the worker thread must not see mutations
        if not tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous; run it in a thread so the
            # event loop (and every SSE stream) keeps running.
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except AuthError:
            self._failures += 1
            logger.error("Massive poll failed: MASSIVE_API_KEY is empty or invalid")
            return
        except BadResponse as e:
            # Body is the API's JSON error, e.g. NOT_AUTHORIZED (plan lacks
            # snapshots) or a rate-limit message on the free tier.
            self._failures += 1
            logger.error("Massive poll rejected: %s", str(e)[:200])
            return
        except Exception as e:
            self._failures += 1
            logger.error("Massive poll failed: %s", e)
            return

        self._failures = 0
        seen: set[str] = set()
        for snap in snapshots:
            parsed = parse_snapshot(snap)
            if parsed is None:
                logger.warning("Massive: no usable price for %s", getattr(snap, "ticker", "???"))
                continue
            # A ticker removed while this request was in flight must not
            # reappear in the cache.
            if parsed.ticker not in self._tickers:
                continue
            self._cache.update(
                ticker=parsed.ticker,
                price=parsed.price,
                timestamp=parsed.timestamp,
                reference_price=parsed.reference_price,
            )
            seen.add(parsed.ticker)

        missing = set(tickers) - seen
        for ticker in sorted(missing - self._unresolved):
            logger.warning("Massive: no data for %s (unknown or delisted symbol?)", ticker)
        self._unresolved = missing
        logger.debug("Massive poll: updated %d/%d tickers", len(seen), len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Synchronous call to the Massive REST API. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
```

### 7.5 Failure behaviour

| Situation | What happens | User-visible result |
|---|---|---|
| Invalid or empty key | `AuthError` / `BadResponse` (401). Logged; backoff 30 → 60 → 120 s | No prices; watchlist shows `null` prices; trades return `price_unavailable` |
| Plan lacks snapshots | `BadResponse` with `NOT_AUTHORIZED`. Logged with the body | Same as above |
| 429 rate limit | `BadResponse`; backoff until a poll succeeds, then back to the normal interval | Prices update less often for a while |
| Network error or timeout | urllib3 retries (3 by default), then logged; backoff | Last-known prices stay in the cache and keep streaming |
| API unreachable at startup | First poll abandoned after 10 s; the loop keeps trying | App starts; prices appear when the API answers |
| One malformed snapshot | That ticker is skipped with a warning; the others are processed | One ticker stale |
| Unknown symbol (`ZZZZ`) | Absent from the response. Warned once, tracked in `_unresolved` | `null` price forever; trades return `price_unavailable` (see 13, item 11) |
| Ticker removed mid-poll | Membership check drops the late result | Removal sticks |
| Market closed | Same price every poll. Version still bumps, so SSE re-sends identical data; `direction` is `"flat"` | No flashing, flat sparklines (expected; 13, item 14) |

The backoff schedule from the default 15 s interval is `15 → 30 → 60 → 120 → 120 …` seconds. One successful poll resets it.

---

## 8. Factory and Configuration

### 8.1 `factory.py`

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

DEFAULT_POLL_INTERVAL = 15.0  # Seconds; safe for the free tier (5 req/min)
MIN_POLL_INTERVAL = 2.0


def _poll_interval() -> float:
    """Read MASSIVE_POLL_INTERVAL, falling back to the free-tier default."""
    raw = os.environ.get("MASSIVE_POLL_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL
    try:
        value = float(raw)
    except ValueError:
        logger.warning("Ignoring invalid MASSIVE_POLL_INTERVAL=%r", raw)
        return DEFAULT_POLL_INTERVAL
    return max(value, MIN_POLL_INTERVAL)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the market data source selected by the environment.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real market data)
    - Otherwise -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        interval = _poll_interval()
        logger.info("Market data source: Massive API (poll every %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)

    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

| `MASSIVE_API_KEY` | `MASSIVE_POLL_INTERVAL` | Result |
|---|---|---|
| unset / `""` / `"   "` | any | `SimulatorDataSource`, 0.5 s ticks |
| `"abc…"` | unset | `MassiveDataSource`, 15 s |
| `"abc…"` | `"5"` | `MassiveDataSource`, 5 s |
| `"abc…"` | `"0.5"` | `MassiveDataSource`, clamped to 2 s |
| `"abc…"` | `"fast"` | `MassiveDataSource`, 15 s plus a warning |

The factory reads `os.environ` only. Getting the root `.env` into the environment is the caller's job: Docker uses `--env-file .env`, and a local run uses `uv run --env-file ../.env uvicorn app.main:app` (Review.md, P2).

### 8.2 Tunables

| Parameter | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | `""` | Selects the source |
| `MASSIVE_POLL_INTERVAL` | env (new) | `15` s | Floor 2 s. Add to `.env.example` as a commented line |
| `update_interval` | `SimulatorDataSource` | 0.5 s | Matches the SSE cadence |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | 0.001 | Set to 0 in tests that need smooth paths |
| `dt` | `GBMSimulator` | 8.48e-8 | One 500 ms tick of trading time |
| `MAX_BACKOFF` | `MassiveDataSource` | 120 s | |
| `STARTUP_POLL_TIMEOUT` | `MassiveDataSource` | 10 s | |
| `PUSH_INTERVAL` | `stream.py` | 0.5 s | SSE change check |
| `KEEPALIVE_INTERVAL` | `stream.py` | 15 s | SSE comment on an idle stream |
| `retry:` | `stream.py` | 1000 ms | EventSource reconnect delay |

---

## 9. SSE Streaming Endpoint

### 9.1 `stream.py`

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

PUSH_INTERVAL = 0.5  # Seconds between change checks
KEEPALIVE_INTERVAL = 15.0  # Seconds of silence before a comment line is sent


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE router bound to a price cache. Each call returns a new router."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """Stream every tracked ticker's price whenever any price changes."""
        return StreamingResponse(
            generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = PUSH_INTERVAL,
    keepalive: float = KEEPALIVE_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted events until the client disconnects."""
    yield "retry: 1000\n\n"

    last_version = -1
    last_sent = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(data)}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= keepalive:
                # Comment line: ignored by EventSource, keeps proxies from
                # closing an idle connection (e.g. an empty watchlist).
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    logger.info("SSE client disconnected: %s", client_ip)
```

When the cache is empty but its version moved (the last ticker was just removed), the stream sends `data: {}`. The frontend then clears its last row instead of keeping a stale one.

### 9.2 Wire format

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 191.24, "previous_price": 191.1, "timestamp": 1791100800.12, "change": 0.14, "change_percent": 0.0733, "direction": "up", "day_change_percent": 0.65}, "MSFT": {...}}

: keepalive

```

Every event carries **every** tracked ticker, so the frontend never needs to merge partial updates. A ticker that disappears from the payload has been removed.

### 9.3 Client usage (for the frontend agent)

```typescript
type Direction = "up" | "down" | "flat";
interface PriceTick {
  ticker: string; price: number; previous_price: number; timestamp: number;
  change: number; change_percent: number; direction: Direction;
  day_change_percent: number | null;
}
type Status = "connected" | "reconnecting" | "disconnected";

export function subscribePrices(
  onPrices: (prices: Record<string, PriceTick>) => void,
  onStatus: (s: Status) => void,
): () => void {
  const es = new EventSource("/api/stream/prices");
  const report = () =>
    onStatus(
      es.readyState === EventSource.OPEN ? "connected"
      : es.readyState === EventSource.CONNECTING ? "reconnecting"
      : "disconnected",
    );
  es.onopen = report;
  es.onerror = report; // fires on drop; readyState is CONNECTING while it retries
  es.onmessage = (e) => onPrices(JSON.parse(e.data));
  return () => es.close();
}
```

This maps green, yellow and red to `OPEN`, `CONNECTING` and `CLOSED`, as PLAN.md §13.2 item 30 proposes. Sparklines append `price` per ticker on every message. The flash animation keys off `direction`.

---

## 10. Application Integration

This section covers the market-facing parts of `app/main.py` and the routers. Database helpers (`init_db`, `get_watchlist_tickers`, …) are owned by the DB layer. Only their signatures matter here.

### 10.1 Lifespan

Routers are registered at import time, before the lifespan runs. `create_stream_router()` needs a concrete cache, so the cache is created at module level and shared by the router and the lifespan. Everything that has to wait for the database (the source, the startup snapshot) happens in the lifespan.

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import api_router  # portfolio, watchlist, chat, health
from app.db import get_watchlist_tickers, init_db, record_portfolio_snapshot
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database first: the source is seeded from the watchlist (PLAN 13.2 item 8)
    init_db()

    # 2. Source, started with the persisted watchlist
    source = create_market_data_source(price_cache)
    await source.start(get_watchlist_tickers())
    app.state.price_cache = price_cache
    app.state.market_source = source

    # 3. Startup snapshot. Simulator: every ticker is priced already.
    #    Massive: priced if the first poll succeeded; valuation falls back to
    #    avg_cost for any held ticker without a price (10.4).
    record_portfolio_snapshot(price_cache)

    yield

    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
app.include_router(api_router)
app.mount("/", StaticFiles(directory="static", html=True), name="static")  # last
```

The static mount comes last, so `/api/*` routes always win and `/` serves `index.html` (PLAN.md §13.2 item 31).

### 10.2 Dependencies

```python
# app/deps.py
from fastapi import Request

from app.market import MarketDataSource, PriceCache


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

Pass these to routes with `Depends(...)`. In tests, `app.dependency_overrides` swaps in a pre-filled `PriceCache` and a fake source.

### 10.3 Watchlist endpoints

The error type is owned by the API layer, as PLAN.md §8 "Errors" requires. It is repeated here so the snippets are complete:

```python
class ApiError(Exception):
    def __init__(self, code: str, status: int, message: str) -> None:
        self.code, self.status, self.message = code, status, message
# A FastAPI exception handler renders {"error": {"code": ..., "message": ...}}.
```

Shared builder for a watchlist entry, matching the `GET /api/watchlist` element shape:

```python
def watchlist_entry(ticker: str, added_at: str, cache: PriceCache) -> dict:
    update = cache.get(ticker)
    return {
        "ticker": ticker,
        "price": update.price if update else None,
        "previous_price": update.previous_price if update else None,
        "direction": update.direction if update else None,
        "day_change_percent": update.day_change_percent if update else None,
        "added_at": added_at,
    }
```

Add (`POST /api/watchlist`): normalize, insert, then register with the source.

```python
@router.post("/watchlist")
async def add_watchlist(
    body: WatchlistAdd,
    response: Response,
    cache: PriceCache = Depends(get_price_cache),
    source: MarketDataSource = Depends(get_market_source),
):
    try:
        ticker = normalize_ticker(body.ticker)
    except InvalidTickerError as e:
        raise ApiError("invalid_ticker", 400, str(e))

    added_at, created = db.add_watchlist_ticker(ticker)    # idempotent insert
    await source.add_ticker(ticker)                        # no-op if already tracked
    response.status_code = 201 if created else 200
    return watchlist_entry(ticker, added_at, cache)
```

In simulator mode the response already carries a price. In Massive mode `price` is `null` until the next poll.

Remove (`DELETE /api/watchlist/{ticker}`): the position check comes before any change.

```python
@router.delete("/watchlist/{ticker}", status_code=204)
async def remove_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    try:
        ticker = normalize_ticker(ticker)
    except InvalidTickerError as e:
        raise ApiError("invalid_ticker", 400, str(e))

    if not db.is_watched(ticker):
        raise ApiError("not_watched", 404, f"{ticker} is not on your watchlist.")
    if db.get_position_quantity(ticker) > 0:
        raise ApiError("position_open", 409, f"Sell your {ticker} shares before removing it.")

    db.remove_watchlist_ticker(ticker)
    await source.remove_ticker(ticker)                     # also clears the cache
```

**Order of operations.** Writing the database first means a crash between the two steps leaves the database correct, and the source is re-seeded from the database on the next start. Neither source method can fail for a normalized ticker, so no rollback is needed.

**Chat actions** (PLAN.md §9) call these same functions, minus the HTTP wrapper, in the fixed order: additions, trades, removals.

### 10.4 Trade execution and valuation

```python
def fill_price(ticker: str, cache: PriceCache) -> float:
    """Price for a market order. Watchlist membership is checked before this."""
    price = cache.get_price(ticker)
    if price is None:
        raise ApiError(
            "price_unavailable", 409,
            f"No price for {ticker} yet. Try again in a few seconds.",
        )
    return price
```

Rule order from PLAN.md §8: `ticker_not_watched` (database) → `price_unavailable` (`fill_price`) → cash or shares.

Read the price **once** per trade and use that value for the cost check, the cash debit, the position update and the trade row. The cache may move during the request.

Portfolio valuation reads `cache.get_price(ticker)` per position. If it gets `None`, which is possible only in Massive mode before the first successful poll, it values the position at `avg_cost` (unrealized P&L 0). This way `GET /api/portfolio` and the startup snapshot never fail. The 409 on watchlist removal guarantees that every held ticker is tracked, so `None` is transient.

### 10.5 LLM context

The chat handler builds its portfolio context from the same `watchlist_entry()` and valuation helpers, so the LLM sees exactly what the UI shows, `null` prices included.

---

## 11. Optional Extension: Server-Side Price History

This addresses PLAN.md §13.2 item 13: charts empty on a page refresh. It is **not** in the current contract. Build it only if that item is resolved as "keep history on the server".

```python
# app/market/history.py
from collections import deque
from threading import Lock

from .models import PriceUpdate


class PriceHistory:
    """Rolling per-ticker (timestamp, price) buffer. 600 points = 5 minutes at 500 ms."""

    def __init__(self, maxlen: int = 600) -> None:
        self._maxlen = maxlen
        self._points: dict[str, deque[tuple[float, float]]] = {}
        self._lock = Lock()

    def record(self, update: PriceUpdate) -> None:
        with self._lock:
            buf = self._points.setdefault(update.ticker, deque(maxlen=self._maxlen))
            buf.append((update.timestamp, update.price))

    def get(self, ticker: str) -> list[tuple[float, float]]:
        with self._lock:
            return list(self._points.get(ticker, ()))

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._points.pop(ticker, None)
```

Wiring: give `PriceCache` an optional `history: PriceHistory | None`. Call `history.record(update)` at the end of `update()` and `history.remove(ticker)` in `remove()`. Expose `GET /api/stream/history` → `{"AAPL": [[ts, price], …], …}`, which the frontend fetches once before opening the `EventSource`. Memory use is bounded: 50 tickers × 600 points × ~100 bytes ≈ 3 MB.

---

## 12. Testing

### 12.1 What changes in the existing suite

- `tests/market/test_massive.py`: the `_make_snapshot()` helper builds a `MagicMock` with `last_trade.timestamp`. That attribute does not exist on the real model, which is how C1 went unnoticed. **Replace it with real model objects:**

  ```python
  from massive.rest.models import TickerSnapshot

  def _make_snapshot(ticker: str, price: float, ts_ns: int = 1_791_100_800_000_000_000,
                     prev_close: float | None = None) -> TickerSnapshot:
      raw = {"ticker": ticker, "lastTrade": {"p": price, "s": 100, "t": ts_ns}}
      if prev_close is not None:
          raw["prevDay"] = {"c": prev_close}
      return TickerSnapshot.from_dict(raw)
  ```

  `test_timestamp_conversion` then asserts `1791100800.0` from a nanosecond input. Patches of `_fetch_snapshots` keep working; it now takes the ticker list as an argument, which `return_value` ignores.

- Everything else passes unchanged against the new code (checked: 83 of 84; the one failure is the mock-based timestamp test above).

### 12.2 New tests

| File | Test | Guards |
|---|---|---|
| `test_massive.py` | `parse_snapshot` on the full JSON from 7.2 → price 191.24, baseline 190.0, ts ≈ 1791100800.123 | C1, C2 |
| `test_massive.py` | Price fallbacks: only `prevDay` → its close; no prices → `None` | 7.3 |
| `test_massive.py` | `to_unix_seconds` for s / ms / µs / ns, `None`, `0` | 7.3 |
| `test_massive.py` | Poll with `["AAPL", "ZZZZ"]`, response has AAPL only → AAPL cached with `day_change_percent == 0.65`, `_unresolved == {"ZZZZ"}` | 7.4, item 11 |
| `test_massive.py` | Removal during an in-flight poll (fetch sleeps 100 ms in its thread) → ticker absent afterwards | C4 |
| `test_massive.py` | Five failures in a row → delays `[30, 60, 120, 120, 120]` | C8 |
| `test_cache.py` | Baseline: first price, kept, overridden; `remove()` bumps the version once, a second `remove()` does not | C2, C3 |
| `test_tickers.py` | `"  brk.b "` → `"BRK.B"`; `""`, `"1ABC"`, `"AB-C"`, 11 chars → `InvalidTickerError` | C6 |
| `test_simulator_source.py` | `start(["aapl", "AAPL"])` → one ticker; re-adding does not bump the version; seed is the baseline | C6, C11 |
| `test_stream.py` | `generate_events` with a fake request: first `retry`, then `data:` including `day_change_percent`, then `: keepalive` | 9 |
| `test_stream.py` | Two `create_stream_router()` calls → distinct routers, one route each | C9 |
| `test_simulator.py` | Cholesky succeeds for the 10 defaults plus 40 unknown tickers | 6.3 |

The SSE generator is tested directly with a stub request, which avoids the difficulty of an infinite streaming response in a test client:

```python
class FakeRequest:
    client = None

    def __init__(self, polls: int) -> None:
        self.polls = polls

    async def is_disconnected(self) -> bool:
        self.polls -= 1
        return self.polls < 0


async def test_sse_events_and_keepalive():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = [
        e async for e in generate_events(cache, FakeRequest(3), interval=0.01, keepalive=0.015)
    ]
    assert events[0] == "retry: 1000\n\n"
    assert events[1].startswith('data: {"AAPL": {"ticker": "AAPL", "price": 190.0')
    assert '"day_change_percent": 0.0' in events[1]
    assert ": keepalive\n\n" in events[2:]
```

The in-flight removal test, the one that reproduces C4:

```python
async def test_removed_during_poll_not_readded():
    cache = PriceCache()
    src = MassiveDataSource("k", cache)
    src._tickers, src._client = ["AAPL"], MagicMock()

    def slow_fetch(tickers):
        time.sleep(0.1)  # runs in the worker thread
        return [TickerSnapshot.from_dict(RAW_AAPL)]

    with patch.object(src, "_fetch_snapshots", side_effect=slow_fetch):
        poll = asyncio.create_task(src._poll_once())
        await asyncio.sleep(0.02)
        await src.remove_ticker("AAPL")
        await poll
    assert cache.get("AAPL") is None
```

### 12.3 Manual check with a real key

```bash
cd backend
MASSIVE_API_KEY=... uv run python -c "
import asyncio, logging
from app.market import PriceCache, create_market_data_source
logging.basicConfig(level=logging.INFO)
async def main():
    cache = PriceCache(); src = create_market_data_source(cache)
    await src.start(['AAPL', 'MSFT', 'ZZZZ'])
    print({t: u.to_dict() for t, u in cache.get_all().items()})
    await src.stop()
asyncio.run(main())"
```

Expected output: AAPL and MSFT with prices and `day_change_percent`, and a one-time warning for ZZZZ. A `NOT_AUTHORIZED` error means the plan lacks snapshots (7.1).

---

## 13. Proposed Resolutions for PLAN.md Open Items

These are proposals. Per PLAN.md §13.3, write each one back into PLAN.md once accepted.

**Item 11: ticker validation on add.**
- *Simulator:* accept any well-formed symbol and give unknown ones a random $50–$300 seed. This is a simulated market, and refusing `PYPL` because it lacks a seed would hurt the demo.
- *Massive:* keep accepting, so the add stays fast and idempotent and needs no extra API call against the 5 req/min budget. The poller already tracks symbols that never come back (`_unresolved`) and logs them once.
- *Follow-up if wanted:* expose `_unresolved` through an `is_unresolved(ticker)` method and have `GET /api/watchlist` mark those entries. That would change the contract, so it is not proposed now.

**Item 12: `day_change_percent` baseline.** `PriceUpdate.reference_price`:
- *Massive:* the previous close (`prev_day.close`), refreshed every poll.
- *Simulator:* the first price seen since server start, i.e. the seed. It resets on restart and when a ticker is removed and re-added.

Also add `day_change_percent` to each SSE ticker object (additive), so the watchlist's "daily change %" updates live without polling `/api/watchlist`.

**Item 13: main chart history.** Accept "since page load" for the MVP. If that is rejected, Section 11 is a small, bounded extension.

**Item 14: Massive outside market hours.** Document it as expected behaviour. Prices hold at the last trade (or the previous close in the early morning), every tick is `flat`, and sparklines are flat. This is not a bug.

**Also new:** `MASSIVE_POLL_INTERVAL` (Section 8). Add it to PLAN.md §5 and `.env.example`.

---

## 14. Implementation Checklist

In dependency order. Each step leaves the suite green.

1. `tickers.py` (new) + `test_tickers.py`.
2. `models.py`: add `reference_price` and `day_change_percent`, plus the `to_dict` key. Update the model tests that assert the exact `to_dict()` output.
3. `cache.py`: `reference_price` parameter, baseline carry-over, version bump on `remove()`, locked `version` read. Add the cache tests.
4. `interface.py`: docstrings.
5. `simulator.py`: normalization, idempotent `start()`, `add_ticker()` guard. Add the simulator source tests.
6. `massive_client.py`: `ParsedSnapshot`, `to_unix_seconds`, `parse_snapshot`; rewrite `_poll_once`; copy of the list; membership check; `_unresolved`; backoff; bounded first poll. Rewrite the test helper with `TickerSnapshot.from_dict` and add the Massive tests.
7. `factory.py`: `MASSIVE_POLL_INTERVAL`. Add factory tests for the table in 8.1.
8. `stream.py`: router inside the factory, `generate_events` made public, keepalive. Add `test_stream.py`.
9. `__init__.py`: export `normalize_ticker` and `InvalidTickerError`.
10. `uv run --extra dev pytest && uv run --extra dev ruff check app tests`.
11. Update `MARKET_DATA_SUMMARY.md` and `backend/CLAUDE.md` with the new API surface. Mark `archive/MASSIVE_API.md` §"Key fields" as superseded by Section 7.2.
12. Write the Section 13 resolutions back into PLAN.md once accepted.
