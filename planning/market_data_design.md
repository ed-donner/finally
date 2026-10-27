# Market Data Backend — Detailed Design

**Scope:** everything under `backend/app/market/` — the unified market data API, the GBM simulator, the Massive (Polygon.io) REST client, the shared price cache, the SSE stream, and how the rest of the backend (watchlist, portfolio, chat) plugs into it.

**Audience:** the Backend agent implementing or extending market data, and any agent writing code that consumes prices.

**Relationship to other docs:**
- `planning/PLAN.md` §6 is the requirement. This doc is the implementation design that satisfies it.
- `planning/MARKET_DATA_SUMMARY.md` describes what is already in `backend/app/market/`. This doc keeps that public API and **adds a set of changes** (listed in §14). The most important one fixes a bug that stops the Massive client from ever writing a price (§8.2).
- `planning/archive/*` are the earlier design notes. Where they disagree with this doc, this doc wins. In particular, `archive/MASSIVE_API.md` uses snapshot field names (`day.previous_close`, `day.change_percent`, `last_trade.timestamp`) that **do not exist** in the `massive` Python package (verified against `massive==2.2.0`).

---

## Table of Contents

1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [Module Layout](#3-module-layout)
4. [Data Model — `PriceUpdate`](#4-data-model--priceupdate)
5. [Price Cache — `PriceCache`](#5-price-cache--pricecache)
6. [Unified Interface — `MarketDataSource`](#6-unified-interface--marketdatasource)
7. [Simulator — `GBMSimulator` + `SimulatorDataSource`](#7-simulator)
8. [Massive API — `MassiveDataSource`](#8-massive-api--massivedatasource)
9. [Factory and Configuration](#9-factory-and-configuration)
10. [SSE Streaming — `GET /api/stream/prices`](#10-sse-streaming)
11. [FastAPI Integration](#11-fastapi-integration)
12. [Watchlist and Portfolio Coordination](#12-watchlist-and-portfolio-coordination)
13. [Testing](#13-testing)
14. [Change List vs Current Code](#14-change-list-vs-current-code)

---

## 1. Requirements

From `PLAN.md` §6 and §10, the market data layer must:

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Two implementations (simulator, Massive) behind **one interface**; downstream code doesn't know which one is running | §6, §9 |
| R2 | Pick the source from env: `MASSIVE_API_KEY` set and non-empty → Massive, otherwise simulator | §9 |
| R3 | Simulator: GBM, ~500 ms ticks, correlated moves, occasional 2–5 % "events", realistic seed prices | §7 |
| R4 | Massive: REST polling (not WebSocket), one call for all tickers, 15 s on free tier, 2–15 s on paid | §8 |
| R5 | One background task writes to an **in-memory cache** holding the latest price, previous price and timestamp per ticker | §5 |
| R6 | `GET /api/stream/prices` SSE stream, ~500 ms cadence, each item has ticker, price, previous price, timestamp, direction | §10 |
| R7 | Watchlist shows **daily change %**, so each price needs a daily reference price | §4, §5 |
| R8 | Trades fill instantly at the current cached price | §11, §12 |
| R9 | Watchlist add/remove (manual or via LLM) changes the set of tracked tickers at runtime | §6, §12 |

Non-goals: order books, historical bars or candles, WebSocket streaming from Massive, multi-user fan-out.

---

## 2. Architecture

```
                 ┌────────────────────────────────────────────┐
  env vars ────► │ create_market_data_source(cache)           │
                 └───────────────┬────────────────────────────┘
                                 │ returns one of:
            ┌────────────────────┴─────────────────────┐
            ▼                                          ▼
  SimulatorDataSource                        MassiveDataSource
  (asyncio task, 0.5 s)                      (asyncio task, 15 s default)
   GBMSimulator.step()                        RESTClient.get_snapshot_all()
            │                                   (in a worker thread)
            │                                          │ parse_snapshot()
            └──────────────► PriceCache ◄──────────────┘
                     (thread-safe, latest price per ticker,
                      monotonic version counter)
                                 │
         ┌───────────────────────┼─────────────────────────┐
         ▼                       ▼                         ▼
  SSE /api/stream/prices   Portfolio valuation       Trade execution
  (polls version, 0.5 s)   GET /api/portfolio        POST /api/portfolio/trade
                           snapshot task (30 s)      LLM chat trades
```

Design rules:

1. **Producers write, consumers read.** A data source never returns prices to its caller. It pushes them into `PriceCache`, and everyone else reads from the cache. That removes coupling between data sources and API routes.
2. **Exactly one producer at a time**, chosen at startup. Switching sources means restarting the process.
3. **The cache is the only shared state.** It is guarded by a `threading.Lock` because the Massive client does its HTTP call in a worker thread (`asyncio.to_thread`).
4. **Tickers are normalized once**, at the interface boundary (`normalize_ticker`, §6). Every key in the cache is uppercase.
5. **Background tasks never die.** Every loop iteration catches and logs exceptions, then carries on.

---

## 3. Module Layout

```
backend/app/market/
├── __init__.py          # Public API re-exports
├── models.py            # PriceUpdate (frozen dataclass)
├── cache.py             # PriceCache
├── interface.py         # MarketDataSource ABC + normalize_ticker()
├── seed_prices.py       # SEED_PRICES, TICKER_PARAMS, correlation constants
├── simulator.py         # GBMSimulator + SimulatorDataSource
├── massive_client.py    # parse_snapshot() + MassiveDataSource
├── factory.py           # MarketDataConfig + create_market_data_source()
└── stream.py            # create_stream_router() — SSE endpoint
```

Public imports for the rest of the backend:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
    normalize_ticker,
)
```

---

## 4. Data Model — `PriceUpdate`

`PriceUpdate` is the only type that leaves the market data layer. It is immutable (`frozen=True`) and small (`slots=True`). Derived values are properties, so they can never drift out of sync with the stored fields.

**New in this design:** a `reference_price` field (R7). It is the price that "daily change" is measured against:
- **Massive:** the previous trading day's close (`snapshot.prev_day.close`). This matches how brokers display day change.
- **Simulator:** the seed price the ticker started at in this process (a stand-in for "today's open").

**File: `backend/app/market/models.py`**

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
    reference_price: float | None = None  # Prior close (Massive) / session start (simulator)

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
        """'up', 'down', or 'flat' relative to the previous update."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_change(self) -> float | None:
        """Absolute change vs the daily reference price, or None if unknown."""
        if self.reference_price is None:
            return None
        return round(self.price - self.reference_price, 4)

    @property
    def day_change_percent(self) -> float | None:
        """Percent change vs the daily reference price, or None if unknown."""
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 4)

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
            "reference_price": self.reference_price,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
        }
```

Example:

```python
>>> u = PriceUpdate("AAPL", price=191.20, previous_price=191.05,
...                 timestamp=1730000000.0, reference_price=190.00)
>>> u.direction, u.change, u.day_change_percent
('up', 0.15, 0.6316)
>>> u.to_dict()
{'ticker': 'AAPL', 'price': 191.2, 'previous_price': 191.05, 'timestamp': 1730000000.0,
 'change': 0.15, 'change_percent': 0.0785, 'direction': 'up',
 'reference_price': 190.0, 'day_change': 1.2, 'day_change_percent': 0.6316}
```

Conventions:
- Prices are stored rounded to 2 decimal places (the cache does the rounding).
- `timestamp` is Unix **seconds** as a float, whatever the source's native unit. The Massive client converts from nanoseconds (§8.2).

---

## 5. Price Cache — `PriceCache`

A dict from ticker to latest `PriceUpdate`, plus a version counter.

| Method | Purpose |
|---|---|
| `update(ticker, price, timestamp=None, reference_price=None) -> PriceUpdate` | Write a new price. `previous_price` comes from the existing entry. `reference_price` is kept from the existing entry when not passed. |
| `get(ticker) -> PriceUpdate \| None` | Latest update for one ticker |
| `get_price(ticker) -> float \| None` | Convenience: the price only |
| `get_all() -> dict[str, PriceUpdate]` | Shallow copy of all entries |
| `remove(ticker)` | Drop a ticker (bumps the version so SSE clients see the removal) |
| `version` | Monotonic int, incremented on every mutation |

Why a version counter: the SSE generator wakes every 500 ms and needs to know cheaply whether anything changed since its last push. Comparing one int is O(1) and needs no per-client state beyond `last_version`.

**File: `backend/app/market/cache.py`**

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
        self._version: int = 0  # Monotonically increasing; bumped on every mutation

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        reference_price: float | None = None,
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        If this is the first update for the ticker, previous_price == price
        (direction='flat') and reference_price defaults to price.
        """
        with self._lock:
            ts = time.time() if timestamp is None else timestamp
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if reference_price is None:
                reference_price = prev.reference_price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                timestamp=ts,
                reference_price=round(reference_price, 2) if reference_price else None,
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
        """Remove a ticker from the cache (e.g., when no longer tracked)."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter. Used by SSE for change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

Example:

```python
cache = PriceCache()
cache.update("AAPL", 190.00)                 # first write: flat, reference=190.00
u = cache.update("AAPL", 190.37)             # previous=190.00, direction='up'
assert u.reference_price == 190.00           # carried forward
cache.get_price("AAPL")                      # 190.37
cache.remove("AAPL"); "AAPL" in cache        # False
```

Thread safety: every read and write takes the lock, `version` included (this fixes review item 3.4, and stays correct on free-threaded Python builds). The critical sections are a dict operation and an object construction, so contention at 10–50 tickers is negligible.

---

## 6. Unified Interface — `MarketDataSource`

**File: `backend/app/market/interface.py`**

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

# 1–10 chars: letters/digits plus '.' or '-' for share classes (BRK.B, BF-B)
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def normalize_ticker(raw: str) -> str:
    """Uppercase and strip a ticker symbol; raise ValueError if it is malformed.

    Every entry point that accepts a ticker from a user or the LLM
    (watchlist routes, trade route, chat actions) calls this first, so the
    cache, the data sources, and the DB all use the same key.
    """
    ticker = raw.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise ValueError(f"Invalid ticker symbol: {raw!r}")
    return ticker


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices;
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices for the given tickers.

        Starts a background task that writes to the PriceCache. Should make a
        best effort to populate the cache before returning, so the first SSE
        event already has data. Must be called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Cancel the background task and release resources. Idempotent."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Start tracking a ticker. No-op if already tracked."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the PriceCache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the currently tracked tickers."""
```

Behavioral contract (both implementations, covered by a shared parametrized test in §13):

| Call | Simulator | Massive |
|---|---|---|
| `start(tickers)` | Seeds cache with seed prices synchronously, then ticks every 0.5 s | Does one poll before returning (cache filled if the API answers), then polls every N s |
| `add_ticker(t)` | Price is in the cache **immediately** | Price is in the cache after the next poll, which is brought forward (§8.4) |
| `remove_ticker(t)` | Removed from sim + cache | Removed from poll list + cache |
| `stop()` | Cancels task; safe to call twice | Cancels task, drops client; safe to call twice |
| Unknown ticker | Simulated from a random $50–$300 start | Absent from the snapshot response, so it never gets a price (logged once) |

---

## 7. Simulator

The current `simulator.py` and `seed_prices.py` already implement this and need only two small changes (marked **CHANGE**). The math is documented in full in `archive/MARKET_SIMULATOR.md`. Summary:

```
S(t+dt) = S(t) · exp((μ − σ²/2)·dt + σ·√dt·Z)

dt = 0.5 s / (252 days · 6.5 h · 3600 s) ≈ 8.48e-8
Z  = L · z,   z ~ N(0, I),   L = cholesky(C)
```

- **C** is the correlation matrix: tech pairs 0.6, finance pairs 0.5, anything involving TSLA 0.3, everything else 0.3.
- **Events:** each tick, each ticker has probability 0.001 of an extra ±2–5 % jump. With 10 tickers at 2 ticks/s that is about one event every 50 s.
- **Per-ticker μ/σ** live in `TICKER_PARAMS`. Unknown tickers use `DEFAULT_PARAMS` (σ=0.25, μ=0.05) and a random $50–$300 start.

### 7.1 `GBMSimulator` (pure, synchronous, no I/O)

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._start_prices: dict[str, float] = {}          # CHANGE: for reference_price
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def step(self) -> dict[str, float]:
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z
        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)
            if random.random() < self._event_prob:
                self._prices[ticker] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
            result[ticker] = round(self._prices[ticker], 2)
        return result

    def get_start_price(self, ticker: str) -> float | None:   # CHANGE: new
        """Price the ticker started at in this process (used as reference_price)."""
        return self._start_prices.get(ticker)

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        start = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._tickers.append(ticker)
        self._prices[ticker] = start
        self._start_prices[ticker] = start                   # CHANGE
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    # add_ticker / remove_ticker / get_price / get_tickers / _rebuild_cholesky /
    # _pairwise_correlation: unchanged from the current implementation.
    # remove_ticker must also `self._start_prices.pop(ticker, None)`.
```

### 7.2 `SimulatorDataSource` (async wrapper)

The only change is passing `reference_price` when seeding the cache, so `day_change_percent` is measured from the start price rather than from the first tick.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in self._sim.get_tickers():
            self._seed(ticker)
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
            self._seed(ticker)            # price available immediately

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    def _seed(self, ticker: str) -> None:
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker, price, reference_price=self._sim.get_start_price(ticker))

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

Note: `_seed` on an already-tracked ticker (a duplicate `add_ticker`) writes the current price again with the same reference. The visible effect is one "flat" tick, which is harmless.

---

## 8. Massive API — `MassiveDataSource`

### 8.1 API facts we depend on (verified against `massive==2.2.0`)

| Item | Value |
|---|---|
| Package / client | `massive` (`from massive import RESTClient`) — the renamed `polygon-api-client` |
| Endpoint | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,...` |
| Python call | `client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])` returns `list[TickerSnapshot]` |
| Auth | `Authorization: Bearer <key>`, set by the client |
| Client retries | urllib3 `Retry(total=3, backoff_factor=0.1)` on 413/429/499/500/502/503/504 |
| Errors | Non-200 raises `massive.exceptions.BadResponse(body_text)`. The body is the JSON error, e.g. `{"status":"NOT_AUTHORIZED",...}`. A missing key raises `AuthError`. |
| Timeouts | `connect_timeout=10.0`, `read_timeout=10.0` by default |
| Snapshot reset | Cleared at 12am ET and repopulated from ~4am ET. Overnight, `last_trade` may be missing for some tickers. |
| Rate limit | Free: 5 req/min. Paid: effectively unlimited (stay < 100 req/s). |
| Plan coverage | The snapshot endpoint is not included in every plan. If the key's plan lacks it, every call returns `NOT_AUTHORIZED`; we log it clearly (§8.5). |

`TickerSnapshot` fields (all `Optional`):

```
TickerSnapshot
├── ticker: str
├── last_trade: LastTrade
│     ├── price: float
│     ├── size: float
│     ├── sip_timestamp: int          # Unix NANOSECONDS  (JSON "t")
│     └── participant_timestamp: int  # Unix NANOSECONDS  (JSON "y")
├── last_quote: LastQuote (bid_price, ask_price, bid_size, ask_size, sip_timestamp)
├── min:      MinuteSnapshot (open, high, low, close, volume, vwap, timestamp[ms])
├── day:      Agg (open, high, low, close, volume, vwap)
├── prev_day: Agg (open, high, low, close, volume, vwap)
├── todays_change: float              # vs prev_day.close
├── todays_change_percent: float
└── updated: int                      # Unix NANOSECONDS
```

Raw JSON for one ticker (abridged):

```json
{
  "ticker": "AAPL",
  "todaysChange": 1.32, "todaysChangePerc": 0.69,
  "updated": 1730048399123456789,
  "day":     {"o": 190.10, "h": 192.00, "l": 189.70, "c": 191.32, "v": 41230011, "vw": 191.02},
  "prevDay": {"o": 188.40, "h": 190.50, "l": 188.00, "c": 190.00, "v": 52011873, "vw": 189.60},
  "min":     {"o": 191.30, "h": 191.35, "l": 191.28, "c": 191.32, "v": 12100, "t": 1730048340000},
  "lastTrade": {"p": 191.32, "s": 100, "x": 4, "t": 1730048399123456789},
  "lastQuote": {"p": 191.31, "P": 191.33, "s": 3, "S": 2, "t": 1730048399200000000}
}
```

### 8.2 Bug in the current implementation

`massive_client.py` currently does:

```python
timestamp = snap.last_trade.timestamp / 1000.0   # ✗ LastTrade has no .timestamp
```

On a real `LastTrade` object this raises `AttributeError`. The per-snapshot `except (AttributeError, TypeError)` catches it and logs "Skipping snapshot". The result is that **with a real API key, no ticker ever receives a price.** The unit tests don't catch this because they build `MagicMock` snapshots, and a `MagicMock` accepts any attribute name. Two more problems:

- The real timestamp (`sip_timestamp`) is in **nanoseconds**, not milliseconds.
- `prev_day.close` isn't read, so there is no daily change.

The fix is a pure parsing function, tested against real `TickerSnapshot.from_dict(...)` objects (§13.3).

### 8.3 Parsing — `parse_snapshot()`

```python
@dataclass(frozen=True, slots=True)
class ParsedQuote:
    ticker: str
    price: float
    timestamp: float              # Unix seconds
    reference_price: float | None # prev_day.close


def _to_seconds(ts: int | float | None) -> float | None:
    """Convert a Massive timestamp of unknown unit (s/ms/µs/ns) to Unix seconds."""
    if not ts:
        return None
    ts = float(ts)
    if ts > 1e17:      # nanoseconds   (~1.7e18 today)
        return ts / 1e9
    if ts > 1e14:      # microseconds
        return ts / 1e6
    if ts > 1e11:      # milliseconds  (~1.7e12 today)
        return ts / 1e3
    return ts          # already seconds


def parse_snapshot(snap: TickerSnapshot) -> ParsedQuote | None:
    """Extract (price, timestamp, reference) from a snapshot, or None if unusable.

    Price preference: last trade → latest minute bar close → today's close.
    The fallbacks cover pre-market and the overnight reset window, when
    last_trade may be missing.
    """
    if not snap.ticker:
        return None

    trade = snap.last_trade
    price: float | None = trade.price if trade and trade.price else None
    ts = (trade.sip_timestamp or trade.participant_timestamp) if trade else None

    if price is None:
        for bar in (snap.min, snap.day):
            if bar and bar.close:
                price = bar.close
                ts = getattr(bar, "timestamp", None)
                break

    if price is None or price <= 0:
        return None

    timestamp = _to_seconds(ts) or _to_seconds(snap.updated) or time.time()
    reference = snap.prev_day.close if snap.prev_day and snap.prev_day.close else None
    return ParsedQuote(snap.ticker.upper(), float(price), timestamp, reference)
```

Example:

```python
snap = TickerSnapshot.from_dict({
    "ticker": "AAPL",
    "lastTrade": {"p": 191.32, "t": 1730048399123456789},
    "prevDay": {"c": 190.00},
})
parse_snapshot(snap)
# ParsedQuote(ticker='AAPL', price=191.32, timestamp=1730048399.1234567, reference_price=190.0)
```

### 8.4 The data source

Behavior beyond the current code:

- **Dedup:** if a ticker's trade timestamp hasn't moved since the last poll (market closed, illiquid name), skip the cache write. Otherwise every poll would reset `previous_price = price` and the frontend would see a stream of "flat" updates.
- **Fast first price for new tickers:** `add_ticker` sets an event that wakes the poll loop early, but never sooner than `min_poll_gap` seconds after the previous call. On the free tier (15 s default, `min_poll_gap` 12 s) we still never go over 5 calls/min.
- **Error backoff:** after a failed poll the wait doubles, up to `max_backoff` (default 120 s). It resets on success. An auth failure (`NOT_AUTHORIZED`, `AuthError`) is logged once at ERROR with a clear message.
- **Missing tickers** (in the poll list, absent from the response) are logged once each at WARNING. This usually means a bad symbol.

**File: `backend/app/market/massive_client.py`**

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from massive import RESTClient
from massive.exceptions import AuthError, BadResponse
from massive.rest.models import SnapshotMarketType, TickerSnapshot

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

# ParsedQuote, _to_seconds, parse_snapshot  — as in §8.3


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) snapshot REST API.

    One GET per poll returns every tracked ticker, so the call rate is
    independent of watchlist size.

    Rate limits:
      - Free tier: 5 req/min → poll_interval=15 (default), min_poll_gap=12
      - Paid tiers: poll_interval 2–5 s, min_poll_gap 1 s
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        min_poll_gap: float = 12.0,
        max_backoff: float = 120.0,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._min_gap = min(min_poll_gap, poll_interval)
        self._max_backoff = max_backoff

        self._tickers: list[str] = []
        self._client: RESTClient | None = None
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()           # set by add_ticker() for an early poll

        self._last_poll_at: float = 0.0        # monotonic
        self._failures: int = 0
        self._auth_error_logged = False
        self._last_trade_ts: dict[str, float] = {}
        self._warned_missing: set[str] = set()

    # --- MarketDataSource API ---

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(dict.fromkeys(tickers))     # de-dupe, keep order
        await self._poll_once()                          # fill the cache before first SSE
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs interval",
                    len(self._tickers), self._interval)

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
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            self._warned_missing.discard(ticker)
            self._wake.set()                              # request an early poll
            logger.info("Massive: added %s (early poll requested)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._tickers = [t for t in self._tickers if t != ticker]
        self._last_trade_ts.pop(ticker, None)
        self._cache.remove(ticker)
        logger.info("Massive: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Poll loop ---

    def _next_delay(self) -> float:
        if self._failures == 0:
            return self._interval
        return min(self._interval * 2 ** self._failures, self._max_backoff)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._next_delay())
            except TimeoutError:
                pass                                       # normal scheduled poll
            self._wake.clear()

            # Respect the minimum gap, even for early polls requested by add_ticker
            gap = self._min_gap - (time.monotonic() - self._last_poll_at)
            if gap > 0:
                await asyncio.sleep(gap)

            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll: fetch snapshots, write changed prices to the cache. Never raises."""
        tickers = list(self._tickers)                      # snapshot; list may change
        if not tickers or not self._client:
            return

        self._last_poll_at = time.monotonic()
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except (AuthError, BadResponse) as e:
            self._failures += 1
            self._log_api_error(e)
            return
        except Exception as e:                              # network, timeouts, etc.
            self._failures += 1
            logger.error("Massive poll failed (%s): %s", type(e).__name__, e)
            return

        self._failures = 0
        tracked = set(self._tickers)                       # drop tickers removed mid-poll
        seen: set[str] = set()
        written = 0
        for snap in snapshots:
            try:
                quote = parse_snapshot(snap)
            except Exception:
                logger.warning("Unparseable snapshot for %s", getattr(snap, "ticker", "?"),
                               exc_info=True)
                continue
            if quote is None or quote.ticker not in tracked:
                continue
            seen.add(quote.ticker)
            if self._last_trade_ts.get(quote.ticker) == quote.timestamp:
                continue                                    # no new trade since last poll
            self._last_trade_ts[quote.ticker] = quote.timestamp
            self._cache.update(quote.ticker, quote.price, quote.timestamp,
                               reference_price=quote.reference_price)
            written += 1

        for missing in tracked - seen - self._warned_missing:
            logger.warning("Massive returned no usable data for %s (bad symbol?)", missing)
            self._warned_missing.add(missing)

        logger.debug("Massive poll: %d/%d tickers updated", written, len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list[TickerSnapshot]:
        """Blocking HTTP call. Runs in a worker thread via asyncio.to_thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )

    def _log_api_error(self, e: Exception) -> None:
        text = str(e)
        if isinstance(e, AuthError) or "NOT_AUTHORIZED" in text or "Unknown API Key" in text:
            if not self._auth_error_logged:
                logger.error(
                    "Massive API rejected the key or plan (%s). Check MASSIVE_API_KEY and that "
                    "your plan includes stock snapshots; unset it to use the simulator.", text)
                self._auth_error_logged = True
        else:
            logger.error("Massive API error: %s (retry in %.0fs)", text, self._next_delay())
```

Notes:
- `_fetch_snapshots` takes the ticker list as an argument, so the worker thread never reads `self._tickers` while the event loop is changing it.
- `asyncio.Event` is created in `__init__`. On Python ≥ 3.10, events bind to the running loop lazily on first use, so building the source before the loop starts is fine.
- If the stored timestamp equals the new one, the cache isn't touched, so `version` doesn't move and SSE doesn't re-send identical data.

### 8.5 Failure modes

| Situation | What happens | What the user sees |
|---|---|---|
| Bad key / plan lacks snapshots | `BadResponse(NOT_AUTHORIZED…)`. One ERROR log, then backoff up to 120 s between attempts. | Watchlist shows no prices. Trades return 400 "price not available". The SSE dot stays green (the connection is fine). |
| 429 rate limit | urllib3 retries 3× quickly, then `BadResponse`. We back off. | Prices pause, then resume. |
| Network down / timeout | Generic exception, backoff | Same as above |
| Ticker unknown to Massive | Missing from the response; one WARNING | Ticker stays priceless; trades on it return 400 |
| Market closed | `last_trade` unchanged, so dedup skips the write | Prices hold steady, with no fake flashes |
| Overnight reset (12am–4am ET) | `last_trade` may be missing; we fall back to `min.close`/`day.close`, and skip the ticker if none exist | Cached price from the last good poll stays |

---

## 9. Factory and Configuration

Environment variables (all optional except as noted in PLAN §5):

| Variable | Default | Meaning |
|---|---|---|
| `MASSIVE_API_KEY` | empty | Non-empty → Massive. Empty/absent → simulator. |
| `MASSIVE_POLL_INTERVAL` | `15` | Seconds between polls. Use 2–5 on a paid plan. |
| `MASSIVE_MIN_POLL_GAP` | `12` | Minimum seconds between any two calls (early polls included) |
| `SIMULATOR_TICK_INTERVAL` | `0.5` | Seconds between simulator steps |
| `SIMULATOR_EVENT_PROBABILITY` | `0.001` | Per-ticker, per-tick probability of a 2–5 % jump |

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


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the market data source selected by environment variables.

    Returns an unstarted source. Caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        interval = _env_float("MASSIVE_POLL_INTERVAL", 15.0)
        logger.info("Market data source: Massive API (poll every %.1fs)", interval)
        return MassiveDataSource(
            api_key=api_key,
            price_cache=price_cache,
            poll_interval=interval,
            min_poll_gap=_env_float("MASSIVE_MIN_POLL_GAP", min(12.0, interval)),
        )

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(
        price_cache=price_cache,
        update_interval=_env_float("SIMULATOR_TICK_INTERVAL", 0.5),
        event_probability=_env_float("SIMULATOR_EVENT_PROBABILITY", 0.001),
    )
```

`.env.example` additions:

```bash
# Optional: Massive polling cadence (seconds). Free tier: keep 15. Paid: 2–5.
MASSIVE_POLL_INTERVAL=15
```

---

## 10. SSE Streaming

### 10.1 Wire format

`GET /api/stream/prices` → `Content-Type: text/event-stream`

```
retry: 1000

data: {"AAPL":{"ticker":"AAPL","price":191.32,"previous_price":191.28,"timestamp":1730048399.12,"change":0.04,"change_percent":0.0209,"direction":"up","reference_price":190.0,"day_change":1.32,"day_change_percent":0.6947},"MSFT":{...}}

: keepalive

data: {...}
```

- Each `data:` event carries the **full map** of tracked tickers (10–50 entries, ~2–8 KB). The full map is simpler than diffs. A ticker that disappears from the map has been removed, so the client deletes it.
- An event is sent only when `PriceCache.version` has changed since the last one. With the simulator that's every 500 ms. With Massive it's roughly once per poll.
- If nothing has been sent for 15 s, a `: keepalive` comment goes out. This stops proxies and load balancers (App Runner, Render) from closing an idle stream while Massive is quiet. `EventSource` ignores comment lines.
- `retry: 1000` tells the browser to reconnect after 1 s. On reconnect, the first event is the full current state, so there's nothing to replay and no `Last-Event-ID` handling.

### 10.2 Implementation

The router is now built **inside** the factory. The current module-level `router` gets the route registered again on every `create_stream_router()` call (review item 3.6).

**File: `backend/app/market/stream.py`**

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

PUSH_INTERVAL = 0.5        # seconds between cache checks
KEEPALIVE_INTERVAL = 15.0  # seconds of silence before a comment line


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Create the SSE router bound to a PriceCache (no module-level state)."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = PUSH_INTERVAL,
    keepalive: float = KEEPALIVE_INTERVAL,
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
                data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
                yield f"data: {json.dumps(data)}\n\n"
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= keepalive:
                yield ": keepalive\n\n"
                last_sent = time.monotonic()
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        logger.info("SSE client disconnected: %s", client)
```

Change from the current code: an empty map (`{}`) **is** sent. This matters when the user removes the last ticker, so the client clears it. The current code skips empty maps.

### 10.3 Frontend consumption (for reference)

```ts
type PriceUpdate = {
  ticker: string; price: number; previous_price: number; timestamp: number;
  change: number; change_percent: number; direction: "up" | "down" | "flat";
  reference_price: number | null; day_change: number | null; day_change_percent: number | null;
};

const es = new EventSource("/api/stream/prices");
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
es.onmessage = (e) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(e.data);
  // For each ticker: flash if price !== last seen price; push {timestamp, price} onto sparkline
  // buffer (dedupe on timestamp — Massive may repeat a timestamp across reconnects).
};
```

---

## 11. FastAPI Integration

**File: `backend/app/main.py` (market-data parts only)**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.market import MarketDataSource, PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_init()                                           # lazy schema + seed (PLAN §7)

    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(load_tracked_tickers())          # watchlist ∪ open positions, §12
    app.state.price_cache = cache
    app.state.market_source = source

    snapshot_task = asyncio.create_task(portfolio_snapshot_loop(app))  # every 30 s, reads cache
    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)


# Routers that need the cache are registered at import time. The cache is
# looked up per-request through dependencies, so registration order doesn't matter.
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

`create_stream_router` needs the cache object itself. Register it inside `lifespan` (`app.include_router(create_stream_router(cache))`, before `yield`). Or, more simply, create the `PriceCache` at module scope (`price_cache = PriceCache()`) and pass it to both `create_stream_router` and the lifespan. **Recommendation: module-scope cache.** It is a plain in-memory object with no I/O, and it keeps all router registration in one place. Either way, `/api/*` routers must be registered **before** the static-file mount at `/`, or the static handler will swallow them.

Trade execution reads the fill price from the cache:

```python
@router.post("/api/portfolio/trade")
async def trade(req: TradeRequest, cache: PriceCache = Depends(get_price_cache)):
    ticker = normalize_ticker(req.ticker)               # 422 on ValueError
    price = cache.get_price(ticker)
    if price is None:
        raise HTTPException(400, f"No live price for {ticker} yet. Add it to the watchlist "
                                 "and try again in a moment.")
    # ... validate cash/shares, write trades + positions, record snapshot ...
```

---

## 12. Watchlist and Portfolio Coordination

**Tracked set = watchlist ∪ tickers with an open position.** If a user removes a ticker from the watchlist while holding it, we keep pricing it so the portfolio value, heatmap and P&L stay correct. Likewise, a sell that closes a position on a non-watchlisted ticker stops its tracking.

One helper keeps the source in sync. Every route and chat action that changes the watchlist or positions calls it, so there is one code path to reason about:

```python
# backend/app/services/tracking.py
async def sync_tracked_tickers(source: MarketDataSource, db) -> None:
    """Make the data source track exactly watchlist ∪ open positions."""
    wanted = set(db.watchlist_tickers()) | set(db.position_tickers())   # quantity > 0
    current = set(source.get_tickers())
    for t in sorted(wanted - current):
        await source.add_ticker(t)
    for t in sorted(current - wanted):
        await source.remove_ticker(t)
```

Flows:

```
POST /api/watchlist {ticker:"pypl"}        (or LLM watchlist_changes: add)
  → t = normalize_ticker("pypl") = "PYPL"     → 422 if invalid
  → INSERT OR IGNORE into watchlist
  → await sync_tracked_tickers(source, db)
       simulator: price in cache immediately
       Massive:   early poll within ≤ min_poll_gap seconds
  → 201 {"ticker":"PYPL","price": cache.get_price("PYPL")}   # price may be null on Massive

DELETE /api/watchlist/PYPL                 (or LLM: remove)
  → DELETE from watchlist
  → await sync_tracked_tickers(source, db)   # keeps PYPL if a position is open
  → 204

POST /api/portfolio/trade (sell closes the position)
  → ... DELETE position row ...
  → await sync_tracked_tickers(source, db)   # drops ticker if not on watchlist
```

`GET /api/watchlist` joins DB rows with the cache:

```python
@router.get("/api/watchlist")
def get_watchlist(cache: PriceCache = Depends(get_price_cache)):
    out = []
    for ticker in db.watchlist_tickers():
        u = cache.get(ticker)
        out.append({"ticker": ticker, **(u.to_dict() if u else {"price": None})})
    return out
```

The LLM chat context (PLAN §9, step 1) is built the same way: `cache.get_all()` for live prices, plus position math done on cached prices.

---

## 13. Testing

All tests live in `backend/tests/market/`. The existing 73 tests stay. Add or adjust the following.

### 13.1 Interface conformance (parametrized over both sources)

```python
import pytest
from unittest.mock import MagicMock, patch

from app.market import PriceCache
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource
from massive.rest.models import TickerSnapshot


def _snap(ticker, price, t_ns=1730048399123456789, prev_close=None):
    d = {"ticker": ticker, "lastTrade": {"p": price, "t": t_ns}}
    if prev_close:
        d["prevDay"] = {"c": prev_close}
    return TickerSnapshot.from_dict(d)


def _sim(cache):
    return SimulatorDataSource(cache, update_interval=0.05)


def _massive(cache):
    src = MassiveDataSource("k", cache, poll_interval=60, min_poll_gap=0)
    fake = MagicMock()
    fake.get_snapshot_all.side_effect = lambda market_type, tickers: [
        _snap(t, 100.0 + i) for i, t in enumerate(tickers)]
    patcher = patch("app.market.massive_client.RESTClient", return_value=fake)
    patcher.start()
    return src, patcher


@pytest.mark.parametrize("kind", ["sim", "massive"])
async def test_lifecycle_contract(kind):
    cache = PriceCache()
    src, patcher = (_sim(cache), None) if kind == "sim" else _massive(cache)
    try:
        await src.start(["AAPL", "MSFT"])
        assert set(src.get_tickers()) == {"AAPL", "MSFT"}
        assert cache.get_price("AAPL") is not None          # populated by start()

        await src.remove_ticker("MSFT")
        assert "MSFT" not in cache and "MSFT" not in src.get_tickers()
        await src.remove_ticker("NOPE")                      # no-op

        await src.stop()
        await src.stop()                                     # idempotent
    finally:
        if patcher:
            patcher.stop()
```

### 13.2 Cache

```python
def test_reference_price_carried_forward():
    c = PriceCache()
    c.update("AAPL", 100.0, reference_price=95.0)
    u = c.update("AAPL", 101.0)
    assert u.reference_price == 95.0 and u.day_change_percent == pytest.approx(6.3158, abs=1e-4)

def test_remove_bumps_version():
    c = PriceCache(); c.update("AAPL", 1.0); v = c.version
    c.remove("AAPL"); assert c.version == v + 1
    c.remove("AAPL"); assert c.version == v + 1            # absent: no bump

def test_concurrent_writers():
    import threading
    c = PriceCache()
    ts = [threading.Thread(target=lambda: [c.update("X", float(i)) for i in range(1000)])
          for _ in range(8)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert c.version == 8000
```

### 13.3 Massive parsing (real model objects; would have caught §8.2)

```python
from app.market.massive_client import parse_snapshot, _to_seconds

def test_parse_real_snapshot():
    q = parse_snapshot(_snap("AAPL", 191.32, prev_close=190.0))
    assert q.price == 191.32 and q.reference_price == 190.0
    assert q.timestamp == pytest.approx(1730048399.123, abs=1e-3)

def test_parse_falls_back_to_minute_bar():
    s = TickerSnapshot.from_dict({"ticker": "AAPL", "min": {"c": 190.5, "t": 1730048340000}})
    q = parse_snapshot(s)
    assert q.price == 190.5 and q.timestamp == 1730048340.0

def test_parse_unusable_returns_none():
    assert parse_snapshot(TickerSnapshot.from_dict({"ticker": "AAPL"})) is None
    assert parse_snapshot(TickerSnapshot.from_dict({"lastTrade": {"p": 1.0}})) is None

@pytest.mark.parametrize("raw,expected", [
    (1730048399, 1730048399.0), (1730048399123, 1730048399.123),
    (1730048399123456, 1730048399.123456), (1730048399123456789, 1730048399.1234567),
])
def test_to_seconds(raw, expected):
    assert _to_seconds(raw) == pytest.approx(expected)
```

### 13.4 Massive poller behavior

```python
async def test_unchanged_trade_is_not_rewritten():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60, min_poll_gap=0)
    src._client, src._tickers = MagicMock(), ["AAPL"]
    with patch.object(src, "_fetch_snapshots", return_value=[_snap("AAPL", 191.0)]):
        await src._poll_once(); v = cache.version
        await src._poll_once()
    assert cache.version == v

async def test_auth_error_backs_off_and_logs_once(caplog):
    from massive.exceptions import BadResponse
    src = MassiveDataSource("k", PriceCache(), poll_interval=15, min_poll_gap=0)
    src._client, src._tickers = MagicMock(), ["AAPL"]
    err = BadResponse('{"status":"NOT_AUTHORIZED","message":"..."}')
    with patch.object(src, "_fetch_snapshots", side_effect=err):
        await src._poll_once(); await src._poll_once()
    assert src._next_delay() == 60          # 15 * 2**2
    assert sum("rejected the key" in r.getMessage() for r in caplog.records) == 1

async def test_add_ticker_triggers_early_poll():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=3600, min_poll_gap=0)
    fake = MagicMock()
    fake.get_snapshot_all.side_effect = lambda market_type, tickers: [_snap(t, 50.0) for t in tickers]
    with patch("app.market.massive_client.RESTClient", return_value=fake):
        await src.start(["AAPL"])
        await src.add_ticker("PYPL")
        for _ in range(50):
            if cache.get_price("PYPL"): break
            await asyncio.sleep(0.01)
        await src.stop()
    assert cache.get_price("PYPL") == 50.0
```

### 13.5 SSE

```python
from fastapi import FastAPI
from app.market.stream import _generate_events, create_stream_router

class _Req:
    client = None
    def __init__(self, n): self.n = n
    async def is_disconnected(self):
        self.n -= 1
        return self.n < 0

async def test_sse_emits_retry_then_data():
    c = PriceCache(); c.update("AAPL", 190.0)
    out = [e async for e in _generate_events(c, _Req(1), interval=0)]
    assert out[0] == "retry: 1000\n\n"
    assert out[1].startswith("data: ") and '"AAPL"' in out[1]

def test_router_factory_is_isolated():
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert len(r1.routes) == len(r2.routes) == 1
```

### 13.6 Manual smoke test against the real API

```bash
cd backend
MASSIVE_API_KEY=... uv run python -c "
import asyncio, logging
from app.market import PriceCache, create_market_data_source
logging.basicConfig(level=logging.INFO)
async def main():
    c = PriceCache(); s = create_market_data_source(c)
    await s.start(['AAPL','MSFT','NVDA'])
    for u in c.get_all().values(): print(u.to_dict())
    await s.stop()
asyncio.run(main())"
```

Expected output: three dicts with non-null `price` and `reference_price`. If the output is empty, look for the "rejected the key or plan" log line.

---

## 14. Change List vs Current Code

Ordered by priority. Each item maps to a section above.

| # | Priority | File | Change | § |
|---|---|---|---|---|
| 1 | **Critical** | `massive_client.py` | Replace `snap.last_trade.timestamp / 1000` with `parse_snapshot()` (reads `sip_timestamp` in ns, with fallbacks). Today every real snapshot is skipped. | 8.2, 8.3 |
| 2 | **Critical** | `tests/market/test_massive.py` | Build snapshots with `TickerSnapshot.from_dict(...)` rather than `MagicMock`, so tests exercise real field names | 13.3 |
| 3 | High | `models.py`, `cache.py` | Add `reference_price` + `day_change` / `day_change_percent` (watchlist "daily change %") | 4, 5 |
| 4 | High | `simulator.py` | Track start prices; seed cache with `reference_price` | 7 |
| 5 | High | `massive_client.py` | Pass `prev_day.close` as `reference_price`; dedup unchanged trades | 8.4 |
| 6 | Medium | `massive_client.py` | Early poll on `add_ticker` (bounded by `min_poll_gap`), exponential backoff, single auth-error log, missing-ticker warning | 8.4 |
| 7 | Medium | `stream.py` | Router created inside factory; keepalive comments; send empty map | 10.2 |
| 8 | Medium | `interface.py`, `__init__.py` | Add and export `normalize_ticker()` | 6 |
| 9 | Low | `cache.py` | `version` read under lock; `remove()` bumps version | 5 |
| 10 | Low | `factory.py` | Read `MASSIVE_POLL_INTERVAL`, `MASSIVE_MIN_POLL_GAP`, `SIMULATOR_*` env vars | 9 |
| 11 | Low | `backend/CLAUDE.md`, `MARKET_DATA_SUMMARY.md` | Document `reference_price`, `normalize_ticker`, new env vars | — |

Compatibility: every public signature keeps its existing arguments. New ones are optional keyword arguments, and new `to_dict()` keys are additions. Existing callers and tests that don't touch Massive internals keep working unchanged.
