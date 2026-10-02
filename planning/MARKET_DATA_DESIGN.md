# Market Data Backend — Design & Implementation Guide

This document is the implementation blueprint for FinAlly's market data layer: the **unified interface**, the **GBM simulator**, the **Massive (Polygon.io) client**, the shared **price cache**, and the **SSE stream** that fans prices out to the browser. It is written against `planning/PLAN.md` (sections 5, 6, 8, 10) and reflects the code that lives in `backend/app/market/`.

> Related docs: `MARKET_DATA_SUMMARY.md` (status), `archive/MASSIVE_API.md` (API reference), `archive/MARKET_DATA_REVIEW.md` (review findings, all resolved). `archive/MARKET_DATA_DESIGN.md` is the earlier, longer draft; this document supersedes it and adds the extensions in section 11 (daily change %, ticker validation, watchlist coordination).

## Table of Contents

1. [Goals and constraints](#1-goals-and-constraints)
2. [Architecture](#2-architecture)
3. [File layout](#3-file-layout)
4. [Data model — `PriceUpdate`](#4-data-model--priceupdate)
5. [Price cache — `PriceCache`](#5-price-cache--pricecache)
6. [Unified interface — `MarketDataSource`](#6-unified-interface--marketdatasource)
7. [Simulator](#7-simulator)
8. [Massive API client](#8-massive-api-client)
9. [Factory and configuration](#9-factory-and-configuration)
10. [SSE streaming](#10-sse-streaming)
11. [Extensions needed by the rest of the app](#11-extensions-needed-by-the-rest-of-the-app)
12. [FastAPI lifecycle integration](#12-fastapi-lifecycle-integration)
13. [Consuming prices from other modules](#13-consuming-prices-from-other-modules)
14. [Frontend contract](#14-frontend-contract)
15. [Testing](#15-testing)
16. [Error handling and edge cases](#16-error-handling-and-edge-cases)
17. [Implementation checklist](#17-implementation-checklist)

---

## 1. Goals and constraints

| Requirement (from PLAN.md) | Design response |
|---|---|
| One interface, two data sources, selected by `MASSIVE_API_KEY` | `MarketDataSource` ABC + `create_market_data_source()` factory |
| Simulator: GBM, ~500 ms ticks, correlated, occasional 2–5% events, realistic seeds | `GBMSimulator` with Cholesky-correlated shocks and per-tick event probability |
| Massive: REST polling (not WebSocket), union of watched tickers, free tier 5 calls/min | One `get_snapshot_all` call per poll for *all* tickers; 15 s default interval |
| Shared price cache that SSE reads from | `PriceCache` — single writer-agnostic store with a version counter |
| SSE `GET /api/stream/prices`, ~500 ms cadence, native `EventSource` | Version-gated generator, `retry: 1000` directive |
| Downstream code is source-agnostic | Only `PriceUpdate` and `PriceCache` cross the module boundary |
| No external deps for default mode | Simulator needs only `numpy`; Massive is used only when a key is set |

Non-goals: tick history persistence (sparkline history is built client-side), historical bars, order books, multi-user fan-out.

## 2. Architecture

```
                     ┌──────────────────────────┐
  env MASSIVE_API_KEY│ create_market_data_source│
          ───────────►        (factory)         │
                     └────────────┬─────────────┘
                                  │ returns
                 ┌────────────────┴────────────────┐
                 ▼                                 ▼
      SimulatorDataSource                  MassiveDataSource
      asyncio task, every 0.5 s            asyncio task, every 15 s
      GBMSimulator.step()                  to_thread(get_snapshot_all)
                 │                                 │
                 └──────────────┬──────────────────┘
                                │ cache.update(ticker, price[, timestamp])
                                ▼
                         ┌─────────────┐
                         │  PriceCache │  latest PriceUpdate per ticker
                         │  + version  │  (thread-safe, in-memory)
                         └──────┬──────┘
          ┌─────────────────────┼──────────────────────┐
          ▼                     ▼                      ▼
   SSE /api/stream/prices   Portfolio valuation   Trade execution
   (browser EventSource)    (GET /api/portfolio)  (fill at cache price)
```

Key principles:

1. **Producers push, consumers pull.** A data source never returns prices to a caller; it writes into the cache on its own schedule. Everything else reads the cache. This is what makes the two sources interchangeable.
2. **The cache holds only the latest value per ticker.** Memory is O(tickers). History for sparklines lives in the browser.
3. **Exactly one source is active** per process, chosen once at startup.

## 3. File layout

```
backend/app/market/
├── __init__.py        # Public exports
├── models.py          # PriceUpdate (frozen dataclass)
├── cache.py           # PriceCache
├── interface.py       # MarketDataSource ABC
├── seed_prices.py     # SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py       # GBMSimulator + SimulatorDataSource
├── massive_client.py  # MassiveDataSource
├── factory.py         # create_market_data_source()
└── stream.py          # create_stream_router() — SSE
```

Public surface (`app/market/__init__.py`):

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = ["PriceUpdate", "PriceCache", "MarketDataSource",
           "create_market_data_source", "create_stream_router"]
```

Dependencies (`backend/pyproject.toml`): `fastapi`, `uvicorn[standard]`, `numpy`, `massive`. `massive` is a core dependency (imported at module top level) so tests and the factory never need lazy-import tricks.

## 4. Data model — `PriceUpdate`

The only value that leaves the market layer. Immutable, so it can be handed to the SSE generator and the portfolio code without copying or locking.

```python
# app/market/models.py
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
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
    def direction(self) -> str:          # "up" | "down" | "flat"
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

Semantics to be aware of:

- `previous_price` is the price **from the previous cache update**, not yesterday's close. It drives the green/red flash animation, not the "daily change %" column. See section 11.1 for how to add the day-level figure.
- `direction` is derived, never stored, so it can't disagree with the prices.
- Timestamps are Unix **seconds** (floats). Massive returns milliseconds; the client converts.

Example payload (`to_dict()`):

```json
{"ticker": "AAPL", "price": 190.52, "previous_price": 190.47, "timestamp": 1760000000.123,
 "change": 0.05, "change_percent": 0.0263, "direction": "up"}
```

## 5. Price cache — `PriceCache`

```python
# app/market/cache.py
from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0               # bumped on every write

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price   # first tick → flat
            update = PriceUpdate(ticker=ticker,
                                 price=round(price, 2),
                                 previous_price=round(previous_price, 2),
                                 timestamp=ts)
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        u = self.get(ticker)
        return u.price if u else None

    def get_all(self) -> dict[str, PriceUpdate]:
        with self._lock:
            return dict(self._prices)       # shallow copy; values are immutable

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)

    @property
    def version(self) -> int:
        with self._lock:                    # keep consistent with the other accessors
            return self._version

    def __len__(self) -> int: ...
    def __contains__(self, ticker: str) -> bool: ...
```

Why each choice:

- **`threading.Lock`, not `asyncio.Lock`.** `MassiveDataSource` calls the synchronous SDK inside `asyncio.to_thread`, and FastAPI may call the cache from sync routes in worker threads. A thread lock is correct for both; the critical sections are microseconds, so blocking the loop is not a concern.
- **Version counter.** The SSE generator wakes every 500 ms but only serializes when `version` changed. With Massive polling every 15 s this avoids sending identical payloads 29 times out of 30.
- **Prices rounded to 2 dp at the boundary.** Internal GBM state keeps full precision; everything downstream sees cents. Portfolio math should treat the cache price as the authoritative fill price.
- **`remove()` on watchlist removal** keeps the stream from advertising tickers the user no longer watches. (If the user still holds a position in the ticker, see section 11.3.)

## 6. Unified interface — `MarketDataSource`

```python
# app/market/interface.py
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing updates. Called exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Cancel background work. Idempotent."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a ticker. No-op if present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking and evict from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""
```

Contract every implementation must honor:

| Rule | Why |
|---|---|
| `start()` primes the cache before returning (simulator: seed prices; Massive: one immediate poll) | The first SSE frame and the first trade must never see an empty cache |
| `add_ticker` / `remove_ticker` normalize case (`upper().strip()`) or leave that to the caller — be consistent | Cache keys are case-sensitive |
| Background loops catch and log all exceptions except `CancelledError` | One bad tick must never kill price streaming |
| `stop()` cancels the task and awaits it, swallowing `CancelledError` | Clean shutdown under uvicorn/pytest |
| `remove_ticker` calls `cache.remove(ticker)` | Single place that keeps cache and tracked set in sync |

Minimal usage, identical for both sources:

```python
cache = PriceCache()
source = create_market_data_source(cache)
await source.start(["AAPL", "MSFT"])
cache.get("AAPL")                 # PriceUpdate(...), available immediately
await source.add_ticker("PYPL")
await source.remove_ticker("MSFT")
await source.stop()
```

## 7. Simulator

### 7.1 Seed data and parameters

```python
# app/market/seed_prices.py
SEED_PRICES = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00,  "V": 280.00,    "NFLX": 600.00,
}

# sigma = annualized volatility, mu = annualized drift
TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05}, "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05}, "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03}, "NVDA":  {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05}, "JPM":   {"sigma": 0.18, "mu": 0.04},
    "V":    {"sigma": 0.17, "mu": 0.04}, "NFLX":  {"sigma": 0.35, "mu": 0.05},
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}      # dynamically added tickers

CORRELATION_GROUPS = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}
INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3        # cross-sector and unknown tickers
TSLA_CORR = 0.3               # TSLA is in no group; it moves mostly on its own
```

Tickers outside `SEED_PRICES` (user-added, e.g. `PYPL`) start at `random.uniform(50, 300)` with `DEFAULT_PARAMS`.

### 7.2 The math

Geometric Brownian Motion, discretized exactly (log-normal step, so prices can never go negative):

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

- `dt = 0.5 s / (252 · 6.5 · 3600 s) ≈ 8.48e-8` — a 500 ms tick expressed as a fraction of a trading year. Per-tick moves are sub-cent for AAPL-like volatility, but accumulate to realistic daily ranges (≈ σ/√252 ≈ 1.4% for AAPL).
- `Z` is **correlated** across tickers: draw `z ~ N(0, I)`, then `Z = L·z` where `L = cholesky(C)` and `C` is the correlation matrix built from the sector groups. Each `Z_i` is still standard normal, but pairs co-move with the configured correlation.
- **Events:** each tick, each ticker has `event_probability = 0.001` of an extra multiplicative shock of ±(2–5)%. With 10 tickers at 2 ticks/s that is roughly one event every ~50 s across the board.

Sanity check you can run: with σ = 0.22 and the default `dt`, one-tick log-return std is `0.22·√8.48e-8 ≈ 6.4e-5` (~1.2 cents on $190). Over 28,800 ticks (4 hours) the std grows to `6.4e-5·√28800 ≈ 1.1%`.

### 7.3 `GBMSimulator`

```python
# app/market/simulator.py (excerpt)
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers, dt=DEFAULT_DT, event_probability=0.001):
        self._dt, self._event_prob = dt, event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for t in tickers:
            self._add_ticker_internal(t)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Hot path (called every 500 ms)."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z                       # correlate

        out = {}
        for i, t in enumerate(self._tickers):
            mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[t] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:        # dramatic move
                self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])

            out[t] = round(self._prices[t], 2)
        return out

    def add_ticker(self, ticker):    # idempotent; rebuilds Cholesky
        if ticker in self._prices: return
        self._add_ticker_internal(ticker); self._rebuild_cholesky()

    def remove_ticker(self, ticker): # idempotent; rebuilds Cholesky
        if ticker not in self._prices: return
        self._tickers.remove(ticker); del self._prices[ticker]; del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker): return self._prices.get(ticker)
    def get_tickers(self): return list(self._tickers)
```

Correlation matrix construction (rebuilt only when the ticker set changes — O(n²), n < 50):

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None
        return
    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            corr[i, j] = corr[j, i] = self._pairwise_correlation(self._tickers[i], self._tickers[j])
    self._cholesky = np.linalg.cholesky(corr)

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

All correlations are in (0, 1) with unit diagonal, so the matrix is positive definite and `cholesky` never fails for any ticker set (a test in section 15 pins this for the full default universe plus unknown tickers).

### 7.4 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001):
        self._cache, self._interval, self._event_prob = price_cache, update_interval, event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers):
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for t in tickers:                                   # prime the cache
            self._cache.update(ticker=t, price=self._sim.get_price(t))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker):
        if self._sim:
            self._sim.add_ticker(ticker)
            self._cache.update(ticker=ticker, price=self._sim.get_price(ticker))  # instant price

    async def remove_ticker(self, ticker):
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self):
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self):
        while True:
            try:
                if self._sim:
                    for t, p in self._sim.step().items():
                        self._cache.update(ticker=t, price=p)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Notes:

- `step()` is pure CPU and takes well under a millisecond for 10–50 tickers, so it runs directly on the event loop; no thread is needed.
- Seeding `add_ticker` immediately means "add TSLA → buy 10 TSLA" in one chat turn always finds a price.
- `sleep(interval)` after the work gives ~500 ms + step time; drift is irrelevant for a simulator.

## 8. Massive API client

### 8.1 What we call

One endpoint, one call per poll — the full-market snapshot filtered to our tickers:

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=key)          # sends Authorization: Bearer <key>
snaps = client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS,
                                tickers=["AAPL", "GOOGL", "MSFT"])
for s in snaps:
    s.ticker                      # "AAPL"
    s.last_trade.price            # 125.07            ← cache price
    s.last_trade.timestamp        # 1675190399000     ← Unix **milliseconds**
    s.day.previous_close          # 129.61            ← used in section 11.1
    s.day.change_percent          # -3.50             ← used in section 11.1
```

Rate budget: free tier = 5 calls/min → a 15 s interval uses 4 calls/min (one spare for ad-hoc calls). Paid tiers can use 2–5 s. The call count is independent of ticker count, which is why we use the batch snapshot rather than per-ticker `get_last_trade`.

### 8.2 `MassiveDataSource`

```python
# app/market/massive_client.py
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key, price_cache, poll_interval: float = 15.0):
        self._api_key, self._cache, self._interval = api_key, price_cache, poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers):
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()                                   # prime the cache
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self): ...                                      # same cancel/await pattern

    async def add_ticker(self, ticker):
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)                           # appears on next poll

    async def remove_ticker(self, ticker):
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)

    def get_tickers(self): return list(self._tickers)

    async def _poll_loop(self):
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self):
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)   # SDK is synchronous
            for snap in snapshots:
                try:
                    self._cache.update(ticker=snap.ticker,
                                       price=snap.last_trade.price,
                                       timestamp=snap.last_trade.timestamp / 1000.0)  # ms → s
                except (AttributeError, TypeError) as e:
                    logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        except Exception as e:
            logger.error("Massive poll failed: %s", e)      # 401 / 403 / 429 / network → retry next tick

    def _fetch_snapshots(self):
        return self._client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS,
                                             tickers=self._tickers)
```

Design points:

- **Never block the event loop.** The SDK uses `requests`-style blocking I/O; `asyncio.to_thread` isolates it, and that is the reason `PriceCache` uses a thread lock.
- **Per-snapshot try/except.** One malformed record (a ticker with no `last_trade`, e.g. a halted or brand-new symbol) must not drop the other nine.
- **Errors never propagate out of `_poll_once`.** The loop simply tries again at the next interval. Persistent failure (bad key) shows up as a stale cache and repeated `ERROR` logs; see 16.3.
- **`add_ticker` is lazy.** The new symbol has no price until the next poll (up to 15 s on the free tier). Section 11.2 describes the optional immediate refresh.
- **Stale prices after hours.** `last_trade.price` is the last print, so after the close prices stop changing; `PriceUpdate.direction` becomes `flat`, which is correct. The SSE stream still emits on version change, so it quietly goes idle.
- **Cache timestamps come from the exchange** (`last_trade.timestamp`), not from `time.time()`. The `timestamp or time.time()` fallback in `PriceCache.update` only applies when the SDK returns 0/None.
- **Same-price polls still bump the version.** `update()` always increments the counter, so SSE emits a frame every poll even if nothing changed. That is harmless at 15 s and keeps the connection warm.

## 9. Factory and configuration

```python
# app/market/factory.py
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

Returns an **unstarted** source; the caller awaits `start(tickers)`.

| Variable | Effect |
|---|---|
| `MASSIVE_API_KEY` unset / empty / whitespace | Simulator (default) |
| `MASSIVE_API_KEY=<key>` | Massive REST polling, 15 s interval |

Optional knobs to add (not yet in code; trivial to wire through the factory):

```python
poll = float(os.environ.get("MASSIVE_POLL_INTERVAL", "15"))
return MassiveDataSource(api_key=api_key, price_cache=price_cache, poll_interval=poll)
```

Paid-tier users set `MASSIVE_POLL_INTERVAL=2`. Clamp to a minimum (e.g. 1 s) to protect against typos.

## 10. SSE streaming

```python
# app/market/stream.py
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])     # create inside the factory

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                     "X-Accel-Buffering": "no"},
        )
    return router


async def _generate_events(price_cache, request, interval: float = 0.5) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"                     # browser reconnect delay
    last_version = -1
    try:
        while True:
            if await request.is_disconnected():
                break
            v = price_cache.version
            if v != last_version:
                last_version = v
                prices = price_cache.get_all()
                if prices:
                    yield f"data: {json.dumps({t: u.to_dict() for t, u in prices.items()})}\n\n"
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
```

> The current code defines `router` at module level and registers the route inside the factory, so calling `create_stream_router` twice in one process (e.g. two test apps) registers `/prices` twice. Move `router = APIRouter(...)` inside the function as shown above. Behavior is otherwise identical.

Wire format — one event per frame, containing **all** tickers (the watchlist is ~10–30 symbols, so a full snapshot is a few KB and simplifies client state):

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":190.52,"previous_price":190.47,"timestamp":1760000000.1,"change":0.05,"change_percent":0.0263,"direction":"up"},"GOOGL":{...}}

data: {...}
```

Why poll-the-version instead of an event-driven queue: it needs no per-client subscription bookkeeping, tolerates slow clients (they just skip intermediate versions), and the 500 ms cadence matches the simulator tick. For Massive (15 s updates) the stream emits one frame per poll.

**Heartbeats:** idle proxies may close a silent connection. The current generator only writes on version change. During market-closed Massive polling it still emits every poll (see section 8), so no extra heartbeat is required; if a lower-frequency source is ever added, yield `": keepalive\n\n"` every ~15 s.

## 11. Extensions needed by the rest of the app

The existing module covers the PLAN's market section. The remaining plan items depend on three small additions.

### 11.1 Daily change % for the watchlist

PLAN §10 requires a **daily change %** column. `PriceUpdate.change_percent` is per-tick (relative to the last update), which would show ~0.00% in the simulator. Add a session baseline to the cache:

```python
# cache.py additions
class PriceCache:
    def __init__(self):
        ...
        self._baselines: dict[str, float] = {}      # reference ("open") price per ticker

    def set_baseline(self, ticker: str, price: float) -> None:
        with self._lock:
            self._baselines[ticker] = price

    def baseline(self, ticker: str) -> float | None:
        with self._lock:
            return self._baselines.get(ticker)
```

```python
# models.py: carry the baseline on the update
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)
    day_open: float | None = None            # baseline for daily change

    @property
    def day_change_percent(self) -> float:
        if not self.day_open:
            return 0.0
        return round((self.price - self.day_open) / self.day_open * 100, 4)
```

`PriceCache.update` stamps `day_open=self._baselines.get(ticker, price)` (first price seen becomes the baseline if none was set) and `to_dict()` adds `"day_open"` and `"day_change_percent"`.

Where the baseline comes from:

- **Simulator:** `SEED_PRICES[ticker]` (or the first generated price). Set it in `SimulatorDataSource.start/add_ticker` right before the first `cache.update`. Day change then reads as "move since the app started", which is the right demo semantics.
- **Massive:** `snap.day.previous_close` from the same snapshot already fetched — no extra API call:

```python
prev_close = getattr(snap.day, "previous_close", None)
if prev_close:
    self._cache.set_baseline(snap.ticker, prev_close)
self._cache.update(ticker=snap.ticker, price=snap.last_trade.price, timestamp=...)
```

Keep `change`/`direction` per-tick (they power the flash); the frontend displays `day_change_percent` in the watchlist.

### 11.2 Ticker validation and immediate pricing on add

`POST /api/watchlist` and LLM `watchlist_changes` must not accept symbols that will never get a price. Add a helper that works for both sources:

```python
async def ensure_priced(source: MarketDataSource, cache: PriceCache, ticker: str,
                        timeout: float = 3.0) -> bool:
    """Add the ticker and wait until the cache has a price. False → unknown symbol."""
    ticker = ticker.upper().strip()
    await source.add_ticker(ticker)
    if isinstance(source, MassiveDataSource):
        await source.refresh()                      # one immediate out-of-cycle poll
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ticker in cache:
            return True
        await asyncio.sleep(0.05)
    await source.remove_ticker(ticker)              # roll back
    return False
```

with a public `refresh()` on `MassiveDataSource` (just `await self._poll_once()`). The simulator accepts any symbol (it invents a seed price), so for `SimulatorDataSource` validation reduces to a format check: `^[A-Z]{1,5}(\.[A-Z])?$`. Do the regex check in the API layer for both sources before calling the market layer. On Massive, `refresh()` costs one of the five free-tier calls per minute; throttle watchlist adds accordingly (the poll loop's 15 s cadence leaves one spare call per minute).

### 11.3 Watchlist ↔ market-source coordination

The market source tracks the **union** of watchlist tickers and tickers with open positions. A user who sells nothing but removes a held ticker from the watchlist must still have valuation prices.

| Event | Action |
|---|---|
| App startup | Read watchlist + positions from SQLite → `await source.start(sorted(union))` |
| `POST /api/watchlist {ticker}` | validate (11.2) → insert row → `source.add_ticker` |
| `DELETE /api/watchlist/{t}` | delete row → if no open position in `t`: `source.remove_ticker(t)`, else keep tracking |
| Trade opens a position in a ticker not on the watchlist | `source.add_ticker(t)` before filling |
| Trade closes the last share of `t` and `t` not on watchlist | `source.remove_ticker(t)` |
| LLM `watchlist_changes` | goes through exactly the same service functions as the REST routes |

Put this logic in one service module (e.g. `app/services/watchlist.py`) so the REST routes and the chat executor share it. The market package stays unaware of the database.

## 12. FastAPI lifecycle integration

```python
# app/main.py
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()
market_source = create_market_data_source(price_cache)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()                                       # lazy schema + seed (PLAN §7)
    tickers = load_tracked_tickers()                # watchlist ∪ open positions
    await market_source.start(tickers)
    app.state.price_cache = price_cache
    app.state.market_source = market_source
    snapshot_task = asyncio.create_task(snapshot_loop(price_cache))   # 30 s portfolio snapshots
    try:
        yield
    finally:
        snapshot_task.cancel()
        await market_source.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
# ... other /api routers ...
app.mount("/", StaticFiles(directory="static", html=True), name="static")   # LAST
```

Notes:

- `create_market_data_source` runs at import time only because it is cheap and unstarted; `start()` happens in `lifespan` where an event loop exists.
- Mount static files **after** all `/api` routers or the catch-all swallows them.
- Routes obtain the cache via `request.app.state.price_cache` (or a FastAPI dependency); tests substitute their own cache.

```python
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache
```

## 13. Consuming prices from other modules

**Trade execution** — fills at the cache price; no price, no trade:

```python
def execute_trade(cache: PriceCache, db, ticker: str, side: str, quantity: float):
    price = cache.get_price(ticker)
    if price is None:
        raise TradeError(f"No live price for {ticker}")      # surface to API / LLM
    cost = price * quantity
    ...                                                      # cash / position validation, DB writes
    return {"ticker": ticker, "side": side, "quantity": quantity, "price": price}
```

**Portfolio valuation** — mark positions to the cache; fall back to avg cost if a price is momentarily missing so the header never shows NaN:

```python
def portfolio_value(cache, cash, positions):
    total = cash
    for p in positions:
        price = cache.get_price(p.ticker) or p.avg_cost
        total += price * p.quantity
    return total
```

**LLM context** — build the watchlist-with-prices block from `cache.get_all()` filtered to watchlist tickers.

Always treat `get_price` returning `None` as a real case (new ticker on Massive before first poll, removed ticker).

## 14. Frontend contract

The frontend knows nothing about Python or the data source. It opens one `EventSource`, merges each frame into a ticker-keyed map, appends to per-ticker sparkline buffers, and toggles a flash class.

```ts
type Tick = {
  ticker: string; price: number; previous_price: number; timestamp: number;
  change: number; change_percent: number; direction: "up" | "down" | "flat";
  day_open?: number; day_change_percent?: number;
};

const es = new EventSource("/api/stream/prices");          // same origin, no CORS
es.onopen  = () => setStatus("connected");                 // green dot
es.onerror = () => setStatus(es.readyState === EventSource.CONNECTING ? "reconnecting" : "disconnected");
es.onmessage = (e) => {
  const frame: Record<string, Tick> = JSON.parse(e.data);
  for (const t of Object.values(frame)) {
    pushSpark(t.ticker, t.price);                           // sparkline accumulated since page load
    if (t.direction !== "flat") flash(t.ticker, t.direction);   // add .flash-up/.flash-down, remove after 500 ms
  }
  setPrices(frame);
};
```

`EventSource` handles reconnection itself (the server's `retry: 1000` sets the delay). Tickers removed from the watchlist simply stop appearing in frames; the client should drop them from state by diffing keys rather than waiting for an explicit delete event.

## 15. Testing

Existing suite: 73 tests under `backend/tests/market/` (models, cache, simulator, simulator source, factory, massive). `asyncio_mode = "auto"` is set, so async tests need no decorator. Add the cases below to close the gaps called out in the review.

**Simulator math and correlation**

```python
def test_prices_stay_positive_over_long_run():
    sim = GBMSimulator(list(SEED_PRICES))
    for _ in range(50_000):
        prices = sim.step()
    assert all(p > 0 for p in prices.values())

def test_cholesky_succeeds_for_full_default_universe_plus_unknowns():
    sim = GBMSimulator(list(SEED_PRICES) + ["PYPL", "XYZ"])
    assert sim._cholesky is not None

def test_tech_pair_more_correlated_than_cross_sector():
    np.random.seed(0)
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0)
    r = {t: [] for t in sim.get_tickers()}
    prev = {t: sim.get_price(t) for t in r}
    for _ in range(5000):
        for t, p in sim.step().items():
            r[t].append(math.log(p / prev[t])); prev[t] = p
    c = np.corrcoef([r["AAPL"], r["MSFT"], r["JPM"]])
    assert c[0, 1] > c[0, 2]            # AAPL~MSFT (0.6) > AAPL~JPM (0.3)
```

(Rounding to cents adds noise; use `dt` scaled up, e.g. `GBMSimulator(..., dt=1e-4)`, if the correlation check is flaky.)

**Cache concurrency**

```python
def test_cache_thread_safety():
    cache = PriceCache()
    def writer(tid):
        for i in range(2000):
            cache.update(f"T{tid}", 100 + i * 0.01)
    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    assert cache.version == 8 * 2000
    assert len(cache) == 8
```

**Massive client (mocked SDK)** — no network, no key:

```python
def _snap(ticker, price, ts_ms, prev_close=100.0):
    return SimpleNamespace(ticker=ticker,
                           last_trade=SimpleNamespace(price=price, timestamp=ts_ms),
                           day=SimpleNamespace(previous_close=prev_close))

async def test_poll_converts_ms_to_seconds_and_skips_bad_snapshots():
    cache = PriceCache()
    src = MassiveDataSource("k", cache)
    src._tickers = ["AAPL", "BAD"]
    src._client = object()                               # non-None so _poll_once proceeds
    src._fetch_snapshots = lambda: [_snap("AAPL", 190.5, 1_700_000_000_000),
                                    SimpleNamespace(ticker="BAD", last_trade=None)]
    await src._poll_once()
    u = cache.get("AAPL")
    assert u.price == 190.5 and u.timestamp == 1_700_000_000.0
    assert "BAD" not in cache

async def test_poll_failure_does_not_raise():
    src = MassiveDataSource("k", PriceCache())
    src._tickers, src._client = ["AAPL"], object()
    def boom(): raise RuntimeError("429")
    src._fetch_snapshots = boom
    await src._poll_once()                               # logs, returns
```

**SSE** — use an httpx ASGI transport and read the first frames:

```python
async def test_sse_emits_snapshot_and_retry():
    cache = PriceCache(); cache.update("AAPL", 190.0)
    app = FastAPI(); app.include_router(create_stream_router(cache))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        async with c.stream("GET", "/api/stream/prices") as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            lines = []
            async for line in r.aiter_lines():
                lines.append(line)
                if line.startswith("data:"):
                    break
    assert "retry: 1000" in lines
    assert json.loads(lines[-1][5:])["AAPL"]["price"] == 190.0
```

Add `httpx` to the `dev` extra. (`ASGITransport` buffers streaming bodies in some httpx versions; if the first read hangs, drive `_generate_events` directly with a stub request whose `is_disconnected()` returns `False` then `True`.)

**Factory:** parametrize `MASSIVE_API_KEY` over `""`, `"   "`, unset (→ `SimulatorDataSource`) and `"abc"` (→ `MassiveDataSource`) using `monkeypatch.setenv/delenv`.

**Baseline/daily change** (section 11.1): first update sets `day_open == price`; `set_baseline` before first update is honored; `day_change_percent` computed from the baseline, not the previous tick.

Run: `cd backend && uv run --extra dev pytest -v` and `uv run --extra dev ruff check app/ tests/`.

## 16. Error handling and edge cases

### 16.1 Empty watchlist at startup
`SimulatorDataSource.start([])` builds an empty simulator; `step()` returns `{}`. `MassiveDataSource._poll_once` returns early when no tickers. SSE emits nothing until a ticker is added (it skips empty snapshots). Fine.

### 16.2 Cache miss during a trade
Raise a clear error (`No live price for XYZ`) and return it to the user/LLM. Never fill at 0 or at a stale guess.

### 16.3 Invalid or restricted Massive key
| Symptom | Cause | Behavior |
|---|---|---|
| `401` in logs every poll | bad key | Cache stays empty/stale; app still serves UI. Surface in `/api/health` (below) |
| `403` | plan lacks snapshot endpoint | same |
| `429` | polling too fast for tier | next poll succeeds; raise `poll_interval` |

Expose staleness in the health route so a bad key is visible:

```python
@app.get("/api/health")
async def health():
    newest = max((u.timestamp for u in price_cache.get_all().values()), default=None)
    return {"status": "ok", "source": type(market_source).__name__,
            "tickers": len(price_cache), "last_price_ts": newest}
```

### 16.4 Thread safety under load
All cache access is under one lock; `get_all()` returns a shallow copy of immutable values, so SSE can serialize outside the lock. Simulator writes ~10 updates per 500 ms; contention is negligible.

### 16.5 Simulator precision
Internal prices are full-precision floats; only the cache boundary rounds to cents. Tiny per-tick moves therefore often yield `direction == "flat"` for low-vol tickers (a 1.2¢ std on a $190 stock rounds to 0–2 cents). That produces the intended "mostly small flickers with occasional visible moves" look; do not increase `dt` just to force changes.

### 16.6 Case and symbol hygiene
Normalize to upper case at the API boundary (`ticker.strip().upper()`). The simulator does **not** normalize internally, so `"aapl"` would create a second, unseeded symbol.

### 16.7 Shutdown
`stop()` cancels the background task and awaits it. The Massive thread in `to_thread` can't be cancelled mid-HTTP-call, so shutdown may wait for one in-flight request (bounded by the SDK's timeout).

### 16.8 Market closed / weekends (Massive)
Prices freeze at the last print; all tickers show `flat`. Day change % (11.1) shows the session's final move. No special handling required, but the UI may want to show a "market closed" hint using the stale `timestamp`.

## 17. Implementation checklist

Already implemented in `backend/app/market/` (verified by 73 passing tests): models, cache, interface, seed data, GBM simulator + data source, Massive client, factory, SSE router, Rich demo (`backend/market_data_demo.py`).

Remaining work derived from this design:

- [ ] Move `router = APIRouter(...)` inside `create_stream_router` (section 10)
- [ ] Take the lock in `PriceCache.version` (section 5)
- [ ] Add baseline / `day_open` / `day_change_percent` and wire it from seed prices (simulator) and `day.previous_close` (Massive) (11.1)
- [ ] Add `MassiveDataSource.refresh()` and the `ensure_priced` validation helper (11.2)
- [ ] Optional `MASSIVE_POLL_INTERVAL` env var in the factory (section 9)
- [ ] Build the watchlist/positions coordination service (11.3) and the `lifespan` wiring (section 12)
- [ ] `GET /api/health` with source name and last price timestamp (16.3)
- [ ] Add tests from section 15 (SSE, concurrency, Cholesky for full universe, Massive error paths, baseline); add `httpx` to dev extras
- [ ] Frontend: `EventSource` hook with connection-status dot, per-ticker sparkline buffers, flash classes (section 14)
