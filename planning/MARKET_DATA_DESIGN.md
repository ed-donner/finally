# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified
`MarketDataSource` interface, the in-memory `PriceCache`, the GBM simulator, the
Massive (Polygon.io) API client, the SSE streaming endpoint, and how the FastAPI
app wires them together.

**Status:** The subsystem described here is fully implemented and tested at
`backend/app/market/` (see [§12](#12-testing-strategy) — 73 tests passing). The
FastAPI application shell (`app/main.py`), database layer, and REST routes that
consume this subsystem have **not** been built yet — [§10](#10-fastapi-lifecycle-integration)
and [§11](#11-watchlist-coordination) are forward-looking guidance for whoever
builds that layer next, written to match the interface that already exists.

All code below is copied verbatim from the real source files (not aspirational
pseudocode), so it can be cross-referenced directly against the repo.

---

## Table of Contents

1. [Architecture](#1-architecture)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model)
4. [Price Cache — `cache.py`](#4-price-cache)
5. [Abstract Interface — `interface.py`](#5-abstract-interface)
6. [Seed Prices & Correlation — `seed_prices.py`](#6-seed-prices--correlation)
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator)
8. [Massive API Client — `massive_client.py`](#8-massive-api-client)
9. [Factory — `factory.py`](#9-factory)
10. [FastAPI Lifecycle Integration](#10-fastapi-lifecycle-integration)
11. [Watchlist Coordination](#11-watchlist-coordination)
12. [Testing Strategy](#12-testing-strategy)
13. [Error Handling & Edge Cases](#13-error-handling--edge-cases)
14. [Configuration Summary](#14-configuration-summary)

---

## 1. Architecture

```
                 ┌────────────────────────┐
                 │   MarketDataSource     │   (abstract interface)
                 │   start / stop /       │
                 │   add_ticker /         │
                 │   remove_ticker /      │
                 │   get_tickers          │
                 └───────────┬────────────┘
                             │ implements
            ┌────────────────┴─────────────────┐
            │                                   │
  ┌─────────▼──────────┐            ┌───────────▼───────────┐
  │ SimulatorDataSource │            │   MassiveDataSource    │
  │ (GBM, ~500ms tick)  │            │ (Polygon.io REST poll) │
  │ default, no API key │            │ used when              │
  │ needed              │            │ MASSIVE_API_KEY is set │
  └─────────┬──────────┘            └───────────┬───────────┘
            │                                   │
            └───────────────┬───────────────────┘
                             │ writes
                    ┌────────▼─────────┐
                    │    PriceCache     │  thread-safe, in-memory
                    │ {ticker: latest}  │  version counter for
                    └────────┬──────────┘  change detection
                             │ reads
        ┌────────────────────┼────────────────────┐
        │                    │                     │
┌───────▼────────┐  ┌────────▼─────────┐  ┌────────▼─────────┐
│ SSE stream      │  │ Portfolio        │  │ Trade execution   │
│ /api/stream/    │  │ valuation        │  │ (fills at cache   │
│ prices          │  │                  │  │  price)           │
└─────────────────┘  └──────────────────┘  └────────────────────┘
```

`create_market_data_source()` selects between the two implementations at
startup based on the `MASSIVE_API_KEY` environment variable. Everything
downstream — SSE streaming, portfolio math, trade fills — talks only to
`PriceCache` and never needs to know which source is active.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py         # Re-exports: PriceUpdate, PriceCache, MarketDataSource,
                           #   create_market_data_source, create_stream_router
      models.py            # PriceUpdate dataclass
      cache.py              # PriceCache (thread-safe in-memory store)
      interface.py           # MarketDataSource ABC
      seed_prices.py          # SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS, CORRELATION_GROUPS
      simulator.py             # GBMSimulator + SimulatorDataSource
      massive_client.py         # MassiveDataSource
      factory.py                 # create_market_data_source()
      stream.py                   # SSE endpoint (FastAPI router factory)
  market_data_demo.py        # Rich terminal demo (`uv run market_data_demo.py`)
  tests/
    market/
      test_models.py
      test_cache.py
      test_simulator.py
      test_simulator_source.py
      test_factory.py
      test_massive.py
```

Downstream code imports only from the package root:

```python
from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source
```

---

## 3. Data Model

**File: `backend/app/market/models.py`**

`PriceUpdate` is the only data structure that leaves the market data layer —
SSE streaming, portfolio valuation, and trade execution all consume this type
exclusively.

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

- **`frozen=True`**: price updates are immutable value objects, safe to share across async tasks without copying.
- **`slots=True`**: memory optimization — many of these are created per second.
- **Computed properties** (`change`, `direction`, `change_percent`): derived from `price`/`previous_price` so they can never drift out of sync with a stale stored field.
- **`to_dict()`**: single serialization point shared by the SSE endpoint and any future REST responses.

---

## 4. Price Cache

**File: `backend/app/market/cache.py`**

The central data hub. Data sources write to it; SSE streaming, portfolio
valuation, and trade execution read from it.

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

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
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
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

### Why a version counter?

The SSE loop polls the cache every ~500ms. Without a version counter it would
re-serialize and resend every price on every tick even when nothing changed
(e.g., the Massive source only updates every 15s). The counter lets the SSE
loop skip a send when nothing is new:

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield format_sse(price_cache.get_all())
    await asyncio.sleep(0.5)
```

### Thread safety rationale

`threading.Lock` is used instead of `asyncio.Lock` because the Massive
client's synchronous call runs via `asyncio.to_thread()` — a real OS thread
that `asyncio.Lock` would not protect against. `threading.Lock` works
correctly from both a sync thread and the async event loop, which is the
combination this cache actually has to survive.

---

## 5. Abstract Interface

**File: `backend/app/market/interface.py`**

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

### Why the source writes to the cache instead of returning prices

This push model decouples timing. The simulator ticks every 500ms, Massive
polls every 15s, but SSE always reads from the cache at its own 500ms
cadence — it never needs to know which data source is active or how often
it updates.

---

## 6. Seed Prices & Correlation

**File: `backend/app/market/seed_prices.py`**

Constants only — no logic, no imports beyond stdlib. Shared by the simulator
(initial prices, GBM parameters, correlation structure) and reusable as
fallback prices for a newly-added ticker before any real data has arrived.

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

A dynamically-added ticker not in `SEED_PRICES`/`TICKER_PARAMS` falls back to
a random seed price in `$50–$300` and `DEFAULT_PARAMS` (see
[§7.4](#74-dynamically-added-tickers)).

---

## 7. GBM Simulator

**File: `backend/app/market/simulator.py`**

Two classes live here:
- `GBMSimulator` — pure math engine, stateful, holds current prices and steps them forward.
- `SimulatorDataSource` — the `MarketDataSource` implementation that wraps it in an async loop and writes to `PriceCache`.

### 7.1 The Math

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where `S(t)` is the current price, `mu` is annualized drift, `sigma` is
annualized volatility, `dt` is the time step as a fraction of a trading year,
and `Z` is a correlated standard-normal draw. `dt` is tiny (~8.48e-8 for a
500ms tick over a 252-day, 6.5-hour trading year), so each tick produces a
sub-cent move that accumulates naturally over many ticks — this is what
makes the price series look organic rather than jumpy.

### 7.2 `GBMSimulator`

```python
class GBMSimulator:
    """Geometric Brownian Motion simulator for correlated stock prices."""

    # 500ms expressed as a fraction of a trading year
    # 252 trading days * 6.5 hours/day * 3600 seconds/hour = 5,896,800 seconds
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

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

        This is the hot path — called every 500ms. Keep it fast.
        """
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

            # Random event: ~0.1% chance per tick per ticker (~every 50s
            # across 10 tickers at 2 ticks/sec) — a 2-5% shock for visual drama
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
        """O(n^2), called on add/remove. Fine for n < 50 tickers."""
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
        """Same tech sector: 0.6. Same finance sector: 0.5. TSLA or
        cross-sector or unknown: 0.3."""
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

Correlated draws are produced by multiplying a vector of independent
standard-normal samples by the Cholesky factor of the correlation matrix
(`L @ z` where `corr = L @ L.T`) — the standard technique for generating
jointly-correlated normals from independent ones.

### 7.3 `SimulatorDataSource` — Async Wrapper

```python
class SimulatorDataSource(MarketDataSource):
    """Runs a background asyncio task that calls GBMSimulator.step() every
    `update_interval` seconds and writes results to the PriceCache."""

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
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Key behaviors:
- **Immediate seeding** — `start()` and `add_ticker()` both write a price to the cache synchronously, before the loop's first tick, so there's never a blank-screen gap.
- **Graceful cancellation** — `stop()` cancels the task and awaits it, swallowing `CancelledError`, so FastAPI lifespan teardown is clean.
- **Exception resilience** — the loop catches exceptions per-step so one bad tick can't kill the whole feed.

### 7.4 Dynamically Added Tickers

A ticker not present in `SEED_PRICES`/`TICKER_PARAMS` (e.g. the user or the
AI assistant adds `"PYPL"` to the watchlist) gets a random seed price in
`$50–$300` and `DEFAULT_PARAMS` (`sigma=0.25, mu=0.05`), and is treated as
uncorrelated with everything else (`CROSS_GROUP_CORR = 0.3`, the same
fallback used for cross-sector pairs).

---

## 8. Massive API Client

**File: `backend/app/market/massive_client.py`**

Polls the Massive (Polygon.io) REST snapshot endpoint on a configurable
interval. The underlying `massive` client is synchronous, so calls run via
`asyncio.to_thread()` to avoid blocking the event loop.

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all
    watched tickers in a single API call, then writes results to the
    PriceCache.

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

        # Immediate first poll so the cache has data right away
        await self._poll_once()

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
            self._tickers.append(ticker)  # appears on the next poll

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
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    # Massive timestamps are Unix milliseconds → seconds
                    timestamp = snap.last_trade.timestamp / 1000.0
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Don't re-raise — retried on the next interval.
            # Common failures: 401 (bad key), 429 (rate limit), network errors.

    def _fetch_snapshots(self) -> list:
        """Synchronous call to the Massive REST API. Runs in a thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Error handling philosophy

| Error | Behavior |
|-------|----------|
| **401 Unauthorized** | Logged as error; poller keeps running (user might fix `.env` and restart). |
| **429 Rate limited** | Logged as error; next poll retries after `poll_interval` seconds. |
| **Network timeout** | Logged as error; retries automatically on the next cycle. |
| **Malformed snapshot** | That ticker is skipped with a warning; other tickers in the same response still get processed. |
| **All tickers fail** | Cache retains last-known prices; SSE keeps streaming stale-but-present data — better than nothing. |

`massive` is a declared core dependency of the backend (`pyproject.toml`), so
the import at the top of the module is unconditional — there is no lazy
import to avoid a hard dependency, since every install already includes it.

---

## 9. Factory

**File: `backend/app/market/factory.py`**

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

Usage at app startup:

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)  # e.g., ["AAPL", "GOOGL", ...]
```

---

## 9.5 SSE Streaming Endpoint

**File: `backend/app/market/stream.py`**

A FastAPI route that holds open a long-lived `text/event-stream` connection
and pushes price updates to the client.

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
                "X-Accel-Buffering": "no",  # Disable nginx buffering if proxied
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    # Tell the browser to retry after 1 second if the connection drops
    yield "retry: 1000\n\n"

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
        pass
```

### Wire format

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{...}}
```

Frontend consumption:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
    const prices = JSON.parse(event.data);
    // prices is { "AAPL": { ticker, price, previous_price, ... }, ... }
};
```

### Why poll-and-push instead of event-driven?

The endpoint polls the cache on a fixed interval rather than being notified
by the data source. This produces predictable, evenly-spaced updates, which
matters because the frontend accumulates these events into sparkline charts
— even spacing keeps that visualization clean regardless of which data
source (500ms simulator vs. 15s Massive poll) is active underneath.

---

## 10. FastAPI Lifecycle Integration

> **Not yet built.** `app/main.py` does not exist in the repo yet — the
> FastAPI app shell, database, and REST routes are the next phase of work.
> This section shows how that layer should wire up the market data
> subsystem, matching the public interface that already exists and is
> already tested.

The market data system should start and stop with the FastAPI app via the
`lifespan` context manager:

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_watchlist_tickers()  # reads from SQLite
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))

    yield  # App is running

    # --- SHUTDOWN ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes (trade execution, watchlist management) should access the cache
and source via dependency injection:

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(404, f"No price available for {trade.ticker}")
    # ... execute trade at current_price ...


@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... insert into watchlist table ...
    await source.add_ticker(payload.ticker)


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... delete from watchlist table ...
    await source.remove_ticker(ticker)
```

---

## 11. Watchlist Coordination

> Also forward-looking — describes how the not-yet-built watchlist routes
> should interact with the market data source.

### Adding a ticker

```
POST /api/watchlist {ticker: "PYPL"}
  → Insert into watchlist table (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache immediately
      Massive:   appends to ticker list, appears on next poll (up to poll_interval delay)
  → Return success (ticker + current price if available)
```

### Removing a ticker

```
DELETE /api/watchlist/PYPL
  → Delete from watchlist table (SQLite)
  → await source.remove_ticker("PYPL")
      Simulator: removes from GBMSimulator, rebuilds Cholesky, removes from cache
      Massive:   removes from ticker list, removes from cache
  → Return success
```

### Edge case: ticker has an open position

If the user removes a ticker from the watchlist while still holding shares,
the data source must keep tracking it so portfolio valuation stays accurate.
The watchlist route should check for an open position before calling
`remove_ticker()`:

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

---

## 12. Testing Strategy

**73 tests, all passing** (`uv run --extra dev pytest -v` from `backend/`),
across 6 modules in `backend/tests/market/`:

| Module | Focus |
|--------|-------|
| `test_models.py` | `PriceUpdate` computed properties (`change`, `change_percent`, `direction`), `to_dict()` serialization |
| `test_cache.py` | update/get/get_all/remove, version counter increments, first-update-is-flat behavior |
| `test_simulator.py` | GBM math: prices stay positive, drift over many steps, add/remove ticker rebuilds Cholesky, unknown tickers get random seed prices, empty simulator returns `{}` |
| `test_simulator_source.py` | `SimulatorDataSource` integration: cache seeded before first tick, prices evolve over time, clean double-`stop()`, add/remove ticker propagates to cache |
| `test_factory.py` | Env-var driven selection between `MassiveDataSource` and `SimulatorDataSource` |
| `test_massive.py` | Mocked `RESTClient` — successful poll updates cache, malformed snapshot skipped without killing the batch, API exception doesn't crash the poll loop |

### Representative patterns

**Simulator math invariants:**

```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0


def test_cholesky_rebuilds_on_add(self):
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim._cholesky is None  # 1 ticker, no correlation matrix needed
    sim.add_ticker("GOOGL")
    assert sim._cholesky is not None
```

**Cache change detection:**

```python
def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

**Massive client resilience (mocked, no network calls):**

```python
async def test_malformed_snapshot_skipped(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "BAD"]

    good_snap = _make_snapshot("AAPL", 190.50, 1707580800000)
    bad_snap = MagicMock(ticker="BAD", last_trade=None)  # triggers AttributeError

    with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50   # good ticker still processed
    assert cache.get_price("BAD") is None       # bad one skipped, no crash


async def test_api_error_does_not_crash(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]

    with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
        await source._poll_once()  # must not raise

    assert cache.get_price("AAPL") is None
```

### Coverage

Overall: 84%. `models.py`, `cache.py`, and `factory.py` at 100%; `simulator.py`
at 98%; `massive_client.py` at 56% (expected — the actual HTTP call is mocked
out, so only the request-construction and response-parsing paths are
exercised, not the `massive` package's own internals).

### Manual verification: terminal demo

```bash
cd backend
uv run market_data_demo.py
```

Runs the simulator standalone for 60 seconds (or until Ctrl+C), rendering a
live Rich dashboard with all 10 default tickers, per-ticker sparklines,
color-coded up/down arrows, and an event log for random shock events — a
fast way to eyeball that the GBM math and correlation structure look right
without booting the full FastAPI app.

---

## 13. Error Handling & Edge Cases

### 13.1 Startup with an empty watchlist

If the database has no watchlist rows (e.g. user deleted everything),
`start([])` is handled gracefully by both sources — the simulator produces no
prices, the Massive poller skips its API call entirely. The SSE endpoint
simply sends no events until a ticker is added, at which point tracking
starts immediately.

### 13.2 Price cache miss during a trade

If a user tries to trade a ticker with no cached price yet (just added,
Massive hasn't polled), the trade route should fail fast with a clear error:

```python
price = price_cache.get_price(ticker)
if price is None:
    raise HTTPException(
        status_code=400,
        detail=f"Price not yet available for {ticker}. Please wait a moment and try again.",
    )
```

The simulator avoids this case entirely by seeding the cache synchronously
in both `start()` and `add_ticker()`. The Massive poller has an unavoidable
gap between "ticker added" and "first successful poll" — the 400 with a
clear message is the correct behavior for that window.

### 13.3 Invalid Massive API key

If `MASSIVE_API_KEY` is set but wrong, the first poll fails with 401. The
poller logs the error and keeps retrying every `poll_interval` — it does not
crash or fall back to the simulator. The SSE endpoint keeps streaming empty
data; the user sees a "connected" status (SSE itself works fine) but no
prices. Fix: correct the key and restart the container.

### 13.4 Thread safety under load

`PriceCache`'s `threading.Lock` protects a tiny critical section (dict
lookup + assignment). At the project's actual scale — 10 tickers, ~2
updates/sec, one SSE reader per browser tab — contention is negligible. If
this ever became a bottleneck (hundreds of tickers, many concurrent
readers), a read-write lock would be the fix, but that's not warranted here.

### 13.5 Numerical stability of the simulator

GBM with a tiny `dt` produces very small per-tick moves, but this isn't a
precision concern: prices are rounded to 2 decimals in `GBMSimulator.step()`,
the `exp()` formulation is numerically stable, and prices are structurally
guaranteed positive (an exponential is never negative or zero).

---

## 14. Configuration Summary

| Parameter | Location | Default | Description |
|-----------|----------|---------|-------------|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use the simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5` s | Time between simulator ticks |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0` s | Time between Massive API polls (5 req/min free tier) |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random 2–5% shock per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step, fraction of a trading year |
| SSE push interval | `stream._generate_events()` | `0.5` s | Time between cache checks / pushes to the client |
| SSE retry directive | `stream._generate_events()` | `1000` ms | Browser `EventSource` reconnection delay |

### Public package API

**File: `backend/app/market/__init__.py`**

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
