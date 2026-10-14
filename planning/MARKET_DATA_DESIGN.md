# Market Data Backend — Design

**Scope:** everything under `backend/app/market/`: the unified data-source interface, the shared price cache, the GBM simulator, the Massive (Polygon.io) REST poller, the SSE stream, and how the rest of the backend (FastAPI lifespan, watchlist, trades, portfolio) plugs into it.

**Audience:** the Backend agent building the rest of the platform, and anyone changing market data code.

**Relationship to existing code and docs.** A first version of this subsystem already exists (see `MARKET_DATA_SUMMARY.md`, with history in `archive/`). This document is the design the code should match. Most of it describes what is already built. Section 14 lists where the current code differs from this design, including one **critical bug in the Massive client**, and the order to fix them in. Code snippets here are complete enough to paste into the modules they name.

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [Module Layout & Public API](#3-module-layout--public-api)
4. [Data Model — `PriceUpdate`](#4-data-model--priceupdate)
5. [Price Cache — `PriceCache`](#5-price-cache--pricecache)
6. [Unified Interface — `MarketDataSource`](#6-unified-interface--marketdatasource)
7. [Seed Data & Parameters](#7-seed-data--parameters)
8. [GBM Simulator](#8-gbm-simulator)
9. [Massive API Client](#9-massive-api-client)
10. [Factory & Configuration](#10-factory--configuration)
11. [SSE Streaming Endpoint](#11-sse-streaming-endpoint)
12. [Integration with the Rest of the Backend](#12-integration-with-the-rest-of-the-backend)
13. [Testing](#13-testing)
14. [Deltas from the Current Implementation](#14-deltas-from-the-current-implementation)

---

## 1. Requirements

Traced from `PLAN.md`:

| # | Requirement | Where satisfied |
|---|---|---|
| R1 | Two sources (simulator, Massive) behind one interface; downstream code is source-agnostic | §6, §8, §9 |
| R2 | Source selected by `MASSIVE_API_KEY` (set and non-empty → Massive, else simulator) | §10 |
| R3 | Simulator: GBM, ~500 ms ticks, per-ticker drift/vol, correlated moves, occasional 2–5% events, realistic seed prices, in-process | §7, §8 |
| R4 | Massive: REST polling (not WebSocket), one call for all tickers, 15 s default (free tier), 2–15 s on paid tiers | §9 |
| R5 | Shared in-memory cache holding latest price, previous price, timestamp per ticker | §5 |
| R6 | `GET /api/stream/prices` SSE, ~500 ms cadence; event has ticker, price, previous price, timestamp, direction | §11 |
| R7 | Watchlist panel shows **daily change %** | §4 (`day_open`), §9 |
| R8 | Dynamic watchlist add/remove, by user or AI | §6, §12.3 |
| R9 | Trade execution and portfolio valuation use the current price | §12.4 |
| R10 | Unit tests: valid prices, GBM math, Massive parsing, interface conformance | §13 |

Non-goals: order books, bid/ask spreads, historical bars, after-hours handling, multiple users (the cache is already shared by all readers, so multi-user needs no change here).

---

## 2. Architecture

```
                 ┌──────────────────────────────┐
                 │ create_market_data_source()  │  reads MASSIVE_API_KEY
                 └──────────────┬───────────────┘
                                │ returns one of
             ┌──────────────────┴──────────────────┐
             ▼                                     ▼
┌───────────────────────────┐        ┌───────────────────────────┐
│ SimulatorDataSource       │        │ MassiveDataSource         │
│  asyncio task, every 0.5s │        │  asyncio task, every 15s  │
│  GBMSimulator.step()      │        │  to_thread(get_snapshot_  │
│                           │        │            all)           │
└─────────────┬─────────────┘        └─────────────┬─────────────┘
              │ cache.update(...)                  │ cache.update(...)
              └──────────────────┬─────────────────┘
                                 ▼
                ┌─────────────────────────────────┐
                │ PriceCache (thread-safe)        │
                │  ticker → PriceUpdate           │
                │  version counter                │
                └───┬────────────┬────────────┬───┘
                    │            │            │   reads only
                    ▼            ▼            ▼
             SSE stream    Trade execution   Portfolio valuation,
          /api/stream/      POST /api/       snapshots, GET /api/
             prices         portfolio/trade  watchlist, LLM context
```

### Key decisions

| Decision | Rationale |
|---|---|
| **Sources push into a cache, not return prices** | Producers and consumers run on different schedules (15 s polls vs 500 ms SSE vs on-demand trades). A cache decouples them; consumers never wait on I/O and never need to know which source is active. |
| **Strategy pattern (ABC + factory)** | Swapping sources is a one-line environment change. Tests can substitute a fake source. |
| **One producer at a time** | Exactly one source writes to the cache. That keeps `previous_price` semantics simple: it's the price from that producer's previous tick. |
| **Single asyncio event loop** | All source methods are `async` and run on the app loop. The simulator is pure CPU (microseconds per step) and runs inline. The Massive SDK is synchronous, so it runs in `asyncio.to_thread`. That thread is the only reason the cache needs a lock. |
| **Full snapshot per SSE event** | 10–50 tickers × ~150 bytes is under 8 KB per event. Sending everything means a reconnecting client is consistent after one event and removed tickers disappear naturally. No diff protocol needed. |
| **Version counter for change detection** | The SSE loop skips sending when nothing changed (e.g., between Massive polls), without comparing payloads. |

### Concurrency model

- Route handlers, the simulator loop, the Massive poll loop and every SSE generator are coroutines on **one** event loop. Between `await` points they cannot interleave. For example, `GBMSimulator.add_ticker()` (called from a route) can never run in the middle of `GBMSimulator.step()`.
- The one exception is `MassiveDataSource._fetch_snapshots`, which runs in a worker thread. It receives a **copy** of the ticker list and never touches the cache. Results are written back on the loop thread after `await asyncio.to_thread(...)` returns.
- `PriceCache` still uses a `threading.Lock` so it stays correct if any other thread (a future background worker, or a test) touches it. This also makes it safe on free-threaded Python builds.

---

## 3. Module Layout & Public API

```
backend/app/market/
├── __init__.py         # Public exports (below)
├── models.py           # PriceUpdate (frozen dataclass)
├── cache.py            # PriceCache
├── interface.py        # MarketDataSource ABC, normalize_ticker()
├── seed_prices.py      # SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py        # GBMSimulator, SimulatorDataSource
├── massive_client.py   # MassiveDataSource, parse_snapshot()
├── factory.py          # create_market_data_source()
└── stream.py           # create_stream_router()
```

```python
# backend/app/market/__init__.py
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate               - Immutable price snapshot dataclass
    PriceCache                - Thread-safe in-memory price store
    MarketDataSource          - Abstract interface for data providers
    normalize_ticker          - Validate/canonicalize a ticker symbol
    create_market_data_source - Factory that selects simulator or Massive
    create_stream_router      - FastAPI router factory for the SSE endpoint
"""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource, normalize_ticker
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "normalize_ticker",
    "create_market_data_source",
    "create_stream_router",
]
```

Downstream code imports **only** from `app.market`, never from submodules (tests are the exception).

---

## 4. Data Model — `PriceUpdate`

The only object that leaves the market data layer.

```python
# backend/app/market/models.py
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
    # Reference price for "daily change": previous close (Massive) or the
    # session's first price (simulator). None only if a source can't supply one.
    day_open: float | None = None

    @property
    def change(self) -> float:
        """Absolute change since the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percent change since the previous tick."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' relative to the previous tick (drives the UI flash)."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_change_percent(self) -> float | None:
        """Percent change versus day_open (drives the watchlist 'daily change %')."""
        if not self.day_open:
            return None
        return round((self.price - self.day_open) / self.day_open * 100, 4)

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
            "day_open": self.day_open,
            "day_change_percent": self.day_change_percent,
        }
```

### Design notes

- **Frozen + slots**: updates are shared across coroutines and threads without copying. Immutability rules out accidental mutation by a reader. Slots keep the per-object footprint small.
- **Derived fields are properties, not stored**: `change`, `direction`, etc. can never disagree with `price`/`previous_price`.
- **Two notions of change**: *tick* change (`change`, `change_percent`, `direction`) for the flash animation, and *day* change (`day_change_percent`) for the watchlist column. They are different numbers and the frontend must not conflate them.
- **Prices are rounded to cents in the cache** (§5), not in the model, so the model stays a plain value object.

Example:

```python
>>> u = PriceUpdate("AAPL", price=191.20, previous_price=191.05, timestamp=1760400000.0, day_open=190.00)
>>> u.direction, u.change, u.change_percent, u.day_change_percent
('up', 0.15, 0.0785, 0.6316)
```

---

## 5. Price Cache — `PriceCache`

```python
# backend/app/market/cache.py
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Latest price for each ticker.

    Writers: exactly one MarketDataSource (simulator or Massive).
    Readers: SSE stream, trade execution, portfolio valuation, LLM context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # Bumped on every mutation (update AND remove)

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        day_open: float | None = None,
    ) -> PriceUpdate:
        """Record a new price and return the resulting PriceUpdate.

        - previous_price is the last cached price (== price on first sight → 'flat').
        - day_open: if omitted, keeps the existing value, or uses this price on first sight.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if day_open is None:
                day_open = prev.day_open if prev and prev.day_open is not None else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
                day_open=round(day_open, 2),
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
                self._version += 1  # So SSE clients see the removal promptly

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

### Behavior

```python
cache = PriceCache()
cache.update("AAPL", 190.00)               # first sight: prev=190.00, flat, day_open=190.00
cache.update("AAPL", 190.123)              # stored 190.12, prev 190.00, 'up'
cache.get("AAPL").day_change_percent       # 0.0632
cache.update("AAPL", 189.90, day_open=188.0)  # Massive passes prev close explicitly
cache.version                              # 3
cache.remove("AAPL"); cache.version        # 4
cache.get_price("ZZZZ")                    # None — callers must handle a miss
```

- **Memory** is O(number of tickers). There is no history. Sparklines and the main chart accumulate history on the frontend from the SSE stream (PLAN §2), and the P&L chart reads `portfolio_snapshots` from the DB.
- **`timestamp is not None`**, not `timestamp or time.time()`, so a legitimate `0.0` is never replaced.
- **Removal bumps the version.** Otherwise, with Massive's 15 s polls, a removed ticker would linger on clients until the next poll.

---

## 6. Unified Interface — `MarketDataSource`

```python
# backend/app/market/interface.py
"""Abstract interface for market data sources."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

_TICKER_RE = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}")


def normalize_ticker(raw: str) -> str:
    """Canonicalize a ticker ('  aapl ' → 'AAPL'). Raises ValueError if malformed.

    Accepts 1-10 chars: a leading letter, then letters, digits, '.' or '-' (e.g. BRK.B).
    """
    ticker = raw.strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise ValueError(f"Invalid ticker symbol: {raw!r}")
    return ticker


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push PriceUpdates into a shared PriceCache on their own
    schedule. Nobody asks a source for a price; they read the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("PYPL")
        await source.remove_ticker("GOOGL")
        await source.stop()

    All tickers passed in are normalized with normalize_ticker(); invalid
    symbols raise ValueError.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing updates for `tickers` (may be empty). Call exactly once.

        Implementations should populate the cache before returning when they
        can, so the first SSE event and the first trade have prices.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task and release resources. Idempotent.
        After stop() returns the source never writes to the cache again."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking `ticker`. No-op if already tracked."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking `ticker` and remove it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers, in insertion order."""
```

### Contract details both implementations must honor

| Situation | Required behavior |
|---|---|
| `add_ticker` of an already-tracked ticker | No-op, no error |
| `remove_ticker` of an untracked ticker | No-op, no error (still removes any stale cache entry) |
| Lower-case / padded input | Normalized: `" aapl "` → `"AAPL"` |
| Malformed input (`""`, `"AAPL;DROP"`, 11+ chars) | `ValueError` from `normalize_ticker` |
| Errors inside the background loop | Logged, never propagated. The loop keeps running. |
| `stop()` called twice, or before `start()` | No-op |
| Price availability after `add_ticker` | Simulator: **immediate**. Massive: **next poll** (≤ poll interval). Callers must tolerate `cache.get(t) is None`. |

> **Why not validate that a ticker really exists?** The simulator can price any symbol. Massive simply returns no snapshot for an unknown symbol, so it never gets a price, and §12.3 lets the API layer report that. Hitting a reference-data endpoint on every add would burn the free tier's 5 calls/minute.

---

## 7. Seed Data & Parameters

```python
# backend/app/market/seed_prices.py
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
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
# sigma: annualized volatility, mu: annualized drift
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

# Tickers added at runtime that aren't listed above
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}
UNKNOWN_SEED_RANGE: tuple[float, float] = (50.0, 300.0)

# Sector groups for the correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3  # Between sectors / unknown tickers
TSLA_CORR = 0.3  # TSLA does its own thing
```

To add a well-known ticker with realistic behavior (e.g., `PYPL`), add one line to each of `SEED_PRICES` and `TICKER_PARAMS` (and optionally a group). No other change is needed.

---

## 8. GBM Simulator

Split in two:

- `GBMSimulator`: pure, synchronous math engine. No asyncio and no cache, so it's trivially testable.
- `SimulatorDataSource`: the `MarketDataSource` adapter that drives the engine from an asyncio task and writes to the cache.

### 8.1 The math

Geometric Brownian Motion, exact discretization:

```
S(t+Δt) = S(t) · exp( (μ − σ²/2)·Δt + σ·√Δt·Z )
```

| Symbol | Meaning | Value |
|---|---|---|
| `μ` | annualized drift | per ticker, e.g. 0.05 |
| `σ` | annualized volatility | per ticker, e.g. 0.22 |
| `Δt` | tick length as a fraction of a trading year | `0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` |
| `Z` | correlated standard normal | from Cholesky, §8.2 |

Properties that matter for the product:

- **Never negative**: `exp(·) > 0`, so prices stay positive no matter what.
- **Realistic tick size**: per-tick σ for AAPL ≈ `190 × 0.22 × √8.48e-8 ≈ $0.012`, about a cent. That's enough to flash every tick without looking silly.
- **Realistic day**: one simulated trading day (6.5 h of ticks) has σ ≈ `22% / √252 ≈ 1.4%` for AAPL and ≈ 3.2% for TSLA.
- **The `−σ²/2` term** makes `E[S(t)] = S(0)·e^{μt}`; without it prices would drift upward faster than `μ`.

### 8.2 Correlated moves

Build a correlation matrix `C` from the sector rules, factor it once as `C = L·Lᵀ` (Cholesky), then each tick:

```
z_indep ~ N(0, I)          # n independent normals
z_corr  = L @ z_indep      # Cov(z_corr) = L·Lᵀ = C
```

Pairwise rule (`_pairwise_correlation`):

| Pair | ρ |
|---|---|
| Either is TSLA | 0.3 |
| Both tech | 0.6 |
| Both finance | 0.5 |
| Anything else (cross-sector, unknown tickers) | 0.3 |

Default 10-ticker matrix (rows/cols in watchlist order):

```
       AAPL GOOGL MSFT AMZN TSLA NVDA META  JPM    V NFLX
AAPL   1.0   0.6  0.6  0.6  0.3  0.6  0.6  0.3  0.3  0.6
GOOGL  0.6   1.0  0.6  0.6  0.3  0.6  0.6  0.3  0.3  0.6
...
JPM    0.3   0.3  0.3  0.3  0.3  0.3  0.3  1.0  0.5  0.3
V      0.3   0.3  0.3  0.3  0.3  0.3  0.3  0.5  1.0  0.3
```

This block structure is positive definite, as is any matrix these rules produce for an arbitrary ticker set. Still, `_rebuild_cholesky` catches `LinAlgError` and falls back to independent draws, so a future rule change can never crash the loop. Rebuilding is O(n³) via numpy. At n ≤ 50 that's microseconds, and it only runs on add/remove.

### 8.3 Shock events

Each ticker, each tick: with probability `p = 0.001`, multiply by `1 ± U(0.02, 0.05)`. At 2 ticks/s × 10 tickers that's ≈ 0.02 events/s, **one visible jump every ~50 s** somewhere on the board. Pass `event_probability=0` for deterministic-statistics tests.

### 8.4 `GBMSimulator`

```python
# backend/app/market/simulator.py
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
    UNKNOWN_SEED_RANGE,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated Geometric Brownian Motion for a dynamic set of tickers.

    S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
    """

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8 (one 500 ms tick)

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        rng: np.random.Generator | None = None,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._rng = rng or np.random.default_rng()

        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}  # Unrounded internal state
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price rounded to cents}.

        Hot path (every 500 ms): one numpy draw + one mat-vec + n scalar ops.
        """
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

            if self._rng.random() < self._event_prob:
                shock = self._rng.uniform(0.02, 0.05) * (1 if self._rng.random() < 0.5 else -1)
                price *= 1 + shock
                logger.debug("Shock event on %s: %+.1f%%", ticker, shock * 100)

            self._prices[ticker] = price
            result[ticker] = round(price, 2)

        return result

    def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        """Unrounded current price (tests use this for exact statistics)."""
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky (batch init)."""
        ticker = normalize_ticker(ticker)
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker) or float(
            self._rng.uniform(*UNKNOWN_SEED_RANGE)
        )
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
            logger.warning("Correlation matrix not positive definite; using independent moves")
            self._cholesky = None

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

Using an injectable `np.random.Generator` (rather than the global `np.random` / `random` modules) makes every test reproducible with `rng=np.random.default_rng(42)`.

Example:

```python
>>> sim = GBMSimulator(["AAPL", "TSLA"], rng=np.random.default_rng(42))
>>> sim.step()
{'AAPL': 190.0, 'TSLA': 250.0}      # sub-cent moves round to the seed on tick 1
>>> for _ in range(7200): last = sim.step()   # one simulated hour
>>> last
{'AAPL': 191.37, 'TSLA': 246.12}    # illustrative
```

### 8.5 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator, ticking every `update_interval` seconds."""

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
        # Seed the cache so the first SSE event / trade has prices immediately.
        # First sight also fixes day_open = seed price (the "session open").
        for ticker in self._sim.get_tickers():
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
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

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._sim is None or ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))
        logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
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
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Notes:

- `step()` takes microseconds, so it runs inline on the loop. A thread would add overhead and a race on `_sim`.
- `asyncio.sleep(interval)` after the work means the real period is slightly more than 500 ms. That drift doesn't matter for a simulation.
- The simulator's `day_open` is the seed price at app start (or at add time). The "daily change %" column then means "change since session start", which is the honest equivalent for a simulated market.

---

## 9. Massive API Client

### 9.1 Endpoint & SDK facts (verified against the installed `massive` package)

- Call: `RESTClient(api_key=...).get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])`. This is `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,...`. **All tickers come back in one call**, which is what makes the free tier (5 req/min) workable.
- Returns `list[TickerSnapshot]` with fields: `ticker`, `day: Agg`, `prev_day: Agg`, `min: MinuteSnapshot`, `last_trade: LastTrade`, `last_quote: LastQuote`, `todays_change`, `todays_change_percent`, `updated`. Any of the nested objects may be `None`.
- **`LastTrade` has no `timestamp` attribute.** Its time is `sip_timestamp` (JSON `t`), with `participant_timestamp` (`y`) as fallback. In snapshot responses these are **Unix nanoseconds**. `TickerSnapshot.updated` is also nanoseconds. `MinuteSnapshot.timestamp` / `Agg.timestamp` are **milliseconds**. Because units vary by field, the parser infers units from magnitude (§9.2).
- Errors: `massive.exceptions.AuthError` (empty/invalid key), `massive.exceptions.BadResponse` (non-200, including 403 plan-not-entitled and 429 rate-limited), plus `urllib3` network errors. The SDK retries 5xx up to 3 times internally.
- The client is **synchronous** (urllib3), so we call it via `asyncio.to_thread`.

### 9.2 Snapshot parsing (pure function)

Keeping parsing separate from I/O lets us unit-test it against **real** `TickerSnapshot.from_dict(...)` objects. That kind of test would have caught the `last_trade.timestamp` bug that `MagicMock`-based tests missed.

```python
# backend/app/market/massive_client.py
"""Massive (Polygon.io) REST poller for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from massive import RESTClient
from massive.exceptions import AuthError
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParsedSnapshot:
    ticker: str
    price: float
    timestamp: float  # Unix seconds
    day_open: float | None  # previous close → basis for daily change %


def to_unix_seconds(value: float | int | None) -> float | None:
    """Convert a Massive timestamp of unknown unit (s/ms/µs/ns) to seconds."""
    if not value:
        return None
    v = float(value)
    if v > 1e17:
        return v / 1e9  # nanoseconds
    if v > 1e14:
        return v / 1e6  # microseconds
    if v > 1e11:
        return v / 1e3  # milliseconds
    return v  # seconds


def _positive(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and x > 0 else None


def parse_snapshot(snap: Any) -> ParsedSnapshot | None:
    """Extract (price, timestamp, day_open) from a TickerSnapshot. None if unusable.

    Price preference:
      1. last_trade.price  (live)
      2. min.close         (latest minute bar)
      3. day.close         (today's session so far)
      4. prev_day.close    (market closed / pre-market with no trades yet)
    """
    ticker = getattr(snap, "ticker", None)
    if not ticker:
        return None

    last_trade = getattr(snap, "last_trade", None)
    minute = getattr(snap, "min", None)
    day = getattr(snap, "day", None)
    prev_day = getattr(snap, "prev_day", None)

    price: float | None = None
    ts: float | None = None
    if last_trade is not None and (price := _positive(getattr(last_trade, "price", None))):
        ts = to_unix_seconds(
            getattr(last_trade, "sip_timestamp", None)
            or getattr(last_trade, "participant_timestamp", None)
        )
    elif minute is not None and (price := _positive(getattr(minute, "close", None))):
        ts = to_unix_seconds(getattr(minute, "timestamp", None))
    elif day is not None and (price := _positive(getattr(day, "close", None))):
        pass
    elif prev_day is not None and (price := _positive(getattr(prev_day, "close", None))):
        ts = to_unix_seconds(getattr(prev_day, "timestamp", None))

    if price is None:
        return None

    ts = ts or to_unix_seconds(getattr(snap, "updated", None)) or time.time()
    day_open = _positive(getattr(prev_day, "close", None)) if prev_day is not None else None
    return ParsedSnapshot(ticker=ticker.upper(), price=price, timestamp=ts, day_open=day_open)
```

Example, using the real SDK model:

```python
>>> from massive.rest.models import TickerSnapshot
>>> snap = TickerSnapshot.from_dict({
...     "ticker": "AAPL",
...     "lastTrade": {"p": 191.23, "t": 1760448600123456789},   # ns
...     "prevDay": {"c": 189.50, "t": 1760385600000},           # ms
...     "todaysChangePerc": 0.91,
... })
>>> parse_snapshot(snap)
ParsedSnapshot(ticker='AAPL', price=191.23, timestamp=1760448600.1234567, day_open=189.5)
```

`day_open = prev_day.close` makes `day_change_percent` equal Massive's own `todays_change_percent` (≈ 0.91% above), which is the number traders expect.

### 9.3 `MassiveDataSource`

```python
class MassiveDataSource(MarketDataSource):
    """Polls the Massive snapshot endpoint for all tracked tickers in one call.

    Rate limits: free tier 5 req/min → default 15 s interval. Paid tiers: 2-5 s.
    On failure, backs off exponentially (interval × 2^n, capped) and recovers
    automatically on the next success.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        max_backoff: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._max_backoff = max_backoff
        self._tickers: list[str] = []
        self._client: RESTClient | None = None
        self._task: asyncio.Task | None = None
        self._consecutive_failures = 0

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))
        await self._poll_once()  # Fill the cache before the app starts serving
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

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (priced on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    def _next_delay(self) -> float:
        if self._consecutive_failures == 0:
            return self._interval
        return min(self._interval * 2**self._consecutive_failures, self._max_backoff)

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._next_delay())
            await self._poll_once()

    async def _poll_once(self) -> None:
        tickers = list(self._tickers)  # Snapshot: the worker thread gets its own copy
        if not tickers or self._client is None:
            return

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except AuthError:
            self._consecutive_failures += 1
            logger.error("Massive auth failed: check MASSIVE_API_KEY")
            return
        except Exception as e:  # BadResponse (403/429/...), network errors
            self._consecutive_failures += 1
            logger.error(
                "Massive poll failed (%d in a row, next try in %.0fs): %s",
                self._consecutive_failures,
                self._next_delay(),
                e,
            )
            return

        self._consecutive_failures = 0
        tracked = set(self._tickers)  # Re-read: tickers may have changed during the await
        updated = 0
        for snap in snapshots:
            parsed = parse_snapshot(snap)
            if parsed is None:
                logger.warning("Skipping unusable snapshot for %s", getattr(snap, "ticker", "?"))
                continue
            if parsed.ticker not in tracked:
                continue  # Removed while the request was in flight; don't resurrect it
            self._cache.update(
                ticker=parsed.ticker,
                price=parsed.price,
                timestamp=parsed.timestamp,
                day_open=parsed.day_open,
            )
            updated += 1

        missing = tracked - {getattr(s, "ticker", None) for s in snapshots}
        if missing:
            logger.debug("Massive returned no snapshot for: %s", ", ".join(sorted(missing)))
        logger.debug("Massive poll: updated %d/%d tickers", updated, len(tracked))

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Blocking HTTP call. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
```

### 9.4 Behavior summary

| Scenario | Behavior |
|---|---|
| App start | One synchronous poll inside `start()`, so prices exist before the first request. If it fails, the app still starts and prices appear on a later successful poll. |
| Steady state | One API call per interval regardless of ticker count |
| `add_ticker("PYPL")` | Included in the next poll (≤ 15 s). Until then `cache.get("PYPL") is None`. |
| `remove_ticker` while a poll is in flight | The returned snapshot for that ticker is ignored, so it doesn't reappear |
| Unknown symbol | Massive omits it from the response, so it never gets a price (logged at debug). See §12.3. |
| 429 / network error | Backoff: 30 s, 60 s, 120 s, 120 s … then back to 15 s after the first success |
| Bad API key | Same backoff, with a clear `check MASSIVE_API_KEY` error log. The SSE stream stays up but sends no prices. |
| Market closed | `last_trade` still holds the last print, so prices are static and `direction` is `flat`. The UI shows no flashes, which is correct. |
| Same price on consecutive polls | Cache records `previous_price == price`, direction `flat` |

Within a single poll interval Massive prices are constant, so the frontend sees a burst of flashes every 15 s rather than continuous motion. That's expected with real data on the free tier.

---

## 10. Factory & Configuration

```python
# backend/app/market/factory.py
"""Select and construct the market data source from the environment."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)

DEFAULT_MASSIVE_POLL_INTERVAL = 15.0  # seconds; safe for the free tier (5 req/min)
MIN_MASSIVE_POLL_INTERVAL = 2.0


def _poll_interval_from_env() -> float:
    raw = os.environ.get("MASSIVE_POLL_INTERVAL", "").strip()
    if not raw:
        return DEFAULT_MASSIVE_POLL_INTERVAL
    try:
        return max(float(raw), MIN_MASSIVE_POLL_INTERVAL)
    except ValueError:
        logger.warning("Ignoring invalid MASSIVE_POLL_INTERVAL=%r", raw)
        return DEFAULT_MASSIVE_POLL_INTERVAL


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty → MassiveDataSource, else SimulatorDataSource.

    Returns an unstarted source; the caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = _poll_interval_from_env()
        logger.info("Market data source: Massive API (poll every %.1fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | empty | Non-empty (after `strip()`) selects Massive |
| `MASSIVE_POLL_INTERVAL` | `15` | Seconds between polls (Massive only), floor 2 s. Use `2`–`5` on paid tiers. |

Simulator tick (0.5 s), event probability (0.001) and SSE cadence (0.5 s) are constructor defaults, not environment variables. Nobody needs to tune them in production, and tests pass them directly.

`.env.example` addition:

```bash
# Optional: seconds between Massive polls (free tier: keep 15; paid: 2-5)
MASSIVE_POLL_INTERVAL=15
```

---

## 11. SSE Streaming Endpoint

### 11.1 Implementation

```python
# backend/app/market/stream.py
"""SSE endpoint streaming live prices from the PriceCache."""

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
    """Build a fresh router (safe to call more than once, e.g. per test app)."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request, interval, heartbeat),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Disable proxy buffering (nginx, App Runner)
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    heartbeat: float = 15.0,
) -> AsyncGenerator[str, None]:
    """Yield an SSE event with all prices whenever the cache version changes.

    - First event is sent immediately (last_version starts at -1).
    - A comment line (': ping') is sent if nothing else went out for `heartbeat`
      seconds, so idle connections (Massive between polls, market closed)
      aren't dropped by proxies.
    """
    yield "retry: 1000\n\n"  # EventSource reconnect delay (ms)

    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    last_version = -1
    last_sent = time.monotonic()

    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(data)}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= heartbeat:
                yield ": ping\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("SSE client disconnected: %s", client)
```

An empty cache sends `data: {}`. That's still useful, because it tells the client the stream is live and the watchlist is empty.

### 11.2 Wire format

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.42, "previous_price": 190.38, "timestamp": 1760448600.51, "change": 0.04, "change_percent": 0.021, "direction": "up", "day_open": 190.0, "day_change_percent": 0.221}, "GOOGL": {...}, ...}

data: {"AAPL": {...}, ...}

: ping

```

One event = one JSON object keyed by ticker, containing **every** tracked ticker. The client replaces its whole price map on each event, so a ticker missing from the event has been removed.

### 11.3 Frontend contract

```ts
// frontend: types + hook sketch
export interface PriceTick {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;            // Unix seconds
  change: number;               // vs previous tick
  change_percent: number;       // vs previous tick
  direction: "up" | "down" | "flat";
  day_open: number | null;
  day_change_percent: number | null;  // watchlist "daily change %"
}
export type PriceEvent = Record<string, PriceTick>;

const es = new EventSource("/api/stream/prices");
es.onopen = () => setStatus("connected");          // green dot
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
es.onmessage = (e) => {
  const prices: PriceEvent = JSON.parse(e.data);
  setPrices(prices);                                // replace, don't merge
  for (const t of Object.values(prices)) appendSparklinePoint(t.ticker, t.timestamp, t.price);
  // flash: if t.direction !== "flat", add .flash-up / .flash-down for ~500 ms
};
```

Sparkline/chart dedupe: with Massive, the same `timestamp` repeats across events until the next poll. Append a point only when `timestamp` changes for that ticker.

---

## 12. Integration with the Rest of the Backend

### 12.1 App wiring (`backend/app/main.py`)

```python
# backend/app/main.py
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import db
from app.market import PriceCache, create_market_data_source, create_stream_router

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def create_app() -> FastAPI:
    price_cache = PriceCache()
    market_source = create_market_data_source(price_cache)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.init_db()  # Lazy schema + seed (PLAN §7)
        await market_source.start(db.get_tracked_tickers())  # watchlist ∪ open positions
        try:
            yield
        finally:
            await market_source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.state.price_cache = price_cache
    app.state.market_source = market_source

    app.include_router(create_stream_router(price_cache))
    # app.include_router(portfolio_router), watchlist_router, chat_router, health_router ...

    if STATIC_DIR.exists():  # Mounted last so /api/* wins
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


app = create_app()
```

```python
# backend/app/deps.py: FastAPI dependencies for route handlers
from fastapi import Request

from app.market import MarketDataSource, PriceCache


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

The background tasks (simulator loop, Massive poller) start in `lifespan`, so they never run at import time and each test app gets its own isolated cache and source.

### 12.2 Which tickers are tracked

**Tracked set = watchlist ∪ tickers with an open position.** If the user removes a ticker from the watchlist while still holding it, its price must keep updating. Otherwise the positions table, heatmap, total value and portfolio snapshots would go stale.

```python
# backend/app/db (sketch): owned by the Backend agent
def get_tracked_tickers(user_id: str = "default") -> list[str]:
    rows = conn.execute(
        """
        SELECT ticker FROM watchlist WHERE user_id = ?
        UNION
        SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0
        """,
        (user_id, user_id),
    ).fetchall()
    return [r[0] for r in rows]
```

One idempotent helper keeps the source in sync. Call it after **any** watchlist change or trade, manual or AI-initiated:

```python
# backend/app/market_sync.py
from app import db
from app.market import MarketDataSource


async def sync_tracked_tickers(source: MarketDataSource) -> None:
    """Make the source track exactly watchlist ∪ positions."""
    desired = set(db.get_tracked_tickers())
    current = set(source.get_tickers())
    for ticker in sorted(desired - current):
        await source.add_ticker(ticker)
    for ticker in sorted(current - desired):
        await source.remove_ticker(ticker)
```

### 12.3 Watchlist routes

```python
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app import db
from app.deps import get_market_source, get_price_cache
from app.market import MarketDataSource, PriceCache, normalize_ticker
from app.market_sync import sync_tracked_tickers

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


@router.get("")
async def list_watchlist(cache: PriceCache = Depends(get_price_cache)):
    items = []
    for ticker in db.get_watchlist():
        update = cache.get(ticker)
        items.append({"ticker": ticker, **(update.to_dict() if update else {"price": None})})
    return items


@router.post("", status_code=201)
async def add_to_watchlist(
    body: AddTickerRequest,
    source: MarketDataSource = Depends(get_market_source),
    cache: PriceCache = Depends(get_price_cache),
):
    try:
        ticker = normalize_ticker(body.ticker)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    db.add_to_watchlist(ticker)  # INSERT OR IGNORE on (user_id, ticker)
    await sync_tracked_tickers(source)
    update = cache.get(ticker)  # Simulator: present. Massive: None until next poll.
    return {"ticker": ticker, "price": update.price if update else None}


@router.delete("/{ticker}")
async def remove_from_watchlist(
    ticker: str, source: MarketDataSource = Depends(get_market_source)
):
    ticker = normalize_ticker(ticker)
    if not db.remove_from_watchlist(ticker):
        raise HTTPException(status_code=404, detail=f"{ticker} is not in the watchlist")
    await sync_tracked_tickers(source)  # Keeps pricing it if a position is open
    return {"ticker": ticker, "removed": True}
```

The LLM's `watchlist_changes` go through the same DB functions plus `sync_tracked_tickers`, so the AI and the UI behave identically.

### 12.4 Trade execution & valuation

```python
async def execute_trade(ticker: str, side: str, quantity: float,
                        source: MarketDataSource, cache: PriceCache) -> dict:
    ticker = normalize_ticker(ticker)
    if ticker not in source.get_tickers():
        await source.add_ticker(ticker)  # Simulator prices it instantly
    price = cache.get_price(ticker)
    if price is None:
        await sync_tracked_tickers(source)  # Drop the speculative add
        raise TradeError(
            f"No live price for {ticker} yet. Add it to the watchlist and retry in a few seconds."
        )
    # ... validate cash / shares, update positions + trades, record snapshot ...
    await sync_tracked_tickers(source)  # A sell to zero may untrack; a buy keeps it tracked
    return {"ticker": ticker, "side": side, "quantity": quantity, "price": price}
```

- The **fill price is the cached price at the moment of the request**, which is the "instant fill at current price" from PLAN §2.
- Portfolio valuation (`GET /api/portfolio`, the 30 s snapshot task, LLM context) uses `cache.get_price(t)` for every position. If it's ever `None` (e.g., a Massive outage right after startup), fall back to `avg_cost` so totals stay defined, and mark the position `"price_stale": true` in the API response.
- `TradeError` is the same validation error type the chat flow reports back to the LLM (PLAN §9).

---

## 13. Testing

Tests live in `backend/tests/market/`. Run with `uv run --extra dev pytest`. The current suite (73 tests) stays. The tests below add coverage for this design and close the gap that let the Massive bug through.

### 13.1 GBM math & simulator

```python
import math
import numpy as np
import pytest

from app.market.simulator import GBMSimulator


def test_prices_stay_positive_under_extreme_volatility():
    sim = GBMSimulator(["TSLA"], dt=1 / 252, event_probability=0.5,
                       rng=np.random.default_rng(1))
    for _ in range(5_000):
        assert sim.step()["TSLA"] > 0


def test_log_returns_match_gbm_moments():
    """Mean/stdev of log returns ≈ (mu - sigma²/2)dt and sigma·√dt."""
    dt = 1 / 252
    sim = GBMSimulator(["AAPL"], dt=dt, event_probability=0.0, rng=np.random.default_rng(7))
    prices = [sim.get_price("AAPL")]
    for _ in range(20_000):
        sim.step()
        prices.append(sim.get_price("AAPL"))  # unrounded
    r = np.diff(np.log(prices))
    mu, sigma = 0.05, 0.22
    assert r.std() == pytest.approx(sigma * math.sqrt(dt), rel=0.03)
    assert r.mean() == pytest.approx((mu - sigma**2 / 2) * dt, abs=4 * r.std() / math.sqrt(len(r)))


def test_tech_pair_correlation_is_about_0_6():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], dt=1 / 252, event_probability=0.0,
                       rng=np.random.default_rng(3))
    hist = {t: [sim.get_price(t)] for t in sim.get_tickers()}
    for _ in range(20_000):
        sim.step()
        for t in hist:
            hist[t].append(sim.get_price(t))
    r = {t: np.diff(np.log(v)) for t, v in hist.items()}
    assert np.corrcoef(r["AAPL"], r["MSFT"])[0, 1] == pytest.approx(0.6, abs=0.03)
    assert np.corrcoef(r["AAPL"], r["JPM"])[0, 1] == pytest.approx(0.3, abs=0.03)


def test_full_default_watchlist_cholesky_succeeds():
    from app.market.seed_prices import SEED_PRICES
    sim = GBMSimulator(list(SEED_PRICES))
    assert sim._cholesky is not None and len(sim.step()) == 10


def test_ticker_normalization_and_validation():
    sim = GBMSimulator([" aapl "])
    assert sim.get_tickers() == ["AAPL"]
    with pytest.raises(ValueError):
        sim.add_ticker("NOT A TICKER")
```

### 13.2 Cache

```python
from app.market.cache import PriceCache


def test_remove_bumps_version():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v = cache.version
    cache.remove("AAPL")
    assert cache.version == v + 1
    cache.remove("AAPL")  # absent → no bump
    assert cache.version == v + 1


def test_day_open_sticky_and_overridable():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    assert cache.update("AAPL", 191.9).day_open == 190.0
    assert cache.update("AAPL", 192.0, day_open=188.0).day_change_percent == pytest.approx(2.1277, abs=1e-3)


def test_zero_timestamp_preserved():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


def test_concurrent_writers():
    import threading
    cache = PriceCache()
    def writer(t):
        for i in range(1_000):
            cache.update(t, 100 + i)
    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()
    assert len(cache) == 8 and cache.version == 8_000
```

### 13.3 Massive parsing against **real** SDK models

```python
from unittest.mock import MagicMock, patch

from massive.rest.models import TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource, parse_snapshot, to_unix_seconds


def snap(d: dict) -> TickerSnapshot:
    return TickerSnapshot.from_dict(d)


def test_parse_live_trade_with_nanosecond_timestamp():
    p = parse_snapshot(snap({
        "ticker": "AAPL",
        "lastTrade": {"p": 191.23, "t": 1760448600123456789},
        "prevDay": {"c": 189.50},
    }))
    assert p.price == 191.23
    assert p.timestamp == pytest.approx(1760448600.123, abs=1e-3)
    assert p.day_open == 189.50


@pytest.mark.parametrize("payload,expected", [
    ({"ticker": "X", "min": {"c": 10.5, "t": 1760448600000}}, 10.5),
    ({"ticker": "X", "day": {"c": 11.0}}, 11.0),
    ({"ticker": "X", "prevDay": {"c": 12.0}}, 12.0),
])
def test_parse_fallback_chain(payload, expected):
    assert parse_snapshot(snap(payload)).price == expected


def test_parse_unusable_returns_none():
    assert parse_snapshot(snap({"ticker": "X"})) is None
    assert parse_snapshot(snap({"ticker": "X", "lastTrade": {"p": 0}})) is None


@pytest.mark.parametrize("raw,secs", [
    (1760448600, 1760448600), (1760448600000, 1760448600),
    (1760448600000000, 1760448600), (1760448600000000000, 1760448600),
])
def test_to_unix_seconds_units(raw, secs):
    assert to_unix_seconds(raw) == pytest.approx(secs)


async def test_poll_ignores_ticker_removed_mid_flight():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._client, src._tickers = MagicMock(), ["AAPL", "MSFT"]

    def fetch(tickers):
        src._tickers.remove("MSFT")  # Simulates remove_ticker() during the request
        return [snap({"ticker": t, "lastTrade": {"p": 100.0, "t": 1760448600000000000}})
                for t in tickers]

    with patch.object(src, "_fetch_snapshots", side_effect=fetch):
        await src._poll_once()
    assert "AAPL" in cache and "MSFT" not in cache


async def test_backoff_then_recovery():
    src = MassiveDataSource("k", PriceCache(), poll_interval=15, max_backoff=120)
    src._client, src._tickers = MagicMock(), ["AAPL"]
    with patch.object(src, "_fetch_snapshots", side_effect=RuntimeError("429")):
        for expected in (30, 60, 120, 120):
            await src._poll_once()
            assert src._next_delay() == expected
    with patch.object(src, "_fetch_snapshots", return_value=[]):
        await src._poll_once()
    assert src._next_delay() == 15
```

### 13.4 Interface conformance (both sources, one test body)

```python
import pytest

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


@pytest.fixture(params=["simulator", "massive"])
async def source(request):
    cache = PriceCache()
    if request.param == "simulator":
        src = SimulatorDataSource(cache, update_interval=0.01)
    else:
        src = MassiveDataSource("k", cache, poll_interval=3600)
        src._fetch_snapshots = lambda tickers: []  # No network
    await src.start(["AAPL", "googl"])
    yield src, cache
    await src.stop()


async def test_contract(source):
    src, cache = source
    assert src.get_tickers() == ["AAPL", "GOOGL"]
    await src.add_ticker("AAPL")             # duplicate → no-op
    await src.add_ticker(" pypl ")
    assert src.get_tickers() == ["AAPL", "GOOGL", "PYPL"]
    await src.remove_ticker("GOOGL")
    await src.remove_ticker("NOPE")          # absent → no-op
    assert "GOOGL" not in src.get_tickers() and "GOOGL" not in cache
    with pytest.raises(ValueError):
        await src.add_ticker("bad ticker!")
    await src.stop(); await src.stop()       # idempotent
```

### 13.5 SSE generator

Testing the generator directly avoids needing a live server for an infinite stream:

```python
import json

from app.market.cache import PriceCache
from app.market.stream import _generate_events


class FakeRequest:
    client = None
    def __init__(self, disconnect_after: int):
        self._n = disconnect_after
    async def is_disconnected(self) -> bool:
        self._n -= 1
        return self._n < 0


async def test_stream_sends_retry_then_snapshot_only_on_change():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    events = [e async for e in _generate_events(cache, FakeRequest(3), interval=0, heartbeat=999)]
    assert events[0] == "retry: 1000\n\n"
    data = [e for e in events if e.startswith("data: ")]
    assert len(data) == 1  # version unchanged on later iterations
    assert json.loads(data[0][6:])["AAPL"]["direction"] == "flat"


async def test_stream_heartbeat_when_idle():
    events = [e async for e in _generate_events(PriceCache(), FakeRequest(3), interval=0, heartbeat=0)]
    assert ": ping\n\n" in events


def test_router_factory_is_reentrant():
    from app.market.stream import create_stream_router
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert len(r1.routes) == len(r2.routes) == 1
```

### 13.6 Manual checks

- `uv run market_data_demo.py`: live terminal dashboard of the simulator.
- `curl -N localhost:8000/api/stream/prices`: raw SSE once `main.py` exists.
- With a real key: `MASSIVE_API_KEY=... uv run python -c "from massive import RESTClient; from app.market.massive_client import parse_snapshot; print([parse_snapshot(s) for s in RESTClient().get_snapshot_all('stocks', tickers=['AAPL','MSFT'])])"`. Every element must be non-`None`.

---

## 14. Deltas from the Current Implementation

The existing code in `backend/app/market/` matches this design except for the items below, in priority order. Items 1–3 should land before the rest of the backend depends on market data.

| # | Severity | File | Current behavior | Change |
|---|---|---|---|---|
| 1 | **Critical** | `massive_client.py` | Reads `snap.last_trade.timestamp`, but the SDK's `LastTrade` has no such attribute (it's `sip_timestamp`, in **ns**). Every snapshot raises `AttributeError` and is skipped, so with a real API key **the cache stays empty**. Tests pass only because they use `MagicMock`. | Use `parse_snapshot()` (§9.2) with ns-aware `to_unix_seconds`. Add the real-model tests in §13.3. |
| 2 | High | `massive_client.py` | A ticker removed during an in-flight poll gets written back into the cache. The worker thread also reads the live `self._tickers` list. | Pass a copy to `_fetch_snapshots`. Skip snapshots for untracked tickers (§9.3). |
| 3 | High | `stream.py` | Module-level `router`. Calling `create_stream_router` twice registers `/prices` twice. | Create the router inside the factory (§11.1). |
| 4 | Medium | `models.py`, `cache.py`, `massive_client.py` | No daily-change basis, though the watchlist needs "daily change %" (PLAN §10). | Add `day_open` / `day_change_percent` (§4, §5). Massive supplies `prev_day.close`. |
| 5 | Medium | `cache.py` | `remove()` doesn't bump `version`, so with Massive, removed tickers linger on clients up to 15 s. `version` is read without the lock. `timestamp or time.time()` discards `0.0`. | As in §5. |
| 6 | Medium | `massive_client.py` | Fixed 15 s retry on every error, including 429 and bad keys. No price fallback when `last_trade` is missing. | Exponential backoff (§9.3) and the price fallback chain (§9.2). |
| 7 | Low | `simulator.py`, `interface.py` | Simulator doesn't normalize ticker case (Massive does). No input validation. | `normalize_ticker()` in both (§6). |
| 8 | Low | `simulator.py` | Uses the global `random` / `np.random`, so runs aren't reproducible in tests. A non-PD matrix would crash. | Injectable `np.random.Generator`. `LinAlgError` fallback (§8.4). |
| 9 | Low | `stream.py` | No heartbeat. Idle streams (Massive, market closed) can be cut by proxies. | `: ping` comment every 15 s (§11.1). |
| 10 | Low | `factory.py` | Poll interval fixed at 15 s. | `MASSIVE_POLL_INTERVAL` env var (§10). |

None of these changes alter the public API used downstream (`PriceCache.get/get_price/get_all`, `MarketDataSource.start/stop/add_ticker/remove_ticker/get_tickers`, `create_stream_router`). They only add the optional `day_open` argument and extra SSE fields, so existing tests continue to pass, apart from the Massive tests that assert on the mocked `last_trade.timestamp` field. Those must be updated to real `TickerSnapshot` objects.
