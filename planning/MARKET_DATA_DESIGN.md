# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem. Covers the unified interface, in-memory price cache, GBM simulator, Massive REST client, SSE streaming endpoint, watchlist coordination, and FastAPI lifecycle integration.

All code in this document lives under `backend/app/market/` unless otherwise noted. Test code lives under `backend/tests/market/`.

This doc synthesises:

- `planning/PLAN.md` §6 (market data) and §8 (API contract) — the product contract
- `planning/MARKET_INTERFACE.md` — the current abstraction shape and known hygiene gaps
- `planning/MARKET_SIMULATOR.md` — GBM math, parameters, correlation structure
- `planning/MASSIVE_API.md` — REST endpoint choice, tiering, timestamp gotchas
- `planning/REVIEW.md` — atomicity, test gaps, and operational risks

It supersedes the older `planning/archive/MARKET_DATA_DESIGN.md` by incorporating fixes for the issues flagged there: nanosecond timestamps for Massive snapshots, fresh `APIRouter` per stream factory call, version-read under lock, public `get_tickers()` on the simulator, correct async generator return type, and DB-first / cache-second ordering for watchlist mutations.

---

## Table of Contents

1. [File Structure](#1-file-structure)
2. [Data Model — `models.py`](#2-data-model--modelspy)
3. [Price Cache — `cache.py`](#3-price-cache--cachepy)
4. [Abstract Interface — `interface.py`](#4-abstract-interface--interfacepy)
5. [Seed Prices & Parameters — `seed_prices.py`](#5-seed-prices--parameters--seed_pricespy)
6. [GBM Simulator — `simulator.py`](#6-gbm-simulator--simulatorpy)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client--massive_clientpy)
8. [Factory — `factory.py`](#8-factory--factorypy)
9. [SSE Streaming Endpoint — `stream.py`](#9-sse-streaming-endpoint--streampy)
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
      __init__.py          # Public re-exports
      models.py            # PriceUpdate dataclass
      cache.py             # PriceCache (thread-safe in-memory store)
      interface.py         # MarketDataSource ABC
      seed_prices.py       # SEED_PRICES, TICKER_PARAMS, CORRELATION_GROUPS
      simulator.py         # GBMSimulator + SimulatorDataSource
      massive_client.py    # MassiveDataSource
      factory.py           # create_market_data_source()
      stream.py            # create_stream_router() → SSE endpoint
  tests/
    market/
      __init__.py
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_massive.py
      test_factory.py
      test_stream.py
```

### Public API (`__init__.py`)

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate              - Immutable price snapshot dataclass
    PriceCache               - Thread-safe in-memory price store
    MarketDataSource         - Abstract interface for data providers
    create_market_data_source - Factory (simulator vs Massive)
    create_stream_router     - FastAPI router factory for SSE
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

Every consumer outside `app/market/` imports from `app.market`, never from submodules. That gives us a single integration surface to guard as the internals evolve.

---

## 2. Data Model — `models.py`

`PriceUpdate` is the only value type that leaves the market data layer. Every SSE frame, every portfolio valuation read, every chat-context snapshot uses it.

```python
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    `previous_price` is the *prior cache value*, not the prior-session close.
    `change_percent` is therefore tick-to-tick, matching PLAN.md §8.
    `timestamp` is the source-reported time (UNIX seconds, float); the cache
    does not overwrite timestamps it already has.
    """

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round(
            (self.price - self.previous_price) / self.previous_price * 100,
            4,
        )

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
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

### Design decisions

- **`frozen=True`** — once created, a `PriceUpdate` cannot mutate. Two async tasks holding the same reference cannot race.
- **`slots=True`** — at ~2 updates/s × 10 tickers × long sessions, we churn hundreds of thousands of these per hour. Slots cuts per-instance memory by ~30%.
- **Derived properties** (`change`, `direction`, `change_percent`) — they can never fall out of sync with `price` / `previous_price` because they are computed each read.
- **Nothing about bid/ask or volume** — out of scope for v1. Adding them later is additive (new optional fields).

### SSE wire payload (must match `PLAN.md §8`)

```json
{
  "ticker": "AAPL",
  "price": 191.42,
  "previous_price": 191.05,
  "timestamp": 1776321000.12,
  "change": 0.37,
  "change_percent": 0.1937,
  "direction": "up"
}
```

---

## 3. Price Cache — `cache.py`

The central data hub. Data sources write, everyone else reads. It must be thread-safe because the Massive client runs inside `asyncio.to_thread()`.

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price per ticker.

    One writer at a time (the active MarketDataSource). Many readers
    (SSE generator, portfolio service, chat context builder, trade API).
    A single threading.Lock guards all state; contention is negligible at
    the scale we care about (~10 tickers, ~2 writes/sec).
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """Record a new price. Returns the PriceUpdate that was stored.

        First update for a ticker → previous_price == price, direction == 'flat'.
        Prices are rounded to 2 decimals on write (UX concern — no $191.4237
        flicker in the UI). Downstream math inherits that rounding, which is
        acceptable for a simulated trading UI.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
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
        """Shallow copy snapshot. Safe to iterate outside the lock."""
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
        """Monotonic change counter. Read under the lock for no-GIL safety."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why `threading.Lock`, not `asyncio.Lock`

The Massive REST client is synchronous (blocking HTTP). We run it via `asyncio.to_thread()`, which puts its writes in a real OS thread. `asyncio.Lock` would not protect that. `threading.Lock` works from both the event loop and worker threads, at a cost of a handful of nanoseconds per acquire.

### Version counter — why it matters

The SSE generator polls the cache on a fixed interval (500 ms). Without a version counter, it would serialise and emit the full price map every tick even when Massive last polled 14 s ago. The counter turns the polling loop into an effectively event-driven one: `if cache.version != last_version` → emit, else sleep.

### Known fix vs. the archived design

The archived doc read `self._version` without taking the lock. Under CPython's GIL that's safe in practice, but free-threaded / no-GIL builds (3.13t+) would race. This design takes the lock on every read. The cost is negligible.

---

## 4. Abstract Interface — `interface.py`

```python
from __future__ import annotations

import abc


class MarketDataSource(abc.ABC):
    """Contract for market data providers.

    Sources are push-based: they own a background task that writes PriceUpdates
    into a shared PriceCache on their own cadence. Consumers (SSE, portfolio,
    chat context) never call the source; they read from the cache.

    Lifecycle:

        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... shutdown ...
        await source.stop()
    """

    @abc.abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Initialise internal state and spawn the background task.

        Must return only when the source is ready to accept add/remove calls.
        Must be idempotent: start → start is a no-op on the second call.
        """

    @abc.abstractmethod
    async def stop(self) -> None:
        """Cancel the background task and clean up. Safe to call twice."""

    @abc.abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Uppercase-normalise, no-op if already present.

        The new ticker must be visible in the cache by the next cycle.
        """

    @abc.abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove from internal set AND from the cache. No-op if absent."""

    @abc.abstractmethod
    def get_tickers(self) -> list[str]:
        """Snapshot list. Callers may mutate the return value freely."""
```

### Contract notes (what every implementation must honour)

- **Exception discipline.** The background task must not die on transient errors. Log and continue. If it did die, the cache would freeze and every downstream surface would silently show stale data.
- **Cadence is private.** Simulator ticks every 500 ms. Massive polls every 15 s. Consumers must not assume either — that is what the version counter is for.
- **Delivery is best-effort.** A source may skip a cycle if upstream didn't change. Consumers must be idempotent.
- **Tick boundary is the cache version bump.** Not the per-ticker write; a batch of writes inside one `step()` or `_poll_once()` will bump the counter N times, but SSE serialisation reads `get_all()` atomically so the frame is consistent.

---

## 5. Seed Prices & Parameters — `seed_prices.py`

Constants only. Zero logic, zero imports. Shared by the simulator (initial prices + GBM parameters) and — as an optional fallback — by the Massive client for pre-warm before the first poll completes.

```python
"""Seed prices, per-ticker GBM parameters, and correlation structure.

Values chosen to look familiar (early-2024 prices) and produce plausibly
correlated motion without the chart looking like a single line. μ is biased
mildly positive across the board so a typical session drifts upward — an
explicit design cheat for the capstone aesthetic.
"""

# Seed prices for the default watchlist (USD).
SEED_PRICES: dict[str, float] = {
    "AAPL":  190.00,
    "GOOGL": 175.00,
    "MSFT":  420.00,
    "AMZN":  180.00,
    "TSLA":  250.00,
    "NVDA":  800.00,
    "META":  500.00,
    "JPM":   195.00,
    "V":     275.00,
    "NFLX":  620.00,
}

# Per-ticker GBM parameters.
# mu: annualised drift (expected log-return per trading year).
# sigma: annualised volatility.
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"mu": 0.08, "sigma": 0.22},
    "GOOGL": {"mu": 0.07, "sigma": 0.24},
    "MSFT":  {"mu": 0.08, "sigma": 0.20},
    "AMZN":  {"mu": 0.06, "sigma": 0.28},
    "TSLA":  {"mu": 0.03, "sigma": 0.50},  # high vol, modest drift
    "NVDA":  {"mu": 0.08, "sigma": 0.42},  # high vol, strong drift
    "META":  {"mu": 0.07, "sigma": 0.30},
    "JPM":   {"mu": 0.05, "sigma": 0.18},  # low vol (bank)
    "V":     {"mu": 0.06, "sigma": 0.17},  # low vol (payments)
    "NFLX":  {"mu": 0.05, "sigma": 0.32},
}

# Fallback for tickers the user adds dynamically that aren't in the table.
DEFAULT_PARAMS: dict[str, float] = {"mu": 0.05, "sigma": 0.25}

# Correlation sector groups. TSLA is deliberately NOT in the tech set because
# it behaves independently in the simulator (TSLA_CORR below).
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # tech vs tech
INTRA_FINANCE_CORR = 0.5   # finance vs finance
CROSS_GROUP_CORR   = 0.3   # tech vs finance, either vs unknown
TSLA_CORR          = 0.3   # TSLA vs anything
DEFAULT_CORR       = 0.3   # unknown vs unknown
```

### Why these parameters

- **Seed prices** — roughly early-2024 close prices. Recognisable to users; no strong claim to accuracy.
- **σ** — reflects realised annualised vol loosely. TSLA / NVDA high, JPM / V low, the rest in the 0.20-0.30 cluster.
- **μ** — biased positive so portfolio value drifts up in a long session. The app is meant to feel rewarding to watch. A real backtest would centre μ near zero.
- **Correlations** — chosen to produce visible co-movement without making the chart look like ten copies of the same line. The exact numbers are not calibrated to empirical data.

### Why constants-only

Any import side effect here runs at `from app.market import ...` time. Keeping this file pure data means tests can import it without spinning up anything.

---

## 6. GBM Simulator — `simulator.py`

Two classes:

- `GBMSimulator` — the pure math engine. Holds state (prices, params, Cholesky factor). Not async.
- `SimulatorDataSource` — the `MarketDataSource` implementation that wraps the math engine in an asyncio task and writes to the cache.

### 6.1 The math

Geometric Brownian Motion, the same model underlying Black-Scholes. For each ticker:

```
S(t + dt) = S(t) · exp( (μ - σ²/2) · dt  +  σ · √dt · Z )
```

Where `Z` is drawn from a correlated multivariate standard normal — we build the correlation matrix from sector membership, Cholesky-decompose it to `L`, then compute `Z_corr = L · Z_ind` each tick. GBM guarantees strictly positive prices (the exponential is never zero) and produces approximately lognormal returns over any finite interval — good enough for a demo.

`dt` is the 500 ms tick expressed as a fraction of a trading year (252 days × 6.5 h × 3600 s = 5,896,800 s). So `dt ≈ 8.48e-8`. This tiny value means the annualised μ and σ stay annualised and interpretable; you don't have to convert them to per-tick numbers.

On top of GBM, each ticker has a ~0.1% probability per tick of a "random event" — a multiplicative shock of ±2-5%. At 10 tickers × 2 ticks/s that's roughly one event every 50 seconds somewhere on the board. Rare enough not to dominate the chart; common enough to give demos drama.

### 6.2 `GBMSimulator`

```python
from __future__ import annotations

import logging
import math
import random

import numpy as np

from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)

logger = logging.getLogger(__name__)

# 500 ms as a fraction of a trading year.
_TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / _TRADING_SECONDS_PER_YEAR   # ~8.48e-8


class GBMSimulator:
    """Pure math engine for correlated GBM price paths.

    State:
        _tickers  — ordered list; index matches the correlation matrix rows.
        _prices   — current price per ticker.
        _params   — {ticker: {'mu': ..., 'sigma': ...}}.
        _cholesky — lower-triangular factor of the correlation matrix, or
                    None when len(tickers) <= 1 (no correlation possible).
    """

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        rng: random.Random | None = None,
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._rng = rng or random
        self._np_rng = np.random.default_rng(
            self._rng.randint(0, 2**31 - 1) if rng else None
        )
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for t in tickers:
            self._add_ticker_internal(t)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance all tickers by one time step. Returns {ticker: new_price}.

        Hot path — called every 500 ms. Keep it allocation-light.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z_ind = self._np_rng.standard_normal(n)
        z_corr = self._cholesky @ z_ind if self._cholesky is not None else z_ind

        out: dict[str, float] = {}
        sqrt_dt = math.sqrt(self._dt)
        for i, ticker in enumerate(self._tickers):
            p = self._params[ticker]
            mu, sigma = p["mu"], p["sigma"]
            drift = (mu - 0.5 * sigma * sigma) * self._dt
            diffusion = sigma * sqrt_dt * z_corr[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if self._rng.random() < self._event_prob:
                magnitude = self._rng.uniform(0.02, 0.05)
                sign = 1 if self._rng.random() < 0.5 else -1
                self._prices[ticker] *= 1.0 + magnitude * sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker,
                    magnitude * 100,
                    "up" if sign > 0 else "down",
                )

            out[ticker] = round(self._prices[ticker], 2)

        return out

    def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker.upper().strip())

    def get_tickers(self) -> list[str]:
        """Public accessor — consumers must not reach into `_tickers`."""
        return list(self._tickers)

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(
            ticker,
            self._rng.uniform(50.0, 300.0),
        )
        self._params[ticker] = dict(
            TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        )

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(
                    self._tickers[i], self._tickers[j]
                )
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
        if t1 in tech or t2 in tech or t1 in finance or t2 in finance:
            return CROSS_GROUP_CORR
        return DEFAULT_CORR
```

### Determinism via `SIMULATOR_SEED`

When the env var is set, `SimulatorDataSource` constructs the simulator with a seeded `random.Random(seed)` instance. That controls the GBM innovations, the event-shock Bernoulli, and the shock magnitude — everything stochastic. It does not control wall-clock interleaving, so tests that assert exact prices at a given time must also drive the simulator manually via `step()` rather than relying on `asyncio.sleep`.

### 6.3 `SimulatorDataSource`

```python
import asyncio
import os


class SimulatorDataSource(MarketDataSource):
    """Async wrapper around GBMSimulator that writes to the shared cache.

    Reads SIMULATOR_SEED at start() time. If set, the RNG is seeded and
    subsequent runs with the same tickers and cadence produce identical
    price paths (modulo async scheduling).
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
        self._lock = asyncio.Lock()  # guards add/remove against _run_loop

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            return  # idempotent
        seed_env = os.environ.get("SIMULATOR_SEED", "").strip()
        rng = random.Random(int(seed_env)) if seed_env else None
        self._sim = GBMSimulator(
            tickers=[t.upper().strip() for t in tickers],
            event_probability=self._event_prob,
            rng=rng,
        )
        # Seed cache with initial prices so SSE has data on its first tick.
        for t in self._sim.get_tickers():
            price = self._sim.get_price(t)
            if price is not None:
                self._cache.update(ticker=t, price=price)
        self._task = asyncio.create_task(
            self._run_loop(), name="simulator-loop"
        )
        logger.info(
            "Simulator started: %d tickers, interval=%.2fs, seed=%s",
            len(self._sim.get_tickers()),
            self._interval,
            seed_env or "<none>",
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._sim = None
        logger.info("Simulator stopped")

    async def add_ticker(self, ticker: str) -> None:
        async with self._lock:
            if not self._sim:
                return
            self._sim.add_ticker(ticker)
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker.upper().strip(), price=price)

    async def remove_ticker(self, ticker: str) -> None:
        async with self._lock:
            if not self._sim:
                return
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker.upper().strip())

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                async with self._lock:
                    prices = self._sim.step() if self._sim else {}
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed; continuing")
            await asyncio.sleep(self._interval)
```

### Why seed the cache eagerly on `start` and `add_ticker`

Without it, the SSE endpoint would stream empty data for the first 500 ms after connect, and the first 500 ms after any watchlist add. The UX impact is real: a newly added ticker would show `—` until the next simulator tick. Eager seeding trades one extra cache write for a better first impression.

### Why an `asyncio.Lock` around add / remove / step

`_run_loop` iterates `self._tickers` indirectly (via `step()` which reads the Cholesky factor and params). An `add_ticker` during that iteration could cause a shape mismatch (new ticker in `_prices` but not in `_cholesky`). The lock serialises the mutations without blocking the event loop.

---

## 7. Massive API Client — `massive_client.py`

Polls the Massive (née Polygon.io) REST snapshot endpoint on a configurable interval. The SDK is synchronous, so poll execution is dispatched to a worker thread via `asyncio.to_thread`.

### 7.1 Endpoint choice

We use the multi-ticker snapshot:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
    ?tickers=AAPL,GOOGL,MSFT,...
    &include_otc=false
```

This returns every watched ticker in one HTTP call, which fits the free tier's 5 req/min budget comfortably at a 15 s poll cadence (4 req/min). See `planning/MASSIVE_API.md` for the full response shape and the alternatives considered (single-ticker snapshot, `/v2/last/trade`, unified `/v3/snapshot`).

### 7.2 Timestamp gotcha

The archived design divided `last_trade.timestamp` by `1000`, assuming milliseconds. **Snapshot responses are in nanoseconds** — `tickers[i].updated` and `tickers[i].lastTrade.t` are both `int` nanoseconds since epoch. Divide by `1e9`. An off-by-1e6 here produces timestamps from the year 57,000 and silently corrupts every SSE frame. Guard with a helper.

### 7.3 Implementation

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

_RATE_LIMIT_COOLDOWN_S = 60.0  # pause on 429


def _ns_to_seconds(ts_ns: int | float | None) -> float | None:
    """Snapshot timestamps are nanoseconds; convert to UNIX seconds (float)."""
    if ts_ns is None:
        return None
    return float(ts_ns) / 1e9


class MassiveDataSource(MarketDataSource):
    """Market data via Massive's REST snapshot endpoint.

    Defaults assume the free tier (5 req/min → poll every 15s). Paid tiers
    can lower poll_interval to 2-5s. WebSocket streaming is Advanced-tier-only
    and intentionally out of scope for v1.
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
        self._cooldown_until: float = 0.0  # event-loop time

    async def start(self, tickers: list[str]) -> None:
        if self._task is not None:
            return  # idempotent
        # Lazy import — polygon/massive SDK is an optional dependency.
        from polygon import RESTClient  # type: ignore

        self._client = RESTClient(api_key=self._api_key)
        self._tickers = [t.upper().strip() for t in tickers]

        # Fire one poll immediately so the cache has data by the time start()
        # returns. Subsequent polls happen on the interval.
        await self._poll_once()

        self._task = asyncio.create_task(
            self._poll_loop(), name="massive-poller"
        )
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
        if ticker and ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info(
                "Massive: added %s (appears on next poll)", ticker
            )

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internals ---

    async def _poll_loop(self) -> None:
        """Interval-driven polling. First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            loop_time = asyncio.get_event_loop().time()
            if loop_time < self._cooldown_until:
                remaining = self._cooldown_until - loop_time
                logger.debug("Massive cooldown active, %.1fs remaining", remaining)
                continue
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    ticker = getattr(snap, "ticker", None)
                    last_trade = getattr(snap, "last_trade", None)
                    if not ticker or last_trade is None:
                        continue
                    price = float(last_trade.price)
                    timestamp = _ns_to_seconds(
                        getattr(last_trade, "timestamp", None)
                        or getattr(snap, "updated", None)
                    )
                    self._cache.update(
                        ticker=ticker,
                        price=price,
                        timestamp=timestamp,
                    )
                    processed += 1
                except (AttributeError, TypeError, ValueError) as e:
                    logger.warning(
                        "Skipping snapshot for %s: %s",
                        getattr(snap, "ticker", "???"),
                        e,
                    )
            logger.debug(
                "Massive poll: updated %d/%d tickers",
                processed,
                len(self._tickers),
            )
        except Exception as e:
            # Distinguish rate limits if the SDK raises a typed error.
            status = getattr(e, "status", None) or getattr(e, "status_code", None)
            if status == 429:
                self._cooldown_until = (
                    asyncio.get_event_loop().time() + _RATE_LIMIT_COOLDOWN_S
                )
                logger.warning(
                    "Massive 429 — cooling down for %.0fs",
                    _RATE_LIMIT_COOLDOWN_S,
                )
            else:
                logger.error("Massive poll failed: %s", e)

    def _fetch_snapshots(self) -> list:
        """Synchronous REST call. Runs in a worker thread."""
        # `get_snapshot_all` returns the full-market-snapshot result; the
        # `tickers` kwarg filters server-side so we get one HTTP call.
        return self._client.get_snapshot_all(
            market_type="stocks",
            tickers=self._tickers,
            include_otc=False,
        )
```

### 7.4 Error-handling matrix

| Failure | Behaviour |
|---|---|
| `401 Unauthorized` (bad key) | Logged as error. Poller keeps trying — user might fix `.env` and restart. |
| `429 Too Many Requests` | 60 s cooldown. Next poll resumes at normal cadence. |
| Network timeout / 5xx | Logged as error. Retries on next interval. |
| Malformed snapshot for a ticker | Skipped with warning. Siblings still processed. |
| All tickers fail | Cache retains last-known prices. SSE keeps streaming stale data — better than no data. |

Auto-failover to the simulator on persistent Massive failure is deliberately out of scope. If the user's API key is bad, the right fix is to correct the key, not to mask the error.

### 7.5 Lazy import rationale

`from polygon import RESTClient` sits inside `start()`, not at module top. Reasons:

- The SDK is a ~20 MB install with transitive scientific dependencies. Simulator-only users should not pay that cost.
- Tests that cover the simulator path should not require the Massive SDK to be installed.
- Mocking: tests patch `polygon.RESTClient` via `unittest.mock.patch("polygon.RESTClient", create=True)` — the `create=True` flag is needed because the name only exists after the lazy import.

---

## 8. Factory — `factory.py`

```python
from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Pick the right data source based on env vars.

    MASSIVE_API_KEY set and non-empty → MassiveDataSource.
    Otherwise → SimulatorDataSource.

    Returns an UNSTARTED source. Caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        # Lazy-import so simulator-only users don't need the polygon SDK.
        from .massive_client import MassiveDataSource

        logger.info("Market data source: Massive REST API")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    from .simulator import SimulatorDataSource

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

### Why an env var (and not a config file)

One process, one source. The simulator is the default because "works with zero config" is a design goal — matching the Docker-run-and-go UX. A single env var matches that posture; a config file would add one more artefact for the student to understand before their container prints its first price.

### Usage

```python
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(initial_tickers)
```

---

## 9. SSE Streaming Endpoint — `stream.py`

One FastAPI route that holds open a long-lived `text/event-stream` response and pushes price frames as the cache version advances.

```python
from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)


def create_stream_router(
    price_cache: PriceCache,
    poll_interval: float = 0.5,
) -> APIRouter:
    """Build a fresh APIRouter bound to the given cache.

    A fresh router per factory call avoids the module-level-state footgun
    where calling the factory twice would re-register the same route on
    the same router object.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request, poll_interval),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Disable nginx/CDN response buffering. Without this, events
                # can sit in an intermediary's buffer for seconds.
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects.

    Emits one frame per ticker per cache-version bump, batched into a single
    JSON array so the client gets an atomic snapshot each push.
    """
    # Ask the browser to reconnect 1s after a drop. EventSource handles
    # reconnection automatically; this just tightens the default.
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
                snapshot = price_cache.get_all()
                if snapshot:
                    payload = json.dumps(
                        [u.to_dict() for u in snapshot.values()]
                    )
                    yield f"data: {payload}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled: %s", client_ip)
        raise
```

### 9.1 Wire format

Each frame is a JSON array of `PriceUpdate.to_dict()` entries. One array per push, all tickers in the array. Example:

```
retry: 1000

data: [{"ticker":"AAPL","price":191.42,"previous_price":191.05,"timestamp":1776321000.12,"change":0.37,"change_percent":0.1937,"direction":"up"},{"ticker":"GOOGL","price":175.12,"previous_price":175.22,"timestamp":1776321000.12,"change":-0.10,"change_percent":-0.057,"direction":"down"}]

```

Array format (not a map keyed by ticker) because (a) it serialises cheaper, and (b) the frontend reducer already keys by `update.ticker` internally — passing a map adds a redundant keying layer.

### 9.2 Client-side consumption

```javascript
const src = new EventSource("/api/stream/prices");
src.onmessage = (ev) => {
  const updates = JSON.parse(ev.data);
  for (const u of updates) {
    dispatch({ type: "PRICE_TICK", payload: u });
  }
};
src.onerror = () => {
  // EventSource auto-reconnects; header dot flips yellow
  setConnectionStatus("reconnecting");
};
```

### 9.3 Cadence semantics

- The generator loop runs every `interval` (default 500 ms). It is a latency cap, not a data cadence.
- In simulator mode, the cache version advances every 500 ms → one frame per loop iteration.
- In Massive mode, the cache advances once every 15 s → ~29 of every 30 loop iterations emit nothing. That is the whole point of the version counter.

### 9.4 Backpressure

If the client reads slowly (e.g., a tab throttled in the background), `yield` will block at the TCP layer. That naturally slows the loop — no need for explicit queue management. The cache continues to update; the client just sees fewer frames while throttled and catches up on the next read.

### 9.5 Hygiene fixes vs. archived design

- Return type annotated as `AsyncGenerator[str, None]`, not `None`.
- The router is created inside the factory, not at module scope, so calling the factory twice produces two distinct routers.
- `CancelledError` is re-raised so FastAPI can tear down the stream cleanly on shutdown.

---

## 10. FastAPI Lifecycle Integration

Market data starts and stops with the app via the `lifespan` context manager.

**`backend/app/main.py`** (relevant section):

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.db.watchlist_repo import load_watchlist_tickers
from app.market import (
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers(user_id="default")
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    try:
        yield
    finally:
        # --- shutdown ---
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Dependency injectors — prefer these over touching app.state directly.
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

### 10.1 Consuming the cache from other routers

Every downstream surface uses FastAPI's dependency injection:

```python
from fastapi import APIRouter, Depends, HTTPException

from app.market import PriceCache

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    quote = price_cache.get(trade.ticker)
    if quote is None:
        raise HTTPException(
            400,
            f"No price available for {trade.ticker}. Try again in a moment.",
        )
    # Stale-quote rejection (see §13.6).
    if time.time() - quote.timestamp > STALE_QUOTE_SECONDS:
        raise HTTPException(400, "Quote is stale")
    # ... execute at quote.price ...
```

### 10.2 Why wrap `yield` in `try/finally`

If a startup step after `source.start()` fails, the `yield` never runs and the `finally` still tears the source down. Otherwise we'd leak a running asyncio task between reloads during development.

### 10.3 Why the cache lives on `app.state`

It's the one shared singleton in the process. We could stash it in a module-level global, but:

- `app.state` is testable — `TestClient(app)` gives each test a fresh instance.
- `Depends(get_price_cache)` is idiomatic FastAPI and makes dependencies explicit in function signatures.
- No circular imports — the cache type is in `app.market.cache`; consumers import the dependency function from `app.main`.

### 10.4 Startup ordering

The order matters:

1. Cache first. Nothing else works without it.
2. Source second, with the cache handed in. `start()` blocks until the source has written at least one snapshot for seeded tickers (simulator) or fired one poll (Massive).
3. Stream router third. SSE clients that connect before the source is started would see empty frames — registering the router after `start()` prevents that race on the very first request.

---

## 11. Watchlist Coordination

When the watchlist changes — via the REST API or via an LLM tool call — the DB and the live market source must stay in sync. This is where `REVIEW.md` finding #2 bites: if we write the DB first and then `add_ticker()` raises, the DB and the source disagree until the next restart.

### 11.1 Ordering rule

**DB first, cache / source second.** Rationale (from `MARKET_INTERFACE.md`):

- Ticker in DB but not yet in cache → recoverable on next startup; the lifespan seeds the source from the DB.
- Ticker in cache but not in DB → vanishes on restart. Confusing for the user (watchlist entry that disappears) and harder to diagnose.

The DB is the durable source of truth; the source is a cache-of-live-data derived from it.

### 11.2 Compensating rollback on source failure

To prevent the split-brain state `REVIEW.md` flagged, wrap the coordinated mutation in a try/except that rolls the DB change back if the source rejects:

```python
from fastapi import APIRouter, Depends, HTTPException

from app.db.watchlist_repo import (
    insert_watchlist_entry,
    delete_watchlist_entry,
    watchlist_entry_exists,
)
from app.market import MarketDataSource, PriceCache
from app.main import get_market_source, get_price_cache

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.post("")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
    price_cache: PriceCache = Depends(get_price_cache),
):
    ticker = payload.ticker.upper().strip()
    _validate_ticker(ticker)

    if await watchlist_entry_exists(user_id="default", ticker=ticker):
        raise HTTPException(409, f"{ticker} is already in the watchlist")

    # DB first.
    entry_id = await insert_watchlist_entry(user_id="default", ticker=ticker)

    # Source second — roll back if it raises.
    try:
        await source.add_ticker(ticker)
    except Exception:
        await delete_watchlist_entry(entry_id)
        logger.exception("add_ticker failed; DB change rolled back")
        raise HTTPException(
            500,
            "Could not start tracking this ticker. Please try again.",
        )

    quote = price_cache.get(ticker)
    return {
        "ticker": ticker,
        "price": quote.price if quote else None,
        "previous_price": quote.previous_price if quote else None,
        "change": quote.change if quote else None,
        "change_percent": quote.change_percent if quote else None,
        "direction": quote.direction if quote else None,
    }
```

### 11.3 Removal with position-aware tracking

A user may remove a ticker from the watchlist but still hold shares. In that case the source must keep tracking it so portfolio valuation stays accurate. Otherwise the positions table would show stale prices the moment the ticker leaves the watchlist.

```python
from app.db.positions_repo import get_position


@router.delete("/{ticker}", status_code=204)
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = ticker.upper().strip()

    deleted = await delete_watchlist_entry(user_id="default", ticker=ticker)
    if not deleted:
        raise HTTPException(404, f"{ticker} is not in the watchlist")

    # Only stop tracking if we don't hold shares.
    position = await get_position(user_id="default", ticker=ticker)
    if position is None or position.quantity == 0:
        try:
            await source.remove_ticker(ticker)
        except Exception:
            # Re-add the DB row to restore consistency.
            await insert_watchlist_entry(user_id="default", ticker=ticker)
            logger.exception("remove_ticker failed; DB change rolled back")
            raise HTTPException(
                500,
                "Could not stop tracking this ticker. Please try again.",
            )
```

### 11.4 Ticker validation

The same validation rule applies everywhere a ticker enters the system — manual trade, watchlist add, chat-applied action:

```python
import re

_TICKER_RE = re.compile(r"^[A-Z0-9]{1,10}$")


def _validate_ticker(ticker: str) -> None:
    if not _TICKER_RE.match(ticker):
        raise HTTPException(
            400,
            "Ticker must be 1-10 alphanumeric characters, uppercase",
        )
```

Centralising this in one helper is how `REVIEW.md`'s "invalid ticker rejected consistently across manual trade, watchlist, and chat" gets enforced.

### 11.5 LLM-driven watchlist changes

When the chat service applies watchlist changes the LLM returned, it goes through the same coordinated path (DB first, source second, with rollback). The chat service does not call `source.add_ticker()` directly; it calls the same internal helper the HTTP route uses so the atomicity guarantee is preserved.

---

## 12. Testing Strategy

Target coverage by module:

| Module | Unit | Integration | Notes |
|---|---|---|---|
| `models.py` | ✅ | — | Derived-property correctness, serialization. |
| `cache.py` | ✅ | ✅ (concurrency) | Concurrent-writer test to catch lock regressions. |
| `simulator.py` — math | ✅ | — | Determinism, positivity, Cholesky-induced correlation. |
| `simulator.py` — source | ✅ | ✅ (lifecycle) | start/stop/add/remove, exception resilience. |
| `massive_client.py` | ✅ (mocked) | — | 429 cooldown, nanosecond parsing, malformed skip. |
| `factory.py` | ✅ | — | Env-var branching. |
| `stream.py` | — | ✅ (TestClient) | Version-bump triggers frame, no bump = no frame. |

### 12.1 `PriceCache` unit tests

```python
import threading

import pytest

from app.market.cache import PriceCache


class TestPriceCache:
    def test_first_update_is_flat(self):
        cache = PriceCache()
        u = cache.update("AAPL", 190.50)
        assert u.direction == "flat"
        assert u.previous_price == 190.50

    def test_direction_up_and_down(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        assert cache.update("AAPL", 191.00).direction == "up"
        assert cache.update("AAPL", 190.50).direction == "down"

    def test_version_monotonic(self):
        cache = PriceCache()
        v0 = cache.version
        cache.update("AAPL", 1.0)
        cache.update("GOOGL", 2.0)
        assert cache.version == v0 + 2

    def test_remove_clears_ticker(self):
        cache = PriceCache()
        cache.update("AAPL", 1.0)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_get_all_is_shallow_copy(self):
        cache = PriceCache()
        cache.update("AAPL", 1.0)
        snap = cache.get_all()
        snap["FAKE"] = None  # type: ignore
        assert "FAKE" not in cache.get_all()

    def test_concurrent_writers(self):
        """Two threads writing different tickers should not corrupt state."""
        cache = PriceCache()
        N = 5_000

        def writer(ticker: str) -> None:
            for i in range(N):
                cache.update(ticker, float(i))

        t1 = threading.Thread(target=writer, args=("AAPL",))
        t2 = threading.Thread(target=writer, args=("GOOGL",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert cache.get_price("AAPL") == float(N - 1)
        assert cache.get_price("GOOGL") == float(N - 1)
        assert cache.version >= 2 * N  # every write bumps the counter
```

### 12.2 `GBMSimulator` math tests

```python
import math
import random
import statistics

import pytest

from app.market.simulator import GBMSimulator
from app.market.seed_prices import SEED_PRICES


class TestGBMSimulator:
    def test_step_returns_all_tickers(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result) == {"AAPL", "GOOGL"}

    def test_prices_stay_positive(self):
        sim = GBMSimulator(tickers=["TSLA"], event_probability=0.0)
        for _ in range(10_000):
            assert sim.step()["TSLA"] > 0

    def test_seeded_run_is_deterministic(self):
        a = GBMSimulator(tickers=["AAPL", "GOOGL"], rng=random.Random(42))
        b = GBMSimulator(tickers=["AAPL", "GOOGL"], rng=random.Random(42))
        for _ in range(100):
            assert a.step() == b.step()

    def test_add_ticker_includes_in_next_step(self):
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("NVDA")
        assert "NVDA" in sim.step()

    def test_remove_ticker_excludes_from_next_step(self):
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        assert "GOOGL" not in sim.step()

    def test_unknown_ticker_gets_fallback_seed_price(self):
        sim = GBMSimulator(tickers=["ZZZZ"])
        p = sim.get_price("ZZZZ")
        assert 50.0 <= p <= 300.0

    def test_cholesky_induces_correlation(self):
        """Two tech tickers should co-move more often than chance."""
        sim = GBMSimulator(
            tickers=["AAPL", "MSFT"],
            event_probability=0.0,
            rng=random.Random(0),
        )
        same_sign = 0
        N = 5_000
        for _ in range(N):
            before = (sim.get_price("AAPL"), sim.get_price("MSFT"))
            sim.step()
            after = (sim.get_price("AAPL"), sim.get_price("MSFT"))
            d_aapl = after[0] - before[0]
            d_msft = after[1] - before[1]
            if d_aapl * d_msft > 0:
                same_sign += 1
        # Uncorrelated → ~50%. Intra-tech corr 0.6 → well above 0.5.
        assert same_sign / N > 0.55
```

### 12.3 `SimulatorDataSource` lifecycle

```python
import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_seeds_cache_before_returning(self):
        cache = PriceCache()
        src = SimulatorDataSource(price_cache=cache, update_interval=1.0)
        await src.start(["AAPL", "GOOGL"])
        try:
            assert cache.get("AAPL") is not None
            assert cache.get("GOOGL") is not None
        finally:
            await src.stop()

    async def test_stop_is_idempotent(self):
        cache = PriceCache()
        src = SimulatorDataSource(price_cache=cache, update_interval=1.0)
        await src.start(["AAPL"])
        await src.stop()
        await src.stop()  # must not raise

    async def test_add_then_remove_updates_cache(self):
        cache = PriceCache()
        src = SimulatorDataSource(price_cache=cache, update_interval=1.0)
        await src.start(["AAPL"])
        try:
            await src.add_ticker("TSLA")
            assert "TSLA" in src.get_tickers()
            assert cache.get("TSLA") is not None
            await src.remove_ticker("TSLA")
            assert "TSLA" not in src.get_tickers()
            assert cache.get("TSLA") is None
        finally:
            await src.stop()

    async def test_exception_in_step_does_not_kill_loop(self, monkeypatch):
        cache = PriceCache()
        src = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await src.start(["AAPL"])
        try:
            calls = {"n": 0}

            def flaky_step():
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("boom")
                return {"AAPL": 191.0}

            monkeypatch.setattr(src._sim, "step", flaky_step)
            await asyncio.sleep(0.25)
            assert calls["n"] >= 2  # loop recovered
        finally:
            await src.stop()
```

### 12.4 `MassiveDataSource` with mocked SDK

```python
from unittest.mock import MagicMock, patch

import pytest

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snap(ticker: str, price: float, ts_ns: int) -> MagicMock:
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ns
    snap.updated = ts_ns
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    async def test_nanosecond_timestamp_conversion(self):
        cache = PriceCache()
        src = MassiveDataSource(
            api_key="k", price_cache=cache, poll_interval=60.0,
        )
        src._tickers = ["AAPL"]
        src._client = MagicMock()
        # 1_776_321_000_120_000_000 ns == 1_776_321_000.12 s
        snap = _make_snap("AAPL", 191.42, 1_776_321_000_120_000_000)
        with patch.object(src, "_fetch_snapshots", return_value=[snap]):
            await src._poll_once()
        update = cache.get("AAPL")
        assert update is not None
        assert update.price == 191.42
        assert abs(update.timestamp - 1_776_321_000.12) < 1e-3

    async def test_malformed_snapshot_is_skipped(self):
        cache = PriceCache()
        src = MassiveDataSource(
            api_key="k", price_cache=cache, poll_interval=60.0,
        )
        src._tickers = ["AAPL", "BAD"]
        src._client = MagicMock()
        good = _make_snap("AAPL", 190.0, 1_000_000_000_000_000_000)
        bad = MagicMock(ticker="BAD", last_trade=None)
        with patch.object(src, "_fetch_snapshots", return_value=[good, bad]):
            await src._poll_once()
        assert cache.get_price("AAPL") == 190.0
        assert cache.get_price("BAD") is None

    async def test_429_triggers_cooldown(self):
        cache = PriceCache()
        src = MassiveDataSource(
            api_key="k", price_cache=cache, poll_interval=60.0,
        )
        src._tickers = ["AAPL"]
        src._client = MagicMock()

        class RateLimited(Exception):
            status = 429

        with patch.object(src, "_fetch_snapshots", side_effect=RateLimited()):
            await src._poll_once()
        import asyncio
        now = asyncio.get_event_loop().time()
        assert src._cooldown_until > now

    async def test_api_error_does_not_crash(self):
        cache = PriceCache()
        src = MassiveDataSource(
            api_key="k", price_cache=cache, poll_interval=60.0,
        )
        src._tickers = ["AAPL"]
        src._client = MagicMock()
        with patch.object(
            src, "_fetch_snapshots", side_effect=Exception("network"),
        ):
            await src._poll_once()  # must not raise
        assert cache.get_price("AAPL") is None
```

### 12.5 Factory env-var branching

```python
from app.market.cache import PriceCache
from app.market.factory import create_market_data_source


def test_factory_picks_simulator_without_key(monkeypatch):
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    src = create_market_data_source(PriceCache())
    assert type(src).__name__ == "SimulatorDataSource"


def test_factory_picks_massive_with_key(monkeypatch):
    monkeypatch.setenv("MASSIVE_API_KEY", "test-key")
    # Prevent the lazy import from failing if the SDK isn't installed.
    import sys, types
    sys.modules.setdefault("polygon", types.SimpleNamespace(RESTClient=object))
    src = create_market_data_source(PriceCache())
    assert type(src).__name__ == "MassiveDataSource"
```

### 12.6 SSE end-to-end

```python
import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market.cache import PriceCache
from app.market.stream import create_stream_router


def test_sse_emits_frame_on_cache_bump():
    cache = PriceCache()
    app = FastAPI()
    app.include_router(create_stream_router(cache, poll_interval=0.05))
    cache.update("AAPL", 190.0)

    with TestClient(app) as client:
        with client.stream("GET", "/api/stream/prices") as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            # Read until we have at least one data frame.
            buf = ""
            for chunk in r.iter_text():
                buf += chunk
                if "data: " in buf:
                    break
            data_line = next(
                line for line in buf.splitlines() if line.startswith("data: ")
            )
            payload = json.loads(data_line.removeprefix("data: "))
            assert any(u["ticker"] == "AAPL" for u in payload)
```

### 12.7 Coverage gaps `REVIEW.md` flagged

- Add failure-injection around `source.add_ticker` / `remove_ticker` so the compensating rollback in §11.2 has its own regression test.
- Add a full-watchlist (10 tickers) simulator smoke test — current simulator tests use 1-2 tickers to keep assertions tractable.
- Add an event-frequency test that runs 10 K+ steps and asserts the observed shock rate matches `event_probability` within a reasonable tolerance.

---

## 13. Error Handling & Edge Cases

### 13.1 Empty watchlist on startup

If the DB has zero watchlist rows (user deleted everything before a restart), `source.start([])` must work:

- Simulator: creates an empty `GBMSimulator`, starts the loop, `step()` returns `{}` each tick, cache stays empty. SSE emits nothing. Adding a ticker later starts everything rolling.
- Massive: skips the first poll (nothing to ask for), enters the loop. Same add-later behaviour.

No special-casing required — the loops handle `n == 0` as an early return.

### 13.2 Price-cache miss during trade

A user (or the LLM) can try to trade a ticker that has no cached quote yet: watchlist just added, Massive hasn't polled, or the simulator is mid-start. Reject with a helpful message:

```python
quote = price_cache.get(trade.ticker)
if quote is None:
    raise HTTPException(
        400,
        f"Price not yet available for {trade.ticker}. Try again in a moment.",
    )
```

The simulator avoids this in practice (it seeds the cache synchronously inside `add_ticker`). Massive has a window of up to `poll_interval` seconds where a freshly added ticker has no price.

### 13.3 Stale-quote rejection

`PLAN.md §8` requires stale quotes to reject trade execution. Use the cached `timestamp`:

```python
import time

STALE_QUOTE_SECONDS = 60.0  # generous; tune per source


def reject_if_stale(quote: PriceUpdate) -> None:
    age = time.time() - quote.timestamp
    if age > STALE_QUOTE_SECONDS:
        raise HTTPException(
            400,
            f"Quote for {quote.ticker} is {age:.0f}s old; refusing to trade",
        )
```

Massive's 15 s poll means quotes are routinely 15 s stale by wall-clock; `STALE_QUOTE_SECONDS = 60` leaves headroom. If Massive enters its 60 s rate-limit cooldown, quotes legitimately age past the threshold and trading is refused until the cache refreshes. That is the correct behaviour.

### 13.4 Massive API key invalid

Symptoms: every `_poll_once` raises a 401. The poller logs and continues. The cache stays empty. SSE streams nothing. The header connection-status dot is green (SSE is healthy) but the watchlist shows `—` for every price.

Fix: update `.env`, restart the container. Auto-failover to the simulator is intentionally out of scope — we'd rather surface the misconfiguration loudly than paper over it.

### 13.5 Simulator parameter divergence

A hand-edited correlation matrix that isn't positive semi-definite (e.g. tech-tech = 0.95 but tech-cross = 0.1) would make `np.linalg.cholesky` raise. The current static matrix in `seed_prices.py` is safe; any future config-driven version must validate PSD before accepting.

Prices can't go negative (GBM exponential). They can drift very far in a long session because `μ > 0` for every ticker. Mitigations, if needed:

- Periodic soft-reset: every N hours, nudge each price back toward its seed by α%.
- Clip above `3 × seed` and below `0.3 × seed` as a hard sanity bound.

Neither is wired up in v1.

### 13.6 Thread-safety under load

At 10 tickers × 2 writes/s plus a handful of SSE readers and occasional API route reads, lock contention is microseconds. The critical section in `PriceCache.update` is a dict set, an `int` increment, and a dataclass construction. If the ticker set ever grew to hundreds or the reader count to thousands, a read-write lock (readers-lock + single-writer-lock) would be the next step — not needed now.

### 13.7 Async task cancellation

Both `_run_loop` (simulator) and `_poll_loop` (Massive) are cancelled during `stop()`. The SSE `_generate_events` is cancelled when the client disconnects or the server shuts down. All three catch `CancelledError`, clean up, and re-raise where appropriate so the event loop sees the cancellation propagate. The anti-pattern to avoid is swallowing `CancelledError` without re-raising — that makes shutdowns hang.

### 13.8 Rounding inheritance

`PriceCache.update` rounds prices to 2 decimals on write. Downstream P&L math inherits that rounding; a trade executed at a rounded price will produce a rounded avg-cost. For a simulated trading UI this is fine and deliberate. It would not be acceptable for a real broker — the fix would be to store full precision in the cache and round only at serialisation time.

### 13.9 SSE reconnection storms

If the backend restarts, every open browser reconnects within ~1 s (the `retry: 1000` directive). For a demo this is nothing; for a multi-user deployment with thousands of connected browsers you'd want jittered reconnects on the client side. Noted for future multi-user work.

---

## 14. Configuration Summary

### 14.1 Environment variables

| Name | Default | Purpose |
|---|---|---|
| `MASSIVE_API_KEY` | `""` | If set and non-empty, use Massive REST. Otherwise, simulator. |
| `SIMULATOR_SEED` | `""` | Integer → deterministic GBM paths (E2E tests, repro). |

Both are consumed in `factory.py` (Massive selection) and `SimulatorDataSource.start` (seed wiring). No other market code reads the environment directly.

### 14.2 Tunable parameters

| Parameter | Where | Default | What it controls |
|---|---|---|---|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks. |
| `event_probability` | `SimulatorDataSource.__init__` / `GBMSimulator.__init__` | `0.001` | Per-ticker-per-tick chance of a ±2-5 % shock. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` | GBM time step (fraction of a trading year). |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between REST polls. |
| `_RATE_LIMIT_COOLDOWN_S` | `massive_client.py` (module constant) | `60.0` s | Back-off after a 429. |
| `STALE_QUOTE_SECONDS` | trade service (not in `market/`) | `60.0` s | Age at which a cached quote refuses trades. |
| `poll_interval` | `create_stream_router` | `0.5` s | SSE latency cap (max delay between cache bump and frame). |
| SSE `retry:` directive | `_generate_events` | `1000` ms | Browser EventSource reconnection delay. |

### 14.3 Correlation structure (from `seed_prices.py`)

| Pair type | Correlation |
|---|---|
| Intra-tech (AAPL/MSFT/…)* | `0.6` |
| Intra-finance (JPM/V) | `0.5` |
| TSLA vs anything | `0.3` |
| Cross-sector, unknown | `0.3` |

\*TSLA is excluded from the tech cluster despite being a tech stock in real life — it is deliberately independent in the simulator.

### 14.4 Default watchlist

Seeded into the DB on first launch (from `PLAN.md §7`): **AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX**. All ten have explicit rows in `SEED_PRICES` and `TICKER_PARAMS`; any ticker added later falls back to `DEFAULT_PARAMS` and a uniform-random seed price in `[50, 300]`.

### 14.5 Dependencies

- **Required:** `fastapi`, `numpy`. That's it for the simulator path.
- **Optional:** `polygon` (a.k.a. `polygon-api-client`). Lazy-imported inside `MassiveDataSource.start`; simulator-only users do not need it installed.

Both should live under `backend/pyproject.toml` — `polygon` as an optional extra (`project.optional-dependencies.massive`) so `uv sync --extra massive` pulls it when real data is wanted.

### 14.6 Implementation order (suggested)

A greenfield implementor can build this in one sitting by walking the modules bottom-up. Dependencies flow only one direction, so each step has everything it needs already in place:

1. `models.py` + `test_models.py` — value type + derived-property tests.
2. `cache.py` + `test_cache.py` — including the concurrent-writer test.
3. `interface.py` — ABC only, no tests needed.
4. `seed_prices.py` — pure constants.
5. `simulator.py` (GBM engine first, then async wrapper) + `test_simulator.py` + `test_simulator_source.py`.
6. `massive_client.py` + `test_massive.py` with mocked SDK.
7. `factory.py` + `test_factory.py`.
8. `stream.py` + `test_stream.py` using `TestClient`.
9. Wire into `main.py` `lifespan`. Verify with `curl -N http://localhost:8000/api/stream/prices` that frames appear as the cache updates.
10. Watchlist routes (`§11`) with DB-first / rollback-on-source-failure. Add the failure-injection test.
11. Plumb the stale-quote check into the trade route.

At every step the existing tests keep passing, and each module is independently usable.

---

## Appendix A — Quick Reference

### Minimum end-to-end smoke test

```bash
# With the simulator (no API key):
cd backend
uv run uvicorn app.main:app --port 8000 &
sleep 1
curl -N http://localhost:8000/api/stream/prices | head -c 2000
```

Expect: a `retry: 1000` line, then repeated `data: [...]` frames with ten tickers each, arriving roughly every 500 ms.

### With Massive

```bash
MASSIVE_API_KEY=pk_live_xxx uv run uvicorn app.main:app --port 8000
```

Expect: first frame within a couple of seconds (first poll), next frames at the `poll_interval`. If you see an empty cache for more than ~30 s, check the logs for 401 / 429.

### Determinism

```bash
SIMULATOR_SEED=42 uv run uvicorn app.main:app --port 8000
```

Two runs with the same seed, same ticker set, and the same number of `step()` calls will produce identical price paths. Wall-clock interleaving is not controlled, so in-process tests should drive `step()` manually rather than relying on `asyncio.sleep` cadence.
