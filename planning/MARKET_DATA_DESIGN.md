# Market Data Backend — Design Document

**Status:** Reflects the implementation as built in `backend/app/market/`. This is a
from-the-code design reference, not a pre-implementation proposal — see
`planning/MARKET_DATA_SUMMARY.md` for the short version and
`planning/archive/` for the original pre-build design notes.

## Table of Contents

1. [Goals & Constraints](#1-goals--constraints)
2. [Architecture Overview](#2-architecture-overview)
3. [File Structure](#3-file-structure)
4. [Data Model — `PriceUpdate`](#4-data-model--priceupdate)
5. [The Price Cache](#5-the-price-cache)
6. [The Abstract Interface — `MarketDataSource`](#6-the-abstract-interface--marketdatasource)
7. [Seed Prices & Ticker Parameters](#7-seed-prices--ticker-parameters)
8. [The Simulator — GBM Engine](#8-the-simulator--gbm-engine)
9. [The Massive (Polygon.io) Client](#9-the-massive-polygonio-client)
10. [The Factory](#10-the-factory)
11. [SSE Streaming Endpoint](#11-sse-streaming-endpoint)
12. [Wiring It Into FastAPI](#12-wiring-it-into-fastapi)
13. [Watchlist Coordination](#13-watchlist-coordination)
14. [Testing Strategy](#14-testing-strategy)
15. [Error Handling & Edge Cases](#15-error-handling--edge-cases)
16. [Configuration Summary](#16-configuration-summary)

---

## 1. Goals & Constraints

- **Source-agnostic downstream code.** SSE streaming, portfolio valuation, and trade
  execution must not know or care whether prices come from the simulator or from
  Massive (Polygon.io).
- **Zero external dependencies by default.** A student with no API key gets a fully
  working, realistic-looking market out of the box.
- **Thread-safe, single writer semantics.** Exactly one background task (simulator loop
  or Massive poller) writes prices; any number of readers (SSE connections, trade
  validation) read concurrently.
- **Cheap to poll for changes.** SSE needs to know "has anything changed since I last
  looked?" without diffing every ticker on every tick.
- **Dynamic watchlist.** Tickers can be added/removed at runtime (manually or via the
  AI chat) without restarting the data source.

These constraints drove three decisions that shape everything below: the **Strategy
pattern** (`MarketDataSource` ABC), the **shared cache as the only handoff point**
between producers and consumers, and a **monotonic version counter** on the cache for
cheap change detection.

---

## 2. Architecture Overview

```
                    ┌─────────────────────────┐
                    │   create_market_data_    │
                    │   source(cache)           │
                    │   (factory.py)             │
                    └───────────┬───────────────┘
                                │  reads MASSIVE_API_KEY
                 ┌──────────────┴───────────────┐
                 ▼                               ▼
   ┌───────────────────────┐       ┌────────────────────────────┐
   │  SimulatorDataSource    │       │  MassiveDataSource            │
   │  (simulator.py)          │       │  (massive_client.py)          │
   │  GBM + Cholesky          │       │  REST poller (Polygon.io)     │
   │  correlated random walk  │       │  via `massive` package        │
   │  ~500ms asyncio loop      │       │  15s asyncio loop (free tier)  │
   └────────────┬──────────────┘       └───────────────┬────────────┘
                │  cache.update(ticker, price)          │  cache.update(ticker, price, ts)
                └──────────────────┬────────────────────┘
                                   ▼
                     ┌───────────────────────────┐
                     │      PriceCache              │
                     │      (cache.py)               │
                     │  dict[ticker] -> PriceUpdate  │
                     │  Lock-protected, version ctr  │
                     └──────────────┬────────────────┘
                                    │  reads only
              ┌─────────────────────┼─────────────────────┐
              ▼                     ▼                     ▼
   ┌────────────────────┐ ┌──────────────────┐ ┌───────────────────────┐
   │ SSE stream router     │ │ Portfolio valuation │ │ Trade execution          │
   │ GET /api/stream/prices│ │ (positions endpoint) │ │ (POST /api/portfolio/  │
   │ (stream.py)            │ │                       │ │  trade)                   │
   └────────────────────┘ └──────────────────┘ └───────────────────────┘
```

Both `SimulatorDataSource` and `MassiveDataSource` implement the same abstract base
class (`MarketDataSource`) and are the **only** things that ever call
`PriceCache.update()`. Everything else — SSE, portfolio math, trade validation — only
ever calls `PriceCache.get()` / `get_all()` / `get_price()`. This one-way data flow is
what makes swapping data sources a one-line change in `factory.py` with zero blast
radius elsewhere.

---

## 3. File Structure

```
backend/app/market/
├── __init__.py         # Public API surface (re-exports)
├── models.py            # PriceUpdate — immutable frozen dataclass
├── cache.py              # PriceCache — thread-safe store + version counter
├── interface.py          # MarketDataSource — abstract base class
├── seed_prices.py        # Seed prices, per-ticker GBM params, correlation groups
├── simulator.py           # GBMSimulator (math) + SimulatorDataSource (async wrapper)
├── massive_client.py       # MassiveDataSource — Polygon.io REST poller
├── factory.py              # create_market_data_source() — env-driven selection
└── stream.py                # create_stream_router() — FastAPI SSE endpoint factory

backend/tests/market/
├── test_models.py
├── test_cache.py
├── test_simulator.py         # Pure GBMSimulator math
├── test_simulator_source.py   # SimulatorDataSource integration (asyncio)
├── test_factory.py
└── test_massive.py            # MassiveDataSource with mocked RESTClient

backend/market_data_demo.py    # Rich terminal dashboard, standalone demo
```

Public API (what the rest of the backend is expected to import):

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

Anything not in `__all__` (e.g. `GBMSimulator`, `MassiveDataSource` directly,
`seed_prices` constants) is an implementation detail. Downstream code should import
only from `app.market`, not from the submodules — this is what lets the internals
change freely.

---

## 4. Data Model — `PriceUpdate`

```python
# backend/app/market/models.py
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

### Design decisions

- **`frozen=True, slots=True`** — a `PriceUpdate` is a value, never mutated in place.
  Producing a new price means constructing a new `PriceUpdate`, not patching fields.
  `slots` keeps the per-tick allocation cheap (no `__dict__`) since this object is
  created for every ticker on every tick.
- **`change` / `change_percent` / `direction` are derived properties, not stored
  fields.** They're computed from `price` and `previous_price` on read, so there's no
  way for them to drift out of sync with the two source values.
- **`to_dict()` is the SSE/JSON wire format.** It's the single place that defines what
  the frontend receives — round-tripping through `dataclasses.asdict()` was avoided
  because the derived fields need to be included explicitly.
- **`timestamp` defaults to `time.time()`** only as a convenience for ad-hoc
  construction (e.g. tests); real writers (`PriceCache.update`) always pass one
  explicitly.

---

## 5. The Price Cache

```python
# backend/app/market/cache.py
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
        """Snapshot of all current prices. Returns a shallow copy."""
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
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE endpoint polls the cache every ~500ms (see §11). Without a cheap "did
anything change" signal it would have to either (a) always push a payload, wasting
bandwidth when nothing moved, or (b) diff the previous snapshot against the current
one on every tick, which is O(n) dict comparison work per client per tick. Instead,
`_version` is a single integer bumped on every `update()` call; the SSE loop just
compares `cache.version` to the value it saw last cycle. First-class cheap, and
correct even under concurrent writes because the bump happens inside the same lock as
the price write.

### Thread safety rationale

`PriceCache` uses a plain `threading.Lock`, not an `asyncio.Lock`, even though every
call site today is inside `async def` methods. This is deliberate:

- `MassiveDataSource._fetch_snapshots` runs the synchronous `massive` REST client via
  `asyncio.to_thread()` (§9) — that call happens on a worker thread, not the event
  loop, so anything it touches needs real (OS-level) thread safety, not just
  cooperative-yield safety.
- A `threading.Lock` is safe to acquire from both the event loop thread and a worker
  thread. An `asyncio.Lock` is not thread-safe across loops/threads at all.
- Critical sections are tiny (dict read/write + int increment) — lock contention is a
  non-issue at this scale (≤ a few dozen tickers).

---

## 6. The Abstract Interface — `MarketDataSource`

```python
# backend/app/market/interface.py
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
        """Stop the background task and release resources. Safe to call multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes it from the PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

This is a textbook **Strategy pattern**: `factory.py` picks a concrete strategy at
startup, everything else programs to the interface. Five methods is the entire
surface area — small enough that a third implementation (e.g. a different vendor,
or a "replay a recorded session" source for demos) is a same-shaped, low-risk addition.

### Why the source writes to the cache instead of returning prices

An alternative design would have `start()` return an async generator the caller
iterates. That was rejected because:

- Multiple independent consumers (SSE per-client, portfolio valuation, trade
  execution) all need the *current* price at arbitrary times, not a stream they'd each
  have to buffer and replay.
- The cache already needs to exist as the change-detection point for SSE (§5) — having
  the source write there directly avoids a second hop.
- It decouples the source's cadence (500ms sim tick vs. 15s Massive poll) from
  consumers' cadence (SSE pushes every 500ms regardless of source, always serving
  the latest cached value).

---

## 7. Seed Prices & Ticker Parameters

```python
# backend/app/market/seed_prices.py

# Realistic starting prices for the default watchlist (as of project creation)
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00, "NVDA": 800.00, "META": 500.00, "JPM": 195.00,
    "V": 280.00, "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},    # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},      # Low volatility (payments)
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
INTRA_TECH_CORR = 0.6      # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR = 0.3     # Between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing
```

A ticker not present in `SEED_PRICES` / `TICKER_PARAMS` (e.g. the user adds `PYPL` via
chat) falls back to `random.uniform(50.0, 300.0)` for its starting price (see
`GBMSimulator._add_ticker_internal` in §8) and `DEFAULT_PARAMS` for its volatility/drift
— it still participates fully in the simulation, just without hand-tuned realism.

---

## 8. The Simulator — GBM Engine

The simulator is split into two layers: **`GBMSimulator`**, pure math with no asyncio
or I/O, and **`SimulatorDataSource`**, a thin async wrapper that runs it on a timer and
writes results into the shared cache. This split is what makes `GBMSimulator` trivially
unit-testable (`test_simulator.py`, 17 tests, 98% coverage) without touching asyncio at
all.

### 8.1 `GBMSimulator` — the math engine

```python
# backend/app/market/simulator.py (excerpt)
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices.

    Math:
        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Where:
        S(t)   = current price
        mu     = annualized drift (expected return)
        sigma  = annualized volatility
        dt     = time step as fraction of a trading year
        Z      = correlated standard normal random variable
    """

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8, i.e. 500ms as a fraction of a trading year

    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001):
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
        """Advance all tickers by one time step. Hot path — called every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign

            result[ticker] = round(self._prices[ticker], 2)
        return result
```

**Correlated randomness via Cholesky decomposition.** Each tick draws `n` independent
standard normals (`np.random.standard_normal(n)`), then multiplies by the Cholesky
factor `L` of the ticker correlation matrix `C` (`C = L @ L.T`). The result
`z_correlated = L @ z_independent` is a vector of standard normals with the desired
pairwise correlations — this is the standard technique for simulating correlated
Brownian motion and is O(n²) only when the matrix is rebuilt (add/remove ticker), not
on every tick.

```python
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

    def _add_ticker_internal(self, ticker: str) -> None:
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
        """
        - TSLA with anything:   0.3 (it does its own thing)
        - Same tech sector:     0.6
        - Same finance sector:  0.5
        - Cross-sector / unknown: 0.3
        """
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

Two batching details worth calling out:

- **Batch init avoids O(n) Cholesky rebuilds on construction.** The constructor calls
  the *internal*, no-rebuild `_add_ticker_internal` for each starting ticker and
  rebuilds the correlation matrix once at the end — not once per ticker. The public
  `add_ticker()` (used at runtime, e.g. from the watchlist) does rebuild every call,
  which is fine because runtime additions are rare (user action) versus the hot 500ms
  `step()` path.
- **Random shock events add visual drama.** At `event_probability=0.001` per
  ticker per tick, with 10 tickers ticking twice a second, the simulator produces a
  visible 2–5% jump roughly every ~50 seconds — enough to make the demo feel alive
  without every stock careening constantly.

### 8.2 `SimulatorDataSource` — async wrapper

```python
# backend/app/market/simulator.py (excerpt)
class SimulatorDataSource(MarketDataSource):
    """Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache."""

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache with initial prices so SSE has data immediately
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

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

### Key behaviors

- **Cache seeded before `_run_loop` starts** — both in `start()` and `add_ticker()` —
  so a ticker's first price is available immediately rather than waiting up to 500ms
  for the first `step()`. Without this, a freshly-added watchlist ticker could briefly
  return `None` from `cache.get_price()`.
- **Cancellation is cooperative and clean.** `stop()` cancels the task and awaits it,
  swallowing the expected `CancelledError` — this is the standard asyncio pattern for
  a clean shutdown that doesn't leak a "task was destroyed but it is pending" warning.
- **Exceptions inside `_run_loop` never kill the loop.** A `step()` failure (should
  never happen, but numpy/state bugs are still possible) is logged and the loop
  continues on the next tick rather than silently going quiet forever.

---

## 9. The Massive (Polygon.io) Client

```python
# backend/app/market/massive_client.py
from massive import RESTClient
from massive.rest.models import SnapshotMarketType


class MassiveDataSource(MarketDataSource):
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free tier: 5 req/min → poll every 15s (default)
      - Paid tiers: higher limits → poll every 2-5s
    """

    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll so cache has data right away
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

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
            self._tickers.append(ticker)  # picked up on the next poll

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
            # The Massive RESTClient is synchronous — run in a thread to avoid
            # blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> s
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — the loop retries on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Design choices

- **REST polling, not WebSocket.** Polygon.io's free/lower tiers don't include
  WebSocket streaming, and REST polling is simpler to reason about, test (mock a
  function call, not a socket), and degrade gracefully (a failed poll just means
  stale-but-present data, not a dropped connection to reconnect). The polling interval
  (`poll_interval`, default 15s) is tuned to the free tier's 5 requests/minute limit;
  paid tiers can pass a shorter interval.
- **One API call per poll cycle, not one per ticker.** `get_snapshot_all` takes the
  full ticker list and returns all snapshots in a single request — this is what makes
  polling within a 5 req/min budget practical for a 10+ ticker watchlist.
- **Synchronous SDK bridged via `asyncio.to_thread`.** The `massive` package's
  `RESTClient` is a blocking `requests`-style client. Calling it directly from
  `_poll_once` would stall the event loop (and therefore every SSE client, every other
  concurrent request) for the duration of the HTTP round-trip. `asyncio.to_thread`
  moves that blocking call to a worker thread so the event loop stays responsive.
- **Failures never propagate out of `_poll_once`.** A bad API key (401), rate limit
  (429), or transient network error is logged, not raised — the poller simply tries
  again next cycle. This matches the cache's semantics: a ticker with no successful
  poll yet just has no entry (`cache.get()` returns `None`); a ticker that *had* a
  price keeps serving the last-known value until the next successful poll, rather than
  the whole app crashing over a single flaky request.
- **Per-snapshot parsing failures are isolated.** If Polygon returns a malformed or
  unexpected snapshot for one ticker (`AttributeError`/`TypeError` from unexpected
  response shape), that one ticker is skipped and logged; the rest of the batch still
  updates.
- **Timestamp conversion.** Polygon's `last_trade.timestamp` is Unix milliseconds;
  `PriceCache`/`PriceUpdate` use Unix seconds throughout (matching `time.time()`), so
  the client divides by 1000 at the boundary — the only place this conversion happens.

### Interface parity with the simulator

Both `add_ticker`/`remove_ticker` update `self._tickers` immediately, but the new
ticker's *price* doesn't appear in the cache until the next scheduled poll (up to
`poll_interval` seconds later) — unlike the simulator, which seeds a price
synchronously. This is an intentional, unavoidable difference given Massive's request
budget (an immediate one-off poll per `add_ticker` call would blow through 5 req/min
almost instantly on a busy watchlist), and downstream code (SSE, trade validation)
already has to handle "ticker known but no price yet" as `cache.get_price()` returning
`None`.

---

## 10. The Factory

```python
# backend/app/market/factory.py
import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """- MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

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

This is the entire environment-variable contract from `planning/PLAN.md` §5 in one
function: presence of a non-empty, whitespace-trimmed `MASSIVE_API_KEY` selects real
data; anything else (unset, empty string, whitespace-only) falls back to the
simulator. `.strip()` matters because a `.env` file with `MASSIVE_API_KEY=` (present
but empty) or `MASSIVE_API_KEY=   ` should not be treated as "configured" — both are
covered by the `if api_key:` check on the stripped value.

The factory returns an **unstarted** source deliberately — construction is
synchronous and cheap, but `start()` is async (it launches a task and, for Massive,
does an immediate network round-trip). Separating "create" from "start" lets the
caller control exactly when the background work begins, which matters for FastAPI's
async lifespan context (§12).

---

## 11. SSE Streaming Endpoint

```python
# backend/app/market/stream.py
import asyncio
import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

router = APIRouter(prefix="/api/stream", tags=["streaming"])


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Factory pattern lets us inject the PriceCache without globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
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
    price_cache: PriceCache, request: Request, interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # tell EventSource to retry after 1s on disconnect

    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
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
        pass  # client disconnected mid-await; let FastAPI clean up
```

### SSE wire format

Each event is a single JSON object keyed by ticker, containing the full
`PriceUpdate.to_dict()` for every tracked ticker:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.42, "previous_price": 190.31, "timestamp": 1735689600.123, "change": 0.11, "change_percent": 0.0578, "direction": "up"}, "GOOGL": {...}, ...}

data: {"AAPL": {...}, "GOOGL": {...}, ...}
```

The frontend's `EventSource` receives one `message` event per push and can
`JSON.parse(event.data)` to get the full ticker → update map — no per-ticker parsing
or reassembly needed.

### Why poll-and-push instead of event-driven?

The endpoint polls `price_cache.version` every 500ms rather than the cache notifying
subscribers on write. A pub/sub cache (e.g. an `asyncio.Condition` per client, or a
broadcast queue) would avoid the poll, but:

- Every SSE connection already needs its own 500ms tick regardless (that's the target
  update cadence per `planning/PLAN.md` §6), so polling isn't adding latency the design
  didn't already accept.
- It keeps `PriceCache` a plain, synchronous, dependency-free data structure — no
  asyncio primitives leak into `cache.py`, which is also what makes it safe to write to
  from a worker thread (§5).
- Version-based short-circuiting (`if current_version != last_version`) still avoids
  re-serializing and re-sending unchanged data, which is the actual cost this pattern
  needed to avoid.

### Reconnection

`EventSource` has built-in auto-reconnect; the `retry: 1000\n\n` directive at stream
start tells the browser to wait 1 second before reconnecting after a drop, rather than
hammering the server immediately. No custom reconnect logic is needed on either side.

### Disconnect detection

`request.is_disconnected()` is checked once per 500ms cycle (not more often — it's an
awaitable that itself has overhead). Combined with catching `asyncio.CancelledError`
around the whole loop, this handles both a graceful client-initiated close and a
server-initiated cancellation (e.g. app shutdown) cleanly, without leaking the
generator or its task.

---

## 12. Wiring It Into FastAPI

The market data subsystem is complete and tested standalone (via
`market_data_demo.py`), but the rest of the backend (routes, database, LLM chat) is
not yet built. This section is the integration contract the API/App layer should
follow when it's built.

### Startup / shutdown via lifespan

```python
# backend/app/main.py (sketch — not yet implemented)
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    market_source = create_market_data_source(price_cache)
    initial_tickers = load_watchlist_tickers()  # from SQLite, per planning/PLAN.md §7
    await market_source.start(initial_tickers)
    app.state.market_source = market_source

    yield

    await market_source.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
```

`price_cache` is a module-level singleton (or an `app.state` attribute set before
`lifespan` runs) because it needs to be shared between the SSE router (created at
import time, via `create_stream_router`) and any REST route handler that reads current
prices (portfolio valuation, trade execution). `market_source`, by contrast, is only
needed by whichever route handles watchlist add/remove (§13) — it's stashed on
`app.state` rather than made module-global so it's reachable via FastAPI's `Request`
without an import cycle.

### Accessing market data from other routes

```python
# e.g. backend/app/routes/portfolio.py (sketch)
from fastapi import APIRouter, Depends, Request

from app.market import PriceCache

router = APIRouter(prefix="/api/portfolio")


def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache  # or the module-level singleton


@router.post("/trade")
async def execute_trade(order: TradeRequest, cache: PriceCache = Depends(get_price_cache)):
    current_price = cache.get_price(order.ticker)
    if current_price is None:
        raise HTTPException(422, detail=f"No price available for {order.ticker}")
    # ... validate cash/shares, execute at current_price, record trade + snapshot ...
```

Every trade fills at whatever `cache.get_price()` returns at the moment of execution —
this *is* "instant fill at current price" from `planning/PLAN.md` §2. No price is
fetched independently for the trade; the cache is the single source of truth for "what
is the price right now" everywhere in the app, matching §2's architecture goal.

---

## 13. Watchlist Coordination

The watchlist lives in SQLite (`planning/PLAN.md` §7), not in the market data module —
`MarketDataSource` only tracks *which tickers currently have live prices flowing*, and
must be kept in sync with the DB by whichever route owns watchlist mutations.

### Flow: adding a ticker

```python
# e.g. backend/app/routes/watchlist.py (sketch)
@router.post("")
async def add_to_watchlist(body: AddTickerRequest, request: Request):
    ticker = body.ticker.upper().strip()
    db.insert_watchlist_entry(ticker)                 # persist first
    await request.app.state.market_source.add_ticker(ticker)  # then start streaming it
    return {"ticker": ticker}
```

DB write happens before the market-data call: if the process restarts between the two
steps, the watchlist entry survives in SQLite and gets picked up on next boot's
`start(initial_tickers)`; the reverse order risks a ticker streaming prices that never
actually got saved.

### Flow: removing a ticker

```python
@router.delete("/{ticker}")
async def remove_from_watchlist(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    await request.app.state.market_source.remove_ticker(ticker)  # stop streaming, evict from cache
    db.delete_watchlist_entry(ticker)                              # then persist
    return {"ticker": ticker}
```

Removal is the mirror image: stop the live feed and evict the stale price from the
cache *before* the DB write, since `remove_ticker` also calls `cache.remove(ticker)` —
you want the ticker gone from what `GET /api/watchlist` and SSE report before
confirming the removal succeeded.

### Edge case: a ticker with an open position is removed from the watchlist

`planning/PLAN.md` doesn't require positions to stay on the watchlist. If a user
removes a ticker they still hold shares in, `remove_ticker()` will stop its price
updates and evict it from the cache — subsequent `cache.get_price()` calls for that
ticker return `None`, which breaks P&L calculation and heatmap rendering for that
position. The recommended fix at the API layer (not something the market data module
should special-case, since it has no concept of "positions"): the watchlist removal
route should check for an open position first and either reject the removal or
silently keep the ticker in `market_source`'s active set (skip calling
`remove_ticker`) while still removing it from the *displayed* watchlist. This is a
decision for whichever agent builds the portfolio/watchlist routes, flagged here so
it isn't missed.

---

## 14. Testing Strategy

73 tests across 6 modules, 84% overall coverage on `app/market/`.

| Module | Tests | What it covers |
|---|---|---|
| `test_models.py` | 11 | `PriceUpdate` properties (`change`, `change_percent`, `direction`, `to_dict`), edge cases (zero previous price, equal prices) |
| `test_cache.py` | 13 | Thread-safety-adjacent behavior, version bumping, `get`/`get_all`/`get_price`/`remove` semantics, first-update-has-no-prior-price case |
| `test_simulator.py` | 17 | Pure `GBMSimulator` math — no asyncio: price stays positive, correlation matrix shape/symmetry, Cholesky rebuild on add/remove, event shock bounds |
| `test_simulator_source.py` | 10 | `SimulatorDataSource` integration — asyncio task lifecycle, cache gets seeded, `stop()` cancels cleanly |
| `test_factory.py` | 7 | Env var selection logic (unset / empty / whitespace / set) |
| `test_massive.py` | 13 | `MassiveDataSource` with `RESTClient` mocked — poll success, malformed snapshot skip, exception doesn't kill the loop |

### 14.1 Testing `GBMSimulator` (pure math, no asyncio)

```python
def test_step_keeps_prices_positive():
    sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
    for _ in range(1000):
        prices = sim.step()
    assert prices["AAPL"] > 0

def test_correlation_matrix_symmetric():
    sim = GBMSimulator(tickers=["AAPL", "GOOGL", "TSLA"])
    sim._rebuild_cholesky()
    corr = sim._cholesky @ sim._cholesky.T
    assert np.allclose(corr, corr.T)

def test_add_ticker_rebuilds_cholesky():
    sim = GBMSimulator(tickers=["AAPL"])
    old = sim._cholesky
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not old
    assert sim._cholesky.shape == (2, 2)
```

Setting `event_probability=0.0` in most tests removes the random-shock nondeterminism
so assertions about drift/variance bounds are stable; a small number of tests set it to
`1.0` specifically to assert the shock path fires and stays within `[0.02, 0.05]`
magnitude.

### 14.2 Testing `PriceCache`

```python
def test_first_update_has_flat_direction():
    cache = PriceCache()
    update = cache.update("AAPL", 190.0)
    assert update.previous_price == update.price
    assert update.direction == "flat"

def test_version_increments_on_every_update():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v1 = cache.version
    cache.update("AAPL", 191.0)
    assert cache.version == v1 + 1

def test_remove_evicts_ticker():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert "AAPL" not in cache
```

### 14.3 Integration test: `SimulatorDataSource`

```python
@pytest.mark.asyncio
async def test_start_seeds_cache_immediately():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.05)
    await source.start(["AAPL", "GOOGL"])
    try:
        assert cache.get_price("AAPL") is not None  # seeded before first tick
        await asyncio.sleep(0.15)
        assert cache.version > 0  # at least one real step happened
    finally:
        await source.stop()

@pytest.mark.asyncio
async def test_stop_cancels_task_cleanly():
    cache = PriceCache()
    source = SimulatorDataSource(cache, update_interval=0.05)
    await source.start(["AAPL"])
    await source.stop()
    assert source._task is None
```

### 14.4 Testing `MassiveDataSource` (mocked)

```python
@pytest.mark.asyncio
async def test_poll_updates_cache(monkeypatch):
    cache = PriceCache()
    source = MassiveDataSource(api_key="fake", price_cache=cache, poll_interval=100)

    class FakeSnapshot:
        ticker = "AAPL"
        class last_trade:
            price = 190.42
            timestamp = 1735689600123  # ms

    source._client = MagicMock()
    monkeypatch.setattr(source, "_fetch_snapshots", lambda: [FakeSnapshot()])

    await source._poll_once()

    update = cache.get("AAPL")
    assert update.price == 190.42
    assert update.timestamp == pytest.approx(1735689600.123)

@pytest.mark.asyncio
async def test_poll_failure_does_not_raise(monkeypatch):
    cache = PriceCache()
    source = MassiveDataSource(api_key="fake", price_cache=cache)
    source._client = MagicMock()
    monkeypatch.setattr(source, "_fetch_snapshots", lambda: (_ for _ in ()).throw(ConnectionError()))
    await source._poll_once()  # must not raise
```

`source._client` is set directly (bypassing `start()`'s real `RESTClient(...)`
construction) so tests never touch the network; `_fetch_snapshots` — the one method
that calls the real SDK — is monkeypatched per-test to return canned data or raise.

---

## 15. Error Handling & Edge Cases

### 15.1 Startup with an empty watchlist

`GBMSimulator.step()` and `MassiveDataSource._poll_once()` both guard `n == 0` /
`not self._tickers` and return early — starting a source with `tickers=[]` is valid
and simply produces no price updates until a ticker is added later via `add_ticker()`.

### 15.2 Price cache miss during trade validation

`cache.get_price(ticker)` returns `None` for a ticker with no successful update yet
(new Massive ticker before first poll, or a typo'd ticker never added to any source).
Route-layer code (§12) must treat `None` as "reject the trade / return 422", never
default to `0` or skip the check — trading against a `None`/`0` price would let a
"buy" execute for free.

### 15.3 Invalid Massive API key

`RESTClient(api_key=...)` doesn't validate the key at construction — the first real
failure surfaces as an exception inside `_poll_once`'s `try/except Exception` (401 from
Polygon), which is caught, logged, and retried on the next interval **forever**. There
is currently no mechanism to detect "this key will never work" and fall back to the
simulator or surface a persistent error to the user — the app would silently show a
watchlist with no prices. If this needs to be more visible, a future addition could
count consecutive `_poll_once` failures and expose an `/api/health` degraded state
(§ `planning/PLAN.md` §8) after N consecutive failures — not implemented today.

### 15.4 Thread safety under load

`PriceCache`'s lock-protected critical sections are O(1) (dict get/set + int
increment); at the project's scale (≤ dozens of tickers, one writer, a handful of SSE
readers) lock contention is not a practical concern. This was validated by the
existing test suite exercising concurrent-ish access patterns, not by dedicated load
testing — if the watchlist ever needed to scale to hundreds/thousands of tickers or
many concurrent writers, this would be the first place to revisit (e.g. splitting into
per-ticker locks, or moving to `asyncio.Lock` + a single-threaded design if the
Massive client's blocking call were replaced with a native async HTTP client).

### 15.5 Simulator numerical precision

`DEFAULT_DT ≈ 8.48e-8` (500ms as a fraction of a 252-trading-day year) is small enough
that `math.exp(drift + diffusion)` is always close to 1.0 per tick — this is
intentional (real markets don't move double digits % in half a second) but means a
single `step()` call rounds to the same price roughly as often as not for
low-volatility, low-price tickers; visible movement accumulates over many ticks, not
within one. `test_simulator.py` asserts statistical properties over many steps (e.g.
"price stays within N standard deviations after 1000 steps"), not single-tick deltas.

---

## 16. Configuration Summary

| Env var | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | unset | Non-empty (after `.strip()`) → `MassiveDataSource`; unset/empty/whitespace → `SimulatorDataSource` |

| Parameter | Where | Default | Notes |
|---|---|---|---|
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` (s) | Matches `planning/PLAN.md` §6 target cadence |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | `0.001` | ~1 shock per ticker per ~50s at 2 ticks/s |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` (s) | Tuned for Polygon.io free tier (5 req/min); pass lower for paid tiers |
| SSE push `interval` | `stream._generate_events` | `0.5` (s) | How often the SSE loop checks `cache.version` |
| SSE `retry` directive | `stream._generate_events` | `1000` (ms) | Browser reconnect delay via `EventSource` |

### Package `__init__.py` (recap)

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate", "PriceCache", "MarketDataSource",
    "create_market_data_source", "create_stream_router",
]
```

Everything the rest of the backend needs from this module is importable from
`app.market` directly:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_stream_router
```
