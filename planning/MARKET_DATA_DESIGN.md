# Market Data Backend — Detailed Design

Implementation-ready design for FinAlly's market data subsystem: the unified API, the GBM simulator, the Massive (Polygon.io) client, the SSE stream, and how the rest of the backend uses them. Every module has full code below. An agent should be able to implement or refactor `backend/app/market/` from this document alone.

**Inputs:** `PLAN.md` §6, §8 and §10; `MASSIVE_API.md` (provider reference); `MARKET_INTERFACE.md` (interface contract); `MARKET_SIMULATOR.md` (simulator approach); `MARKET_DATA_SUMMARY.md` and `archive/` (the first implementation and its review).

**Relationship to the existing code:** `backend/app/market/` already implements v1 of this design (73 passing tests). This document is **v2**. It keeps the v1 architecture and public names, and fixes what the Massive research found. §13 lists every change from v1.

---

## Contents

1. [Goals and non-goals](#1-goals-and-non-goals)
2. [Architecture](#2-architecture)
3. [File layout](#3-file-layout)
4. [`models.py` — PriceUpdate](#4-modelspy--priceupdate)
5. [`cache.py` — PriceCache](#5-cachepy--pricecache)
6. [`interface.py` — MarketDataSource](#6-interfacepy--marketdatasource)
7. [`seed_prices.py` — simulator data](#7-seed_pricespy--simulator-data)
8. [`simulator.py` — GBM simulator](#8-simulatorpy--gbm-simulator)
9. [`massive_client.py` — Massive API source](#9-massive_clientpy--massive-api-source)
10. [`factory.py` and `__init__.py`](#10-factorypy-and-__init__py)
11. [`stream.py` — SSE endpoint](#11-streampy--sse-endpoint)
12. [Integration with the rest of the backend](#12-integration-with-the-rest-of-the-backend)
13. [Changes from v1](#13-changes-from-v1)
14. [Testing](#14-testing)
15. [Configuration](#15-configuration)
16. [Edge cases and failure modes](#16-edge-cases-and-failure-modes)

---

## 1. Goals and non-goals

**Goals**

- One interface; the simulator (default) and Massive (when `MASSIVE_API_KEY` is set) are interchangeable.
- Prices for every **watched or held** ticker are always in an in-memory cache, ready for SSE, trades, portfolio valuation and LLM context.
- SSE pushes all prices about every 500 ms, with tick direction (for flashes) and day change % (for the watchlist column).
- Works with a **free** Massive key (end-of-day, 5 requests/min) and with paid keys (snapshot, real-time or 15-minute delayed).
- Never crashes the app: data-source failures are logged and retried.

**Non-goals**

- Historical charts from the provider. Sparklines and the main chart are accumulated on the frontend from SSE (PLAN §2).
- WebSocket feeds from Massive. REST polling only (PLAN §6).
- Persisting prices to SQLite. Prices are ephemeral. Only portfolio snapshots are stored, by the portfolio module.

---

## 2. Architecture

```
                         ┌───────────────────────────────┐
  MASSIVE_API_KEY ──────►│ create_market_data_source()   │
                         └──────────────┬────────────────┘
                     empty              │             set
              ┌─────────────────────────┴────────────────────────┐
              ▼                                                  ▼
   SimulatorDataSource                                 MassiveDataSource
   GBMSimulator.step() every 0.5 s                     poll loop:
                                                         snapshot mode (paid) every 15 s
                                                         EOD mode (free)      every 300 s
              │ cache.update(...)                                │ cache.update(...)
              └─────────────────────────┬────────────────────────┘
                                        ▼
                              PriceCache (thread-safe)
                              latest PriceUpdate per ticker + version counter
                                        │ read-only
       ┌──────────────────┬─────────────┼───────────────────┬──────────────────────┐
       ▼                  ▼             ▼                   ▼                      ▼
 GET /api/stream/   POST /api/      GET /api/          snapshot task         POST /api/chat
 prices (SSE)       portfolio/trade portfolio          (every 30 s)          (LLM context)
```

Rules:

1. **Sources write and everyone else reads.** No consumer calls a data source to get a price.
2. **The data source tracks `watchlist ∪ held tickers`.** The database owns those sets. After any change, `source.set_tickers(...)` reconciles the two (§12.3).
3. **Lifecycle is async; reads are sync.** `start/stop/add/remove/set_tickers` are coroutines. `PriceCache` reads are plain, lock-protected method calls.
4. **Blocking I/O never runs on the event loop.** The Massive SDK is synchronous, so it runs under `asyncio.to_thread`.

---

## 3. File layout

```
backend/
├── app/
│   ├── main.py                  # FastAPI app + lifespan (owned by backend agent; market wiring in §12)
│   └── market/
│       ├── __init__.py          # public re-exports
│       ├── models.py            # PriceUpdate
│       ├── cache.py             # PriceCache
│       ├── interface.py         # MarketDataSource ABC + normalize_ticker
│       ├── seed_prices.py       # seed prices, GBM params, correlation groups, seed_price_for()
│       ├── simulator.py         # GBMSimulator + SimulatorDataSource
│       ├── massive_client.py    # MassiveDataSource (snapshot + EOD modes)
│       ├── factory.py           # create_market_data_source()
│       └── stream.py            # create_stream_router() → GET /api/stream/prices
├── tests/market/
│   ├── fixtures.py              # real Massive JSON samples (new)
│   ├── test_models.py  test_cache.py  test_interface.py (new)
│   ├── test_simulator.py  test_simulator_source.py
│   ├── test_massive.py  test_factory.py  test_stream.py (new)
└── market_data_demo.py          # Rich terminal demo (unchanged)
```

Dependencies (`pyproject.toml`): `fastapi`, `uvicorn[standard]`, `numpy`, `massive>=2.0`, `tzdata` (**new**: guarantees `zoneinfo("America/New_York")` works in `python:3.12-slim`). Dev dependencies: `pytest`, `pytest-asyncio`, `pytest-cov`, `httpx` (**new**, for SSE tests), `ruff`.

---

## 4. `models.py` — PriceUpdate

An immutable value object: one ticker's price at one moment, plus everything the UI needs to render it.

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    previous_price  - price at the previous update (tick-to-tick; drives flash colour)
    reference_price - "start of day" price used for day change:
                        simulator: price when the ticker started being simulated
                        Massive:   previous session's close
                      None when unknown.
    """

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds
    reference_price: float | None = None

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
        if self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_change(self) -> float | None:
        if not self.reference_price:
            return None
        return round(self.price - self.reference_price, 2)

    @property
    def day_change_percent(self) -> float | None:
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 2)

    def to_dict(self) -> dict:
        """JSON-ready dict; this is the per-ticker SSE payload (see §11.2)."""
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

- `frozen=True, slots=True`: safe to share across threads and SSE clients without copying, and cheap.
- Derived fields are properties, so they can never disagree with `price`.
- New fields are additive and default to `None`, so the v1 constructor calls and tests keep working.

---

## 5. `cache.py` — PriceCache

The single source of truth for "what is the price of X right now".

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate

_UNSET = object()


class PriceCache:
    """Latest PriceUpdate per ticker.

    Writers: exactly one MarketDataSource (event-loop thread or a to_thread worker).
    Readers: SSE, trade execution, portfolio valuation, snapshot task, LLM context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # bumped on every mutation (update AND remove)

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        reference_price: float | None | object = _UNSET,
    ) -> PriceUpdate:
        """Record a new price. previous_price/direction are derived from the prior entry.

        reference_price: omit to carry forward the existing value; pass None to clear it.
        """
        with self._lock:
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if reference_price is _UNSET:
                reference_price = prev.reference_price if prev else None
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=timestamp or time.time(),
                reference_price=None if reference_price is None else round(reference_price, 2),
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
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1  # so SSE pushes the shrunken set promptly

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

Why it's built this way:

- **Version counter.** SSE checks `version` every 500 ms and sends only when it changed. In Massive EOD mode prices change about once a day, so this avoids pushing identical payloads to every client.
- **Bump on `remove`.** v1 didn't, so in EOD mode a removed ticker stayed on screen until the next poll, up to 5 minutes later.
- **`threading.Lock`, not `asyncio.Lock`.** Massive writes from a `to_thread` worker. Critical sections are a dict lookup and an assignment, so contention is negligible.
- **Rounding at the cache boundary.** Sources may keep full precision internally (the simulator does); everything downstream sees cents.

```python
cache = PriceCache()
cache.update("AAPL", 190.00, reference_price=190.00)
u = cache.update("AAPL", 190.37)
u.direction, u.change, u.day_change_percent       # ('up', 0.37, 0.19)
cache.get_price("MSFT")                           # None → not tracked / not yet priced
```

---

## 6. `interface.py` — MarketDataSource

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable


def normalize_ticker(ticker: str) -> str:
    """Canonical ticker form used everywhere (DB, cache, sources): stripped, upper-case."""
    return ticker.strip().upper()


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push PriceUpdates into a shared PriceCache on their own schedule.
    Consumers read the cache, never the source.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL"])
        await source.add_ticker("TSLA")
        await source.set_tickers({"AAPL", "TSLA"})   # reconcile with DB state
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices. Call once. Returns after an initial cache seed/fetch attempt."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background work. Idempotent; safe before start()."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker (normalized). No-op if already tracked."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and evict it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""

    async def set_tickers(self, tickers: Iterable[str]) -> None:
        """Reconcile the tracked set with `tickers` (adds missing, removes extras).

        Concrete helper so routes never compute diffs themselves.
        """
        wanted = {normalize_ticker(t) for t in tickers if t and t.strip()}
        current = set(self.get_tickers())
        for t in sorted(wanted - current):
            await self.add_ticker(t)
        for t in sorted(current - wanted):
            await self.remove_ticker(t)
```

Contract, which the conformance tests check against both implementations (§14.2):

| # | Rule |
|---|---|
| C1 | All public methods normalize tickers with `normalize_ticker`. |
| C2 | After `start()` returns, the simulator has a cached price for every starting ticker. Massive has completed one fetch attempt. |
| C3 | `add_ticker` is idempotent. The simulator writes a price immediately; Massive writes one on the next poll. |
| C4 | `remove_ticker` is idempotent and evicts the ticker from the cache. |
| C5 | `stop()` is idempotent and safe before `start()`. After it returns, nothing writes to the cache. |
| C6 | Background exceptions are logged and never propagate out of the task. |
| C7 | No method blocks the event loop for more than a few milliseconds. |

---

## 7. `seed_prices.py` — simulator data

Pure data plus one helper. Values are unchanged from v1; the helper is new.

```python
"""Seed prices and per-ticker parameters for the market simulator."""

import hashlib

SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# sigma: annualized volatility; mu: annualized drift
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},
    "NVDA": {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},
    "V": {"sigma": 0.17, "mu": 0.04},
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3


def seed_price_for(ticker: str) -> float:
    """Starting price for a ticker: table value, else a stable pseudo-random $50.00–$299.99.

    Deterministic (hash of the symbol), so a ticker gets the same seed on every restart.
    """
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker]
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    return round(50 + (h % 25_000) / 100, 2)
```

---

## 8. `simulator.py` — GBM simulator

The approach (the maths, the parameter choices, why correlations are used) is in `MARKET_SIMULATOR.md`. This section is the code.

### 8.1 `GBMSimulator` — pure maths engine

No asyncio, no cache. Deterministic when given a `seed`.

```python
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import logging
import math

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker
from .seed_prices import (
    CORRELATION_GROUPS, CROSS_GROUP_CORR, DEFAULT_PARAMS, INTRA_FINANCE_CORR,
    INTRA_TECH_CORR, TICKER_PARAMS, TSLA_CORR, seed_price_for,
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated Geometric Brownian Motion:

        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z),  Z ~ N(0, C)
    """

    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600          # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR          # ≈ 8.48e-8 (one 500 ms tick)

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
        initial_prices: dict[str, float] | None = None,
        seed: int | None = None,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._initial = dict(initial_prices or {})
        self._rng = np.random.default_rng(seed)           # private RNG: reproducible, no global state

        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}               # full precision internally
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None

        for t in tickers:
            self._add_ticker_internal(t)
        self._rebuild_cholesky()

    # --- public ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price rounded to 2 dp}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z = self._rng.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        out: dict[str, float] = {}
        for i, t in enumerate(self._tickers):
            mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
            self._prices[t] *= math.exp((mu - 0.5 * sigma * sigma) * self._dt
                                        + sigma * self._sqrt_dt * z[i])

            if self._rng.random() < self._event_prob:      # random "news" shock
                shock = self._rng.uniform(0.02, 0.05) * (1 if self._rng.random() < 0.5 else -1)
                self._prices[t] *= 1 + shock
                logger.debug("Random event on %s: %+.1f%%", t, shock * 100)

            out[t] = round(self._prices[t], 2)
        return out

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker], self._params[ticker]
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
        self._prices[ticker] = self._initial.get(ticker) or seed_price_for(ticker)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                corr[i, j] = corr[j, i] = self._pairwise_correlation(self._tickers[i], self._tickers[j])
        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            logger.warning("Correlation matrix not positive-definite; using independent moves")
            self._cholesky = None

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR
        tech, fin = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in fin and t2 in fin:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

`initial_prices` lets the app start a held ticker at its `avg_cost` (or last known price) instead of the table seed. Otherwise every restart would show a fake profit or loss jump on positions in tickers outside the seed table.

### 8.2 `SimulatorDataSource` — async adapter

```python
class SimulatorDataSource(MarketDataSource):
    """Runs GBMSimulator.step() every `update_interval` seconds and writes to the PriceCache."""

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
        initial_prices: dict[str, float] | None = None,
        seed: int | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._initial = {normalize_ticker(k): v for k, v in (initial_prices or {}).items()}
        self._seed = seed
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))   # dedupe, keep order
        self._sim = GBMSimulator(
            tickers, event_probability=self._event_prob,
            initial_prices=self._initial, seed=self._seed,
        )
        for t in tickers:
            self._seed_cache(t)
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
        if self._sim is None or ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        self._seed_cache(ticker)                 # price visible immediately (C3)
        logger.info("Simulator: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    # --- internals ---

    def _seed_cache(self, ticker: str) -> None:
        price = self._sim.get_price(ticker) if self._sim else None
        if price is not None:
            # reference = session-start price → "day change" = change since tracking began
            self._cache.update(ticker, price, reference_price=price)

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for t, p in self._sim.step().items():
                        self._cache.update(t, p)            # reference_price carried forward
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Concurrency: everything runs on the event-loop thread. `add_ticker` and `remove_ticker` don't `await` between changing the simulator and changing the cache, so they can't interleave with a `step()`.

---

## 9. `massive_client.py` — Massive API source

The provider details (endpoints, plans, field names, timestamp units) are in `MASSIVE_API.md`. Three facts drive this design:

1. The **Full Market Snapshot** (`get_snapshot_all`) returns all watched tickers in one call, but it is **not on the free Basic plan**.
2. The free plan gets **end-of-day data at 5 requests/min**. **Grouped Daily** (`get_grouped_daily_aggs`) returns the whole market for a date in one call, and it is available on every plan.
3. In the SDK, `LastTrade.sip_timestamp` is in **nanoseconds** and there is no `.timestamp` attribute. Bar timestamps (`Agg.timestamp`, `GroupedDailyAgg.timestamp`) are in **milliseconds**.

So the source has two modes, detected automatically.

| Mode | Plan | Call per poll | Default interval | Price | Reference |
|---|---|---|---|---|---|
| `snapshot` | Starter and above | `get_snapshot_all(STOCKS, tickers=[...])` | 15 s (`MASSIVE_POLL_INTERVAL`) | `last_trade.price` → `min.close` → `day.close` → `prev_day.close` | `prev_day.close` |
| `eod` | Basic (free) | `get_grouped_daily_aggs(date)` | 300 s | `close` for the latest trading day | close of the trading day before that |

### 9.1 Pure parsing helpers (unit-testable without network)

```python
"""Massive (Polygon.io) API data source: snapshot mode (paid) or end-of-day mode (free)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
Mode = Literal["snapshot", "eod"]


@dataclass(frozen=True, slots=True)
class Quote:
    """Provider-neutral parsed price, ready for PriceCache.update()."""
    ticker: str
    price: float
    timestamp: float                  # Unix seconds
    reference_price: float | None


def quote_from_snapshot(snap) -> Quote | None:
    """TickerSnapshot → Quote, with fallbacks for plans without trades and the overnight reset."""
    ticker = getattr(snap, "ticker", None)
    if not ticker:
        return None
    lt, mn, day, prev = snap.last_trade, snap.min, snap.day, snap.prev_day
    ref = prev.close if prev and prev.close else None

    if lt and lt.price:
        ts = lt.sip_timestamp / 1e9 if lt.sip_timestamp else time.time()          # ns
        return Quote(ticker, lt.price, ts, ref)
    if mn and mn.close:
        ts = mn.timestamp / 1e3 if mn.timestamp else time.time()                  # ms
        return Quote(ticker, mn.close, ts, ref)
    if day and day.close:
        ts = snap.updated / 1e9 if snap.updated else time.time()                  # ns
        return Quote(ticker, day.close, ts, ref)
    if ref:
        return Quote(ticker, ref, time.time(), ref)
    return None


def classify_error(exc: Exception) -> Literal["not_entitled", "rate_limited", "auth", "other"]:
    """BadResponse carries only the body text; classify by content."""
    text = str(exc).lower()
    if "not_authorized" in text or "not entitled" in text or "upgrade your plan" in text:
        return "not_entitled"
    if "exceeded the maximum requests" in text or "429" in text or "too many" in text:
        return "rate_limited"
    if "unknown api key" in text or ("api key" in text and "invalid" in text) or "401" in text:
        return "auth"
    return "other"


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:             # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


class CallBudget:
    """Sliding-window limiter (free plan: 5 calls / 60 s). Non-blocking."""

    def __init__(self, max_calls: int, per_seconds: float) -> None:
        self._max, self._per = max_calls, per_seconds
        self._calls: deque[float] = deque()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        while self._calls and now - self._calls[0] >= self._per:
            self._calls.popleft()
        if len(self._calls) >= self._max:
            return False
        self._calls.append(now)
        return True


class BudgetExhausted(Exception):
    pass
```

### 9.2 `MassiveDataSource`

```python
class MassiveDataSource(MarketDataSource):
    FREE_CALLS_PER_MINUTE = 4          # keep 1 of the 5 spare for safety
    MAX_BACKOFF = 60.0

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        eod_poll_interval: float = 300.0,
        client: RESTClient | None = None,          # injectable for tests
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._snapshot_interval = poll_interval
        self._eod_interval = eod_poll_interval
        self._client = client
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._mode: Mode | None = None
        self._backoff: float | None = None
        self._warned_missing: set[str] = set()
        # EOD state
        self._budget = CallBudget(self.FREE_CALLS_PER_MINUTE, 60.0)
        self._eod_date: date | None = None
        self._eod_closes: dict[str, float] = {}
        self._ref_date: date | None = None
        self._ref_closes: dict[str, float] = {}

    # --- lifecycle ---

    async def start(self, tickers: list[str]) -> None:
        if self._client is None:
            # retries=0: the SDK's built-in 429 retry (0.1 s backoff) would burn a free quota instantly
            self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))
        await self._poll_once()                      # detects mode, seeds cache (C2)
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive source started: mode=%s, %d tickers", self._mode, len(self._tickers))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        logger.info("Massive source stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self._mode == "eod" and ticker in self._eod_closes:     # free data already in hand
            self._write_eod(ticker)
        logger.info("Massive: added %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    @property
    def mode(self) -> Mode | None:
        return self._mode

    # --- polling ---

    def _interval(self) -> float:
        if self._backoff:
            return self._backoff
        return self._eod_interval if self._mode == "eod" else self._snapshot_interval

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval())
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers:
            return
        tickers = list(self._tickers)               # snapshot; list may change during the thread call
        try:
            if self._mode is None:
                self._mode = await asyncio.to_thread(self._detect_mode, tickers[0])
                logger.info("Massive mode: %s", self._mode)
            if self._mode == "snapshot":
                quotes = await asyncio.to_thread(self._fetch_snapshot, tickers)
                self._write_quotes(quotes, tickers)
            else:
                try:
                    await asyncio.to_thread(self._refresh_eod)
                finally:                               # publish whatever we have, even if the
                    for t in self._tickers:            # budget ran out before the reference fetch
                        self._write_eod(t)
            self._backoff = None
        except BudgetExhausted:
            logger.debug("Massive: free-plan call budget exhausted; retrying next poll")
        except BadResponse as e:
            self._handle_bad_response(e)
        except Exception:
            logger.exception("Massive poll failed")    # network etc. — retry next interval (C6)

    def _handle_bad_response(self, e: BadResponse) -> None:
        kind = classify_error(e)
        if kind == "rate_limited":
            self._backoff = min((self._backoff or self._interval()) * 2, self.MAX_BACKOFF)
            logger.warning("Massive rate limited; next poll in %.0fs", self._backoff)
        elif kind == "not_entitled" and self._mode == "snapshot":
            logger.warning("Massive plan lost snapshot access; switching to EOD mode")
            self._mode = "eod"
        elif kind == "auth":
            logger.error("Massive rejected MASSIVE_API_KEY (%s). Fix the key and restart.", e)
            self._mode = None                         # re-detect on next poll
            self._backoff = self._eod_interval
        else:
            logger.error("Massive error: %s", e)

    # --- mode detection (runs in a thread) ---

    def _detect_mode(self, probe_ticker: str) -> Mode:
        try:
            self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=[probe_ticker])
            return "snapshot"
        except BadResponse as e:
            if classify_error(e) == "not_entitled":
                self._budget.try_acquire()             # the probe counted against the free quota
                return "eod"
            raise

    # --- snapshot mode (runs in a thread) ---

    def _fetch_snapshot(self, tickers: list[str]) -> list[Quote]:
        snaps = self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=tickers)
        quotes = []
        for s in snaps:
            try:
                q = quote_from_snapshot(s)
            except (AttributeError, TypeError) as e:
                logger.warning("Skipping malformed snapshot for %s: %s", getattr(s, "ticker", "?"), e)
                continue
            if q:
                quotes.append(q)
        return quotes

    def _write_quotes(self, quotes: list[Quote], requested: list[str]) -> None:
        live = set(self._tickers)                      # ignore tickers removed mid-poll
        seen = set()
        for q in quotes:
            if q.ticker in live:
                self._cache.update(q.ticker, q.price, q.timestamp, reference_price=q.reference_price)
                seen.add(q.ticker)
        for t in set(requested) - seen:
            if t not in self._warned_missing:
                logger.warning("Massive returned no data for %s (invalid ticker?)", t)
                self._warned_missing.add(t)

    # --- EOD mode (runs in a thread) ---

    def _grouped(self, d: date) -> dict[str, float]:
        if not self._budget.try_acquire():
            raise BudgetExhausted
        aggs = self._client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        return {a.ticker: a.close for a in (aggs or []) if a.ticker and a.close}

    def _latest_with_data(self, start: date, floor: date | None = None) -> tuple[date, dict[str, float]] | None:
        """Walk back over weekdays from `start` (inclusive) to the first date with data."""
        d = start if start.weekday() < 5 else previous_weekday(start)
        for _ in range(5):                              # covers long weekends / holidays
            if floor and d <= floor:
                return None
            closes = self._grouped(d)
            if closes:
                return d, closes
            d = previous_weekday(d)
        return None

    def _refresh_eod(self) -> None:
        now = datetime.now(ET)
        today = now.date()
        # Nothing newer can exist yet: we already have today, or yesterday's data before ~5pm ET.
        fresh = self._eod_date is not None and (
            self._eod_date == today or (self._eod_date >= previous_weekday(today) and now.hour < 17)
        )
        if not fresh:
            found = self._latest_with_data(today, floor=self._eod_date)
            if found:
                if self._eod_date and found[0] > self._eod_date:
                    # the old "latest" day becomes the reference day for free
                    self._ref_date, self._ref_closes = self._eod_date, self._eod_closes
                self._eod_date, self._eod_closes = found
        if self._eod_date and (self._ref_date is None or self._ref_date >= self._eod_date):
            ref = self._latest_with_data(previous_weekday(self._eod_date))
            if ref:
                self._ref_date, self._ref_closes = ref

    def _write_eod(self, ticker: str) -> None:
        price = self._eod_closes.get(ticker)
        if price is None:
            return
        ts = datetime.combine(self._eod_date, datetime.min.time(), ET).replace(hour=16).timestamp()
        ref = self._ref_closes.get(ticker)
        current = self._cache.get(ticker)
        if (current and current.price == round(price, 2) and current.timestamp == ts
                and current.reference_price == (round(ref, 2) if ref else None)):
            return                                       # unchanged → don't bump cache version
        self._cache.update(ticker, price, ts, reference_price=ref)
```

Free-plan quota walk-through for a normal weekday startup at 10:00 ET: the probe (1 call, 403) → `_refresh_eod`: today has no data yet (1) → yesterday has data (1) → reference: the day before yesterday (1). That's **4 calls**, and every 5-minute poll after that until ~17:00 ET costs **0 calls**. After 17:00 it costs 1 call per poll until today's data appears. If a holiday adds calls and the budget runs out, the poll stops at `BudgetExhausted` and finishes on the next one.

### 9.3 Usage

```python
cache = PriceCache()
source = MassiveDataSource(api_key=os.environ["MASSIVE_API_KEY"], price_cache=cache)
await source.start(["AAPL", "MSFT", "NVDA"])
source.mode                      # "snapshot" (paid) or "eod" (free)
cache.get("AAPL").to_dict()      # {"price": 227.41, "reference_price": 225.10, "day_change_percent": 1.03, ...}
```

---

## 10. `factory.py` and `__init__.py`

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


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        logger.warning("Invalid %s; using %s", name, default)
        return default


def create_market_data_source(
    price_cache: PriceCache,
    initial_prices: dict[str, float] | None = None,
) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-blank → MassiveDataSource; otherwise SimulatorDataSource.

    initial_prices (e.g. avg_cost of held positions) only affects the simulator.
    Returns an unstarted source; caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        interval = max(_float_env("MASSIVE_POLL_INTERVAL", 15.0), 1.0)
        logger.info("Market data source: Massive API (poll %.0fs)", interval)
        return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=interval)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache, initial_prices=initial_prices)
```

```python
# __init__.py
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource, normalize_ticker
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = ["PriceUpdate", "PriceCache", "MarketDataSource", "normalize_ticker",
           "create_market_data_source", "create_stream_router"]
```

`massive` is a core dependency, so it's imported at module top level. That keeps tests simple (`patch` targets exist), as v1 decided after review.

---

## 11. `stream.py` — SSE endpoint

### 11.1 Code

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

PUSH_INTERVAL = 0.5        # seconds between change checks
HEARTBEAT_INTERVAL = 15.0  # comment line when idle, keeps proxies/load balancers from closing


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])   # new router per call

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            generate_price_events(price_cache, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )

    return router


async def generate_price_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = PUSH_INTERVAL,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"
    last_version = -1
    last_sent = time.monotonic()
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                payload = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"id: {version}\ndata: {json.dumps(payload)}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= HEARTBEAT_INTERVAL:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("SSE client disconnected: %s", client)
```

Notes:

- The first event is sent immediately on connect (`last_version = -1`), so the UI fills in at once.
- Each event is the **full** map of tracked tickers, not a diff. The frontend replaces its map, so removals propagate automatically and a reconnecting client needs no catch-up logic.
- An empty cache still sends `data: {}`, which tells the UI "connected, no tickers".
- Polling the version every 500 ms with one task per client is simple and cheap for a single-user app. An `asyncio.Condition` notify would be the upgrade path for many clients.

### 11.2 Wire format (frontend contract)

```
retry: 1000

id: 4182
data: {"AAPL":{"ticker":"AAPL","price":190.37,"previous_price":190.31,"timestamp":1762876800.51,
       "change":0.06,"change_percent":0.0315,"direction":"up",
       "reference_price":190.0,"day_change":0.37,"day_change_percent":0.19},
       "MSFT":{...}}

: keepalive
```

Frontend usage:

```ts
const es = new EventSource("/api/stream/prices");
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
es.onmessage = (e) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(e.data);
  for (const p of Object.values(prices)) {
    if (p.direction !== "flat") flash(p.ticker, p.direction);           // green/red, ~500 ms fade
    appendSparkline(p.ticker, p.timestamp, p.price);                    // only if timestamp is new
  }
  setPrices(prices);                                                    // full replace
};
```

Watchlist "daily change %" = `day_change_percent` (`null` → show "—"). Flash colour = `direction`.

---

## 12. Integration with the rest of the backend

### 12.1 Lifespan wiring (`app/main.py`)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request

from app.market import PriceCache, MarketDataSource, create_market_data_source, create_stream_router
from app.db import repo          # backend agent's DB layer: lazy init + seed (PLAN §7)


price_cache = PriceCache()          # module-level: the SSE router needs it at import time


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo.init_db()                                           # creates tables + seed if missing
    positions = repo.get_positions()                         # [{ticker, quantity, avg_cost}, ...]
    source = create_market_data_source(
        price_cache, initial_prices={p["ticker"]: p["avg_cost"] for p in positions},
    )
    await source.start(sorted(tracked_tickers()))
    app.state.price_cache = price_cache
    app.state.market = source
    try:
        yield
    finally:
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
# ... include the other /api routers here ...
# app.mount("/", StaticFiles(directory="static", html=True))   ← LAST, or it swallows /api/*


def tracked_tickers() -> set[str]:
    """What the data source must price: everything watched plus everything held."""
    return set(repo.get_watchlist_tickers()) | {p["ticker"] for p in repo.get_positions()
                                                if p["quantity"] > 0}


def get_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market(request: Request) -> MarketDataSource:
    return request.app.state.market
```

The cache is created at module level because it has no I/O and the router needs it. The source is created in lifespan because it starts background tasks and reads the DB.

### 12.2 Reading prices in routes

```python
from fastapi import APIRouter, Depends, HTTPException
from app.market import normalize_ticker

router = APIRouter(prefix="/api")

@router.post("/portfolio/trade")
async def trade(body: TradeRequest, cache: PriceCache = Depends(get_cache),
                market: MarketDataSource = Depends(get_market)):
    ticker = normalize_ticker(body.ticker)
    price = cache.get_price(ticker)
    if price is None:
        await market.add_ticker(ticker)              # simulator: priced immediately
        price = cache.get_price(ticker)
    if price is None:                                # Massive: not until the next poll
        raise HTTPException(400, f"No price available yet for {ticker}; try again shortly")
    result = portfolio.execute(ticker, body.side, body.quantity, price)   # validation lives here
    await market.set_tickers(tracked_tickers())      # sold out of an unwatched ticker → stop tracking
    return result
```

Portfolio valuation (`GET /api/portfolio`, the 30-second snapshot task, LLM context) uses one consistent read:

```python
prices = cache.get_all()                                    # one lock, one consistent view
value = cash + sum(p.quantity * (prices[p.ticker].price if p.ticker in prices else p.avg_cost)
                   for p in positions)                      # fall back to cost if unpriced
```

### 12.3 Watchlist coordination

All watchlist and position changes, whether from REST or from the LLM's `watchlist_changes` and `trades`, end with the same reconcile call. No route needs to reason about "is it still held?":

```python
@router.post("/watchlist")
async def add_watch(body: WatchlistAdd, cache=Depends(get_cache), market=Depends(get_market)):
    ticker = normalize_ticker(body.ticker)
    if not TICKER_RE.fullmatch(ticker):                     # r"[A-Z][A-Z0-9.\-]{0,9}"
        raise HTTPException(422, "Invalid ticker symbol")
    repo.add_watchlist(ticker)                              # UNIQUE(user_id, ticker) → idempotent
    await market.set_tickers(tracked_tickers())
    return {"ticker": ticker, "price": cache.get_price(ticker)}

@router.delete("/watchlist/{ticker}")
async def remove_watch(ticker: str, market=Depends(get_market)):
    repo.remove_watchlist(normalize_ticker(ticker))
    await market.set_tickers(tracked_tickers())             # stays tracked if still held
    return {"ok": True}
```

```
POST /api/watchlist {"ticker":"pypl"}
  → normalize "PYPL" → DB insert → set_tickers(watch ∪ held)
      simulator: GBMSimulator.add_ticker → Cholesky rebuild → cache seeded now
      Massive:   appended → priced on next poll (EOD: immediately if already fetched)
  → SSE: version bumped → next event (≤500 ms) includes PYPL
```

### 12.4 LLM context

```python
def market_context(cache: PriceCache, watchlist: list[str]) -> str:
    prices = cache.get_all()
    rows = []
    for t in watchlist:
        u = prices.get(t)
        rows.append(f"{t}: ${u.price:.2f} ({u.day_change_percent:+.2f}% today)"
                    if u and u.day_change_percent is not None
                    else f"{t}: ${u.price:.2f}" if u else f"{t}: no price yet")
    return "\n".join(rows)
```

### 12.5 Health endpoint

Expose the data source in `/api/health` so it's obvious which source is running:

```python
@router.get("/health")
async def health(request: Request):
    market = request.app.state.market
    return {"status": "ok",
            "market_source": type(market).__name__,
            "massive_mode": getattr(market, "mode", None),
            "tickers": len(market.get_tickers())}
```

---

## 13. Changes from v1

| # | Change | Why | Files |
|---|---|---|---|
| 1 | `last_trade.timestamp/1000` → `quote_from_snapshot()` using `sip_timestamp/1e9` and fallbacks | **v1 bug:** `LastTrade` has no `.timestamp`, so every snapshot raised and was skipped. Real data never reached the cache. Tests passed only because they used `MagicMock`s. | `massive_client.py`, tests |
| 2 | Snapshot/EOD mode auto-detection, `get_grouped_daily_aggs` path, `CallBudget` | Snapshots are not on the free plan; the free plan is 5 calls/min, EOD only | `massive_client.py` |
| 3 | `RESTClient(retries=0)` and our own exponential back-off | The SDK retries 429s after 0.1 s, burning the quota | `massive_client.py` |
| 4 | `reference_price`, `day_change`, `day_change_percent` | Watchlist "daily change %" (PLAN §10) needs a session reference | `models.py`, `cache.py`, sources, SSE payload |
| 5 | `cache.remove` bumps `version`; `version` read under lock | Removed tickers vanish promptly; consistent locking | `cache.py` |
| 6 | `normalize_ticker` everywhere; simulator dedupes start list | Simulator didn't upper-case tickers | `interface.py`, `simulator.py` |
| 7 | `MarketDataSource.set_tickers()` and tracking watchlist ∪ held | Held-but-unwatched tickers kept a price | `interface.py`, routes |
| 8 | Deterministic `seed_price_for`, `initial_prices`, private seeded RNG | Restarts no longer reprice unknown tickers randomly; tests are reproducible | `seed_prices.py`, `simulator.py` |
| 9 | Router created inside `create_stream_router`; SSE `id:` and heartbeat | Calling the factory twice duplicated the route; idle EOD streams survive proxies | `stream.py` |
| 10 | `MASSIVE_POLL_INTERVAL` env var | Paid tiers can poll every 2–5 s | `factory.py` |
| 11 | Cholesky `LinAlgError` guard | Safety if correlation groups are edited | `simulator.py` |
| 12 | Add `tzdata` (main) and `httpx` (dev) dependencies | ET date logic in slim images; SSE tests | `pyproject.toml` |

Tests that need updating: `test_unknown_ticker_gets_random_seed_price` (now deterministic); `test_massive.py` (replace `MagicMock` snapshots and `_fetch_snapshots` patches with SDK-model fixtures and an injected fake client); any `len(sim._tickers)` checks (use `get_tickers()`).

Suggested implementation order: 1 → 12 (pyproject) → 4/5 → 6/7 → 2/3 → 8/11 → 9/10. Run the full test suite after each step.

---

## 14. Testing

### 14.1 Real SDK fixtures (catch attribute-name bugs)

```python
# tests/market/fixtures.py
from massive.rest.models import TickerSnapshot, GroupedDailyAgg

SNAPSHOT_FULL = {
    "ticker": "AAPL", "todaysChange": 0.98, "todaysChangePerc": 0.82, "updated": 1605195918306274000,
    "day": {"o": 119.62, "h": 120.53, "l": 118.81, "c": 120.42, "v": 28727868, "vw": 119.725},
    "prevDay": {"o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49, "v": 110597265, "vw": 118.4998},
    "min": {"o": 120.43, "h": 120.47, "l": 120.37, "c": 120.44, "v": 270796, "t": 1605195900000},
    "lastTrade": {"p": 120.47, "s": 236, "x": 10, "i": "4046", "t": 1605195918306274000},
}
SNAPSHOT_NO_TRADES = {k: v for k, v in SNAPSHOT_FULL.items() if k != "lastTrade"}
SNAPSHOT_OVERNIGHT = {"ticker": "AAPL", "day": {"c": 0}, "prevDay": {"c": 119.49}, "updated": 0}

def snap(d: dict) -> TickerSnapshot:
    return TickerSnapshot.from_dict(d)

def grouped(rows: dict[str, float]) -> list[GroupedDailyAgg]:
    return [GroupedDailyAgg.from_dict({"T": t, "c": c, "t": 1762808400000}) for t, c in rows.items()]
```

### 14.2 Interface conformance (both sources)

```python
@pytest.fixture(params=["sim", "massive"])
async def source(request):
    cache = PriceCache()
    if request.param == "sim":
        src = SimulatorDataSource(cache, update_interval=0.05, seed=1)
    else:
        fake = FakeMassiveClient(snapshot=lambda tickers: [snap({**SNAPSHOT_FULL, "ticker": t}) for t in tickers])
        src = MassiveDataSource("k", cache, poll_interval=0.05, client=fake)
    yield src, cache
    await src.stop()

async def test_start_normalizes_and_prices(source):
    src, cache = source
    await src.start([" aapl", "MSFT", "AAPL"])
    assert src.get_tickers() == ["AAPL", "MSFT"]
    assert cache.get_price("AAPL") is not None

async def test_remove_evicts_and_is_idempotent(source):
    src, cache = source
    await src.start(["AAPL"])
    await src.remove_ticker("aapl"); await src.remove_ticker("AAPL")
    assert "AAPL" not in cache and src.get_tickers() == []

async def test_set_tickers_reconciles(source):
    src, _ = source
    await src.start(["AAPL", "MSFT"])
    await src.set_tickers({"msft", "TSLA"})
    assert sorted(src.get_tickers()) == ["MSFT", "TSLA"]

async def test_stop_twice_and_before_start(source):
    src, _ = source
    await src.stop(); await src.stop()
```

### 14.3 Massive

```python
from massive.exceptions import BadResponse

def test_quote_prefers_last_trade_with_ns_timestamp():
    q = quote_from_snapshot(snap(SNAPSHOT_FULL))
    assert q.price == 120.47 and q.reference_price == 119.49
    assert q.timestamp == pytest.approx(1605195918.306, abs=1e-3)

def test_quote_falls_back_to_minute_bar_then_prev_close():
    assert quote_from_snapshot(snap(SNAPSHOT_NO_TRADES)).price == 120.44
    assert quote_from_snapshot(snap(SNAPSHOT_OVERNIGHT)).price == 119.49

async def test_free_key_switches_to_eod(monkeypatch):
    fake = FakeMassiveClient(
        snapshot=BadResponse('{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data."}'),
        grouped={"2026-11-10": {"AAPL": 227.41}, "2026-11-09": {"AAPL": 225.10}},  # 11-11 (today): no data yet
    )
    # frozen_et: datetime subclass whose now() returns Wed 2026-11-11 10:00 ET
    monkeypatch.setattr(mc, "datetime", frozen_et("2026-11-11 10:00"))
    src = MassiveDataSource("k", cache := PriceCache(), client=fake)
    await src.start(["AAPL"])
    assert src.mode == "eod"
    u = cache.get("AAPL")
    assert (u.price, u.reference_price) == (227.41, 225.10)
    assert fake.call_count == 4          # probe + 11-11 (empty) + 11-10 + 11-09 — within budget
    await src._poll_once()
    assert fake.call_count == 4          # before 17:00 ET nothing newer can exist → no calls

async def test_rate_limit_backs_off():
    fake = FakeMassiveClient(snapshot=BadResponse("You've exceeded the maximum requests per minute"))
    src = MassiveDataSource("k", PriceCache(), poll_interval=15, client=fake)
    src._mode = "snapshot"; src._tickers = ["AAPL"]
    await src._poll_once()
    assert src._interval() == 30

def test_call_budget():
    b = CallBudget(2, 60)
    assert b.try_acquire() and b.try_acquire() and not b.try_acquire()
```

`FakeMassiveClient` is a ~20-line class in `conftest.py`: `get_snapshot_all(market_type, tickers)` returns or raises the configured value (or calls it); `get_grouped_daily_aggs(date, adjusted)` looks up `grouped[date]` and builds models with `grouped(...)`. It records calls so tests can assert the free-plan call counts from §9.2.

### 14.4 Simulator

```python
def _path(sim: GBMSimulator, tickers: list[str], steps: int) -> np.ndarray:
    """Unrounded price path, shape (steps + 1, len(tickers))."""
    rows = [[sim.get_price(t) for t in tickers]]
    for _ in range(steps):
        sim.step()
        rows.append([sim.get_price(t) for t in tickers])
    return np.array(rows)

def test_log_return_volatility_matches_sigma():
    sim = GBMSimulator(["AAPL"], event_probability=0.0, seed=42)
    r = np.diff(np.log(_path(sim, ["AAPL"], 20_000)[:, 0]))
    assert r.std() == pytest.approx(0.22 * math.sqrt(GBMSimulator.DEFAULT_DT), rel=0.05)

def test_sector_correlation():
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0.0, seed=7)
    c = np.corrcoef(np.diff(np.log(_path(sim, ["AAPL", "MSFT", "JPM"], 8_000)), axis=0).T)
    assert c[0, 1] == pytest.approx(0.6, abs=0.05) and c[0, 2] == pytest.approx(0.3, abs=0.05)

def test_full_default_set_factorises():
    assert GBMSimulator(list(SEED_PRICES))._cholesky is not None

def test_unknown_seed_is_deterministic():
    assert seed_price_for("PYPL") == seed_price_for("PYPL") and 50 <= seed_price_for("PYPL") < 300

def test_initial_prices_override():
    assert GBMSimulator(["PYPL"], initial_prices={"PYPL": 71.5}).get_price("PYPL") == 71.5
```

### 14.5 SSE

```python
async def test_sse_sends_snapshot_then_skips_when_unchanged():
    cache = PriceCache(); cache.update("AAPL", 190.0, reference_price=190.0)
    req = FakeRequest(disconnect_after=3)              # is_disconnected() → True on 3rd call
    events = [e async for e in generate_price_events(cache, req, interval=0)]
    assert events[0] == "retry: 1000\n\n"
    data = [e for e in events if e.startswith("id:")]
    assert len(data) == 1                              # version unchanged → no repeat
    assert json.loads(data[0].split("data: ", 1)[1])["AAPL"]["day_change_percent"] == 0.0

def test_router_factory_is_reentrant():
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert len(r1.routes) == len(r2.routes) == 1
```

Commands: `uv run --extra dev pytest tests/market -v --cov=app.market` and `uv run --extra dev ruff check app tests`. Target coverage ≥90% for `app/market` (Massive is now fully testable through the fake client).

### 14.6 E2E hooks (PLAN §12)

E2E tests run with the simulator (no key in `test/docker-compose.test.yml`). Assert: SSE delivers all 10 default tickers within 2 s; at least one price changes within 5 s; adding `PYPL` shows a price within 1 s.

---

## 15. Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | empty | Non-blank → Massive; mode auto-detected |
| `MASSIVE_POLL_INTERVAL` | env | `15` | Snapshot mode only; min 1 s |
| EOD poll interval | `MassiveDataSource(eod_poll_interval=)` | `300` s | Free plan |
| Free-plan budget | `MassiveDataSource.FREE_CALLS_PER_MINUTE` | `4` | One below the 5/min limit |
| Max back-off | `MassiveDataSource.MAX_BACKOFF` | `60` s | 429 handling |
| Simulator tick | `SimulatorDataSource(update_interval=)` | `0.5` s | |
| Event probability | `SimulatorDataSource(event_probability=)` | `0.001` | ≈ 1 event / 50 s across 10 tickers |
| GBM `dt` | `GBMSimulator(dt=)` | `0.5 / 5,896,800` | One tick = 0.5 s of trading time |
| RNG seed | `SimulatorDataSource(seed=)` | `None` | Tests and reproducible demos |
| SSE push / heartbeat | `stream.py` | `0.5` s / `15` s | `retry: 1000` ms |

---

## 16. Edge cases and failure modes

| Situation | Behaviour |
|---|---|
| Empty watchlist and no positions | Sources idle; Massive makes no calls; SSE sends `data: {}` |
| Trade on an unpriced ticker | Route calls `add_ticker`. The simulator prices it at once; for Massive the route returns 400 "try again shortly" |
| Invalid ticker added (Massive) | Absent from the snapshot. One WARNING is logged and no price is shown; the watchlist entry stays, and the user or LLM can remove it |
| Invalid ticker added (simulator) | It's simulated like any other. Acceptable: the simulator can't know real symbols; route-level regex validation rejects garbage |
| Bad `MASSIVE_API_KEY` | ERROR logged; retried every 300 s; SSE connected with no prices; `/api/health` shows `massive_mode: null` |
| Free key, market closed or weekend | EOD mode shows the last trading day's closes; nothing flashes. This is expected |
| Paid key overnight (snapshot cleared ~midnight–4 AM ET) | `quote_from_snapshot` falls back to `prev_day.close` |
| 429 from Massive | Interval doubles up to 60 s and resets after a successful poll |
| Network outage | Exception logged; the cache keeps the last prices; polling continues |
| Ticker removed during an in-flight poll | `_write_quotes` writes only tickers still tracked, so nothing reappears |
| Container restart (simulator) | Prices restart from seed (or `avg_cost` for held tickers); `reference_price` resets, so day change is measured from the restart |
| Many SSE clients | Each client is one coroutine polling an int under a lock; fine for dozens |
