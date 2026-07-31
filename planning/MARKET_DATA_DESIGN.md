# Market Data Backend — Design

Authoritative, implementation-accurate design for the FinAlly market data subsystem: the unified
interface, the in-memory price cache, the GBM simulator, the Massive (Polygon.io) REST client, the
SSE streaming endpoint, and FastAPI lifecycle wiring.

**Status:** this describes the code as it actually ships in `backend/app/market/` (8 modules, 73
passing tests, 84% coverage). It is generated from a direct reading of the source, not derived from
an earlier proposal — see `planning/MARKET_DATA_SUMMARY.md` for the short version and
`planning/archive/` for prior-iteration drafts (some details there, e.g. simulator seeding and the
Massive call shape, were superseded during code review and are corrected here).

## Table of Contents

1. [Architecture at a Glance](#1-architecture-at-a-glance)
2. [Data Model — `models.py`](#2-data-model--modelspy)
3. [Unified Interface — `interface.py`](#3-unified-interface--interfacepy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Factory — `factory.py`](#5-factory--factorypy)
6. [Simulator — `seed_prices.py` + `simulator.py`](#6-simulator--seed_pricespy--simulatorpy)
7. [Massive API Client — `massive_client.py`](#7-massive-api-client--massive_clientpy)
8. [SSE Streaming Endpoint — `stream.py`](#8-sse-streaming-endpoint--streampy)
9. [FastAPI Lifecycle Integration](#9-fastapi-lifecycle-integration)
10. [Watchlist Coordination](#10-watchlist-coordination)
11. [Testing Strategy](#11-testing-strategy)
12. [Error Handling & Edge Cases](#12-error-handling--edge-cases)
13. [Configuration Reference](#13-configuration-reference)

---

## 1. Architecture at a Glance

```
                 create_market_data_source(cache)
                             │
                 MASSIVE_API_KEY set & non-empty?
                    ┌────────┴────────┐
                   yes                no
                    │                  │
                    ▼                  ▼
          MassiveDataSource     SimulatorDataSource
        (REST poller, ~15s)      (GBM, ~500ms)
                    │                  │
                    └────────┬─────────┘
                             ▼
                       PriceCache
                (thread-safe, in-memory,
                 version counter)
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
     GET /api/stream/prices  Portfolio      Trade
        (SSE, ~500ms)       valuation     execution
```

`backend/app/market/` is a self-contained subsystem. Everything outside it — REST routes, trade
execution, the frontend — depends only on `PriceCache` and `PriceUpdate`. Neither the simulator nor
the Massive client is ever referenced by name outside this package; they're selected once, at
startup, by `create_market_data_source()`.

```
backend/app/market/
├── __init__.py         # re-exports PriceUpdate, PriceCache, MarketDataSource,
│                       #   create_market_data_source, create_stream_router
├── models.py           # PriceUpdate
├── interface.py        # MarketDataSource ABC
├── cache.py            # PriceCache
├── factory.py          # create_market_data_source()
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, correlation constants
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # MassiveDataSource
└── stream.py            # create_stream_router() — SSE endpoint factory
```

---

## 2. Data Model — `models.py`

`PriceUpdate` is the only object that crosses the boundary out of `app/market/`. Both data sources
produce it (indirectly, via `PriceCache.update()` — see §4); every consumer (SSE, portfolio
valuation, trade execution) consumes only this type.

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
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Single serialization point, used by the SSE endpoint (and any REST
        response that echoes a price)."""
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

Design notes:

- **`frozen=True, slots=True`** — value objects created many times a second across two hot loops
  (simulator tick, Massive poll); frozen makes them safe to hand to concurrent readers without
  copying, slots trims per-instance memory.
- **`timestamp` is `float` (Unix seconds), not `datetime`** — cheaper to construct, trivially
  JSON-serializable, and matches what both producers naturally have on hand (`time.time()` for the
  simulator, a converted Massive epoch-millis field for the live feed).
- **`direction`/`change`/`change_percent` are computed properties**, not stored fields — they can
  never drift out of sync with `price`/`previous_price`.

---

## 3. Unified Interface — `interface.py`

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
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Safe to call
        multiple times."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. Also removes it from the
        PriceCache."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

This is the entire surface area either implementation exposes. Neither `SimulatorDataSource` nor
`MassiveDataSource` exposes its internals (no HTTP client, no simulation state) publicly, so callers
can't accidentally couple to one concrete implementation.

**Why the source pushes into a cache instead of returning prices from a method call:** it decouples
timing. The simulator ticks every ~500ms; Massive polls every ~15s on the free tier. The SSE layer
reads the cache at its own fixed ~500ms cadence regardless of which producer is active or how often
it actually refreshes data — see §8.

---

## 4. Price Cache — `cache.py`

The single point of truth between producer and every consumer. One data source writes; the SSE
endpoint, portfolio valuation, and trade execution all read.

```python
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
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price
        (direction == 'flat').
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
        """Snapshot of all current prices. Returns a shallow copy so callers
        can iterate without holding the lock."""
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
        """Bumped on every update() call. Lets the SSE loop cheaply detect
        'has anything changed since I last looked' without diffing the dict."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Design points:**

- **`PriceCache` owns `PriceUpdate` construction** — callers pass a raw `(ticker, price,
  timestamp?)`, and the cache itself looks up the previous value and computes `previous_price`.
  Neither data source constructs a `PriceUpdate` directly; both just call `cache.update(...)`. This
  keeps "what counts as the previous price" defined in exactly one place.
- **`threading.Lock`, not `asyncio.Lock`.** The Massive client's synchronous `RESTClient` call runs
  inside `asyncio.to_thread(...)` — a real OS thread, which an `asyncio.Lock` would not protect
  against. A plain mutex works correctly from both the event loop and any thread-pool worker.
- **The cache never loses a value except via explicit `remove()`.** Nothing times it out. This is
  what makes Massive rate limits or a slow poll invisible to the frontend — stale-but-present data
  beats a gap.

---

## 5. Factory — `factory.py`

The only place in the codebase that reads `MASSIVE_API_KEY`.

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
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
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

`massive` is a core dependency (`pyproject.toml`), imported at module top level in both
`massive_client.py` and here — no lazy import. Whether or not a student ever sets
`MASSIVE_API_KEY`, `uv sync` installs `massive` and the import always succeeds; only the *choice* of
which class to instantiate is conditional.

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g. ["AAPL", "GOOGL", ...] from the watchlist table
```

---

## 6. Simulator — `seed_prices.py` + `simulator.py`

The default data source — no external dependencies beyond `numpy`, no API key, runs fully offline.

### 6.1 Seed prices and per-ticker parameters — `seed_prices.py`

Constants only. Shared by `simulator.py` for initial prices, GBM parameters, and correlation
structure.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# Per-ticker GBM parameters.
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added, e.g. via watchlist/chat)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition.
# Tickers in the same group have higher intra-group correlation.
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # tech stocks move together
INTRA_FINANCE_CORR = 0.5   # finance stocks move together
CROSS_GROUP_CORR = 0.3     # between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing, even though it's in the "tech" set
```

Ticker not in `SEED_PRICES`/`TICKER_PARAMS` (added at runtime via the watchlist or the AI chat) get
a random seed price in `[50, 300]` and `DEFAULT_PARAMS` — see `_add_ticker_internal` below.

### 6.2 `GBMSimulator` — the math engine

Discrete-time geometric Brownian motion:

```
S(t+dt) = S(t) * exp((mu - sigma²/2) * dt + sigma * sqrt(dt) * Z)
```

`Z` is a *correlated* standard normal draw, not independent per ticker — see the Cholesky step
below — so sector groups move together the way real markets do, rather than each ticker jittering
in isolation.

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

    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 "trading seconds" per year
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8, for a 500ms tick

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
        """Advance all tickers by one time step. Returns {ticker: new_price}.
        Hot path — called every 500ms."""
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

            # ~0.1% chance per tick per ticker of a 2-5% shock (visual drama)
            if random.random() < self._event_prob:
                shock_magnitude = random.uniform(0.02, 0.05)
                shock_sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock_magnitude * shock_sign
                logger.debug(
                    "Random event on %s: %.1f%% %s",
                    ticker, shock_magnitude * 100, "up" if shock_sign > 0 else "down",
                )

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

    # --- internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2), called on every add/remove — fine at this scale (n < 50)."""
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
        """Same tech sector: 0.6. Same finance sector: 0.5. TSLA with anything:
        0.3 (it does its own thing). Everything else: 0.3."""
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

**Why Cholesky:** drawing `n` independent standard normals and left-multiplying by the Cholesky
factor `L` of a target correlation matrix `Σ` (`L @ L.T == Σ`) produces a vector of normals with
exactly that correlation structure. It's the standard trick for correlated Monte Carlo draws and is
cheap enough to redo on every ticker add/remove at watchlist scale (≤ a few dozen names).

**Why GBM specifically:** returns are log-normal, prices can never go negative (it's an
exponential), and `(mu, sigma)` map directly onto "steady mover" vs. "choppy" stock intuitions —
easy to hand-tune per ticker and get results that feel roughly right without being predictive.

### 6.3 `SimulatorDataSource` — async wrapper implementing `MarketDataSource`

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
        # Seed the cache immediately so SSE has data on its very first tick
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
                self._cache.update(ticker=ticker, price=price)  # visible immediately
            logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed ticker %s", ticker)

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
                logger.exception("Simulator step failed")  # one bad tick doesn't kill the feed
            await asyncio.sleep(self._interval)
```

Key behaviors: immediate cache seeding on `start()`/`add_ticker()` (no blank watchlist row while
waiting for the first tick), clean cancellation on `stop()`, and per-tick exception isolation in
`_run_loop` so a transient failure doesn't take down the whole background task.

---

## 7. Massive API Client — `massive_client.py`

The optional, real-data source, active only when `MASSIVE_API_KEY` is set. Polls the batched
multi-ticker snapshot endpoint — one HTTP call regardless of watchlist size — on an interval sized
for the free tier's 5 requests/minute budget.

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

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()  # immediate first poll so the cache has data right away
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval", len(tickers), self._interval
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
            logger.info("Massive: added ticker %s (will appear on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: removed ticker %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- internal ---

    async def _poll_loop(self) -> None:
        """First poll already happened in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return

        try:
            # RESTClient is synchronous — offload to a thread to avoid blocking the event loop
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.timestamp / 1000.0  # ms -> seconds
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    # Per-ticker bad entry (e.g. NOT_FOUND) — skip it, keep the rest of the batch
                    logger.warning(
                        "Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e
                    )
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Never re-raise: the whole cache is left untouched (last-known prices
            # keep serving), and the loop retries on the next scheduled interval.
            # Common causes: 401 (bad key), 429 (rate limit), network/timeout errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs inside asyncio.to_thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Why the multi-ticker snapshot endpoint

The **Full Market Snapshot** endpoint (`GET /v2/snapshot/locale/us/markets/stocks/tickers`) accepts
a comma-separated ticker list — up to 250 symbols — and returns the latest trade/quote/day bar for
each in one response. That's what makes a single `RESTClient.get_snapshot_all(...)` call cover the
entire watchlist regardless of size, which matters directly for the free tier's 5 req/min ceiling:
one poll of 10 tickers costs exactly the same as one poll of 1.

### Fields actually consumed

Only `snap.last_trade.price` and `snap.last_trade.timestamp` are read from each snapshot entry —
the client doesn't touch `day`/`prevDay` bars. `previous_price` for direction/change is always
computed by `PriceCache.update()` from whatever was cached from the *previous poll* (see §4), so the
very first update for a ticker is always `direction == "flat"` (`previous_price == price`), the same
convention the simulator uses on its first tick for a new ticker.

### Error handling philosophy

| Situation | Behavior |
|---|---|
| Per-ticker bad entry (`NOT_FOUND`, missing fields) | That entry is skipped (`AttributeError`/`TypeError` caught); the rest of the batch is still processed. |
| `401 Unauthorized` (bad key) | Whole poll fails, logged as error; cache untouched; retried on the next interval. |
| `429 Too Many Requests` | Same — logged, cache untouched, retried next interval. Sign the poll interval is too aggressive for the plan tier; raise `poll_interval` via configuration if it recurs. |
| Network timeout | Same — logged, cache untouched, retried next interval. |

The invariant across all of these: **a failed poll never clears or blanks the cache.** The SSE
stream keeps serving the last known prices, so a transient Massive outage or a rate-limit hiccup is
invisible to the frontend — it just sees prices stop moving for a few cycles rather than
disappearing.

### Why REST polling, not the Massive WebSocket

Massive offers a WebSocket product for true tick-by-tick pushes, but FinAlly doesn't use it: the
free tier's WebSocket access is more restricted than even the 5 req/min REST limit, and a persistent
outbound WebSocket from the backend adds reconnect/backoff complexity a simple polling loop avoids.
FinAlly's own client-facing feed is already SSE — one-way, polling-friendly — so the backend's
internal refresh cadence against Massive (15s free tier) is fully decoupled from the external
cadence it pushes to the browser (~500ms, replaying the last known value between real upstream
updates — see §8).

---

## 8. SSE Streaming Endpoint — `stream.py`

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
    """Factory pattern: injects the PriceCache without module-level globals."""

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """SSE endpoint for live price updates.

        Streams all tracked ticker prices every ~500ms. The client connects
        with EventSource and receives events shaped like:

            data: {"AAPL": {"ticker": "AAPL", "price": 190.50, ...}, ...}
        """
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
    yield "retry: 1000\n\n"  # tell the browser to auto-reconnect after 1s on drop

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
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}

```

Frontend consumption:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);   // { "AAPL": { ticker, price, ... }, ... }
  // update watchlist rows, trigger flash animation on changed tickers, append to sparkline buffers
};
```

### Why version-based change detection

`price_cache.version` is bumped on every `PriceCache.update()` call (§4). The SSE loop polls it
every 500ms; if it hasn't changed since the last iteration, nothing is sent — a single integer
comparison replaces diffing the whole price dict on every tick, and (in the Massive case) it means
the SSE loop naturally skips 29 out of 30 500ms ticks between real upstream polls, without any
Massive-specific logic in `stream.py` at all.

### Why poll-and-push instead of event-driven (pub/sub)

The endpoint polls the cache on a fixed interval rather than being notified by the producer. This
keeps updates evenly spaced regardless of upstream jitter, which matters because the frontend
accumulates SSE payloads into sparkline series — even spacing makes for a clean chart. It also means
the SSE layer needs zero knowledge of, or coupling to, whichever `MarketDataSource` happens to be
running.

---

## 9. FastAPI Lifecycle Integration

The market data system starts and stops with the app via FastAPI's `lifespan` context manager.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # from SQLite; seeded with the default 10
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # app is running

    # --- shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

### Consuming the cache from other routes

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(trade: TradeRequest, price_cache: PriceCache = Depends(get_price_cache)):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into the watchlist table ...
    await source.add_ticker(payload.ticker)
    # ...


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, source: MarketDataSource = Depends(get_market_source)):
    # ... delete from the watchlist table ...
    await source.remove_ticker(ticker)
    # ...
```

Both routes are written against `MarketDataSource`, never against `SimulatorDataSource` or
`MassiveDataSource` — swapping the active source (i.e. setting/unsetting `MASSIVE_API_KEY` and
restarting) requires no change here.

---

## 10. Watchlist Coordination

### Adding a ticker

```
User (or LLM) → POST /api/watchlist {ticker: "PYPL"}
  → insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: seeds a price, rebuilds the Cholesky matrix, writes into the cache immediately
      Massive:   appended to the polled ticker list, appears on the next scheduled poll
  → respond with the ticker (+ price if already cached)
```

### Removing a ticker

```
User (or LLM) → DELETE /api/watchlist/PYPL
  → delete from watchlist table (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: dropped from GBMSimulator, Cholesky rebuilt, removed from cache
      Massive:   dropped from the polled ticker list, removed from cache
  → respond with success
```

### Edge case: ticker removed from the watchlist but still held

If the user drops a ticker from the watchlist while still holding a position, the data source
must keep tracking it so portfolio valuation stays accurate — the watchlist route, not the market
data layer, is responsible for this check:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, source: MarketDataSource = Depends(get_market_source)):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)  # only stop tracking if nothing is held

    return {"status": "ok"}
```

---

## 11. Testing Strategy

The real suite lives in `backend/tests/market/` (6 modules, 73 tests, 84% coverage — run with
`uv run --extra dev pytest -v` from `backend/`). Shape of the coverage:

| Module | Focus |
|---|---|
| `test_models.py` | `PriceUpdate` properties (`change`, `change_percent`, `direction`, `to_dict()`) across up/down/flat/zero-previous-price cases. |
| `test_cache.py` | `update`/`get`/`get_all`/`get_price`/`remove`, first-update-is-flat, direction/change on subsequent updates, `version` increments exactly once per `update()`. |
| `test_simulator.py` | `GBMSimulator` unit tests: prices stay positive over many steps, initial price matches the seed, `add_ticker`/`remove_ticker` (including duplicate/nonexistent no-ops), unknown tickers get a random seed in range, empty-ticker-list `step()` returns `{}`, Cholesky is `None` for a single ticker and non-`None` once a second is added. |
| `test_simulator_source.py` | `SimulatorDataSource` integration: `start()` populates the cache before the first tick, prices actually move over several ticks, `stop()` is idempotent, `add_ticker`/`remove_ticker` propagate to both the simulator and the cache. |
| `test_factory.py` | `create_market_data_source` returns `MassiveDataSource` when `MASSIVE_API_KEY` is set (`monkeypatch.setenv`) and `SimulatorDataSource` otherwise (`monkeypatch.delenv`), including the empty-string/whitespace case. |
| `test_massive.py` | `MassiveDataSource` with `_fetch_snapshots` mocked: successful batch parsing updates the cache; a malformed snapshot (missing `last_trade`) is skipped without affecting other tickers in the same batch; an exception raised during polling leaves the cache untouched rather than clearing it. |

Representative examples:

```python
# test_cache.py
def test_direction_up():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00

def test_version_increments():
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

```python
# test_simulator.py
def test_prices_are_positive():
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_cholesky_rebuilds_on_add():
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("GOOGL")
    assert sim.get_tickers() == ["AAPL", "GOOGL"]
```

```python
# test_massive.py
async def test_api_error_does_not_crash():
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
        await source._poll_once()  # must not raise

    assert cache.get_price("AAPL") is None  # no update happened; cache stays untouched
```

**Why `SimulatorDataSource` also doubles as a fake for other subsystems' tests:** because both
implementations satisfy the same `MarketDataSource` ABC, anything downstream (portfolio valuation,
trade execution, chat trade auto-execution) that needs *a* data source in its own tests can just use
`SimulatorDataSource` with a short `update_interval` rather than building a bespoke fake.

---

## 12. Error Handling & Edge Cases

- **Empty watchlist at startup.** `start([])` is valid for both sources — the simulator's `step()`
  returns `{}`, the Massive poller's `_poll_once()` short-circuits (`if not self._tickers: return`).
  The SSE endpoint simply sends nothing until a ticker is added.
- **Trading a ticker with no cached price yet** (just added, Massive hasn't polled): the trade route
  should treat `price_cache.get_price(ticker) is None` as a 400, not attempt the trade at a
  fabricated price. In practice this only affects the Massive path — the simulator seeds the cache
  synchronously inside `add_ticker()`.
- **Invalid Massive API key.** The first poll (inside `start()`) fails with `401`; it's caught,
  logged, and the poller keeps retrying every `poll_interval` seconds. Nothing crashes; the SSE
  connection reports "connected" but streams no data for that ticker set until the key is fixed
  and the process restarted (env vars are read once, at `create_market_data_source()` call time).
- **Thread safety under load.** `PriceCache`'s `threading.Lock` guards a tiny critical section (dict
  read + write); at watchlist scale (≤ dozens of tickers, one writer, N SSE readers) contention is
  negligible. This isn't a bottleneck worth engineering around for this project.
- **Floating-point/precision.** Prices are `round()`-ed to 2 decimal places at the point of writing
  into `GBMSimulator._prices` and again in `PriceCache.update()`; the GBM exponential formulation is
  numerically stable and always positive, so there's no risk of drifting or negative prices even
  over a long-running demo session.

---

## 13. Configuration Reference

| Parameter | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | unset | Non-empty → `MassiveDataSource`; unset/empty → `SimulatorDataSource`. Read once, in `create_market_data_source()`. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Simulator tick cadence. |
| `event_probability` | `GBMSimulator.__init__` (via `SimulatorDataSource`) | `0.001` | Per-ticker, per-tick chance of a 2–5% shock move. |
| `dt` | `GBMSimulator.__init__` | `~8.48e-8` (`0.5 / TRADING_SECONDS_PER_YEAR`) | GBM time step, derived from the 500ms tick cadence over a 252-day × 6.5h trading year. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Sized for the free tier's 5 req/min; lower for paid tiers. |
| SSE push interval | `_generate_events(interval=...)` | `0.5` s | Cache poll cadence on the SSE side; independent of the upstream producer's own cadence. |
| SSE retry directive | `_generate_events` (`retry: 1000`) | `1000` ms | Browser `EventSource` auto-reconnect delay after a dropped connection. |

Everything in this table is a constructor default — all are overridable per-instance without
touching call sites elsewhere, since every consumer depends only on the `MarketDataSource` /
`PriceCache` interfaces.
