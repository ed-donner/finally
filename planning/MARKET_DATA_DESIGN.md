# Market Data Backend — Detailed Design

Implementation-ready design for the FinAlly market data subsystem: the unified `MarketDataSource` interface, the in-memory `PriceCache`, the GBM simulator, the Massive API client, SSE streaming, and the still-outstanding history buffer / `GET /api/market/history/{ticker}` endpoint.

**Sources consulted**: `planning/PLAN.md` §6–§8, the current per-component docs (`planning/MARKET_INTERFACE.md`, `planning/MARKET_SIMULATOR.md`, `planning/MASSIVE_API.md`), the doc-review passes (`planning/review-claude.md`, `planning/review-deepseek.md`, `planning/REVIEW.md`), the archived first-pass design and code review (`planning/archive/MARKET_DATA_DESIGN.md`, `planning/archive/MARKET_DATA_REVIEW.md`), and the actual code under `backend/app/market/` and `backend/tests/market/` as of 2026-08-25.

## Status legend

Every section below is tagged so this document doesn't repeat the spec/implementation mismatch the review round flagged in the archived design:

- ✅ **Implemented** — matches `backend/app/market/*` today; snippets are verbatim from source.
- 🚧 **Proposed** — designed here, not yet in the codebase. Needed to close a gap between `planning/PLAN.md` and the shipped code.

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy) ✅
4. [Price Cache — `cache.py`](#4-price-cache--cachepy) ✅ + 🚧 history buffer
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy) ✅
6. [Seed Prices & Ticker Parameters — `seed_prices.py`](#6-seed-prices--ticker-parameters) ✅
7. [GBM Simulator — `simulator.py`](#7-gbm-simulator--simulatorpy) ✅
8. [Massive API Client — `massive_client.py`](#8-massive-api-client--massive_clientpy) ✅
9. [Factory — `factory.py`](#9-factory--factorypy) ✅
10. [SSE Streaming Endpoint — `stream.py`](#10-sse-streaming-endpoint--streampy) ✅
11. [History Endpoint — `GET /api/market/history/{ticker}`](#11-history-endpoint) 🚧
12. [FastAPI Lifecycle Integration](#12-fastapi-lifecycle-integration) 🚧
13. [Watchlist Coordination](#13-watchlist-coordination) 🚧
14. [Daily-Change-% Semantics](#14-daily-change--semantics)
15. [Testing Strategy](#15-testing-strategy)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Configuration Summary](#17-configuration-summary)

---

## 1. Architecture Overview

```
                 ┌──────────────────────────┐
watchlist ∪      │   MarketDataSource (ABC)  │
positions.ticker │  ┌──────────────────────┐ │
   ──────────────┼─▶│  SimulatorDataSource  │ │   (GBM math engine)
                 │  └──────────────────────┘ │
                 │  ┌──────────────────────┐ │
                 │  │   MassiveDataSource   │ │   (REST poller)
                 │  └──────────────────────┘ │
                 └────────────┬─────────────┘
                              │ writes
                              ▼
                     ┌──────────────────┐
                     │    PriceCache     │   (thread-safe, shared)
                     │  latest + history  │
                     └────────┬──────────┘
                    reads     │     reads
              ┌───────────────┴───────────────┐
              ▼                                ▼
   GET /api/stream/prices (SSE)   GET /api/market/history/{ticker}
              │                                │
              ▼                                ▼
        Frontend: live ticks          Frontend: chart/sparkline seed
```

One interface, two implementations, selected once at startup by `create_market_data_source()` based on `MASSIVE_API_KEY`. Everything downstream — SSE, the history endpoint, portfolio valuation, trade execution — talks only to `PriceCache`. Neither data source is ever called directly outside `backend/app/market/`.

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py            # Public API re-exports                         ✅
      models.py               # PriceUpdate dataclass                        ✅
      cache.py                 # PriceCache (latest price + history buffer)  ✅ (history 🚧)
      interface.py              # MarketDataSource ABC                       ✅
      seed_prices.py              # SEED_PRICES, TICKER_PARAMS, correlations ✅
      simulator.py                  # GBMSimulator + SimulatorDataSource     ✅
      massive_client.py               # MassiveDataSource                    ✅
      factory.py                        # create_market_data_source()        ✅
      stream.py                          # SSE endpoint factory              ✅
      history.py                          # GET /api/market/history router   🚧 new file
    main.py                                # FastAPI app, lifespan wiring    🚧 doesn't exist yet
  tests/
    market/
      test_models.py     test_cache.py       test_factory.py                 ✅
      test_simulator.py  test_simulator_source.py  test_massive.py           ✅
      test_history.py                                                        🚧 new file
```

`backend/app/main.py` does not exist yet — nothing under `backend/app/market/` is wired into a running FastAPI app today. §12 below is the missing glue.

---

## 3. Data Model — `models.py`

✅ Implemented exactly as follows. `PriceUpdate` is the only type that leaves the market data layer — SSE payloads, history responses, and (later) portfolio valuation all consume it via `to_dict()`.

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

- `frozen=True, slots=True`: value objects created many times per second, safe to hand across async tasks / threads without copying, and cheap to allocate.
- `change` / `change_percent` / `direction` are **tick-over-tick** (this update vs. the immediately prior one), not session-relative — see §14 for why that matters and what to do about it.

---

## 4. Price Cache — `cache.py`

### 4.1 Current implementation ✅

```python
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker."""

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0  # bumped on every update

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

**Why `threading.Lock`, not `asyncio.Lock`**: `MassiveDataSource` runs the synchronous Massive SDK inside `asyncio.to_thread`, so writes can originate from a real OS thread, not just the event loop. `asyncio.Lock` only synchronizes coroutines on one loop and would not protect against that. `threading.Lock` protects both.

**Why a version counter**: the SSE loop polls the cache every ~500ms regardless of the underlying source's cadence (2 ticks/sec for the simulator, one poll per 15s for Massive). Without `version`, it would re-serialize and re-send the full price map on every tick even when Massive hasn't produced anything new. `version` lets it skip the no-op ticks cheaply — an `int` compare instead of a dict diff.

### 4.2 Proposed extension: rolling history buffer 🚧

`planning/PLAN.md` §6 specifies that `PriceCache` also holds "a bounded rolling history buffer (most recent 500 points)" per ticker, feeding both the SSE stream and `GET /api/market/history/{ticker}`. **This does not exist in the code today** — `PriceCache` stores only the single latest `PriceUpdate` per ticker. This is the one concrete gap blocking sparkline/chart pre-population (`planning/PLAN.md` §2, §10).

The fix is additive, not a rewrite — a bounded deque alongside the existing single-value store:

```python
from __future__ import annotations

import time
from collections import deque
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price *and* a bounded
    rolling history per ticker.
    """

    def __init__(self, history_size: int = 500) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[PriceUpdate]] = {}
        self._history_size = history_size
        self._lock = Lock()
        self._version: int = 0

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
            self._history.setdefault(ticker, deque(maxlen=self._history_size)).append(update)
            self._version += 1
            return update

    def get_history(self, ticker: str) -> list[PriceUpdate]:
        """Oldest-first list of up to `history_size` recent updates for a ticker."""
        with self._lock:
            return list(self._history.get(ticker, ()))

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)
            self._history.pop(ticker, None)

    # get, get_all, get_price, version, __len__, __contains__ unchanged from §4.1
```

Design notes:

- **`deque(maxlen=...)`** gives O(1) amortized bounded appends for free — no manual `list[-500:]` slicing on every tick, and memory per ticker is capped regardless of how long the process runs.
- **Coverage math**: at the simulator's 500ms cadence, 500 points ≈ 4 minutes of history — plenty for a sparkline, and the main chart is expected to extend live from SSE after this seed (`planning/PLAN.md` §6, §10), not to hold a full trading day. At Massive's 15s poll interval, 500 points ≈ 2 hours.
- **`remove()`** must drop both `_prices[ticker]` and `_history[ticker]` — a ticker fully un-tracked (removed from both watchlist and positions, per §13) should not leak its buffer.
- **`history_size` is a constructor parameter, not a hardcoded `500`**, so tests can use a small buffer (e.g. `PriceCache(history_size=3)`) to assert eviction behavior without pushing 500 updates.
- No new locking primitive — reuses the existing `threading.Lock`, so the thread-safety story from §4.1 is unchanged.

---

## 5. Abstract Interface — `interface.py`

✅ Implemented exactly as follows. No `get_price()` method by design — this is a **push** interface (source → cache on its own schedule); callers read prices from `PriceCache`, never block on a network round trip or a simulation step through this interface.

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

Both implementations own an internal `asyncio.Task` started in `start()` and cancelled in `stop()`, so this ABC needs no `run()`/`loop()` method of its own — the task's existence is an implementation detail of each subclass.

---

## 6. Seed Prices & Ticker Parameters

**File: `seed_prices.py`.** ✅ Constants only, no logic, no imports beyond the type hints — tuning a starting price or a sector's volatility never touches `simulator.py`.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # High volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # High volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # Low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # Low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR = 0.3     # Between sectors / unknown tickers
TSLA_CORR = 0.3            # TSLA does its own thing
```

Tickers not in `SEED_PRICES` (dynamically added via chat or the watchlist route) start at a uniform random price in `[$50, $300]` and use `DEFAULT_PARAMS` — there's no real-world volatility figure to look up for an arbitrary user-entered symbol. Note there is deliberately **no separate `DEFAULT_CORR` constant** — the fallback for any pair not covered by the tech/finance/TSLA special cases is `CROSS_GROUP_CORR`, and the two names are not duplicated (an earlier draft had both `DEFAULT_CORR` and `CROSS_GROUP_CORR` at the same value, which a code review flagged as confusing; the current code has one name for one concept).

---

## 7. GBM Simulator — `simulator.py`

✅ Implemented. Two classes in one module: `GBMSimulator` (pure math, synchronous, no asyncio/cache knowledge) and `SimulatorDataSource` (the async `MarketDataSource` adapter).

### 7.1 Math

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)` current price, `mu` annualized drift, `sigma` annualized volatility, `dt` this tick as a fraction of a trading year, `Z` a correlated standard-normal draw.
- Multiplicative (`exp()`-based) update ⇒ prices can never go negative, unlike an additive random walk.
- `dt = 0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8` for 500ms ticks over a 252-day, 6.5h/day trading year — tiny enough that each tick moves sub-cent amounts which accumulate into realistic intraday ranges over minutes of wall-clock time.

### 7.2 Correlated moves

Real stocks don't move independently. The simulator builds a Cholesky decomposition `L` of a correlation matrix `C` (`L = cholesky(C)`) and applies it to independent normal draws: `Z_correlated = L @ Z_independent`. Exact as long as `C` is positive semi-definite, which any matrix built from pairwise correlations in `[0, 1)` with a unit diagonal always is — so `np.linalg.cholesky` never needs a defensive `try/except` for a non-PSD failure here.

Pairwise correlation lookup checks TSLA first (`0.3` with everything, even though it's nominally in the `tech` set), then tech-tech (`0.6`), then finance-finance (`0.5`), else `0.3`.

### 7.3 Random events

Each tick, each ticker independently has a small chance of a sudden 2–5% shock:

```python
if random.random() < event_probability:  # default 0.001
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= 1 + shock
```

At 2 ticks/sec, `0.001` per ticker per tick ⇒ an event on a *given* ticker roughly every 500s (~8 min); across a 10-ticker default watchlist, expect *some* ticker to have an event roughly every 50s — enough drama for a live dashboard without destabilizing the price path.

### 7.4 `GBMSimulator`

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT, event_probability: float = 0.001) -> None:
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
        """Advance every tracked ticker by one time step. Hot path — every 500ms."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z_independent = np.random.standard_normal(n)
        z = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

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
        """O(n^2) rebuild whenever tickers are added/removed. n stays small (<50)."""
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
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

`step()` is allocation-light on purpose (one vectorized `numpy` draw per tick for *all* tickers, not per-ticker `random.gauss()` calls) since it runs every 500ms for the life of the process. The Cholesky matrix is cached and rebuilt only on `add_ticker`/`remove_ticker`, never inside `step()`. `round(self._prices[ticker], 2)` only rounds the **emitted** value — the internal `self._prices[ticker]` stays full precision so tiny per-tick drifts aren't lost to repeated rounding over a long-running session.

### 7.5 `SimulatorDataSource` — async adapter

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
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

Two resilience choices worth preserving in any future edit: **seed the cache synchronously inside `start()`**, before the loop task even exists, so an SSE client connecting immediately after boot sees prices on its first read instead of an empty payload; and **wrap each step in `try/except Exception`** so one bad draw logs and continues rather than silently killing the price feed for the rest of the process's life.

---

## 8. Massive API Client — `massive_client.py`

✅ Implemented, and already reflects the two corrections flagged during research against the live `massive` package (`planning/MASSIVE_API.md` "Discrepancies & Gotchas" — both were live bugs against an *earlier* draft of this file, already fixed in current code):

- `get_snapshot_all("stocks", tickers)` — positional string + list, not `SnapshotMarketType.STOCKS`.
- `snap.last_trade.sip_timestamp / 1_000_000_000` — Massive's `LastTrade` has no `.timestamp` field; `sip_timestamp` is Unix **nanoseconds**, not milliseconds.

```python
from __future__ import annotations

import asyncio
import logging

from massive import RESTClient

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST API.

    Polls GET /v2/snapshot/locale/us/markets/stocks/tickers for all watched
    tickers in a single API call, then writes results to the PriceCache.

    Rate limits:
      - Free (Basic) tier: does not include this endpoint at all (403) — a
        Starter plan or above is required for get_snapshot_all.
      - Starter and above: effectively unlimited (soft guidance: stay under
        100 req/s), so the poll interval is a design choice, not a rate-limit
        constraint; default here is a conservative 15s.
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
        await self._poll_once()  # immediate first poll so the cache isn't empty on connect
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
            self._tickers.append(ticker)

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
            # RESTClient is synchronous — run in a thread to avoid blocking the event loop.
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
            processed = 0
            for snap in snapshots:
                try:
                    price = snap.last_trade.price
                    timestamp = snap.last_trade.sip_timestamp / 1_000_000_000  # ns -> s
                    self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
                    processed += 1
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
            logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))
        except Exception as e:
            logger.error("Massive poll failed: %s", e)
            # Never re-raise — the loop retries on the next interval.
            # Common failures: 401 (bad key), 403 (plan doesn't include this endpoint), 429, network errors.

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all("stocks", self._tickers)
```

Two resilience choices worth keeping in any future rewrite: an **immediate poll inside `start()`** (so the SSE stream has data on the very first client connection, not just after the first `poll_interval` elapses), and a **per-snapshot `try/except` inside the batch loop** (one malformed ticker in a 20-ticker response doesn't drop the other 19).

| Error | Behavior |
|---|---|
| 401 Unauthorized | Logged; poller keeps running (user might fix `.env` and restart) |
| 403 Plan doesn't include endpoint | Same — this is the practical failure mode for a free/Basic key on `get_snapshot_all` |
| 429 Rate limited | Logged; next poll retries after `poll_interval` |
| Network timeout | Logged; retries next cycle |
| Malformed single snapshot | That ticker skipped with a warning; others still processed |
| All tickers fail | Cache retains last-known prices — SSE keeps streaming stale data rather than nothing |

**Open item carried from `planning/MASSIVE_API.md`**: a free (Basic) `MASSIVE_API_KEY` will most likely 403 on `get_snapshot_all` rather than just being rate-limited, since that endpoint family isn't included in the Basic plan. Not a code bug — worth documenting for users ("Massive real-data mode requires Starter tier or above") rather than fixing in code, since `get_previous_close_agg` (which *is* free-tier) can't produce intraday movement.

---

## 9. Factory — `factory.py`

✅ Implemented.

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
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource, else -> SimulatorDataSource.

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

`.strip()` matters: an `.env` with `MASSIVE_API_KEY=` present but empty must fall through to the simulator, not attempt a Massive connection with an empty key.

---

## 10. SSE Streaming Endpoint — `stream.py`

✅ Implemented. `create_stream_router(price_cache)` is a **factory** returning a FastAPI `APIRouter` — the cache is injected, not reached through a module global.

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
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
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
    price_cache: PriceCache, request: Request, interval: float = 0.5,
) -> AsyncGenerator[str, None]:
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
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

### Wire format (as implemented — note this diverges from a literal reading of `planning/PLAN.md` §8's "each SSE event contains ticker, price, previous price, timestamp, direction")

Each `data:` line is **one batched JSON object keyed by ticker**, covering every currently tracked ticker whose cache entry has changed since the last send — not one event per ticker:

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up"},"GOOGL":{"ticker":"GOOGL","price":175.12,...}}

```

- Gated by `PriceCache.version` — a whole batch is skipped if nothing changed since the last poll (important for the Massive source's 15s cadence vs. the 500ms poll loop).
- No `event:` name is set (defaults to the generic `message` event) and there is no periodic heartbeat/comment line — only the one-time `retry: 1000\n\n` directive at connection start, which sets the browser's `EventSource` reconnect delay to 1s.
- Client:

```javascript
const eventSource = new EventSource('/api/stream/prices');
eventSource.onmessage = (event) => {
  const prices = JSON.parse(event.data);   // { "AAPL": {...}, "GOOGL": {...}, ... }
};
```

The SSE loop **polls** the cache on a fixed interval rather than being notified by the data source — deliberately simpler, and it produces predictable, evenly-spaced updates, which matters for building clean sparklines on the frontend.

---

## 11. History Endpoint 🚧

**File: `backend/app/market/history.py` (new).** Depends on the `PriceCache.get_history()` extension in §4.2. Thin by design — the buffer already holds oldest-first `PriceUpdate`s, so the route is a direct read-through:

```python
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .cache import PriceCache

router = APIRouter(prefix="/api/market", tags=["market"])


def create_history_router(price_cache: PriceCache) -> APIRouter:
    @router.get("/history/{ticker}")
    async def get_history(ticker: str) -> list[dict]:
        """Recent price history for a ticker, oldest first.

        Used to pre-populate sparklines and the main chart on page load;
        the frontend appends subsequent points from the SSE stream.
        404 if the ticker isn't currently tracked (never in the cache, or
        removed from both the watchlist and all positions).
        """
        ticker = ticker.upper().strip()
        if ticker not in price_cache:
            raise HTTPException(status_code=404, detail=f"No price data for {ticker}")
        return [u.to_dict() for u in price_cache.get_history(ticker)]

    return router
```

Notes:

- **404 vs. empty list**: a ticker that's tracked but has zero history yet (added this instant, before the first `update()` call lands) is a narrow race — both data sources seed the cache synchronously in `start()`/`add_ticker()` before returning, so in practice `ticker in price_cache` and `get_history(ticker)` non-empty happen together. If that race is ever observed, prefer returning `200` with an empty list over `404`, since the ticker *is* validly tracked — reserve `404` for "never heard of this ticker."
- **Uppercasing the path param** mirrors the normalization already applied in `MassiveDataSource.add_ticker/remove_ticker` (`ticker.upper().strip()`), so `GET /api/market/history/aapl` and `GET /api/market/history/AAPL` hit the same cache entry.
- Registered in `main.py` the same way as `create_stream_router` — see §12.

---

## 12. FastAPI Lifecycle Integration 🚧

`backend/app/main.py` does not exist yet. This is the wiring that starts/stops the market data subsystem with the app's lifespan and exposes `PriceCache` / `MarketDataSource` to the rest of the backend (trade execution, watchlist routes) via dependency injection.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.factory import create_market_data_source
from app.market.history import create_history_router
from app.market.interface import MarketDataSource
from app.market.stream import create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    source = create_market_data_source(price_cache)
    app.state.market_source = source

    initial_tickers = await load_tracked_tickers()  # watchlist ∪ positions.ticker, from SQLite — see §13
    await source.start(initial_tickers)

    app.include_router(create_stream_router(price_cache))
    app.include_router(create_history_router(price_cache))

    yield  # app is running

    # --- shutdown ---
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


def get_price_cache() -> PriceCache:
    return app.state.price_cache


def get_market_source() -> MarketDataSource:
    return app.state.market_source
```

Other routes reach the cache/source through FastAPI's `Depends`, never through a module-level global:

```python
from fastapi import APIRouter, Depends, HTTPException

router = APIRouter(prefix="/api")

@router.post("/portfolio/trade")
async def execute_trade(trade: TradeRequest, price_cache: PriceCache = Depends(get_price_cache)):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(400, f"Price not yet available for {trade.ticker}")
    # ... execute trade at current_price ...
```

**Startup ordering matters**: `source.start(initial_tickers)` seeds the cache synchronously before either router is registered, so the very first SSE connection and the very first history request both see data immediately — there's no window where the routes exist but the cache is empty.

---

## 13. Watchlist Coordination 🚧

Per `planning/PLAN.md` §6, the set passed to `start()` / `add_ticker()` / `remove_ticker()` must always be **`watchlist ∪ positions.ticker`**, not just the watchlist — this keeps a live price available for the positions table and portfolio valuation even after a held ticker is removed from the watchlist. Neither `MarketDataSource` implementation enforces this itself (by design — see §5); it's the responsibility of whichever route mutates the watchlist or a position.

### Adding a ticker

```
POST /api/watchlist {ticker: "PYPL"}
  → validate: watchlist not already at the 20-ticker cap (else 400)
  → INSERT INTO watchlist (SQLite)
  → await source.add_ticker("PYPL")
      Simulator: adds to GBMSimulator, rebuilds Cholesky, seeds cache (+ history, once §4.2 lands)
      Massive:   appends to poll list, appears on the next cycle
  → 200 { ticker, price: price_cache.get_price("PYPL") }  # may be null if Massive hasn't polled yet
```

### Removing a ticker

```python
@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_entry(ticker)

    # Only stop tracking if there's no open position — a held ticker must
    # keep a live price for the positions table / portfolio valuation
    # even once it's off the watchlist.
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)

    return {"status": "ok"}
```

The inverse case — a trade creates a *new* position in a ticker that isn't on the watchlist (e.g. the LLM buys something not currently watched) — needs the same treatment on the trade path: after a buy that opens a new position, call `source.add_ticker(ticker)` if it isn't already tracked, so the positions table has a live price without requiring the ticker to also be added to the watchlist.

---

## 14. Daily-Change-% Semantics

`planning/PLAN.md` §10 requires "daily change %" in the watchlist panel and positions table. `PriceUpdate.change_percent` (§3) is **tick-over-tick** — this update vs. the immediately preceding one, not vs. session open or previous close. For the simulator that's ~500ms of movement; for Massive that's ~15s. Labeling that as "daily change" would be misleading — a review flagged this as unresolved in the plan text.

Two ways to close this, neither requiring a `PriceCache` redesign:

1. **Relabel in the UI as "change" (drop "daily")** and keep using tick-over-tick `change_percent` as-is. Simplest; accurate; loses the "since market open" framing.
2. **Add a session-open baseline.** Record each ticker's *first* price of the current process/session (or, for Massive, use `TickerSnapshot.todays_change_percent`, which the API already computes against the real previous close — see `planning/MASSIVE_API.md` §1) and compute `%` against that instead of the previous tick. This means the simulator and Massive would compute "daily change" differently (simulator: first price since process start; Massive: real previous-close-relative), which is honest given the simulator has no concept of a trading session, but should be called out explicitly in the frontend contract so it isn't read as identical semantics across both data sources.

This document doesn't pick one — it's a product decision belonging in `planning/PLAN.md` (already flagged there as open) — but recommends whichever choice is made gets encoded once, in `PriceCache` or a thin wrapper, rather than computed ad hoc in the frontend from `previous_price`.

---

## 15. Testing Strategy

Existing coverage under `backend/tests/market/` (all passing as of the last review pass against an environment with `massive` installed): `test_models.py`, `test_cache.py`, `test_factory.py`, `test_simulator.py`, `test_simulator_source.py`, `test_massive.py`. Representative cases already in place:

- **`GBMSimulator`**: `step()` returns all tracked tickers; prices always positive over 10k steps; initial price matches seed; add/remove ticker updates the tracked set and rebuilds correlation; duplicate add / unknown-ticker remove are no-ops; unknown ticker gets a `[$50, $300]` random seed; empty ticker list steps to `{}`; Cholesky is `None` for 1 ticker, non-`None` for 2+.
- **`PriceCache`**: update/get round-trip; first update is `direction="flat"`; up/down direction and `change` sign; `remove()`; `get_all()`; `version` increments once per `update()`; `get_price()` convenience.
- **`SimulatorDataSource`**: `start()` populates the cache before the loop's first tick; prices change over multiple intervals; `stop()` is idempotent (safe to call twice); `add_ticker`/`remove_ticker` reflected in both `get_tickers()` and the cache.
- **`MassiveDataSource`** (mocked, no network): `_poll_once` updates the cache from mocked snapshots; a malformed snapshot (`last_trade=None`) is skipped without dropping other tickers; an exception from `_fetch_snapshots` doesn't propagate out of `_poll_once`.

### New tests needed for the proposed additions 🚧

**`backend/tests/market/test_cache.py`** — extend for the history buffer:

```python
class TestPriceCacheHistory:
    def test_history_starts_empty(self):
        cache = PriceCache()
        assert cache.get_history("AAPL") == []

    def test_history_accumulates_in_order(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("AAPL", 191.00)
        cache.update("AAPL", 192.00)
        prices = [u.price for u in cache.get_history("AAPL")]
        assert prices == [190.00, 191.00, 192.00]

    def test_history_is_bounded(self):
        cache = PriceCache(history_size=3)
        for p in [1.0, 2.0, 3.0, 4.0, 5.0]:
            cache.update("AAPL", p)
        prices = [u.price for u in cache.get_history("AAPL")]
        assert prices == [3.0, 4.0, 5.0]  # oldest evicted first

    def test_history_independent_per_ticker(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.update("GOOGL", 175.00)
        assert len(cache.get_history("AAPL")) == 1
        assert len(cache.get_history("GOOGL")) == 1

    def test_remove_clears_history(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00)
        cache.remove("AAPL")
        assert cache.get_history("AAPL") == []
```

**`backend/tests/market/test_history.py`** — the new route, via FastAPI's test client:

```python
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market.cache import PriceCache
from app.market.history import create_history_router


@pytest.fixture
def client():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 191.00)
    app = FastAPI()
    app.include_router(create_history_router(cache))
    return TestClient(app), cache


class TestHistoryEndpoint:
    def test_returns_history_oldest_first(self, client):
        test_client, _ = client
        resp = test_client.get("/api/market/history/AAPL")
        assert resp.status_code == 200
        body = resp.json()
        assert [p["price"] for p in body] == [190.00, 191.00]

    def test_lowercase_ticker_normalized(self, client):
        test_client, _ = client
        resp = test_client.get("/api/market/history/aapl")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_unknown_ticker_404s(self, client):
        test_client, _ = client
        resp = test_client.get("/api/market/history/ZZZZ")
        assert resp.status_code == 404
```

### Still worth adding (carried over from `planning/archive/MARKET_DATA_REVIEW.md`, not yet done)

- At least one SSE integration test against a running ASGI test client (`stream.py` currently has the lowest coverage of the package — it needs a live server to exercise the generator).
- A `GBMSimulator` test with the full 10-ticker default set, to catch any future correlation-matrix issue that only appears at that size (existing tests use 1–2 tickers).
- A concurrent-write test for `PriceCache` (multiple threads calling `update()` simultaneously) to empirically confirm the lock, not just inspect it.

---

## 16. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Empty initial watchlist at startup | `start([])` — simulator produces no prices, Massive poller skips its API call entirely (`if not self._tickers: return`). SSE sends nothing (`if prices:` guard). First ticker added starts tracking immediately. |
| Trade on a ticker with no cached price | `price_cache.get_price(ticker) is None` → route returns `400` with a message telling the user to retry ("Price not yet available for {ticker}"). The simulator avoids this in practice by seeding synchronously in `start()`/`add_ticker()`; Massive may have a real gap between "ticker added" and "first successful poll." |
| Massive API key set but invalid | First poll 401s, logged, poller keeps retrying every `poll_interval`. SSE endpoint is "connected" but streams empty/stale data — a UI relying solely on the SSE connection-status dot would show green with no prices, which is misleading; consider surfacing cache staleness (e.g. via `timestamp` age) in the frontend rather than only connection state. |
| Massive 403 on Basic/free tier | Every poll fails identically; same behavior as an invalid key. Document that real-data mode needs Starter+ (see §8). |
| Ticker removed from watchlist while still held | Must **not** call `source.remove_ticker()` — see §13. Removing the ticker from `PriceCache` would break positions-table valuation. |
| `GET /api/market/history/{ticker}` for an untracked ticker | `404` (§11) — distinguishes "never tracked" from "tracked but momentarily empty." |
| Simulator step raises | Caught per-tick inside `_run_loop`'s `try/except Exception`; logged, loop continues on the next `sleep`. A single bad `numpy` draw does not kill the price feed. |
| `PriceCache` under concurrent load | `threading.Lock` serializes all reads/writes; critical sections are a dict lookup + assignment, negligible contention at the project's scale (≤50 tracked tickers, sub-second cadence). A `ReadWriteLock` would only matter at a scale this project doesn't target. |

---

## 17. Configuration Summary

| Parameter | Location | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env var | `""` | Non-empty (after `.strip()`) → Massive; else simulator |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5s` | Simulator tick cadence |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0s` | Conservative default; safe even on a rate-limited plan |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Shock chance per ticker per tick |
| `dt` | `GBMSimulator.__init__` | `≈8.48e-8` | Fraction of a trading year per 500ms tick |
| SSE poll interval | `_generate_events()` | `0.5s` | Independent of the underlying source's own cadence |
| SSE retry directive | `_generate_events()` | `1000ms` | Browser `EventSource` auto-reconnect delay |
| `history_size` 🚧 | `PriceCache.__init__` | `500` | Points retained per ticker (~4 min simulator / ~2 hr Massive) |
| Watchlist cap | route-level validation 🚧 | `20` | `POST /api/watchlist` → `400` past the cap; positions are never capped |

### `__init__.py` — public API (extend once §11 lands)

```python
"""Market data subsystem for FinAlly."""

from .cache import PriceCache
from .factory import create_market_data_source
from .history import create_history_router
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
    "create_history_router",
]
```
