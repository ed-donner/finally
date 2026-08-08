# Market Data Backend — Detailed Design

Implementation-ready design for FinAlly's market data subsystem: the unified
provider interface, the shared price cache, the GBM simulator, the Massive
(Polygon.io) REST client, the SSE streaming endpoint, and FastAPI wiring.

Everything described here lives under `backend/app/market/`.

Companion documents:
- `MARKET_INTERFACE.md` — the contract, in prose
- `MARKET_SIMULATOR.md` — the simulation model, verified magnitudes
- `MASSIVE_API.md` — the real API, verified field names and units
- `MARKET_DATA_SUMMARY.md` — what shipped, test counts, coverage

---

## Table of Contents

1. [Design Goals](#1-design-goals)
2. [Architecture](#2-architecture)
3. [File Layout](#3-file-layout)
4. [`models.py` — PriceUpdate](#4-modelspy--priceupdate)
5. [`cache.py` — PriceCache](#5-cachepy--pricecache)
6. [`interface.py` — MarketDataSource](#6-interfacepy--marketdatasource)
7. [`seed_prices.py` — Tunable Constants](#7-seed_pricespy--tunable-constants)
8. [`simulator.py` — GBM Simulator](#8-simulatorpy--gbm-simulator)
9. [`massive_client.py` — Massive REST Poller](#9-massive_clientpy--massive-rest-poller)
10. [`factory.py` — Source Selection](#10-factorypy--source-selection)
11. [`stream.py` — SSE Endpoint](#11-streampy--sse-endpoint)
12. [`__init__.py` — Public Surface](#12-__init__py--public-surface)
13. [FastAPI Wiring](#13-fastapi-wiring)
14. [Consumer Patterns](#14-consumer-patterns)
15. [Frontend Integration](#15-frontend-integration)
16. [Testing Strategy](#16-testing-strategy)
17. [Error Handling & Edge Cases](#17-error-handling--edge-cases)
18. [Configuration Reference](#18-configuration-reference)
19. [Extending to a New Provider](#19-extending-to-a-new-provider)
20. [Deltas From the Current Implementation](#20-deltas-from-the-current-implementation)

---

## 1. Design Goals

| Goal | How it is achieved |
|---|---|
| Consumers never know where prices come from | Both sources implement one ABC; consumers read only from `PriceCache` |
| Swapping simulator ↔ real data is config, not code | `create_market_data_source()` reads `MASSIVE_API_KEY` |
| A data outage never takes down the app | Every loop catches all exceptions and continues; cache serves stale prices |
| The UI never shows a blank watchlist | `start()` and `add_ticker()` seed the cache before returning |
| The math is unit-testable without an event loop | `GBMSimulator` is pure, synchronous, cache-free |
| No wasted SSE bandwidth | Monotonic `version` counter drives change detection |

The single most important structural rule:

> **No consumer ever holds a reference to a `MarketDataSource`.** Sources are
> write-only producers. `PriceCache` is the only read contract.

The one exception is the watchlist route, which must call `add_ticker` /
`remove_ticker` — that is lifecycle management, not price reading.

---

## 2. Architecture

```
                    ┌──────────────────────────────────────┐
                    │       MarketDataSource (ABC)         │
                    │  start / stop / add_ticker /         │
                    │  remove_ticker / get_tickers         │
                    └──────────────────────────────────────┘
                              ▲                ▲
                              │                │
              ┌───────────────┴─────┐   ┌──────┴──────────────┐
              │ SimulatorDataSource │   │  MassiveDataSource  │
              │ GBM step, 500 ms    │   │  REST poll, 15 s    │
              │ in-process, no I/O  │   │  asyncio.to_thread  │
              └───────────────┬─────┘   └──────┬──────────────┘
                              │                │
                              └─── writes ─────┘
                                      │
                                      ▼
                        ┌───────────────────────────┐
                        │        PriceCache         │
                        │  dict + threading.Lock    │
                        │  + monotonic version      │
                        └─────────────┬─────────────┘
                                      │ reads
              ┌───────────────┬───────┴────────┬────────────────┐
              ▼               ▼                ▼                ▼
      GET /api/stream    Portfolio        Trade            LLM context
        /prices (SSE)    valuation        execution        builder
```

Timing is fully decoupled. The simulator writes every 500 ms; Massive writes
every 15 s; the SSE generator reads every 500 ms and emits only on change.
None of the three knows the others' cadence.

---

## 3. File Layout

```
backend/app/market/
├── __init__.py          # Public re-exports
├── models.py            # PriceUpdate — the unit of price data
├── cache.py             # PriceCache — thread-safe store, versioned
├── interface.py         # MarketDataSource — the producer ABC
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py         # GBMSimulator (pure math) + SimulatorDataSource (async)
├── massive_client.py    # MassiveDataSource — REST poller
├── factory.py           # create_market_data_source()
└── stream.py            # create_stream_router() — SSE endpoint
```

Dependency direction is strictly downward — `models` depends on nothing,
`cache` depends on `models`, the sources depend on `cache` + `interface` +
`seed_prices`, `factory` depends on the sources, and nothing depends on
`factory` except application startup. There are no cycles.

---

## 4. `models.py` — PriceUpdate

The only data structure that leaves the market data layer.

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
        """Absolute price change from the previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update."""
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

### Design decisions

- **`frozen=True`** — instances are handed simultaneously to the SSE
  serializer and to portfolio math. Nothing may mutate a price after the fact.
- **`slots=True`** — with 10 tickers at 2 Hz we allocate ~20 of these per
  second, plus one per cache write. Slots remove the per-instance `__dict__`.
- **Derived values are properties, not fields.** `change`, `change_percent`,
  and `direction` cannot drift out of sync with the prices they derive from.
  There is no code path that can produce `direction="up"` with a falling price.
- **`previous_price` is the previous *tick*, not the previous *close*.** It
  exists to drive the frontend's green/red flash. Day-over-day change is a
  separate concern owned by the portfolio layer.
- **`timestamp` is always Unix seconds (float).** Massive's nanosecond
  timestamps are normalized at the source boundary (§9), never downstream.
- **Zero-division guard** returns `0.0` rather than raising. A feed glitch must
  not propagate an exception into the SSE generator and kill a client stream.

### Wire format

`to_dict()` is the single serialization point, used by SSE and by any REST
route that returns a price:

```json
{
  "ticker": "AAPL",
  "price": 190.42,
  "previous_price": 190.35,
  "timestamp": 1785432000.123,
  "change": 0.07,
  "change_percent": 0.0368,
  "direction": "up"
}
```

---

## 5. `cache.py` — PriceCache

The central hub. Producers write, everyone else reads.

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MassiveDataSource (exactly one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution,
             LLM context builder.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # Monotonic; bumped on every update()

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Computes previous_price from the prior cached value. On the first
        update for a ticker, previous_price == price, so direction is 'flat'
        and the frontend does not flash on page load.
        """
        with self._lock:
            ts = time.time() if timestamp is None else timestamp
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
        """Latest update for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Drop a ticker (e.g. removed from the watchlist)."""
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        """Monotonic counter, incremented on every update()."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why `threading.Lock` and not `asyncio.Lock`

The Massive client is synchronous and runs inside `asyncio.to_thread(...)`, so
writes genuinely originate on a worker thread while SSE reads happen on the
event loop. An `asyncio.Lock` does not protect that boundary — it only
serializes coroutines on one loop. `threading.Lock` is correct from both sides.

Contention is negligible: the critical section is a dict lookup plus an
assignment, executed ~20 times/second.

### `ts = time.time() if timestamp is None else timestamp`

Written this way, not `timestamp or time.time()`. The `or` form silently
replaces a legitimate `0.0` timestamp with the current time — an epoch-zero
timestamp is exactly what a broken feed produces, and it should be visible in
the data rather than laundered into something plausible.

### Rounding at the boundary

Prices are rounded to 2 dp on write, in one place. Every consumer therefore
sees identical values: the UI cannot display $190.42 while a trade fills at
$190.41999999.

### The version counter

`version` answers "has anything changed since I last sent?" in O(1), with no
dict diffing and no timestamp comparison:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

Note it is incremented on *every* `update()`, even when the price is unchanged.
That is intentional — "the source produced a fresh observation" is the event
the stream cares about, not "the number differs". When the source stops
producing (loop cancelled, Massive polls failing), the version freezes and the
stream goes quiet rather than re-sending identical frames.

The property takes the lock. On CPython an `int` read is atomic under the GIL,
so this is not strictly required today, but it costs nothing and keeps the
class correct on a free-threaded build (PEP 703).

---

## 6. `interface.py` — MarketDataSource

```python
"""Abstract interface for market data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never asks a data source for a price — it reads
    from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Must populate the cache with initial prices BEFORE returning, then
        start a background task. Called exactly once per instance.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task and release resources.

        Idempotent — safe to call multiple times, including when never
        started. After stop(), the source never writes to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. Idempotent."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Idempotent.

        Must also remove the ticker from the PriceCache so a de-watchlisted
        symbol stops appearing in the SSE payload.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers. Synchronous; returns a copy."""
```

### Contract semantics

| Method | Guarantee | Simulator | Massive |
|---|---|---|---|
| `start(tickers)` | Cache populated before return | Seeds from `SEED_PRICES` | Performs one blocking poll |
| `stop()` | Idempotent, no further writes | Cancels task | Cancels task, drops client |
| `add_ticker(t)` | Idempotent | Price available immediately | Appears next poll cycle |
| `remove_ticker(t)` | Idempotent, evicts from cache | Rebuilds Cholesky | Filters ticker list |
| `get_tickers()` | Sync, returns a copy | Delegates to `GBMSimulator` | Copies internal list |

### Why this shape

- **Async lifecycle, sync accessor.** `start`/`stop`/`add`/`remove` are async
  because Massive performs network I/O and both manage asyncio tasks.
  `get_tickers()` is a pure in-memory read; forcing `await` on it would be
  noise at every call site.
- **No `get_price()` on the interface — deliberately.** If sources exposed
  price reads, consumers would couple to the source and the cache would become
  an implementation detail rather than the contract. Prices come from
  `PriceCache`, always.
- **Push, not pull.** Sources write on their own schedule. A consumer can never
  trigger a fetch, so a slow API can never block an HTTP request.

---

## 7. `seed_prices.py` — Tunable Constants

Constants only. No logic, no imports. Everything a person would want to tune
lives here rather than scattered through the simulator.

```python
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
#   sigma: annualized volatility (higher = more movement)
#   mu:    annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},     # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Applied to any ticker the user or the LLM adds
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the Cholesky decomposition
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6     # Tech names move together
INTRA_FINANCE_CORR = 0.5  # Bank / payments move together
CROSS_GROUP_CORR = 0.3    # Broad market beta; also the unknown-ticker default
TSLA_CORR = 0.3           # TSLA does its own thing
```

Volatilities are chosen to match each name's real-world character. The
*ordering* is what a user actually notices — TSLA visibly jumping while V
barely moves is the detail that sells the simulation.

There is deliberately **no separate `DEFAULT_CORR`**. An earlier revision
defined one alongside `CROSS_GROUP_CORR` with the same value 0.3; only
`CROSS_GROUP_CORR` was ever referenced. One constant, one meaning: "any pair
we have no specific rule for."

---

## 8. `simulator.py` — GBM Simulator

Two classes with a hard split:

```
GBMSimulator          ← pure math, synchronous, no asyncio, no PriceCache
    ├── step()          → advance all tickers one tick, return {ticker: price}
    ├── add_ticker()    → seed price/params, rebuild Cholesky
    ├── remove_ticker() → drop state, rebuild Cholesky
    ├── get_price()     → current price for one ticker
    └── get_tickers()   → tracked tickers

SimulatorDataSource   ← implements MarketDataSource, owns the asyncio task
    ├── start()         → build simulator, seed cache, launch _run_loop
    ├── stop()          → cancel task
    ├── add_ticker()    → delegate + seed cache immediately
    ├── remove_ticker() → delegate + evict from cache
    ├── get_tickers()   → delegate
    └── _run_loop()     → step → write cache → sleep, forever
```

`GBMSimulator` being free of asyncio and of `PriceCache` is what makes the math
directly testable: seed numpy, call `step()` ten thousand times, assert on the
distribution of log returns. No event loop, no mocks.

### 8.1 The model

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt  +  σ·√dt·Z )
          └──────┘   └────────────┘     └───────┘
           current       drift          diffusion
```

GBM is the standard equity price model (the basis of Black–Scholes) and has the
two properties that matter here: prices stay **strictly positive**, and
*returns* rather than absolute prices are normally distributed — so an $800
stock and a $15 stock both move in plausible percentage terms.

The `−σ²/2` term is the Itô correction. Without it the median path drifts below
the intended `μ`, because `E[exp(X)] ≠ exp(E[X])` for normal `X`. Its presence
is what makes `μ` mean "expected annual return" rather than an arbitrary knob.

**Choosing `dt`.** `dt` must be in the same units as `μ` and `σ`, which are
annualized. A *trading* year is:

```
252 trading days × 6.5 hours/day × 3600 s/hour = 5,896,800 seconds
```

so a 500 ms tick is `0.5 / 5_896_800 ≈ 8.479e-8`. Using wall-clock seconds per
year (31.5 M) instead would understate volatility by ~2.3×, making the terminal
look sleepy. Anchoring to trading time means an hour of watching FinAlly
produces about as much price action as an hour of watching a real market.

Resulting per-tick standard deviation (`σ·√dt`), measured against the shipped
seeds:

| Ticker | σ | Per-tick move | Per-tick $ | Over 1 min | Over 1 hr |
|---|---|---|---|---|---|
| AAPL | 0.22 | 0.0064% | $0.012 | 0.070% | 0.544% |
| TSLA | 0.50 | 0.0146% | $0.036 | 0.159% | 1.235% |
| NVDA | 0.40 | 0.0116% | $0.093 | 0.128% | 0.988% |
| JPM | 0.18 | 0.0052% | $0.010 | 0.057% | 0.445% |
| V | 0.17 | 0.0050% | $0.014 | 0.054% | 0.420% |

### 8.2 Correlation via Cholesky decomposition

Independent random walks look wrong. In a real market tech names move together
— when the sector sells off the whole watchlist reddens at once, and that
collective motion is most of what makes a terminal feel alive.

To generate correlated normals with target correlation matrix `C`:

1. Compute `C = L·Lᵀ` (Cholesky), `L` lower-triangular.
2. Draw independent standard normals `Z`.
3. `L·Z` has correlation matrix exactly `C`.

Each ticker then applies its own `σ` to its correlated draw, so tickers share
directional tendency while keeping individual volatility.

Pairwise resolution order:

```python
if t1 == "TSLA" or t2 == "TSLA":     return TSLA_CORR           # 0.3
if t1 in tech    and t2 in tech:     return INTRA_TECH_CORR     # 0.6
if t1 in finance and t2 in finance:  return INTRA_FINANCE_CORR  # 0.5
return CROSS_GROUP_CORR                                          # 0.3
```

TSLA is checked **first**, before the tech test. It is a member of the tech set
but deliberately decorrelated — it is the ticker most likely to be doing
something idiosyncratic, and giving it independence adds visual variety.

Verified on the default 10-ticker matrix: `AAPL/MSFT → 0.600`, `JPM/V → 0.500`,
`AAPL/JPM → 0.300`, `TSLA/AAPL → 0.300`, minimum eigenvalue `0.400`
(positive definite ✓). Empirical correlation of log returns over 20,000
simulated steps reproduces the targets to within sampling error (0.593, 0.497,
0.298, 0.295).

> **Positive-definiteness constraint.** `np.linalg.cholesky` raises
> `LinAlgError` on a non-positive-definite matrix, and hand-tuned correlations
> are easy to get wrong — A/B = 0.9, A/C = 0.9, B/C = 0.0 is not a realizable
> correlation matrix. Any change to these constants must be checked with
> `np.linalg.eigvalsh(corr).min() > 0`, and there is a unit test asserting the
> matrix builds for the default watchlist.

### 8.3 `GBMSimulator`

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

        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    where Z is a Cholesky-correlated standard normal draw. The tiny dt
    (~8.5e-8 for 500ms ticks) produces sub-cent moves per tick that
    accumulate naturally over a session.
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.479e-8

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
        """Advance all tickers one time step. Returns {ticker: new_price}.

        Hot path — called every 500ms. Keep it fast.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        if self._cholesky is not None:
            z_correlated = self._cholesky @ z_independent
        else:
            z_correlated = z_independent  # n == 1: nothing to correlate with

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu = params["mu"]
            sigma = params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock: ~0.1% chance per tick per ticker.
            # 10 tickers * 2 ticks/sec * 0.001 -> one event every ~50 seconds.
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
        """Add a ticker. Rebuilds the correlation matrix. Idempotent."""
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker. Rebuilds the correlation matrix. Idempotent."""
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
        """Currently tracked tickers (a copy)."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky (used for batch initialization)."""
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        # dict(...) is a COPY — sharing DEFAULT_PARAMS would let per-ticker
        # tuning silently mutate the defaults for every other unknown ticker.
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky factor of the correlation matrix.

        Called on every add/remove. O(n^2) to build, O(n^3) to factor, but
        n < 50 and it happens only on watchlist mutations.
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
        """Correlation between two tickers, by sector grouping.

          - TSLA with anything:   0.3 (checked first; it does its own thing)
          - Both tech:            0.6
          - Both finance:         0.5
          - Anything else:        0.3
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

### 8.4 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Runs a background asyncio task calling GBMSimulator.step() every
    `update_interval` seconds and writing the results to the PriceCache.
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
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
        )
        # Seed the cache BEFORE returning so the first SSE frame has data.
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
            # Seed immediately so the ticker shows a price without waiting a tick.
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

Three details carry weight:

- **`try`/`except` is *inside* the `while`.** A transient failure (say a
  Cholesky error after a pathological ticker add) logs and retries next tick
  rather than silently killing the task and freezing every price in the app.
  An exception escaping `_run_loop` would be swallowed by the Task and produce
  a UI that looks connected but never updates — the worst failure mode
  available.
- **`stop()` is idempotent**, including when never started: `self._task` is
  `None`, both guards short-circuit. FastAPI's lifespan teardown can call it
  unconditionally.
- **`get_tickers()` delegates** to `GBMSimulator.get_tickers()` rather than
  reaching into `_tickers`. The private-attribute access was flagged in review;
  the public accessor keeps the boundary clean and returns a copy either way.

### 8.5 Known limitations

Documented deliberately, since the simulator is the default experience.

**Low-priced tickers barely move visibly.** Prices round to 2 dp at the cache
boundary, so when `σ·√dt·S` is well under a cent most ticks round to the same
displayed price:

| Price level | Flat ticks | Visible move |
|---|---|---|
| $800 (NVDA) | 4.4% | 95.6% |
| $190 (AAPL) | 31.3% | 68.7% |
| $195 (JPM) | 35.3% | 64.7% |
| **$15** | **92.4%** | **7.6%** |

The default watchlist is unaffected (cheapest is GOOGL at $175), but a
user-added sub-$20 name looks nearly frozen. Mitigation if it ever matters:
scale `DEFAULT_PARAMS["sigma"]` up for low-priced tickers, or floor the
per-tick move. Not implemented — the default watchlist does not hit it.

Other simplifications: no mean reversion (prices random-walk and can wander far
over a long session); no volume, bid/ask, or order book (market orders with
instant fill need none); no market hours (a frozen weekend terminal would look
broken); no day-open reference (day-change % is computed by the portfolio layer
against the session's first observed price); static correlations.

---

## 9. `massive_client.py` — Massive REST Poller

Polls the full-market snapshot endpoint for the union of watched tickers in a
**single HTTP call per cycle**, so watchlist size never affects request count —
which is what makes the free tier's 5 req/min survivable.

| Tier | Poll interval | Requests/min |
|---|---|---|
| Free (Basic) | 15 s | 4 |
| Starter / Developer | 5 s | 12 |
| Advanced | 2 s | 30 |

> The snapshot endpoints are **not included in the free Basic plan** — a free
> key returns HTTP 403. FinAlly treats that as non-fatal and keeps serving
> cached prices, but users without a paid plan should leave `MASSIVE_API_KEY`
> unset and use the simulator.

### 9.1 Two boundary helpers

Both exist because of verified traps in the Massive SDK (see `MASSIVE_API.md`
§4.1–4.2). They are module-level functions, not methods, so they can be unit
tested without constructing a source.

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

# Anything larger than this is not plausibly Unix seconds, so scale it down.
_YEAR_2100_SECONDS = 4_102_444_800


def normalize_timestamp(raw: float | None) -> float | None:
    """Coerce a Massive timestamp (s / ms / us / ns) to Unix seconds.

    Massive returns NANOSECONDS for trade/quote SIP timestamps and the
    top-level `updated` field, but MILLISECONDS for aggregate bars. Rather
    than hardcode a divisor per field, scale by orders of magnitude until the
    value is a plausible epoch-seconds value. This is the fix for the classic
    "prices from 1970 / prices from year 55000" bug.
    """
    if raw is None:
        return None
    ts = float(raw)
    while ts > _YEAR_2100_SECONDS:
        ts /= 1000.0
    return ts


def extract_price(snap: Any) -> float | None:
    """Best available current price, in priority order.

    last_trade is only populated when the plan carries trade data AND a trade
    has occurred. Outside US market hours, or on a delayed plan, relying on it
    alone yields no data at all. This chain guarantees a usable price whenever
    the API returns anything.
    """
    if snap.last_trade and snap.last_trade.price:
        return snap.last_trade.price
    if snap.min and snap.min.close:            # most recent minute bar
        return snap.min.close
    if snap.day and snap.day.close:            # today's close so far
        return snap.day.close
    if snap.prev_day and snap.prev_day.close:  # market closed / pre-open
        return snap.prev_day.close
    return None


def extract_timestamp(snap: Any) -> float | None:
    """Unix-seconds timestamp for a snapshot.

    NOTE: the snapshot LastTrade has NO `.timestamp` attribute. The raw JSON
    key `t` maps to `.sip_timestamp`; writing `snap.last_trade.timestamp`
    raises AttributeError and silently drops every ticker in the batch.
    """
    if snap.last_trade and snap.last_trade.sip_timestamp:
        return normalize_timestamp(snap.last_trade.sip_timestamp)
    return normalize_timestamp(getattr(snap, "updated", None))
```

### 9.2 `MassiveDataSource`

```python
class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in one call, then writes the results to the PriceCache.

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
        self._tickers = [t.upper().strip() for t in tickers]

        # Immediate first poll so the cache has data before we return.
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
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added ticker %s (appears on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal ---

    async def _poll_loop(self) -> None:
        """Poll on interval. The first poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll cycle: fetch snapshots, write to cache."""
        if not self._tickers or not self._client:
            return

        try:
            # RESTClient is SYNCHRONOUS (urllib3). Calling it directly on the
            # event loop would stall every SSE stream for the request duration.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)

            processed = 0
            for snap in snapshots:
                try:
                    price = extract_price(snap)
                    if price is None:
                        continue
                    self._cache.update(
                        ticker=snap.ticker,
                        price=price,
                        timestamp=extract_timestamp(snap),
                    )
                    processed += 1
                except (AttributeError, TypeError) as e:
                    # Per-ticker guard: one malformed snapshot must not abort
                    # the whole batch.
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug(
                "Massive poll: updated %d/%d tickers", processed, len(self._tickers)
            )

        except Exception as e:
            # Never let a data outage kill the loop. Common: 401 (bad key),
            # 403 (plan lacks snapshots), 429 (rate limit), network timeout.
            logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Blocking call to the Massive REST API. Runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 9.3 Error handling philosophy

| Failure | Behaviour |
|---|---|
| 401 invalid key | Logged as error; poller keeps running (fix `.env`, restart) |
| 403 plan lacks snapshots | Logged as error; cache stays empty; user should unset the key |
| 429 rate limited | Logged; next cycle retries after `poll_interval` |
| Network timeout / 5xx | SDK auto-retries 3×, then logged; retried next cycle |
| Malformed single snapshot | That ticker skipped with a warning; batch continues |
| Every ticker fails | Cache retains last-known prices; SSE keeps streaming stale data |

The rule shared with the simulator: **the loop never dies.** A market data
problem degrades to stale prices, never to a 500 on the SSE endpoint.

### 9.4 Why imports are at module level

`from massive import RESTClient` sits at the top of the file, not inside
`start()`. `massive` is a declared core dependency in `pyproject.toml`, so it
is always installed; a lazy import bought nothing and actively broke tests —
`patch("app.market.massive_client.RESTClient")` cannot patch a name that does
not exist at module level until `start()` runs. Top-level imports make the
mock target real and the failure mode (missing dependency) loud at import time
instead of at first poll.

---

## 10. `factory.py` — Source Selection

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
    """Select a market data source from the environment.

    - MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real data)
    - Otherwise                         -> SimulatorDataSource (GBM)

    Returns an UNSTARTED source. The caller owns the lifecycle and must
    await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

| `MASSIVE_API_KEY` | Result |
|---|---|
| Unset | Simulator |
| Empty string | Simulator |
| Whitespace only | Simulator (`.strip()`) |
| Non-empty | Massive |

The `.strip()` matters in practice: a `.env` line reading `MASSIVE_API_KEY= `
with a trailing space would otherwise select the real API with a garbage key,
and the app would silently show no prices at all.

Returning an **unstarted** source keeps the factory synchronous and makes
selection logic trivially unit-testable — `monkeypatch.setenv`, call, assert on
the type, no event loop and no background tasks.

---

## 11. `stream.py` — SSE Endpoint

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


def create_stream_router(
    price_cache: PriceCache,
    interval: float = 0.5,
) -> APIRouter:
    """Build the SSE router bound to a specific PriceCache.

    The router is created INSIDE the factory, not at module level: a
    module-level router would accumulate a duplicate /prices route on every
    call, which bites as soon as two tests each build their own app.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE stream of live prices.

        Clients connect with the native EventSource API and receive frames of
        the form:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}
        """
        return StreamingResponse(
            _generate_events(price_cache, request, interval),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",  # Defeat nginx response buffering
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames, one per cache version change.

    Runs until the client disconnects. Note the return annotation: this is an
    async generator, so `-> None` would be a lie to type checkers.
    """
    # Browser EventSource reconnects after this many ms if the stream drops.
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
                    data = {t: u.to_dict() for t, u in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1785432000.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

data: {...}
```

### Design notes

- **All tickers in every frame, keyed by ticker.** Simpler for the client than
  per-ticker events, and at ~10 tickers the payload is ~1 KB — trivial. The
  frontend does a single `Object.entries()` pass per frame.
- **Poll-and-push rather than event-driven.** The generator reads the cache on
  a fixed cadence instead of being woken by the producer. This gives evenly
  spaced frames, which matters because the frontend accumulates them into
  sparklines — irregular spacing would produce visibly lumpy charts.
- **Stream cadence is independent of source cadence.** With the simulator
  (500 ms) the client sees roughly one frame per tick. With Massive (15 s) the
  version is unchanged between polls, so frames are emitted only when new data
  actually lands. No wasted bandwidth, no configuration coupling.
- **Disconnect detection** via `await request.is_disconnected()` at the top of
  each iteration; without it, a closed tab leaves the generator running until
  the next write fails.

### Optional: idle keepalive

If the source stops producing (Massive polls failing, simulator stopped) the
stream goes completely silent, and an intermediate proxy may reap the
connection as idle. A comment frame is a cheap insurance policy — SSE comment
lines start with `:` and are ignored by `EventSource`:

```python
last_emit = time.monotonic()
...
    if current_version != last_version:
        ...
        last_emit = time.monotonic()
    elif time.monotonic() - last_emit > 15.0:
        yield ": keepalive\n\n"
        last_emit = time.monotonic()
```

Not required for the single-container localhost deployment, which has no proxy
in the path. Add it if FinAlly is ever put behind nginx, App Runner, or a CDN.

---

## 12. `__init__.py` — Public Surface

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate               - Immutable price snapshot dataclass
    PriceCache                - Thread-safe in-memory price store
    MarketDataSource          - Abstract interface for data providers
    create_market_data_source - Factory selecting simulator or Massive
    create_stream_router      - FastAPI router factory for the SSE endpoint
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

The rest of the backend imports from `app.market`, never from
`app.market.simulator` or `app.market.massive_client`. Concrete source classes
are intentionally *not* exported — importing one by name is exactly the
coupling this design exists to prevent. Tests may import them directly; that is
the only legitimate use.

---

## 13. FastAPI Wiring

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import load_watchlist_tickers
from app.market import (
    MarketDataSource,
    PriceCache,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    cache = PriceCache()
    source = create_market_data_source(cache)

    tickers = load_watchlist_tickers()   # from SQLite; seeds on first run
    await source.start(tickers)          # cache is populated before this returns

    app.state.price_cache = cache
    app.state.market_source = source

    try:
        yield
    finally:
        # --- shutdown --- (finally: runs even if startup of a later
        # component raised, so we never leak the background task)
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
```

### Router registration

The SSE router needs the cache, which is created inside `lifespan`. Two
workable orders:

**A. Create the cache at module scope** (simplest, and what the routes below
assume):

```python
price_cache = PriceCache()
app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

with `lifespan` reusing that instance rather than constructing its own.

**B. Include the router inside `lifespan`** before `yield`. FastAPI permits
this, but routes added after startup do not appear in the OpenAPI schema
generated at first request in some versions, so prefer A.

Either way, **do not use module-level globals for the cache in tests** — each
test app should get its own `PriceCache` so state cannot leak between tests.

### Dependency accessors

```python
from fastapi import Depends, HTTPException, Request


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

Taking `Request` rather than closing over a module global is what keeps these
usable from a test app built with a different cache.

### Watchlist coordination

Watchlist mutations must update the database **and** the data source. Ticker
normalization happens at the API boundary and defensively inside both sources,
because the cache is keyed by exact string.

```python
@router.post("/api/watchlist")
async def add_to_watchlist(
    body: TickerBody,
    source: MarketDataSource = Depends(get_market_source),
    cache: PriceCache = Depends(get_price_cache),
):
    ticker = body.ticker.upper().strip()
    db_add_ticker(ticker)
    await source.add_ticker(ticker)
    # Simulator: price is available immediately. Massive: None until next poll.
    update = cache.get(ticker)
    return {"ticker": ticker, "price": update.price if update else None}


@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper().strip()
    db_remove_ticker(ticker)

    # Keep tracking if the user still holds shares — portfolio valuation
    # reads the same cache, and dropping the ticker would zero the position.
    position = db_get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"ok": True}
```

That position check is the one piece of business logic the market layer does
not own but depends on. It belongs in the watchlist route, not in the data
source — the source has no concept of a portfolio.

---

## 14. Consumer Patterns

```python
cache: PriceCache = request.app.state.price_cache

# Single price, for trade execution
price = cache.get_price("AAPL")
if price is None:
    raise HTTPException(400, "No price available for AAPL yet")

# Full update with direction, for display
update = cache.get("AAPL")

# Portfolio valuation across all positions
prices = cache.get_all()
total_value = cash + sum(
    pos.quantity * prices[pos.ticker].price
    for pos in positions
    if pos.ticker in prices
)
```

**Always handle `None`.** A ticker is absent from the cache when it was just
added and the first Massive poll has not landed, or when every poll has failed
since startup. Trade execution must reject rather than assume a price:

```python
@router.post("/api/portfolio/trade")
async def execute_trade(
    body: TradeRequest,
    cache: PriceCache = Depends(get_price_cache),
):
    ticker = body.ticker.upper().strip()
    price = cache.get_price(ticker)
    if price is None:
        raise HTTPException(
            400, f"Price not yet available for {ticker}. Try again in a moment."
        )
    # ... validate cash / shares, insert trade, update position, snapshot ...
```

The LLM context builder reads the same way — cash, positions marked to
`cache.get_all()`, watchlist with live prices — so the assistant sees exactly
the numbers the user sees on screen.

---

## 15. Frontend Integration

```typescript
type PriceUpdate = {
  ticker: string;
  price: number;
  previous_price: number;
  timestamp: number;          // Unix seconds
  change: number;
  change_percent: number;
  direction: "up" | "down" | "flat";
};

const es = new EventSource("/api/stream/prices");

es.onmessage = (event) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(event.data);
  for (const [ticker, update] of Object.entries(prices)) {
    applyPrice(ticker, update);          // flash green/red on direction
    appendSparklinePoint(ticker, update.price, update.timestamp);
  }
  setConnectionStatus("connected");
};

es.onerror = () => setConnectionStatus("reconnecting");
es.onopen = () => setConnectionStatus("connected");
```

`EventSource` reconnects on its own using the `retry: 1000` directive; there is
no client reconnect logic to write. `onerror` fires on the drop, `onopen` on
recovery — which is exactly the signal the header's connection dot needs
(green / yellow / red).

Sparklines accumulate client-side from the stream since page load, per the
plan; there is no historical price endpoint, and none is needed.

---

## 16. Testing Strategy

The layering makes each piece independently testable. Backend tests live in
`backend/tests/market/`, run with `uv run --extra dev pytest`.

| Module | Test file | What it covers |
|---|---|---|
| `models.py` | `test_models.py` | Property math, zero-division guard, `to_dict()` shape |
| `cache.py` | `test_cache.py` | update/get/remove, version, flat-first, rounding, threads |
| `simulator.py` | `test_simulator.py` | GBM math, correlation, shocks, add/remove |
| `simulator.py` | `test_simulator_source.py` | Async lifecycle against a real cache |
| `massive_client.py` | `test_massive.py` | Parsing, timestamp units, error resilience |
| `factory.py` | `test_factory.py` | Env-var selection across all four cases |

### 16.1 Pure math — no event loop, no mocks

```python
import numpy as np
import pytest

from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator


def test_prices_stay_positive():
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        assert sim.step()["AAPL"] > 0


def test_initial_price_is_the_seed():
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]


def test_unknown_ticker_gets_random_seed_in_range():
    sim = GBMSimulator(tickers=["ZZZZ"])
    assert 50.0 <= sim.get_price("ZZZZ") <= 300.0


def test_default_params_are_copied_not_shared():
    sim = GBMSimulator(tickers=["ZZZZ", "YYYY"])
    sim._params["ZZZZ"]["sigma"] = 99.0
    assert sim._params["YYYY"]["sigma"] == 0.25


def test_cholesky_builds_for_full_default_watchlist():
    sim = GBMSimulator(tickers=list(SEED_PRICES))
    assert sim._cholesky is not None
    corr = sim._cholesky @ sim._cholesky.T
    assert np.linalg.eigvalsh(corr).min() > 0        # positive definite
    i, j = list(SEED_PRICES).index("AAPL"), list(SEED_PRICES).index("MSFT")
    assert corr[i, j] == pytest.approx(0.6, abs=1e-9)


def test_empirical_volatility_matches_sigma():
    np.random.seed(42)
    sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
    prices = [sim.get_price("AAPL")]
    for _ in range(20_000):
        prices.append(sim.step()["AAPL"])
    returns = np.diff(np.log(prices))
    expected = 0.22 * np.sqrt(GBMSimulator.DEFAULT_DT)
    assert returns.std() == pytest.approx(expected, rel=0.1)


def test_shock_events_are_deterministic_at_the_extremes():
    always = GBMSimulator(tickers=["AAPL"], event_probability=1.0)
    before = always.get_price("AAPL")
    after = always.step()["AAPL"]
    assert abs(after / before - 1) >= 0.019          # a 2-5% shock landed

    never = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
    b = never.get_price("AAPL")
    a = never.step()["AAPL"]
    assert abs(a / b - 1) < 0.001                    # diffusion only


def test_single_and_empty_ticker_do_not_crash():
    assert GBMSimulator(tickers=[]).step() == {}
    assert GBMSimulator(tickers=["AAPL"])._cholesky is None
```

Setting `event_probability=0.0` is what makes the volatility test stable — a
stray 2-5% shock in a 20,000-step sample would swamp a `σ·√dt` measurement.

### 16.2 Cache — including real thread contention

```python
import threading

from app.market.cache import PriceCache


def test_first_update_is_flat():
    cache = PriceCache()
    update = cache.update("AAPL", 190.50)
    assert update.direction == "flat"
    assert update.previous_price == 190.50
    assert update.change == 0.0


def test_direction_and_change():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    up = cache.update("AAPL", 191.00)
    assert (up.direction, up.change) == ("up", 1.0)
    down = cache.update("AAPL", 189.00)
    assert (down.direction, down.change) == ("down", -2.0)


def test_price_is_rounded_at_the_boundary():
    cache = PriceCache()
    assert cache.update("AAPL", 190.41999999).price == 190.42


def test_explicit_zero_timestamp_is_preserved():
    cache = PriceCache()
    assert cache.update("AAPL", 190.0, timestamp=0.0).timestamp == 0.0


def test_version_increments_on_every_update():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 190.00)      # same price still counts as an observation
    assert cache.version == v0 + 2


def test_concurrent_writes_do_not_lose_updates():
    cache = PriceCache()

    def writer(ticker: str):
        for i in range(1000):
            cache.update(ticker, 100.0 + i)

    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert cache.version == 8000
    assert len(cache) == 8
```

### 16.3 Simulator source — async integration

```python
import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


async def test_start_populates_cache_before_returning():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
    await source.start(["AAPL", "GOOGL"])
    # No sleep — start() must have seeded synchronously.
    assert cache.get("AAPL") is not None
    assert cache.get("GOOGL") is not None
    await source.stop()


async def test_stop_halts_writes():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
    await source.start(["AAPL"])
    await asyncio.sleep(0.05)
    await source.stop()

    frozen = cache.version
    await asyncio.sleep(0.05)
    assert cache.version == frozen


async def test_stop_is_idempotent_even_if_never_started():
    source = SimulatorDataSource(price_cache=PriceCache())
    await source.stop()
    await source.stop()


async def test_add_seeds_and_remove_evicts():
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
    await source.start(["AAPL"])

    await source.add_ticker("TSLA")
    assert "TSLA" in source.get_tickers()
    assert cache.get("TSLA") is not None    # seeded immediately, no tick needed

    await source.remove_ticker("TSLA")
    assert "TSLA" not in source.get_tickers()
    assert cache.get("TSLA") is None        # evicted from the cache too

    await source.stop()


async def test_step_failure_does_not_kill_the_loop(monkeypatch):
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
    await source.start(["AAPL"])

    calls = {"n": 0}
    real_step = source._sim.step

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return real_step()

    monkeypatch.setattr(source._sim, "step", flaky)
    await asyncio.sleep(0.05)
    assert calls["n"] > 1                   # loop survived and kept ticking
    await source.stop()
```

Tests use `update_interval=0.01` so integration cases finish in milliseconds.
`asyncio_mode = "auto"` in `pyproject.toml` means no `@pytest.mark.asyncio`
decorator is needed.

### 16.4 Massive — build snapshots from raw JSON

Hand-constructing a `MagicMock` snapshot passes even when the attribute names
are wrong, which is exactly the bug class that hurts here. Build from raw JSON
via the SDK's own `from_dict` so a `sip_timestamp`/`timestamp` mix-up fails the
test:

```python
from unittest.mock import patch

from massive.rest.models import TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import (
    MassiveDataSource,
    extract_price,
    normalize_timestamp,
)

RAW = {
    "ticker": "AAPL",
    "todaysChange": 1.25,
    "todaysChangePerc": 0.66,
    "updated": 1785432000_000_000_000,          # nanoseconds
    "day": {"o": 189.0, "h": 191.5, "l": 188.7, "c": 190.5, "v": 41_000_000},
    "prevDay": {"o": 187.0, "h": 189.9, "l": 186.5, "c": 189.25, "v": 38_000_000},
    "min": {"o": 190.3, "h": 190.6, "l": 190.2, "c": 190.42, "v": 120_000},
    "lastTrade": {"p": 190.42, "s": 100, "t": 1785432000_123_000_000},
}


def _snapshot(*drop: str) -> TickerSnapshot:
    """Build a real TickerSnapshot from raw JSON, omitting the named keys.

    Keys are dropped rather than set to None: `from_dict` calls `.get()` on
    each nested object, so an explicit None raises inside the SDK.
    """
    return TickerSnapshot.from_dict({k: v for k, v in RAW.items() if k not in drop})


def test_normalize_timestamp_handles_every_unit():
    seconds = 1785432000.0
    assert normalize_timestamp(1785432000) == seconds
    assert normalize_timestamp(1785432000_000) == seconds            # ms
    assert normalize_timestamp(1785432000_000_000) == seconds        # us
    assert normalize_timestamp(1785432000_000_000_000) == seconds    # ns
    assert normalize_timestamp(None) is None


def test_price_fallback_chain():
    assert extract_price(_snapshot()) == 190.42                  # last_trade
    assert extract_price(_snapshot("lastTrade")) == 190.42       # min bar
    assert extract_price(_snapshot("lastTrade", "min")) == 190.5  # day
    assert extract_price(_snapshot("lastTrade", "min", "day")) == 189.25  # prev_day
    assert extract_price(_snapshot("lastTrade", "min", "day", "prevDay")) is None


def test_snapshot_last_trade_has_no_timestamp_attribute():
    """Guards against regressing to `snap.last_trade.timestamp` (see §20)."""
    with pytest.raises(AttributeError):
        _snapshot().last_trade.timestamp


async def test_poll_writes_price_and_seconds_timestamp():
    cache = PriceCache()
    source = MassiveDataSource("k", cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    source._client = object()          # non-None so _poll_once proceeds

    with patch.object(source, "_fetch_snapshots", return_value=[_snapshot()]):
        await source._poll_once()

    update = cache.get("AAPL")
    assert update.price == 190.42
    assert 1_700_000_000 < update.timestamp < 2_000_000_000   # plausible seconds


async def test_one_bad_snapshot_does_not_abort_the_batch():
    cache = PriceCache()
    source = MassiveDataSource("k", cache, poll_interval=60.0)
    source._tickers = ["AAPL", "BAD"]
    source._client = object()

    class Broken:
        ticker = "BAD"
        last_trade = None
        min = None
        day = None
        prev_day = None

    with patch.object(
        source, "_fetch_snapshots", return_value=[_snapshot(), Broken()]
    ):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.42
    assert cache.get_price("BAD") is None


async def test_api_failure_is_swallowed():
    cache = PriceCache()
    source = MassiveDataSource("k", cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    source._client = object()

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("429")):
        await source._poll_once()      # must not raise

    assert cache.get_price("AAPL") is None
```

Setting `source._client` directly (rather than calling `start()`) is what keeps
these tests network-free while still exercising the real `_poll_once` path.

### 16.5 Factory — all four env-var cases

```python
import pytest

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, SimulatorDataSource),
        ("", SimulatorDataSource),
        ("   ", SimulatorDataSource),
        ("real-key", MassiveDataSource),
    ],
)
def test_selection(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MASSIVE_API_KEY", value)
    assert isinstance(create_market_data_source(PriceCache()), expected)
```

### 16.6 Conformance — both sources, one test

The contract in §6 is only real if both implementations satisfy it identically:

```python
@pytest.fixture(params=["simulator", "massive"])
def source_and_cache(request):
    cache = PriceCache()
    if request.param == "simulator":
        yield SimulatorDataSource(price_cache=cache, update_interval=0.01), cache
    else:
        src = MassiveDataSource("k", cache, poll_interval=60.0)
        with patch.object(src, "_fetch_snapshots", return_value=[_snapshot()]):
            yield src, cache


async def test_contract(source_and_cache):
    source, cache = source_and_cache
    await source.start(["AAPL"])
    assert cache.get("AAPL") is not None            # seeded before return
    assert source.get_tickers() == ["AAPL"]

    await source.remove_ticker("AAPL")
    assert cache.get("AAPL") is None                # evicted
    assert source.get_tickers() == []

    await source.stop()
    await source.stop()                             # idempotent
```

### 16.7 SSE — needs an ASGI client

`stream.py` is the least covered module because the generator needs a running
ASGI app. One integration test is worth the setup:

```python
import httpx
from fastapi import FastAPI

from app.market import PriceCache, create_stream_router


async def test_sse_emits_a_frame_per_version_change():
    cache = PriceCache()
    app = FastAPI()
    app.include_router(create_stream_router(cache, interval=0.01))
    cache.update("AAPL", 190.42)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        async with client.stream("GET", "/api/stream/prices") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            frames = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                    if len(frames) == 1:
                        break

    assert frames[0]["AAPL"]["price"] == 190.42
    assert frames[0]["AAPL"]["direction"] == "flat"
```

### 16.8 Testing consumers without any data source

The payoff of the cache-as-contract design: portfolio math, trade execution,
and LLM context are tested by pre-populating a cache. No simulator, no mocks,
no async.

```python
def test_portfolio_valuation():
    cache = PriceCache()
    cache.update("AAPL", 200.00)
    cache.update("MSFT", 400.00)

    positions = [Position("AAPL", qty=10, avg_cost=190.0),
                 Position("MSFT", qty=5, avg_cost=420.0)]

    assert compute_total_value(cash=1000.0, positions=positions, cache=cache) == 5000.0
```

---

## 17. Error Handling & Edge Cases

| Situation | Behaviour |
|---|---|
| **Empty watchlist at startup** | `start([])` is valid. Simulator produces `{}` per tick; Massive skips the HTTP call. SSE emits nothing (empty dict is falsy). Adding a ticker starts tracking immediately. |
| **Trade on an uncached ticker** | `get_price()` returns `None` → HTTP 400 with a clear retry message. Never assume or fabricate a price. |
| **Invalid Massive key** | First poll 401s, logged, poller keeps retrying. SSE connects successfully but carries no data — the connection dot is green while the grid is empty. Documented so it is not mistaken for a stream bug. |
| **Free-tier Massive key** | Snapshot endpoint 403s every poll. Same visible outcome as above. Fix: unset `MASSIVE_API_KEY`. |
| **Massive returns a partial batch** | Missing tickers keep their last cached value. No entry is deleted on a failed poll. |
| **Ticker removed while held** | Watchlist route keeps it in the source when a position exists (§13), so valuation stays correct. |
| **Duplicate `add_ticker`** | Idempotent in both sources; no duplicate Cholesky row, no duplicated poll entry. |
| **Cholesky failure on a pathological correlation set** | Raised inside `step()` → caught by `_run_loop` → logged → retried. Prices freeze for one tick rather than forever. |
| **Client disconnects mid-frame** | `is_disconnected()` breaks the loop; `CancelledError` is caught and logged. No traceback spam in the logs on every closed tab. |
| **Float precision** | Not a concern: `exp()` is numerically stable, prices are strictly positive by construction, and everything rounds to 2 dp at the cache boundary. |
| **Memory** | The cache stores exactly one `PriceUpdate` per ticker — O(tickers), not O(time). No history accumulates server-side. |

---

## 18. Configuration Reference

| Parameter | Location | Default | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | environment / `.env` | `""` | Non-empty selects the real API; otherwise simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick cadence |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Massive poll cadence (match your tier) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Shock chance per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.479e-8` | GBM step as a fraction of a trading year |
| `interval` | `create_stream_router` / `_generate_events` | `0.5` s | SSE cache-poll cadence |
| retry directive | `_generate_events` | `1000` ms | Browser `EventSource` reconnect delay |
| `SEED_PRICES` | `seed_prices.py` | see §7 | Starting prices |
| `TICKER_PARAMS` | `seed_prices.py` | see §7 | Per-ticker σ and μ |
| correlation constants | `seed_prices.py` | 0.6 / 0.5 / 0.3 | Sector coupling |

### Tuning guide

| Goal | Change |
|---|---|
| More dramatic price action | Raise `sigma` in `TICKER_PARAMS` |
| Faster/slower updates | `SimulatorDataSource(update_interval=...)` |
| More/fewer shock events | `event_probability` |
| Stronger sector coupling | Raise `INTRA_TECH_CORR` — **re-verify positive definiteness** |
| Persistent bull market | Raise `mu` across `TICKER_PARAMS` |

`update_interval` and `dt` are independent knobs. Halving the interval to
250 ms while leaving `dt` at its 500 ms value makes the market run at half
speed. To keep them consistent, derive one from the other:

```python
dt = update_interval / GBMSimulator.TRADING_SECONDS_PER_YEAR
```

---

## 19. Extending to a New Provider

To add an Alpaca, Finnhub, or IEX feed:

1. Implement `MarketDataSource` in a new module under `app/market/`.
2. Convert that provider's price and timestamp to floats in Unix **seconds**,
   and call `cache.update(ticker=..., price=..., timestamp=...)`. Normalize at
   this boundary — never let provider units leak downstream.
3. Wrap any synchronous client in `asyncio.to_thread(...)`.
4. Catch all exceptions in the poll loop; log and continue.
5. Seed the cache before `start()` returns.
6. Add a branch to `create_market_data_source()`.
7. Add the class to the conformance-test parametrization (§16.6).

No consumer changes, no SSE changes, no frontend changes. That property is the
entire point of this design.

---

## 20. Deltas From the Current Implementation

The code in `backend/app/market/` predates this document and is broadly
identical to it. Four points in §5, §9, and §11 above describe the intended
design where the shipped code currently differs. Listed here so the difference
is a decision rather than a surprise.

| # | Location | Current code | This design | Severity |
|---|---|---|---|---|
| 1 | `massive_client.py` `_poll_once` | `snap.last_trade.timestamp` | `extract_timestamp()` → `sip_timestamp`, normalized | **High** — the snapshot `LastTrade` has no `.timestamp`; per `MASSIVE_API.md` §4.1 this raises `AttributeError`, and the per-ticker guard catches it, so *every* ticker is skipped and the cache never fills |
| 2 | `massive_client.py` `_poll_once` | `snap.last_trade.price` only | `extract_price()` fallback chain `last_trade → min → day → prev_day` | **High** — outside US market hours, or on a delayed plan, `last_trade` is `None` and no price is ever produced |
| 3 | `massive_client.py` | `/ 1000.0` (assumes milliseconds) | `normalize_timestamp()` scales any unit to seconds | **Medium** — snapshot SIP timestamps are nanoseconds; dividing by 1000 yields a year-56000 timestamp |
| 4 | `stream.py` | Module-level `router`, mutated by `create_stream_router()` | Router constructed inside the factory | **Low** — a second call registers a duplicate `/prices` route; only bites when two tests each build an app |
| 5 | `cache.py` | `ts = timestamp or time.time()` | `ts = time.time() if timestamp is None else timestamp` | **Low** — an explicit `0.0` is silently replaced by "now" |
| 6 | `cache.py` | `version` property reads without the lock | Reads under the lock | **Trivial** — atomic under the GIL today |

Items 1–3 are all in the Massive path, which is unreachable without a paid API
key and is therefore untested against the live API. They share one root cause:
the original client was written against an assumed field layout rather than the
SDK's verified one. `MASSIVE_API.md` is the authority — its field tables were
verified by introspecting the installed SDK.

Fixing 1–3 is a contained change to `_poll_once` plus the two helper functions,
and §16.4 gives the tests that would have caught them: building snapshots with
`TickerSnapshot.from_dict(raw_json)` rather than `MagicMock` makes a wrong
attribute name fail loudly instead of silently returning another mock.
