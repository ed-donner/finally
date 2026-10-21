# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified
interface, the shared price cache, the GBM simulator, the Massive (Polygon.io)
REST client, the SSE stream, and how the rest of the backend plugs into them.

All code lives in `backend/app/market/`. Everything here is written against the
code that is already in the repo (73 tests passing) and the requirements in
`planning/PLAN.md` §6. Where this design **changes** the current code, the
section is tagged **Δ** and the reason is given. Untagged code matches what is
already implemented.

> Relationship to other docs: `archive/MARKET_DATA_DESIGN.md` is the original
> design (pre-review). `archive/MARKET_INTERFACE.md`, `archive/MARKET_SIMULATOR.md`
> and `archive/MASSIVE_API.md` are the research notes it was built from.
> `MARKET_DATA_SUMMARY.md` describes what was built. This document supersedes
> the archived design.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [File Layout](#3-file-layout)
4. [Change Log vs. Current Code (Δ)](#4-change-log-vs-current-code-δ)
5. [Data Model — `models.py`](#5-data-model--modelspy)
6. [Price Cache — `cache.py`](#6-price-cache--cachepy)
7. [Unified Interface — `interface.py`](#7-unified-interface--interfacepy)
8. [Seed Data — `seed_prices.py`](#8-seed-data--seed_pricespy)
9. [GBM Simulator — `simulator.py`](#9-gbm-simulator--simulatorpy)
10. [Massive API Client — `massive_client.py`](#10-massive-api-client--massive_clientpy)
11. [Factory — `factory.py`](#11-factory--factorypy)
12. [SSE Stream — `stream.py`](#12-sse-stream--streampy)
13. [Package Exports — `__init__.py`](#13-package-exports--__init__py)
14. [Integrating with the App](#14-integrating-with-the-app)
15. [Frontend Contract](#15-frontend-contract)
16. [Testing](#16-testing)
17. [Error Handling & Edge Cases](#17-error-handling--edge-cases)
18. [Configuration Reference](#18-configuration-reference)
19. [Implementation Checklist](#19-implementation-checklist)

---

## 1. Requirements

Traced from `PLAN.md`:

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Two sources (simulator, Massive) behind **one interface**; downstream code is source-agnostic | `MarketDataSource` ABC, §7 |
| R2 | Source chosen by env: `MASSIVE_API_KEY` non-empty → Massive, else simulator | `create_market_data_source()`, §11 |
| R3 | Simulator: GBM, per-ticker drift/vol, ~500 ms ticks, correlated moves, random 2–5 % events, realistic seeds | `GBMSimulator`, §9 |
| R4 | Massive: REST polling (not WebSocket) of the union of tracked tickers, one call per poll, 15 s on free tier | `MassiveDataSource`, §10 |
| R5 | Shared in-memory cache: latest price, previous price, timestamp per ticker | `PriceCache`, §6 |
| R6 | `GET /api/stream/prices` SSE, ~500 ms cadence; each event has ticker, price, previous price, timestamp, direction | `stream.py`, §12 |
| R7 | Watchlist shows **daily change %** | `PriceUpdate.day_change_percent` **Δ**, §5 |
| R8 | Trades fill at the current cached price; portfolio valued from cache | §14 |
| R9 | Supports future multi-user without data-layer changes | Cache is keyed by ticker only, not by user |

---

## 2. Architecture

```
                   ┌───────────────────────────── backend/app/market ─────────────────────────────┐
                   │                                                                              │
 MASSIVE_API_KEY ──┼─► create_market_data_source(cache)                                           │
                   │         │                                                                    │
                   │         ├─ key set ──► MassiveDataSource ── every 15 s ──┐                   │
                   │         │              (REST snapshot, in a thread)      │                   │
                   │         │                                                ▼                   │
                   │         └─ no key ───► SimulatorDataSource ─ every 0.5 s ► PriceCache        │
                   │                        (GBMSimulator.step())          (lock + version)      │
                   │                                                           │                  │
                   └───────────────────────────────────────────────────────────┼──────────────────┘
                                                                               │ read
                     ┌──────────────────────────┬──────────────────────────────┼─────────────────┐
                     ▼                          ▼                              ▼                 ▼
          /api/stream/prices (SSE)    POST /api/portfolio/trade      GET /api/portfolio   snapshot task
          every 0.5 s if version       fill at cache.get_price()     value positions      every 30 s
          changed
```

**Push into a cache, pull out of it.** Sources never return prices to callers.
Each runs its own background task and writes into `PriceCache` on its own clock
(0.5 s or 15 s). Every consumer reads the cache on *its* clock. This is what
makes downstream code agnostic: the SSE loop doesn't know or care whether the
last write was 0.5 s or 14 s ago.

**One writer, many readers.** Exactly one source is active per process. The
cache uses a `threading.Lock` (not `asyncio.Lock`) because the Massive client's
blocking HTTP call runs in a worker thread via `asyncio.to_thread`.

### Timeline of one simulator tick

```
t=0.000  simulator task: sim.step() → {"AAPL": 190.12, ...}; cache.update(...) x10; version 41→51
t=0.000  simulator task: await asyncio.sleep(0.5)
t=0.2xx  SSE task (per client): version 51 != 41 → get_all() → yield "data: {...}\n\n"
t=0.500  simulator task: next step ...
```

---

## 3. File Layout

```
backend/
  app/
    market/
      __init__.py          # Public API re-exports
      models.py            # PriceUpdate (+ normalize_ticker Δ)
      cache.py             # PriceCache
      interface.py         # MarketDataSource ABC
      seed_prices.py       # Seed prices, GBM params, correlation groups
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource (+ parse_snapshot Δ)
      factory.py           # create_market_data_source()
      stream.py            # create_stream_router() — SSE endpoint
  tests/
    market/
      test_models.py  test_cache.py  test_simulator.py  test_simulator_source.py
      test_massive.py  test_factory.py  test_stream.py (Δ new)
  market_data_demo.py      # Rich terminal demo: `uv run market_data_demo.py`
```

Dependencies (`backend/pyproject.toml`): `fastapi`, `uvicorn[standard]`,
`numpy` (simulator), `massive` (Massive SDK, imported at module top level).
Dev: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `pytest-cov`, `ruff`.

---

## 4. Change Log vs. Current Code (Δ)

| Δ | Severity | Change | Why |
|---|---|---|---|
| Δ1 | **Critical** | Read `last_trade.sip_timestamp` (nanoseconds), not `last_trade.timestamp` | The SDK's `LastTrade` model has **no `timestamp` attribute**. Against a real `TickerSnapshot`, every ticker raises `AttributeError` and is skipped, so **the cache is never populated in Massive mode**. Tests pass only because they use `MagicMock`, which invents any attribute. Verified with `TickerSnapshot.from_dict(...)`. |
| Δ2 | High | Price fallback chain: `last_trade.price` → `min.close` → `day.close` → `prev_day.close` | `last_trade` is `None` for some tickers/plans and before 4 am ET (snapshot data is cleared at midnight ET). |
| Δ3 | High | Add `session_open` + `day_change` / `day_change_percent` to `PriceUpdate` | PLAN §10 asks for *daily* change % in the watchlist. Today only tick-to-tick `change_percent` exists, which is ~0.00 % every tick. |
| Δ4 | Medium | One `normalize_ticker()` used by both sources | Massive upper-cases and strips; the simulator doesn't, so `"aapl"` and `"AAPL"` become two simulated tickers. |
| Δ5 | Medium | Pass an immutable copy of the ticker list into the worker thread | `_fetch_snapshots` reads `self._tickers` from a thread while the event loop may be rebinding it. |
| Δ6 | Medium | Build the `APIRouter` inside `create_stream_router()` | A module-level router gets a duplicate `/prices` route if the factory is called twice (review item 3.6; bites in tests). |
| Δ7 | Medium | SSE heartbeat comment every 15 s when nothing changed | In Massive mode the version only changes every 15 s; proxies and some browsers drop idle streams. |
| Δ8 | Low | `PriceCache`: `timestamp if timestamp is not None`, read `version` under the lock | `0.0` is a valid timestamp; consistency on free-threaded Python (review item 3.4). |
| Δ9 | Low | `MASSIVE_POLL_INTERVAL` env var; exponential backoff on poll errors | PLAN §6: "configurable interval". Backoff avoids hammering on 429. |
| Δ10 | Low | Cholesky fallback to independent draws on `LinAlgError` | Defensive: a future custom correlation could make the matrix non-PSD and kill the feed. |
| Δ11 | Low | Massive tests build snapshots with `TickerSnapshot.from_dict()` | Would have caught Δ1. |

---

## 5. Data Model — `models.py`

`PriceUpdate` is the only type that leaves the market layer. It is a frozen,
slotted dataclass; derived fields are properties so they can never disagree
with the stored prices.

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker form used everywhere: stripped, upper-case. (Δ4)"""
    return ticker.strip().upper()


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds
    session_open: float | None = None  # Reference price for daily change (Δ3)

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

    @property
    def day_change(self) -> float:
        """Absolute change since the session reference price. (Δ3)"""
        if self.session_open is None:
            return 0.0
        return round(self.price - self.session_open, 4)

    @property
    def day_change_percent(self) -> float:
        """Percentage change since the session reference price. (Δ3)"""
        if not self.session_open:
            return 0.0
        return round((self.price - self.session_open) / self.session_open * 100, 4)

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
            "session_open": self.session_open,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
        }
```

### What `session_open` means per source

| Source | `session_open` value | Notes |
|---|---|---|
| Simulator | The seed price at the moment the ticker started being simulated | So "daily change" = change since app start (or since the ticker was added). |
| Massive | `prev_day.close` from the snapshot | Matches how brokers show "today's change". Falls back to `day.open`. |

### Example

```python
>>> u = PriceUpdate("AAPL", price=191.90, previous_price=191.88, timestamp=1707580800.5,
...                 session_open=190.00)
>>> u.direction, u.change, u.day_change, u.day_change_percent
('up', 0.02, 1.9, 1.0)
>>> u.to_dict()["day_change_percent"]
1.0
```

---

## 6. Price Cache — `cache.py`

The cache holds **one** `PriceUpdate` per ticker (memory is O(tickers), no
history — sparklines are accumulated on the frontend from the stream). A
monotonically increasing `version` lets the SSE loop skip identical payloads.

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

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        session_open: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        First update for a ticker: previous_price == price (direction 'flat').
        session_open: if omitted, the previous value is kept; for a brand-new
        ticker it defaults to the first price seen.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()  # Δ8
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if session_open is None:
                session_open = prev.session_open if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
                session_open=round(session_open, 2),
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
        with self._lock:  # Δ8
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Usage

```python
cache = PriceCache()
cache.update("AAPL", 190.00)                      # flat, session_open=190.00
cache.update("AAPL", 190.25)                      # up, change=0.25
cache.get("AAPL").day_change_percent              # 0.1316
cache.get_price("MSFT")                           # None — never priced
{t: u.price for t, u in cache.get_all().items()}  # {'AAPL': 190.25}
```

**Why `threading.Lock`:** `asyncio.Lock` only coordinates coroutines on one
loop; it does nothing for the OS thread `asyncio.to_thread` uses. The critical
sections are a dict lookup and assignment, so contention is negligible.

**Why prices are rounded here:** one place guarantees 2-dp prices for every
source, so trade fills, the DB and the UI agree to the cent.

---

## 7. Unified Interface — `interface.py`

The contract both sources implement. It is deliberately small: lifecycle plus
the tracked-ticker set. It has **no** `get_price()` — prices come from the cache.

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

### Contract details every implementation must honour

| Rule | Simulator | Massive |
|---|---|---|
| Tickers are normalized with `normalize_ticker()` on every entry point (Δ4) | ✓ | ✓ |
| `start()` seeds the cache before returning when it can | Seeds all tickers instantly | Does one blocking poll before returning |
| `add_ticker()` on a duplicate is a no-op | ✓ | ✓ |
| `remove_ticker()` also calls `cache.remove()` | ✓ | ✓ |
| `stop()` is idempotent and awaits task cancellation | ✓ | ✓ |
| A failing tick/poll is logged and the loop continues | ✓ | ✓ |
| Never raises out of the background task | ✓ | ✓ |

### Writing a third source

Anything that implements the five methods and writes `cache.update(...)` works
unchanged with the SSE stream and the portfolio code. A minimal replay source,
useful for deterministic E2E tests:

```python
class ReplayDataSource(MarketDataSource):
    """Plays back a fixed list of {ticker: price} frames in a loop."""

    def __init__(self, price_cache: PriceCache, frames: list[dict[str, float]],
                 interval: float = 0.5) -> None:
        self._cache, self._frames, self._interval = price_cache, frames, interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._tickers = [normalize_ticker(t) for t in tickers]
        self._task = asyncio.create_task(self._run(), name="replay-loop")

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def _run(self) -> None:
        for frame in itertools.cycle(self._frames):
            for ticker in self._tickers:
                if ticker in frame:
                    self._cache.update(ticker, frame[ticker])
            await asyncio.sleep(self._interval)
```

---

## 8. Seed Data — `seed_prices.py`

Constants only. Unchanged from the current code.

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

Unknown tickers (e.g. `PYPL` added via chat) get a uniform random seed in
\$50–\$300, `DEFAULT_PARAMS`, and `CROSS_GROUP_CORR` with everything. To make a
new ticker realistic, add it to `SEED_PRICES` and `TICKER_PARAMS` (and a group
if relevant) — no code changes needed.

---

## 9. GBM Simulator — `simulator.py`

Two classes:

- `GBMSimulator` — pure, synchronous math engine. Holds current prices and
  advances them one step at a time. No asyncio, no cache — easy to unit-test.
- `SimulatorDataSource` — the `MarketDataSource` that drives `GBMSimulator` from
  an asyncio task and writes into the cache.

### 9.1 The math

Per tick, each price follows the exact solution of geometric Brownian motion:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

| Symbol | Meaning | Value |
|---|---|---|
| `μ` | annualized drift | 0.03–0.08 |
| `σ` | annualized volatility | 0.17 (V) – 0.50 (TSLA) |
| `dt` | tick length in trading-years | 0.5 s / (252 · 6.5 · 3600 s) = **8.48 × 10⁻⁸** |
| `Z` | correlated standard normal | `L @ N(0, I)` where `L = cholesky(C)` |

Properties: prices are always positive (`exp` > 0); log-returns are normal, so
the price distribution is log-normal like real equities; `−σ²/2` is the Itô
correction that keeps the *expected* price growing at `μ`.

### 9.2 What that looks like on screen

`σ·√dt` is the per-tick standard deviation of the log-return
(`√8.48e-8 = 2.91e-4`):

| Ticker | σ | 1-σ move per tick | 1-σ move per hour (7 200 ticks) | 1-σ move per 6.5 h "day" |
|---|---|---|---|---|
| V | 0.17 | 0.0049 % → \$0.014 on \$280 | 0.42 % | 1.07 % |
| AAPL | 0.22 | 0.0064 % → \$0.012 on \$190 | 0.54 % | 1.39 % |
| NVDA | 0.40 | 0.0116 % → \$0.093 on \$800 | 0.99 % | 2.52 % |
| TSLA | 0.50 | 0.0146 % → \$0.036 on \$250 | 1.24 % | 3.15 % |

Drift is invisible tick-to-tick (`μ·dt ≈ 4e-9`). Because ticks are about a cent,
many ticks round to the same 2-dp price and show as `flat`; that's realistic.
The visible drama comes from **shock events**:

- Each ticker, each tick: probability `0.001` of a jump of ±2–5 %.
- 10 tickers × 2 ticks/s × 0.001 = 0.02 events/s → **one event every ~50 s**
  somewhere on the board; each ticker gets one roughly every 8 minutes.

### 9.3 Correlation

Pairwise ρ by sector, assembled into a matrix `C` and factored once with
Cholesky whenever the ticker set changes (O(n³), trivial for n < 50):

| Pair | ρ |
|---|---|
| tech × tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| finance × finance (JPM, V) | 0.5 |
| TSLA × anything | 0.3 |
| everything else (cross-sector, unknown tickers) | 0.3 |

For the default 10 tickers the matrix is positive-definite (verified:
`np.linalg.cholesky` succeeds, shape 10×10). Any matrix built purely from these
constants is positive-definite, but Δ10 adds a fallback anyway.

Ticker order in `_tickers` is the row order of `C`, so `z_correlated[i]`
belongs to `_tickers[i]`.

### 9.4 `GBMSimulator`

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
from .models import normalize_ticker
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

    The tiny dt (~8.5e-8 for 500ms ticks over 252 trading days * 6.5h/day)
    produces sub-cent moves per tick that accumulate naturally over time.
    """

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

        # Per-ticker state
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}

        # Cholesky decomposition of the correlation matrix (for correlated moves)
        self._cholesky: np.ndarray | None = None

        # Batch-add without rebuilding the matrix each time
        for ticker in tickers:
            self._add_ticker_internal(normalize_ticker(ticker))
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
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent

        sqrt_dt = math.sqrt(self._dt)
        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            # GBM: S(t+dt) = S(t) * exp((mu - 0.5*sigma^2)*dt + sigma*sqrt(dt)*Z)
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * sqrt_dt * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random event: ~0.1% chance per tick per ticker
            # With 10 tickers at 2 ticks/sec, expect an event ~every 50 seconds
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

            # Internal state stays full precision; only the output is rounded
            result[ticker] = round(self._prices[ticker], 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the simulation. Rebuilds the correlation matrix."""
        ticker = normalize_ticker(ticker)
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the simulation. Rebuilds the correlation matrix."""
        ticker = normalize_ticker(ticker)
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Current price for a ticker, or None if not tracked."""
        return self._prices.get(normalize_ticker(ticker))

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
        """Rebuild the Cholesky decomposition of the ticker correlation matrix."""
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

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:  # Δ10
            logger.warning("Correlation matrix not positive-definite; using independent moves")
            self._cholesky = None

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        """Correlation between two tickers based on sector grouping."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]

        # TSLA is not in the tech set but is called out explicitly
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR

        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR

        return CROSS_GROUP_CORR
```

#### Standalone example

```python
>>> import numpy as np, random
>>> np.random.seed(0); random.seed(0)
>>> sim = GBMSimulator(["AAPL", "msft "], event_probability=0.0)
>>> sim.get_tickers()
['AAPL', 'MSFT']
>>> sim.step()
{'AAPL': 190.01, 'MSFT': 420.03}   # exact numbers depend on the seed
>>> for _ in range(7200): last = sim.step()   # one simulated hour
>>> last                              # typically within ~±1% of seed
```

### 9.5 `SimulatorDataSource`

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
        # Seed the cache so SSE has data on its first tick; the seed price
        # becomes the session_open for daily change.
        for ticker in self._sim.get_tickers():
            self._seed_cache(ticker)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(self._sim.get_tickers()))

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
            # Seed cache immediately so the ticker is tradable right away
            self._seed_cache(ticker)
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    def _seed_cache(self, ticker: str) -> None:
        price = self._sim.get_price(ticker) if self._sim else None
        if price is not None:
            self._cache.update(ticker=ticker, price=price, session_open=price)

    async def _run_loop(self) -> None:
        """Core loop: step the simulation, write to cache, sleep."""
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Notes:

- `step()` for 10 tickers is ~20 µs, so it runs directly on the event loop; no
  thread offload needed.
- The loop uses fixed `sleep(interval)` rather than a deadline schedule. Drift
  of a few ms per tick is irrelevant for a visual feed.
- `add_ticker` before `start()` is a no-op (there is no simulator yet). The app
  always calls `start()` in lifespan before serving requests.

---

## 10. Massive API Client — `massive_client.py`

### 10.1 Endpoint

One call returns every tracked ticker, which is what keeps the free tier
(5 requests/min) viable:

```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
Authorization: Bearer <MASSIVE_API_KEY>
```

SDK call (synchronous, uses `urllib3`; the client retries 5xx internally):

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)
snapshots: list[TickerSnapshot] = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],   # list is joined with commas by the SDK
)
```

### 10.2 Response → SDK model → our fields

Raw JSON for one ticker (abridged) and the SDK attribute it becomes:

```json
{
  "ticker": "AAPL",                          // snap.ticker
  "todaysChange": -4.54,                     // snap.todays_change
  "todaysChangePerc": -3.5,                  // snap.todays_change_percent
  "updated": 1675190399000000000,            // snap.updated            (ns)
  "day":     {"o": 129.61, "c": 125.07, ...},// snap.day.open / .close
  "prevDay": {"c": 129.61, ...},             // snap.prev_day.close
  "min":     {"c": 125.10, "t": 1675190340000}, // snap.min.close / .timestamp (ms)
  "lastTrade": {"p": 125.07, "s": 100, "x": 4,
                "t": 1675190399000000000}    // snap.last_trade.price / .sip_timestamp (ns)
}
```

> **Δ1 — the bug in the current code.** `snap.last_trade` is a
> `massive.rest.models.trades.LastTrade`. Its fields are `price`, `size`,
> `exchange`, `sip_timestamp`, `participant_timestamp`, `trf_timestamp`, … —
> **there is no `timestamp`**. `"t"` maps to `sip_timestamp` and is in
> **nanoseconds**. The current `snap.last_trade.timestamp / 1000.0` raises
> `AttributeError` for every ticker:
>
> ```
> Skipping snapshot for AAPL: 'LastTrade' object has no attribute 'timestamp'
> cached AAPL: None
> ```

Field mapping used by the new parser:

| Our field | Source, in priority order |
|---|---|
| `price` | `last_trade.price` → `min.close` → `day.close` → `prev_day.close` |
| `timestamp` (s) | `last_trade.sip_timestamp` (ns) → `updated` (ns) → `min.timestamp` (ms) → `time.time()` |
| `session_open` | `prev_day.close` → `day.open` → *(cache keeps previous)* |

Units differ by field, so timestamps go through one magnitude-based converter
instead of hard-coded divisors.

### 10.3 Implementation

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .models import normalize_ticker

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    ticker: str
    price: float
    timestamp: float  # Unix seconds
    session_open: float | None


def to_unix_seconds(value: float | int | None) -> float | None:
    """Convert a Massive timestamp of unknown unit (s/ms/µs/ns) to seconds.

    Snapshot fields mix units: last_trade.sip_timestamp and updated are ns,
    min.timestamp is ms. Present-day values are unambiguous by magnitude.
    """
    if not value:
        return None
    v = float(value)
    if v > 1e17:
        return v / 1e9  # nanoseconds
    if v > 1e14:
        return v / 1e6  # microseconds
    if v > 1e11:
        return v / 1e3  # milliseconds
    return v


def _first_positive(*values: Any) -> float | None:
    for v in values:
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def parse_snapshot(snap: Any) -> ParsedSnapshot | None:
    """Extract (ticker, price, timestamp, session_open) from a TickerSnapshot.

    Pure function so it can be tested against real SDK models. Returns None
    when the snapshot has no usable ticker or price.
    """
    ticker = getattr(snap, "ticker", None)
    if not ticker:
        return None

    last_trade = getattr(snap, "last_trade", None)
    minute = getattr(snap, "min", None)
    day = getattr(snap, "day", None)
    prev_day = getattr(snap, "prev_day", None)

    price = _first_positive(
        getattr(last_trade, "price", None),
        getattr(minute, "close", None),
        getattr(day, "close", None),
        getattr(prev_day, "close", None),
    )
    if price is None:
        return None

    timestamp = (
        to_unix_seconds(getattr(last_trade, "sip_timestamp", None))
        or to_unix_seconds(getattr(snap, "updated", None))
        or to_unix_seconds(getattr(minute, "timestamp", None))
        or time.time()
    )
    session_open = _first_positive(
        getattr(prev_day, "close", None),
        getattr(day, "open", None),
    )
    return ParsedSnapshot(normalize_ticker(ticker), price, timestamp, session_open)


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
        self._consecutive_failures = 0

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))

        # Do an immediate first poll so the cache has data right away
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
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            # Rebind instead of append so a list handed to a worker thread is never mutated
            self._tickers = [*self._tickers, ticker]
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    def _next_delay(self) -> float:
        """Normal interval, doubled per consecutive failure, capped. (Δ9)"""
        if self._consecutive_failures == 0:
            return self._interval
        return min(self._interval * 2**self._consecutive_failures, MAX_BACKOFF_SECONDS)

    async def _poll_loop(self) -> None:
        """Poll on interval. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._next_delay())
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Execute one poll cycle: fetch snapshots, update cache."""
        tickers = tuple(self._tickers)  # Δ5: immutable copy for the worker thread
        if not tickers or not self._client:
            return

        try:
            # The Massive RESTClient is synchronous — run in a thread to
            # avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except Exception as e:
            self._consecutive_failures += 1
            logger.error(
                "Massive poll failed (%d in a row, next try in %.0fs): %s",
                self._consecutive_failures,
                self._next_delay(),
                e,
            )
            # Common failures: 401 (bad key), 403 (plan), 429 (rate limit), network.
            return

        self._consecutive_failures = 0
        tracked = set(self._tickers)  # Skip tickers removed while the call was in flight
        processed = 0
        for snap in snapshots:
            parsed = parse_snapshot(snap)
            if parsed is None:
                logger.warning("Skipping unusable snapshot for %s", getattr(snap, "ticker", "???"))
                continue
            if parsed.ticker not in tracked:
                continue
            self._cache.update(
                ticker=parsed.ticker,
                price=parsed.price,
                timestamp=parsed.timestamp,
                session_open=parsed.session_open,
            )
            processed += 1

        missing = tracked - {getattr(s, "ticker", None) for s in snapshots}
        if missing:
            logger.debug("Massive returned no data for: %s", ", ".join(sorted(missing)))
        logger.debug("Massive poll: updated %d/%d tickers", processed, len(tickers))

    def _fetch_snapshots(self, tickers: tuple[str, ...]) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=list(tickers),
        )
```

### 10.4 Behaviour notes

- **Rate budget.** Default 15 s → 4 calls/min, under the free tier's 5. The
  first poll in `start()` counts, so a restart within the same minute can hit
  429; backoff absorbs that.
- **Newly added tickers** have no price until the next poll (≤ 15 s). Trades in
  that window get a clear 400 (see §17.2). Waking the loop early would burn
  rate budget on the free tier, so we don't.
- **Invalid tickers** (e.g. `ZZZZ`) are simply absent from the response. They
  stay tracked, never get a price, and the watchlist row shows "—". The
  watchlist route can optionally reject a ticker that still has no price after
  the next poll.
- **Market closed / weekends.** `last_trade.price` is the last trade (may be
  after-hours); the cache version still bumps each poll, so the UI stays
  "live" but flat. Between midnight and ~4 am ET the snapshot is cleared —
  the fallback chain lands on `prev_day.close`.
- **Ticker list length.** The tickers go into the query string; a few hundred
  symbols is fine. FinAlly watchlists are tens.

---

## 11. Factory — `factory.py`

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

DEFAULT_MASSIVE_POLL_INTERVAL = 15.0


def _poll_interval_from_env() -> float:
    """MASSIVE_POLL_INTERVAL in seconds; invalid or < 1 falls back to 15. (Δ9)"""
    raw = os.environ.get("MASSIVE_POLL_INTERVAL", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MASSIVE_POLL_INTERVAL
    return value if value >= 1.0 else DEFAULT_MASSIVE_POLL_INTERVAL


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        interval = _poll_interval_from_env()
        logger.info("Market data source: Massive API (real data, %.0fs poll)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

| Env | Result |
|---|---|
| `MASSIVE_API_KEY` unset / `""` / `"   "` | `SimulatorDataSource` |
| `MASSIVE_API_KEY=abc` | `MassiveDataSource(poll_interval=15)` |
| `MASSIVE_API_KEY=abc MASSIVE_POLL_INTERVAL=3` | `MassiveDataSource(poll_interval=3)` — paid tiers |

---

## 12. SSE Stream — `stream.py`

### 12.1 Implementation

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

HEARTBEAT_SECONDS = 15.0


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE streaming router with a reference to the price cache.

    A fresh router per call (Δ6) so calling this twice never double-registers.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates (~500ms cadence)."""
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
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncGenerator[str, None]:
    """Yield SSE-formatted price events until the client disconnects.

    Sends the full price map whenever the cache version changed since the last
    send, checked every `interval` seconds. Sends a comment line as a
    keep-alive if nothing was sent for `heartbeat` seconds. (Δ7)
    """
    # Tell the client to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

    last_version = -1
    last_sent = time.monotonic()
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
                    last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": keep-alive\n\n"  # SSE comment; EventSource ignores it
                last_sent = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### 12.2 Wire format

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.12,"previous_price":190.10,"timestamp":1707580800.5,"change":0.02,"change_percent":0.0105,"direction":"up","session_open":190.0,"day_change":0.12,"day_change_percent":0.0632},"GOOGL":{...}}

data: {...}

: keep-alive

```

- One event = the **whole** map of tracked tickers (≈ 2.5 KB for 10 tickers).
  The client never has to merge partial state; a ticker missing from the map
  has been removed.
- In simulator mode an event goes out every 0.5 s; in Massive mode, one per
  poll (15 s) with heartbeats in between.
- Payload is all prices, not per-user. With auth in future, filter by the
  user's tracked set inside `_generate_events`.

### 12.3 Try it

```bash
cd backend && uv run uvicorn app.main:app --port 8000 &
curl -N http://localhost:8000/api/stream/prices | head -c 600
```

---

## 13. Package Exports — `__init__.py`

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate               - Immutable price snapshot dataclass
    PriceCache                - Thread-safe in-memory price store
    MarketDataSource          - Abstract interface for data providers
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router      - FastAPI router factory for SSE endpoint
    normalize_ticker          - Canonical ticker formatting
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate, normalize_ticker
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
    "normalize_ticker",
]
```

Downstream code imports only from `app.market`, never from submodules.

---

## 14. Integrating with the App

These snippets live **outside** `app/market/` (in `app/main.py` and the route
modules) and show how the rest of the backend consumes the market layer.

### 14.1 Lifespan wiring — `app/main.py`

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    price_cache = PriceCache()
    source = create_market_data_source(price_cache)

    # Track everything the user can see or owns: watchlist ∪ open positions
    tickers = await db.tracked_tickers(user_id="default")
    await source.start(tickers)

    app.state.price_cache = price_cache
    app.state.market_source = source
    app.include_router(create_stream_router(price_cache))

    snapshot_task = asyncio.create_task(record_snapshots_forever(price_cache))
    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependencies — read from request.app.state, no module globals
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

`db.tracked_tickers` is one query:

```sql
SELECT ticker FROM watchlist WHERE user_id = ?
UNION
SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0;
```

### 14.2 Watchlist routes — keep the source in sync

The rule: **a ticker is tracked iff it is on the watchlist or held.** Both the
REST routes and the LLM chat's `watchlist_changes` go through the same service
functions so the rule lives in one place.

```python
async def add_to_watchlist(ticker: str, source: MarketDataSource) -> None:
    ticker = normalize_ticker(ticker)
    await db.insert_watchlist(ticker)          # UNIQUE(user_id, ticker) → idempotent
    await source.add_ticker(ticker)            # no-op if already tracked (e.g. held)


async def remove_from_watchlist(ticker: str, source: MarketDataSource) -> None:
    ticker = normalize_ticker(ticker)
    await db.delete_watchlist(ticker)
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)     # also clears it from the cache
```

```python
@router.get("/api/watchlist")
async def get_watchlist(cache: PriceCache = Depends(get_price_cache)):
    rows = await db.list_watchlist()
    return [
        {"ticker": r.ticker, **(u.to_dict() if (u := cache.get(r.ticker)) else {"price": None})}
        for r in rows
    ]
```

### 14.3 Trade execution — fill at the cached price

```python
@router.post("/api/portfolio/trade")
async def trade(
    req: TradeRequest,
    cache: PriceCache = Depends(get_price_cache),
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = normalize_ticker(req.ticker)
    price = cache.get_price(ticker)
    if price is None:
        raise HTTPException(400, f"Price not yet available for {ticker}. Try again in a moment.")

    result = await portfolio.execute(ticker, req.side, req.quantity, price)  # validates cash/shares

    # A buy of an unwatched ticker must keep being priced; a full sell of an
    # unwatched ticker can stop.
    if req.side == "buy":
        await source.add_ticker(ticker)
    elif result.remaining_quantity == 0 and not await db.is_watched(ticker):
        await source.remove_ticker(ticker)

    await record_snapshot(cache)  # PLAN §7: snapshot immediately after each trade
    return result
```

### 14.4 Portfolio valuation

```python
def portfolio_value(cash: float, positions: list[Position], cache: PriceCache) -> dict:
    prices = cache.get_all()  # one lock acquisition for a consistent view
    rows, total = [], cash
    for p in positions:
        u = prices.get(p.ticker)
        price = u.price if u else p.avg_cost       # fall back to cost if not priced yet
        value = p.quantity * price
        total += value
        rows.append({
            "ticker": p.ticker,
            "quantity": p.quantity,
            "avg_cost": p.avg_cost,
            "current_price": price,
            "unrealized_pnl": round(value - p.quantity * p.avg_cost, 2),
            "pnl_percent": round((price / p.avg_cost - 1) * 100, 2) if p.avg_cost else 0.0,
        })
    return {"cash": cash, "positions": rows, "total_value": round(total, 2)}
```

The 30 s snapshot task (`record_snapshots_forever`) calls `portfolio_value(...)`
and inserts `total_value` into `portfolio_snapshots`.

### 14.5 LLM context

The chat handler builds its "watchlist with live prices" block straight from
the cache:

```python
lines = [
    f"{u.ticker}: ${u.price:.2f} ({u.day_change_percent:+.2f}% today)"
    for u in cache.get_all().values()
]
```

---

## 15. Frontend Contract

TypeScript type for one entry in the SSE payload:

```ts
export type Direction = "up" | "down" | "flat";

export interface PriceUpdate {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;          // Unix seconds (float)
  change: number;             // vs previous tick
  change_percent: number;     // vs previous tick
  direction: Direction;       // drives the green/red flash
  session_open: number | null;
  day_change: number;
  day_change_percent: number; // shown in the watchlist "Chg %" column
}

export type PriceMap = Record<string, PriceUpdate>;
```

Client usage, including the connection-status dot:

```ts
type Status = "connected" | "reconnecting" | "disconnected";

export function connectPrices(onPrices: (p: PriceMap) => void, onStatus: (s: Status) => void) {
  const es = new EventSource("/api/stream/prices");
  es.onopen = () => onStatus("connected");
  es.onmessage = (e) => onPrices(JSON.parse(e.data) as PriceMap);
  es.onerror = () =>
    onStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
  return () => es.close();
}
```

- **Flash:** on each message, for tickers whose `price` differs from the last
  rendered value, add a `flash-up`/`flash-down` class based on `direction`,
  remove it after ~500 ms.
- **Sparklines:** append `{t: timestamp, v: price}` per ticker per message; cap
  at ~300 points. Drop the series if a ticker vanishes from the map.
- Comment lines (`: keep-alive`) never reach `onmessage`.

---

## 16. Testing

Existing: 73 tests in `backend/tests/market/` (run `cd backend && uv run --extra dev pytest`).
Below are the tests to **add or change** for this design. All use
`asyncio_mode = "auto"`.

### 16.1 Massive parsing against real SDK models (Δ1, Δ2, Δ11)

```python
"""tests/market/test_massive_parse.py"""

from massive.rest.models import TickerSnapshot

from app.market.massive_client import parse_snapshot, to_unix_seconds

FULL = {
    "ticker": "AAPL",
    "day": {"o": 129.61, "c": 125.07},
    "prevDay": {"c": 129.61},
    "min": {"c": 125.10, "t": 1675190340000},
    "lastTrade": {"p": 125.07, "s": 100, "t": 1675190399000000000},
    "updated": 1675190399000000000,
}


def snap(d: dict) -> TickerSnapshot:
    return TickerSnapshot.from_dict(d)


def test_full_snapshot():
    p = parse_snapshot(snap(FULL))
    assert p.ticker == "AAPL"
    assert p.price == 125.07
    assert p.timestamp == 1675190399.0          # ns → s
    assert p.session_open == 129.61             # prev_day.close


def test_no_last_trade_falls_back_to_minute_close():
    d = {k: v for k, v in FULL.items() if k != "lastTrade"}
    p = parse_snapshot(snap(d))
    assert p.price == 125.10
    assert p.timestamp == 1675190399.0          # from `updated`


def test_only_prev_day():
    p = parse_snapshot(snap({"ticker": "AAPL", "prevDay": {"c": 129.61}}))
    assert p.price == 129.61


def test_no_price_returns_none():
    assert parse_snapshot(snap({"ticker": "AAPL"})) is None


def test_no_ticker_returns_none():
    assert parse_snapshot(snap({"lastTrade": {"p": 1.0}})) is None


def test_timestamp_units():
    assert to_unix_seconds(1675190399) == 1675190399
    assert to_unix_seconds(1675190399000) == 1675190399
    assert to_unix_seconds(1675190399000000) == 1675190399
    assert to_unix_seconds(1675190399000000000) == 1675190399
    assert to_unix_seconds(None) is None
```

### 16.2 Massive polling (update existing `test_massive.py`)

```python
from unittest.mock import MagicMock, patch

from massive.rest.models import TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def make_source(tickers=("AAPL", "GOOGL")) -> tuple[MassiveDataSource, PriceCache]:
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = list(tickers)
    source._client = MagicMock()
    return source, cache


async def test_poll_updates_cache_from_real_models():
    source, cache = make_source()
    snaps = [
        TickerSnapshot.from_dict({"ticker": "AAPL", "prevDay": {"c": 189.0},
                                  "lastTrade": {"p": 190.5, "t": 1707580800000000000}}),
        TickerSnapshot.from_dict({"ticker": "GOOGL", "prevDay": {"c": 176.0},
                                  "lastTrade": {"p": 175.25, "t": 1707580800000000000}}),
    ]
    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()

    aapl = cache.get("AAPL")
    assert aapl.price == 190.5
    assert aapl.timestamp == 1707580800.0
    assert aapl.session_open == 189.0
    assert cache.get_price("GOOGL") == 175.25


async def test_ticker_removed_mid_poll_is_not_written():
    source, cache = make_source()
    snaps = [TickerSnapshot.from_dict({"ticker": "GOOGL", "lastTrade": {"p": 175.0}})]
    await source.remove_ticker("GOOGL")
    with patch.object(source, "_fetch_snapshots", return_value=snaps):
        await source._poll_once()
    assert cache.get("GOOGL") is None


async def test_failures_back_off_and_reset():
    source, _ = make_source()
    with patch.object(source, "_fetch_snapshots", side_effect=Exception("429")):
        await source._poll_once()
        await source._poll_once()
    assert source._next_delay() == 240.0        # 60 * 2**2
    with patch.object(source, "_fetch_snapshots", return_value=[]):
        await source._poll_once()
    assert source._next_delay() == 60.0


async def test_add_ticker_normalizes():
    source, _ = make_source(tickers=())
    await source.add_ticker(" pypl ")
    assert source.get_tickers() == ["PYPL"]
```

### 16.3 Model & cache (Δ3, Δ8)

```python
def test_day_change_from_session_open():
    cache = PriceCache()
    cache.update("AAPL", 190.0)                 # session_open defaults to first price
    u = cache.update("AAPL", 191.9)
    assert u.session_open == 190.0
    assert u.day_change == 1.9
    assert u.day_change_percent == 1.0


def test_explicit_session_open_wins():
    cache = PriceCache()
    u = cache.update("AAPL", 125.07, session_open=129.61)
    assert round(u.day_change_percent, 2) == -3.5


def test_zero_timestamp_is_kept():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


def test_to_dict_has_day_fields():
    d = PriceCache().update("AAPL", 190.0).to_dict()
    assert {"session_open", "day_change", "day_change_percent"} <= d.keys()
```

### 16.4 Simulator (Δ4, Δ10)

```python
from app.market.seed_prices import SEED_PRICES


def test_tickers_are_normalized():
    sim = GBMSimulator(["aapl", " AAPL "])
    assert sim.get_tickers() == ["AAPL"]


def test_full_default_set_builds_cholesky():
    sim = GBMSimulator(list(SEED_PRICES))
    assert sim._cholesky.shape == (10, 10)


def test_non_psd_matrix_falls_back(monkeypatch):
    # ρ(A,B)=ρ(A,C)=0.99 but ρ(B,C)=-0.99 is impossible → matrix not positive-definite
    rho = lambda a, b: -0.99 if {a, b} == {"B", "C"} else 0.99
    monkeypatch.setattr(GBMSimulator, "_pairwise_correlation", staticmethod(rho))
    sim = GBMSimulator(["A", "B", "C"])
    assert sim._cholesky is None
    assert set(sim.step()) == {"A", "B", "C"}


def test_correlation_is_visible():
    """Tech pairs move together far more often than chance."""
    np.random.seed(1); random.seed(1)
    sim = GBMSimulator(["AAPL", "MSFT"], event_probability=0.0)
    prev = {t: sim.get_price(t) for t in sim.get_tickers()}
    same = 0
    for _ in range(2000):
        sim.step()
        cur = {t: sim.get_price(t) for t in sim.get_tickers()}
        same += (cur["AAPL"] > prev["AAPL"]) == (cur["MSFT"] > prev["MSFT"])
        prev = cur
    assert same / 2000 > 0.65                   # ρ=0.6 → ~70% same sign; independent → 50%


async def test_source_seeds_session_open():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=10)
    await source.start(["AAPL"])
    assert cache.get("AAPL").session_open == SEED_PRICES["AAPL"]
    await source.stop()
```

### 16.5 SSE generator (Δ6, Δ7) — `tests/market/test_stream.py`

Drive `_generate_events` directly with a fake request; no server needed.

```python
import json

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    def __init__(self, disconnect_after: int):
        self._calls = 0
        self._limit = disconnect_after
        self.client = None

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._limit


async def collect(cache, n_loops, **kw) -> list[str]:
    return [e async for e in _generate_events(cache, FakeRequest(n_loops), interval=0, **kw)]


async def test_first_event_is_retry_then_data():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = await collect(cache, 1)
    assert events[0] == "retry: 1000\n\n"
    payload = json.loads(events[1].removeprefix("data: "))
    assert payload["AAPL"]["price"] == 190.0


async def test_no_duplicate_when_version_unchanged():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = await collect(cache, 5, heartbeat=1e9)
    assert sum(e.startswith("data:") for e in events) == 1


async def test_heartbeat_when_idle():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = await collect(cache, 3, heartbeat=0)
    assert ": keep-alive\n\n" in events


async def test_empty_cache_sends_nothing():
    assert await collect(PriceCache(), 3, heartbeat=1e9) == ["retry: 1000\n\n"]


def test_router_factory_is_independent():
    cache = PriceCache()
    r1, r2 = create_stream_router(cache), create_stream_router(cache)
    assert r1 is not r2
    assert len(r1.routes) == len(r2.routes) == 1   # module-level router would have 2
```

### 16.6 Factory (Δ9)

```python
def test_poll_interval_env(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MASSIVE_POLL_INTERVAL", "3")
    assert create_market_data_source(PriceCache())._interval == 3.0


def test_poll_interval_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "k")
    monkeypatch.setenv("MASSIVE_POLL_INTERVAL", "fast")
    assert create_market_data_source(PriceCache())._interval == 15.0
```

### 16.7 Manual check against the live API

Not part of CI (needs a key and network). Confirms Δ1 end-to-end:

```bash
cd backend
MASSIVE_API_KEY=... uv run python -c "
import asyncio
from app.market import PriceCache, create_market_data_source
async def main():
    c = PriceCache(); s = create_market_data_source(c)
    await s.start(['AAPL', 'MSFT'])
    print({t: u.to_dict() for t, u in c.get_all().items()})
    await s.stop()
asyncio.run(main())"
```

Expected: two populated entries. With the current code it prints `{}`.

---

## 17. Error Handling & Edge Cases

### 17.1 Failure matrix

| Situation | Simulator | Massive | What the user sees |
|---|---|---|---|
| Exception in one tick / poll | Logged, next tick runs | Logged, backoff, retry | Nothing, or a stale price briefly |
| 401 bad key / 403 plan | — | Logged every attempt, backoff up to 5 min | Connected dot green, no prices ("—"). Fix `.env`, restart |
| 429 rate limit | — | Backoff doubles the delay, resets on success | Prices update less often |
| Network down | — | Same as 429 | Stale prices |
| One malformed snapshot | — | That ticker skipped, others written | One row stale |
| Unknown ticker | Random seed \$50–300, simulated | Never priced | Simulator: fake price. Massive: "—" |
| Empty watchlist at start | `step()` returns `{}` | Poll skipped | Empty grid; `add_ticker` starts tracking |
| Client disconnects | — | — | SSE generator exits on next `is_disconnected()` check (≤ 0.5 s) |
| Server restart | Prices restart from seeds | Prices restart from first poll | EventSource reconnects after 1 s |

### 17.2 Trading a ticker with no price yet

```python
price = cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"Price not yet available for {ticker}. Try again in a moment.")
```

Simulator: never happens for tracked tickers (seeded on `add_ticker`). Massive:
possible for ≤ 1 poll interval after adding. The LLM gets this error string in
its trade results and relays it.

### 17.3 Concurrency

- Only one coroutine writes per source; the cache lock makes the worker-thread
  write path in Massive safe.
- `MassiveDataSource._tickers` is only **rebound** (never mutated in place)
  and the worker thread gets a tuple copy (Δ5).
- `GBMSimulator` is touched only from the event loop (tick task and route
  handlers), so it needs no lock.

### 17.4 Precision

Simulator state is full-precision floats; outputs and cache entries are
rounded to cents. Change fields are rounded to 4 dp to avoid `0.020000000001`
in JSON.

---

## 18. Configuration Reference

| Setting | Where | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | `""` | Non-empty → Massive, else simulator |
| `MASSIVE_POLL_INTERVAL` | env (Δ9) | `15` | Seconds between Massive polls (min 1) |
| `update_interval` | `SimulatorDataSource()` | `0.5` | Seconds between simulator ticks |
| `event_probability` | `SimulatorDataSource()` / `GBMSimulator()` | `0.001` | Per-ticker per-tick shock chance |
| `dt` | `GBMSimulator()` | `8.48e-8` | Tick length in trading-years |
| `MAX_BACKOFF_SECONDS` | `massive_client.py` | `300` | Cap on Massive retry delay |
| `interval` | `_generate_events()` | `0.5` | SSE check cadence |
| `HEARTBEAT_SECONDS` | `stream.py` (Δ7) | `15` | Idle keep-alive spacing |
| `retry:` | `_generate_events()` | `1000` ms | EventSource reconnect delay |
| Seed prices / σ / μ / ρ | `seed_prices.py` | see §8 | Simulator realism |

`.env.example` additions:

```bash
# Optional: seconds between Massive polls (15 for free tier, 2-5 for paid)
MASSIVE_POLL_INTERVAL=15
```

---

## 19. Implementation Checklist

In order; each step keeps the suite green.

1. **Δ1 + Δ2 + Δ11** — add `to_unix_seconds`, `parse_snapshot`; use them in
   `_poll_once`; rewrite Massive tests on `TickerSnapshot.from_dict`. *This is
   the only change that fixes broken behaviour; do it first.*
2. **Δ5** — `_poll_once` passes a tuple to `_fetch_snapshots`; `add_ticker`
   rebinds; skip tickers removed mid-poll.
3. **Δ4** — `normalize_ticker()` in `models.py`; use in simulator, Massive,
   export from `__init__`.
4. **Δ3 + Δ8** — `session_open` and day-change properties; cache `update()`
   signature; sources pass `session_open`; extend `to_dict()`.
5. **Δ6 + Δ7** — router inside the factory; heartbeat; add `test_stream.py`.
6. **Δ9** — `MASSIVE_POLL_INTERVAL`, backoff; `.env.example`.
7. **Δ10** — Cholesky fallback.
8. Run `uv run --extra dev ruff check . && uv run --extra dev pytest --cov=app`;
   run `uv run market_data_demo.py` to eyeball the simulator; run §16.7 if a
   key is available.
9. Update `planning/MARKET_DATA_SUMMARY.md` (test counts, new fields) and tell
   the frontend agent about the new `day_change_percent` field (§15).
