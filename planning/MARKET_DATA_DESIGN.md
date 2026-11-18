# Market Data Backend — Detailed Design

**Status:** Current design for `backend/app/market/`. Supersedes `planning/archive/MARKET_DATA_DESIGN.md`.
**Audience:** The Backend agent implementing milestone **M0** (Task 0 fixes, `PLAN.md` §6, §13) and everyone who builds on the market data layer in M1 and M2 (portfolio, watchlist, chat).
**Sources:** `PLAN.md` §6–§8 and §11, `MARKET_DATA_SUMMARY.md`, the archived design, interface, simulator, Massive and review docs, the current code in `backend/app/market/`, and the installed `massive` package (checked against its real models, see §7.1).

This document gives the complete target code for every module. Where the target differs from today's code, a **Δ** marker says what changes and why. Code that the plan already treats as complete is repeated here so this file can serve as the single reference.

---

## Table of Contents

1. [Goals and Non-Goals](#1-goals-and-non-goals)
2. [Architecture](#2-architecture)
3. [File Layout and Public API](#3-file-layout-and-public-api)
4. [Data Model: `PriceUpdate`](#4-data-model-priceupdate)
5. [Price Cache](#5-price-cache)
6. [Unified Interface: `MarketDataSource`](#6-unified-interface-marketdatasource)
7. [Massive API Source](#7-massive-api-source)
8. [GBM Simulator Source](#8-gbm-simulator-source)
9. [Factory](#9-factory)
10. [SSE Streaming Endpoint](#10-sse-streaming-endpoint)
11. [Tracked Set: watchlist ∪ positions](#11-tracked-set-watchlist--positions)
12. [FastAPI Lifespan Integration](#12-fastapi-lifespan-integration)
13. [Ticker Validation at the API Layer](#13-ticker-validation-at-the-api-layer)
14. [Testing Plan](#14-testing-plan)
15. [Edge Cases and Failure Modes](#15-edge-cases-and-failure-modes)
16. [Configuration Reference](#16-configuration-reference)
17. [M0 Change Checklist](#17-m0-change-checklist)

---

## 1. Goals and Non-Goals

**Goals**

- One abstract interface (`MarketDataSource`) with two interchangeable implementations: a GBM **simulator** (default) and a **Massive** (Polygon.io) REST poller (used when `MASSIVE_API_KEY` is set).
- One shared, thread-safe, in-memory **`PriceCache`** that holds the latest price for each tracked ticker. It is the only way downstream code reads prices.
- One **SSE endpoint**, `GET /api/stream/prices`, that pushes the full price map whenever the cache changes.
- The tracked set is always **watchlist ∪ positions**, so held tickers keep live prices after they leave the watchlist.
- Everything runs in **one process with one worker**. No external services are needed in simulator mode.

**Non-goals**

- Historical bars, order books, bid/ask, volume. The frontend builds sparklines from the SSE stream (`PLAN.md` §2).
- Massive WebSocket streaming. REST polling works on every tier.
- Multiple workers or instances (`PLAN.md` §11).

---

## 2. Architecture

```
                    ┌──────────────────────────────────────────┐
                    │   create_market_data_source(cache)       │
                    │   MASSIVE_API_KEY set?                   │
                    └──────────────┬───────────────────────────┘
                     yes           │            no
          ┌────────────────────────┴──────────────────────┐
          ▼                                               ▼
┌─────────────────────────┐                 ┌──────────────────────────┐
│ MassiveDataSource       │                 │ SimulatorDataSource      │
│  poll every 15 s        │                 │  step every 0.5 s        │
│  RESTClient (sync)      │                 │  GBMSimulator (numpy)    │
│  via asyncio.to_thread  │                 │  correlated GBM + shocks │
└───────────┬─────────────┘                 └────────────┬─────────────┘
            │ cache.update(ticker, price, ts)            │
            └──────────────────┬─────────────────────────┘
                               ▼
                 ┌───────────────────────────────┐
                 │ PriceCache (threading.Lock)    │
                 │  _prices: {ticker: PriceUpdate}│
                 │  _version: int  (bump on       │
                 │   update AND remove)           │
                 └──┬──────────────┬──────────┬───┘
                    │              │          │
        get_all() + version   get_price()  get_price()
                    ▼              ▼          ▼
         SSE /api/stream/prices  Trades   Portfolio valuation,
         (per-client generator)           snapshots, chat context
```

**Rules**

1. **Producers write, consumers read.** Sources never return prices to callers. They push into the cache on their own schedule. Consumers never call a source for a price.
2. **Exactly one producer** runs per process. The factory picks it once at startup.
3. **The cache only holds tracked tickers.** `remove_ticker()` on a source also removes the ticker from the cache, which bumps `version`, so SSE clients drop it.
4. **Normalization happens twice.** The API layer is the authority (`^[A-Z]{1,5}$` after `strip().upper()`, failing with `422`). Sources also apply `strip().upper()` as defence in depth (T0.1).

---

## 3. File Layout and Public API

```
backend/
├── app/
│   ├── market/
│   │   ├── __init__.py        # Re-exports the public API
│   │   ├── models.py          # PriceUpdate (frozen dataclass)
│   │   ├── cache.py           # PriceCache                       Δ T0.2
│   │   ├── interface.py       # MarketDataSource ABC + normalize_ticker()  Δ new helper
│   │   ├── seed_prices.py     # Seed prices, GBM params, correlations
│   │   ├── simulator.py       # GBMSimulator + SimulatorDataSource Δ T0.1
│   │   ├── massive_client.py  # MassiveDataSource                Δ T0.1, timestamp fix
│   │   ├── factory.py         # create_market_data_source()
│   │   └── stream.py          # create_stream_router()           Δ T0.3, T0.4
│   └── tracking.py            # TickerTracker (watchlist ∪ positions), built in M1
└── tests/market/              # One test module per source module (+ test_stream.py, new)
```

**Public API (`app/market/__init__.py`)**

```python
"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate               - Immutable price snapshot dataclass
    PriceCache                - Thread-safe in-memory price store
    MarketDataSource          - Abstract interface for data providers
    normalize_ticker          - strip().upper() helper used by both sources
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

Downstream code imports only from `app.market`, never from submodules.

---

## 4. Data Model: `PriceUpdate`

`PriceUpdate` is the only data structure that leaves the market data layer. It is immutable, so it can be shared between threads and SSE clients without copying.

**File: `app/market/models.py`** (unchanged)

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
        """Absolute price change from the previous update (tick-to-tick)."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous update (tick-to-tick)."""
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
        """Serialize for JSON / SSE transmission (the 7 fields in PLAN.md §6)."""
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

**Design notes**

- `change`, `change_percent` and `direction` are derived properties, so they can never disagree with `price` and `previous_price`.
- All three are **tick-to-tick**, not day-change. The frontend shows `change_percent` in the watchlist (`PLAN.md` §10).
- `timestamp` is Unix **seconds** (float). Massive sends nanoseconds. The Massive source converts before calling the cache (§7.3).

**Example**

```python
>>> u = PriceUpdate("AAPL", 190.50, 190.00, 1_700_000_000.0)
>>> u.to_dict()
{'ticker': 'AAPL', 'price': 190.5, 'previous_price': 190.0, 'timestamp': 1700000000.0,
 'change': 0.5, 'change_percent': 0.2632, 'direction': 'up'}
```

---

## 5. Price Cache

The cache stores one `PriceUpdate` per ticker and keeps a version counter that SSE generators poll to detect changes.

**File: `app/market/cache.py`**: **Δ T0.2:** `remove()` bumps `_version`. The `version` getter now reads under the lock (review §3.4, needed for free-threaded Python).

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

    `version` increments on every mutation (update or remove), so a reader
    that remembers the last version it saw can tell whether anything changed.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version: int = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker and return the created PriceUpdate.

        The first update for a ticker has previous_price == price (direction 'flat').
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
        """Latest PriceUpdate for a ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        """Latest price for a ticker, or None if unknown."""
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all current prices."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Remove a ticker. Bumps version only if the ticker was present."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1  # Δ T0.2: SSE clients must see the removal

    @property
    def version(self) -> int:
        """Monotonic mutation counter for SSE change detection."""
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Δ minor:** `ts = timestamp or time.time()` became `timestamp if timestamp is not None else time.time()`, so a legitimate `0.0` timestamp is not silently replaced. Only tests pass `0.0`, but the explicit check is clearer.

**Why bump only when present?** A no-op remove (for example, the API layer removing an untracked ticker) shouldn't make every SSE client re-send an identical payload. The T0.2 test (§14) covers both the "present" and "absent" cases.

**Why `threading.Lock` and not `asyncio.Lock`?** The Massive source runs its HTTP call in a worker thread (`asyncio.to_thread`). Today it writes to the cache back on the event loop, but a thread lock keeps the cache correct whichever thread writes. The critical sections are a dict lookup plus an assignment, so the lock costs nothing measurable.

**Why rounding lives in the cache:** both producers then emit prices at cent precision. That keeps `direction` stable: a sub-cent wiggle shows as `flat`, not as `up` then `down`.

---

## 6. Unified Interface: `MarketDataSource`

**File: `app/market/interface.py`**: **Δ:** adds `normalize_ticker()`, used by both implementations for T0.1.

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


def normalize_ticker(ticker: str) -> str:
    """Defence-in-depth normalization applied by every source.

    The API layer has already validated against ^[A-Z]{1,5}$; sources only
    strip/upper so that internal callers (lifespan, tests, scripts) can't
    create duplicate entries like 'aapl' and 'AAPL'.
    """
    return ticker.strip().upper()


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never asks a source for a price; it reads the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])   # once
        await source.add_ticker("TSLA")              # any time
        await source.remove_ticker("GOOGL")          # any time
        await source.stop()                          # idempotent

    Invariants every implementation must uphold:
      * start(), add_ticker() and remove_ticker() normalize with normalize_ticker().
      * add_ticker() of a present ticker and remove_ticker() of an absent one are no-ops.
      * remove_ticker() also calls price_cache.remove(ticker).
      * get_tickers() returns a new list (callers may mutate it).
      * After stop() the source never writes to the cache again.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers. Call exactly once."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources. Safe to call repeatedly."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the tracked set. No-op if already present."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the tracked set and from the PriceCache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return a copy of the currently tracked tickers."""
```

**Behaviour contract, side by side**

| Operation | `SimulatorDataSource` | `MassiveDataSource` |
|---|---|---|
| `start(tickers)` | Builds `GBMSimulator`, seeds the cache with seed prices **immediately**, starts the 0.5 s loop | Creates `RESTClient`, does one **immediate** poll, starts the 15 s loop |
| `add_ticker(t)` | Adds to the simulator (Cholesky rebuilt) and seeds the cache **immediately** | Appends to the list. Price arrives on the **next poll** (up to 15 s) |
| `remove_ticker(t)` | Removes from the simulator and the cache | Removes from the list and the cache |
| Price available right after `add_ticker`? | Yes | No, so a trade gets `400 "Price not yet available for <T>"` |
| Timestamp source | `time.time()` at the tick | Trade's SIP timestamp (ns → s) |

---

## 7. Massive API Source

### 7.1 What the API really returns

The archived docs assumed `snap.last_trade.timestamp` in milliseconds and `snap.day.previous_close`. **The installed `massive` package has neither.** This was checked against `massive/rest/models/snapshot.py` and `trades.py`:

```python
# massive.rest.models.TickerSnapshot (fields used by FinAlly)
ticker: Optional[str]
last_trade: Optional[LastTrade]     # may be None (e.g. no trades yet today)
day: Optional[Agg]                  # Agg.close may be 0/None pre-market
prev_day: Optional[Agg]             # previous session's bar
updated: Optional[int]              # nanoseconds since epoch
todays_change_percent: Optional[float]

# massive.rest.models.LastTrade
price: Optional[float]
sip_timestamp: Optional[int]        # JSON key "t", NANOSECONDS since epoch
participant_timestamp: Optional[int]
size: Optional[float]
```

Quick check:

```python
>>> from massive.rest.models import TickerSnapshot
>>> s = TickerSnapshot.from_dict({"ticker": "AAPL",
...     "lastTrade": {"p": 190.12, "t": 1707580800123456789},
...     "prevDay": {"c": 188.5}})
>>> s.last_trade.price, s.last_trade.sip_timestamp, hasattr(s.last_trade, "timestamp")
(190.12, 1707580800123456789, False)
```

**Consequence for today's code:** `massive_client.py:103` reads `snap.last_trade.timestamp / 1000.0`. Against the real API that raises `AttributeError`, which the per-snapshot `except` swallows, **so every snapshot is skipped and the cache stays empty in Massive mode.** The unit tests pass only because they build snapshots from `MagicMock`, where any attribute exists. The fix (§7.3) belongs in M0, and the tests must switch to real `TickerSnapshot.from_dict(...)` objects (§14.4). This gap should also be recorded in `PLAN.md` §6 "Known Gaps" (as T0.5) per the coordination rules.

**Endpoint:** `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...` is **one request for all tracked tickers**, which is what lets a 15 s interval fit the free tier's 5 requests per minute. The client splits the result on the `"tickers"` key and returns `list[TickerSnapshot]`. Unknown symbols are simply absent from the list.

**Client defaults** (`RESTClient.__init__`): `connect_timeout=10.0`, `read_timeout=10.0`, `retries=3` (urllib3 retry on 5xx/429). The worst case per poll is therefore well under the 15 s interval, and the thread never blocks the event loop.

### 7.2 Price extraction rules

For each snapshot, take the first usable value:

| Priority | Price | Timestamp |
|---|---|---|
| 1 | `last_trade.price` | `last_trade.sip_timestamp / 1e9` |
| 2 | `day.close` (if > 0) | `snap.updated / 1e9` |
| 3 | `prev_day.close` (if > 0) | `snap.updated / 1e9` |
| — | none usable → skip, log at DEBUG | `None` → cache uses `time.time()` |

Prices must be finite and `> 0`. A ticker that's skipped keeps its previous cached price (or stays absent until it gets one).

### 7.3 Implementation

**File: `app/market/massive_client.py`**: **Δ T0.1** (normalize in `start/add/remove`, de-duplicate). **Δ timestamp fix** (§7.1). **Δ** the thread receives a **copy** of the ticker list, so `add_ticker()` on the loop can't mutate a list the worker thread is reading.

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource, normalize_ticker

logger = logging.getLogger(__name__)

_NS_PER_SECOND = 1_000_000_000


def _positive(value: Any) -> float | None:
    """Return value as float if it is a finite number > 0, else None."""
    if isinstance(value, (int, float)) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def extract_price(snap: Any) -> tuple[float, float | None] | None:
    """Pick (price, unix_seconds) from a TickerSnapshot per the §7.2 priority table.

    Returns None if the snapshot has no usable price.
    Pure function: easy to unit test with TickerSnapshot.from_dict(...).
    """
    updated = getattr(snap, "updated", None)
    fallback_ts = updated / _NS_PER_SECOND if _positive(updated) else None

    trade = getattr(snap, "last_trade", None)
    if trade is not None and (price := _positive(trade.price)) is not None:
        sip = trade.sip_timestamp
        ts = sip / _NS_PER_SECOND if _positive(sip) else fallback_ts
        return price, ts

    for bar in (getattr(snap, "day", None), getattr(snap, "prev_day", None)):
        if bar is not None and (price := _positive(bar.close)) is not None:
            return price, fallback_ts

    return None


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive (Polygon.io) REST snapshot endpoint.

    One API call per poll fetches every tracked ticker.
      * Free tier: 5 req/min, so poll every 15 s (default).
      * Paid tiers: 2–5 s is reasonable.
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

    # --- MarketDataSource ---------------------------------------------------

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        # Δ T0.1: normalize + de-duplicate, preserving order
        self._tickers = list(dict.fromkeys(normalize_ticker(t) for t in tickers))

        # Immediate first poll so the cache has data right away.
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
        ticker = normalize_ticker(ticker)
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (price arrives on next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)
        if ticker in self._tickers:
            self._tickers.remove(ticker)
            logger.info("Massive: removed %s", ticker)
        self._cache.remove(ticker)  # no-op (and no version bump) if absent

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Internal -----------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Poll forever on the interval. The first poll already ran in start()."""
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll cycle: fetch snapshots in a worker thread, update the cache."""
        tickers = list(self._tickers)  # Δ snapshot for the worker thread
        if not tickers or not self._client:
            return

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots, tickers)
        except Exception as e:  # 401 bad key, 403 plan, 429 rate limit, network
            logger.error("Massive poll failed: %s", e)
            return  # the loop retries on the next interval

        tracked = set(self._tickers)  # re-read: tickers may have changed mid-poll
        processed = 0
        for snap in snapshots:
            ticker = getattr(snap, "ticker", None)
            if ticker not in tracked:
                continue  # removed while the request was in flight; don't resurrect
            extracted = extract_price(snap)
            if extracted is None:
                logger.debug("Massive: no usable price for %s", ticker)
                continue
            price, ts = extracted
            self._cache.update(ticker=ticker, price=price, timestamp=ts)
            processed += 1

        logger.debug("Massive poll: updated %d/%d tickers", processed, len(tickers))

    def _fetch_snapshots(self, tickers: list[str]) -> list:
        """Blocking call to the Massive REST API. Runs in a worker thread."""
        assert self._client is not None
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
```

**Why the "tracked" re-check after the await?** Suppose `DELETE /api/watchlist/PYPL` runs while a poll is in flight. Without the check, the poll's result would write PYPL back into the cache after `remove_ticker()` has cleared it, and the ticker would stay on screen forever. Test: §14.4 `test_removed_mid_poll_not_resurrected`.

**Market-closed behaviour:** outside trading hours `last_trade.price` is the last trade (possibly after-hours). Prices simply stop changing. The cache version still bumps every 15 s, and the SSE stream re-sends identical prices with `direction: "flat"`. That's correct and harmless.

**Error philosophy:** never raise from the loop. A bad key logs `ERROR` every 15 s and the app keeps serving: the SSE stream connects, and watchlist prices stay `null`. Trades then fail with the standard `400 "Price not yet available"`.

---

## 8. GBM Simulator Source

### 8.1 The math

Each tick, every ticker's price evolves by geometric Brownian motion:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

- `μ` is annualized drift and `σ` is annualized volatility (per ticker, §8.2).
- `dt = 0.5 s / (252 days · 6.5 h · 3600 s) ≈ 8.48e-8` of a trading year.
- `Z` is a correlated standard normal: `Z = L · z`, where `z ~ N(0, I)` and `L = cholesky(C)` for the sector correlation matrix `C`.

**Scale check:** per-tick standard deviation is `σ·√dt`. For AAPL that's `0.22 · √8.48e-8 ≈ 6.4e-5`, about 1.2¢ on $190. Over a 6.5 h simulated day the moves add up to roughly `σ/√252 ≈ 1.4%`, which matches realistic intraday ranges.

**Shock events:** with probability `p = 0.001` per ticker per tick, multiply the price by `1 ± U(0.02, 0.05)`. With 10 tickers at 2 ticks/s, that's about one event every 50 s somewhere on the board, which provides the "drama" from `PLAN.md` §6.

**Properties:** prices stay positive (exponential form, and shocks never go below −5%). Correlation 0.3–0.6 across a ≤50-ticker block-structured matrix is always positive definite, so Cholesky never fails (§14.3 checks this for the full default set plus unknowns).

### 8.2 Seed data

**File: `app/market/seed_prices.py`** (unchanged)

```python
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
    "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00,
}

# Per-ticker GBM parameters. sigma: annualized volatility, mu: annualized drift
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

# Parameters for dynamically added tickers not listed above
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Sector groups for the correlation matrix
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6     # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3    # Between sectors / unknown tickers
TSLA_CORR = 0.3           # TSLA does its own thing
```

Unknown tickers (for example `PYPL`) start at `random.uniform(50, 300)` with `DEFAULT_PARAMS` and `CROSS_GROUP_CORR` against everything. The start price is fixed on `add_ticker()`, and the price is in the cache before `add_ticker()` returns.

### 8.3 `GBMSimulator`: the pure math engine

**File: `app/market/simulator.py` (part 1)** (unchanged apart from normalization, which belongs in the wrapper). The engine has no asyncio and no cache, so it can be tested deterministically.

```python
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import logging
import math
import random

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
)

logger = logging.getLogger(__name__)


class GBMSimulator:
    """Correlated geometric Brownian motion over a dynamic set of tickers."""

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
        self._rebuild_cholesky()  # once, after the batch

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price rounded to cents}."""
        n = len(self._tickers)
        if n == 0:
            return {}

        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        sqrt_dt = math.sqrt(self._dt)
        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu = self._params[ticker]["mu"]
            sigma = self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * sqrt_dt * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock
                logger.debug("Random event on %s: %+.1f%%", ticker, shock * 100)

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

    # --- Internals ---

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n²) rebuild on membership change; n < 50 in practice."""
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

**Δ minor:** `TICKER_PARAMS.get(...)` is now wrapped in `dict(...)` for every ticker, not just defaults, so mutating one ticker's params can never alter the module-level constant.

### 8.4 `SimulatorDataSource`: async wrapper

**File: `app/market/simulator.py` (part 2)**: **Δ T0.1:** normalize in `start/add/remove`. **Δ:** `add_ticker()` before `start()` is no longer silently dropped. It's queued and picked up by `start()`.

```python
class SimulatorDataSource(MarketDataSource):
    """Runs GBMSimulator.step() every `update_interval` seconds into the PriceCache."""

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
        self._pending: list[str] = []  # add_ticker() calls made before start()

    async def start(self, tickers: list[str]) -> None:
        # Δ T0.1: normalize + de-duplicate, preserving order
        normalized = list(dict.fromkeys(normalize_ticker(t) for t in [*tickers, *self._pending]))
        self._pending.clear()
        self._sim = GBMSimulator(tickers=normalized, event_probability=self._event_prob)

        # Seed the cache so the first SSE event already has every ticker.
        for ticker in normalized:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(normalized))

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
        ticker = normalize_ticker(ticker)  # Δ T0.1
        if self._sim is None:
            if ticker not in self._pending:
                self._pending.append(ticker)
            return
        if ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        price = self._sim.get_price(ticker)  # seed immediately (PLAN.md §6)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)
        logger.info("Simulator: added %s at %.2f", ticker, price or 0.0)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = normalize_ticker(ticker)  # Δ T0.1
        if ticker in self._pending:
            self._pending.remove(ticker)
        if self._sim is not None:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: removed %s", ticker)

    def get_tickers(self) -> list[str]:
        if self._sim is None:
            return list(self._pending)
        return self._sim.get_tickers()

    async def _run_loop(self) -> None:
        """Step, write, sleep. Exceptions are logged and never kill the loop."""
        while True:
            try:
                if self._sim is not None:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

**Concurrency note:** `step()`, `add_ticker()` and `remove_ticker()` all run on the event loop and contain no `await`, so they can't interleave. No lock is needed around `GBMSimulator`.

**Cost:** one tick for 10 tickers is one 10×10 mat-vec plus 10 `exp` calls, a few microseconds. Even at 50 tickers it's far below 1% of a core.

---

## 9. Factory

**File: `app/market/factory.py`** (unchanged)

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
    """MASSIVE_API_KEY set and non-empty → MassiveDataSource, else SimulatorDataSource.

    Returns an unstarted source. The caller must `await source.start(tickers)`.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

The factory reads the environment at call time, not import time. `python-dotenv` must have run before the lifespan calls it (§12). The `massive` import is eager because the package is a core dependency (review fix #2).

---

## 10. SSE Streaming Endpoint

**File: `app/market/stream.py`**: **Δ T0.4:** the `APIRouter` is created **inside** the factory. **Δ T0.3:** emits `data: {}` when the cache is empty. **Δ:** periodic keep-alive comment so idle proxies don't drop the connection.

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

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
}


def create_stream_router(
    price_cache: PriceCache,
    interval: float = 0.5,
    keepalive_seconds: float = 15.0,
) -> APIRouter:
    """Build a fresh router per call (Δ T0.4), so test apps never share routes."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices", response_class=StreamingResponse)
    async def stream_prices(request: Request) -> StreamingResponse:
        """Long-lived SSE stream. Each event is the full {ticker: PriceUpdate.to_dict()} map."""
        return StreamingResponse(
            _generate_events(price_cache, request, interval, keepalive_seconds),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    return router


def format_prices_event(price_cache: PriceCache) -> str:
    """Serialize the whole cache as one SSE `data:` frame (may be `{}`)."""
    data = {ticker: update.to_dict() for ticker, update in price_cache.get_all().items()}
    return f"data: {json.dumps(data)}\n\n"


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    keepalive_seconds: float = 15.0,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # browser reconnects 1 s after a drop

    last_version = -1  # guarantees an initial frame, even if the cache is empty
    last_sent = time.monotonic()
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client)
                break

            version = price_cache.version
            if version != last_version:
                last_version = version
                # Δ T0.3: no `if prices:` guard. An empty map is sent as `data: {}`.
                yield format_prices_event(price_cache)
                last_sent = time.monotonic()
            elif time.monotonic() - last_sent >= keepalive_seconds:
                yield ": keep-alive\n\n"  # SSE comment, ignored by EventSource
                last_sent = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled: %s", client)
        raise
```

**Wire format example** (two frames from a simulator session):

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.0, "previous_price": 190.0, "timestamp": 1763460000.12, "change": 0.0, "change_percent": 0.0, "direction": "flat"}, "GOOGL": {...}}

data: {"AAPL": {"ticker": "AAPL", "price": 190.01, "previous_price": 190.0, "timestamp": 1763460000.62, "change": 0.01, "change_percent": 0.0053, "direction": "up"}, "GOOGL": {...}}

: keep-alive

data: {}
```

**Key behaviours**

- **Full map every frame.** The frontend replaces its price map on each event (`PLAN.md` §10), so removals propagate without a separate "delete" message. That's why T0.2 and T0.3 matter: without them, the last removed ticker would stay frozen on screen.
- **Change-driven, not timer-driven.** In simulator mode the version changes every 0.5 s, so a frame goes out every 0.5 s. In Massive mode, a frame goes out after each 15 s poll (or a watchlist change), with keep-alives in between.
- **Re-raise `CancelledError`.** The current code swallows it. Re-raising lets Starlette finish the task cleanly on shutdown.
- **Auth:** `APP_PASSWORD` is enforced by the M1 middleware, not here. For this route the middleware accepts `?key=<password>` because `EventSource` can't set headers (`PLAN.md` §11). The frontend connects with `new EventSource('/api/stream/prices?key=' + encodeURIComponent(pw))`.
- **Per-client cost:** one coroutine that wakes every 0.5 s and does one `json.dumps` of ≤ 50 small dicts. Negligible.

**Frontend consumption sketch** (for the M3 agent):

```ts
const es = new EventSource(`/api/stream/prices${key ? `?key=${encodeURIComponent(key)}` : ""}`);
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
es.onmessage = (e) => {
  const next: Record<string, PriceUpdate> = JSON.parse(e.data); // may be {}
  setPrices(next);                       // replace, don't merge
  for (const [t, p] of Object.entries(next)) appendSparkline(t, p.timestamp, p.price);
};
```

---

## 11. Tracked Set: watchlist ∪ positions

The source must track exactly `watchlist ∪ positions` (`PLAN.md` §6). The watchlist routes, the trade route and the chat action executor all change these sets, so the rule lives in **one helper**. Routes never call `source.add_ticker` or `source.remove_ticker` directly.

**File: `app/tracking.py`** (new, built in M1, designed here because it is the only correct way to drive the source)

```python
"""Keeps MarketDataSource tracking exactly watchlist ∪ positions."""

from __future__ import annotations

import sqlite3

from app.market import MarketDataSource

USER_ID = "default"


class TickerTracker:
    def __init__(self, source: MarketDataSource) -> None:
        self._source = source

    @staticmethod
    def desired(conn: sqlite3.Connection, user_id: str = USER_ID) -> set[str]:
        rows = conn.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? "
            "UNION SELECT ticker FROM positions WHERE user_id = ?",
            (user_id, user_id),
        ).fetchall()
        return {r[0] for r in rows}

    async def ensure_tracked(self, ticker: str) -> None:
        """Call after a ticker enters the watchlist or a position is opened."""
        await self._source.add_ticker(ticker)  # idempotent

    async def release_if_unused(self, conn: sqlite3.Connection, ticker: str) -> None:
        """Call AFTER committing a watchlist removal or a position close."""
        if ticker not in self.desired(conn):
            await self._source.remove_ticker(ticker)
```

**Call sites**

| Event | DB change (committed first) | Then |
|---|---|---|
| `POST /api/watchlist {PYPL}` | insert watchlist row | `ensure_tracked("PYPL")` |
| `DELETE /api/watchlist/PYPL` | delete watchlist row | `release_if_unused(conn, "PYPL")`. Stays tracked if PYPL is held |
| Buy (manual or chat) of an unwatched ticker | insert watchlist row, then the trade transaction | `ensure_tracked(...)` **before** the price check (see below) |
| Sell that closes a position | delete position row (same txn as the trade) | `release_if_unused(conn, ticker)` |
| Chat `watchlist_changes` | same as the REST rows | same |

**Buy of a brand-new ticker:** `PLAN.md` §7 says to add it to the watchlist and source, then require a cached price. In simulator mode `add_ticker()` seeds the cache synchronously, so the buy succeeds at the seed price. In Massive mode the cache has no price until the next poll, so the buy returns `400 "Price not yet available for PYPL"`, but the ticker is now watched and the retry succeeds within about 15 s. This is intended behaviour.

**Example: trade route fragment (M1)**

```python
@router.post("/portfolio/trade", response_model=TradeResponse)
async def trade(req: TradeRequest, deps: Deps = Depends(get_deps)) -> TradeResponse:
    if req.side == "buy" and not db.in_watchlist(deps.conn, req.ticker):
        db.add_watchlist(deps.conn, req.ticker)          # commits
        await deps.tracker.ensure_tracked(req.ticker)
    price = deps.price_cache.get_price(req.ticker)
    if price is None:
        raise HTTPException(400, f"Price not yet available for {req.ticker}")
    result = portfolio.execute(deps.conn, deps.price_cache, req, price)  # one transaction
    if result.position_closed:
        await deps.tracker.release_if_unused(deps.conn, req.ticker)
    return result.response
```

---

## 12. FastAPI Lifespan Integration

**File: `app/main.py`** (M1). Market-data-relevant parts shown. DB helpers come from `app/db/`.

```python
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI

from app.db import init_db, open_connection
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.tracking import TickerTracker

load_dotenv()  # before the factory reads MASSIVE_API_KEY


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 1. Database: create schema + seed + initial snapshot if needed
    conn = open_connection()        # honours DB_PATH, creates parent dir
    init_db(conn)

    # 2. Market data: cache → source → start with watchlist ∪ positions
    price_cache = PriceCache()
    source = create_market_data_source(price_cache)
    await source.start(sorted(TickerTracker.desired(conn)))

    app.state.conn = conn
    app.state.price_cache = price_cache
    app.state.market_source = source
    app.state.tracker = TickerTracker(source)

    # 3. Background tasks (snapshot every 30 s: see PLAN.md §7)
    snapshot_task = start_snapshot_task(app)

    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()
        conn.close()


def create_app() -> FastAPI:
    app = FastAPI(title="FinAlly", lifespan=lifespan)
    # The SSE router needs the cache at include time, so the app owns one cache
    # object that the lifespan fills. See note below.
    ...
    return app
```

**Router/cache wiring:** `create_stream_router(price_cache)` needs the cache when the router is built, but `app.include_router` should happen at app construction, not inside the lifespan. Use **one** of these patterns and stick with it:

```python
# Pattern A (recommended): build the cache in create_app(), hand it to the lifespan.
def create_app() -> FastAPI:
    price_cache = PriceCache()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.price_cache = price_cache
        source = create_market_data_source(price_cache)
        ...
        yield
        await source.stop()

    app = FastAPI(title="FinAlly", lifespan=lifespan)
    app.include_router(create_stream_router(price_cache))   # fresh router (T0.4)
    app.include_router(portfolio_router)                     # uses Depends(get_price_cache)
    ...
    return app

app = create_app()   # uvicorn app.main:app


# Dependencies for routes
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache

def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

Pattern A plus T0.4 lets each test build an isolated app with `create_app()` and its own cache, with no shared router state.

**Startup ordering guarantees**

1. `load_dotenv()` runs before `create_market_data_source()`.
2. The DB is initialised before `start()`, because the starting ticker list comes from the DB.
3. `start()` completes before the app accepts requests. Simulator: cache already seeded. Massive: first poll already attempted (≤ ~10 s connect/read timeout if the API is slow, which is acceptable at startup).
4. Shutdown stops the source **before** closing the DB.

---

## 13. Ticker Validation at the API Layer

Sources normalize, but **the API layer validates** (`PLAN.md` §6, §8). Shared type for every request model (M1):

```python
# app/schemas/common.py
import re
from typing import Annotated

from pydantic import AfterValidator, BeforeValidator

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def _normalize(v: object) -> object:
    return v.strip().upper() if isinstance(v, str) else v


def _check(v: str) -> str:
    if not _TICKER_RE.fullmatch(v):
        raise ValueError("ticker must be 1-5 letters A-Z")
    return v


Ticker = Annotated[str, BeforeValidator(_normalize), AfterValidator(_check)]
```

```python
class WatchlistAdd(BaseModel):
    ticker: Ticker            # " pypl " → "PYPL";  "BRK.B" → 422

# DELETE /api/watchlist/{ticker}: validate the path param the same way
from pydantic import TypeAdapter
_ticker_adapter = TypeAdapter(Ticker)
```

LLM-proposed tickers (§9 of the plan) go through the same `TypeAdapter(Ticker).validate_python(...)`. A `ValidationError` becomes an action with `status: "failed"` instead of a 422.

---

## 14. Testing Plan

All tests live in `backend/tests/market/`. They run with `uv run --extra dev pytest`, with `asyncio_mode = "auto"` already configured. The **existing 73 tests stay green.** The tests below are added in M0. Each Task 0 fix has at least one dedicated test, as `PLAN.md` §6 requires.

### 14.1 T0.2: cache `remove()` bumps version (`test_cache.py`)

```python
def test_remove_bumps_version():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v = cache.version
    cache.remove("AAPL")
    assert cache.version == v + 1
    assert "AAPL" not in cache


def test_remove_absent_ticker_does_not_bump_version():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    v = cache.version
    cache.remove("ZZZZ")
    assert cache.version == v


def test_zero_timestamp_is_preserved():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


def test_concurrent_writers_are_consistent():
    import threading
    cache = PriceCache()
    def writer(t):
        for i in range(1000):
            cache.update(t, 100 + i)
    threads = [threading.Thread(target=writer, args=(f"T{i}",)) for i in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()
    assert len(cache) == 8 and cache.version == 8000
```

### 14.2 T0.1: sources normalize (`test_simulator_source.py`, `test_massive.py`)

```python
async def test_simulator_start_normalizes():
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=10)
    await src.start([" aapl", "AAPL", "msft "])
    try:
        assert src.get_tickers() == ["AAPL", "MSFT"]
        assert set(cache.get_all()) == {"AAPL", "MSFT"}
    finally:
        await src.stop()


async def test_simulator_add_remove_normalize():
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=10)
    await src.start(["AAPL"])
    try:
        await src.add_ticker(" pypl ")
        assert "PYPL" in src.get_tickers() and cache.get_price("PYPL") is not None
        await src.remove_ticker("pypl")
        assert "PYPL" not in src.get_tickers() and "PYPL" not in cache
    finally:
        await src.stop()


async def test_simulator_add_before_start_is_kept():
    src = SimulatorDataSource(PriceCache(), update_interval=10)
    await src.add_ticker("nvda")
    await src.start(["AAPL"])
    try:
        assert src.get_tickers() == ["AAPL", "NVDA"]
    finally:
        await src.stop()


async def test_massive_start_normalizes():
    src = MassiveDataSource("k", PriceCache(), poll_interval=60)
    with patch("app.market.massive_client.RESTClient"), \
         patch.object(src, "_fetch_snapshots", return_value=[]):
        await src.start([" aapl", "AAPL", "googl"])
    assert src.get_tickers() == ["AAPL", "GOOGL"]
    await src.stop()


async def test_massive_remove_normalizes():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._tickers = ["AAPL"]
    cache.update("AAPL", 190.0)
    await src.remove_ticker(" aapl ")
    assert src.get_tickers() == [] and "AAPL" not in cache
```

### 14.3 Simulator math (`test_simulator.py`, additions)

```python
def test_full_default_set_plus_unknowns_cholesky_ok():
    sim = GBMSimulator(list(SEED_PRICES) + ["PYPL", "UBER", "COIN"])
    assert sim._cholesky is not None
    for _ in range(100):
        assert all(p > 0 for p in sim.step().values())


def test_per_tick_volatility_matches_sigma():
    np.random.seed(0); random.seed(0)
    sim = GBMSimulator(["MSFT"], event_probability=0.0)
    logs = []
    for _ in range(20_000):
        p1 = sim._prices["MSFT"]; sim.step()
        logs.append(math.log(sim._prices["MSFT"] / p1))
    expected = 0.20 * math.sqrt(GBMSimulator.DEFAULT_DT)
    assert abs(np.std(logs) / expected - 1) < 0.05


def test_correlation_tech_pair_higher_than_cross():
    np.random.seed(1)
    sim = GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0.0)
    rets = {t: [] for t in sim.get_tickers()}
    for _ in range(5_000):
        before = dict(sim._prices); sim.step()
        for t in rets: rets[t].append(math.log(sim._prices[t] / before[t]))
    c = np.corrcoef([rets["AAPL"], rets["MSFT"], rets["JPM"]])
    assert c[0, 1] > c[0, 2] + 0.15   # ~0.6 vs ~0.3
```

### 14.4 Massive parsing against real models (`test_massive.py`)

Replace `MagicMock` snapshots with real ones, so attribute typos fail the tests:

```python
from massive.rest.models import TickerSnapshot

def snap(ticker, price=None, t_ns=None, day_close=None, prev_close=None, updated=None):
    d = {"ticker": ticker}
    if price is not None:
        d["lastTrade"] = {"p": price, **({"t": t_ns} if t_ns else {})}
    if day_close is not None:
        d["day"] = {"c": day_close}
    if prev_close is not None:
        d["prevDay"] = {"c": prev_close}
    if updated is not None:
        d["updated"] = updated
    return TickerSnapshot.from_dict(d)


def test_extract_uses_last_trade_and_ns_timestamp():
    assert extract_price(snap("AAPL", 190.5, t_ns=1_707_580_800_000_000_000)) == (190.5, 1_707_580_800.0)


def test_extract_falls_back_to_day_then_prev_day():
    assert extract_price(snap("AAPL", day_close=189.0, updated=2_000_000_000_000_000_000)) == (189.0, 2_000_000_000.0)
    assert extract_price(snap("AAPL", day_close=0, prev_close=188.0)) == (188.0, None)


def test_extract_none_when_nothing_usable():
    assert extract_price(snap("AAPL")) is None
    assert extract_price(snap("AAPL", price=float("nan"))) is None


async def test_poll_updates_cache_with_real_snapshots():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._tickers, src._client = ["AAPL", "GOOGL"], MagicMock()
    with patch.object(src, "_fetch_snapshots", return_value=[
        snap("AAPL", 190.5, t_ns=1_707_580_800_000_000_000),
        snap("GOOGL", 175.25, t_ns=1_707_580_800_000_000_000),
    ]):
        await src._poll_once()
    assert cache.get_price("AAPL") == 190.5
    assert cache.get("GOOGL").timestamp == 1_707_580_800.0


async def test_removed_mid_poll_not_resurrected():
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    src._tickers, src._client = ["AAPL", "PYPL"], MagicMock()

    def fetch(tickers):
        src._tickers.remove("PYPL")   # simulates DELETE while request is in flight
        return [snap("AAPL", 190.0), snap("PYPL", 60.0)]

    with patch.object(src, "_fetch_snapshots", side_effect=fetch):
        await src._poll_once()
    assert "PYPL" not in cache and cache.get_price("AAPL") == 190.0
```

### 14.5 T0.3 and T0.4: SSE stream (`test_stream.py`, new)

Test the generator directly with a fake request. That's deterministic and needs no server:

```python
import asyncio
import json

from fastapi import FastAPI

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    client = None
    def __init__(self): self.disconnected = False
    async def is_disconnected(self): return self.disconnected


async def take(gen, n):
    return [await gen.__anext__() for _ in range(n)]


async def test_first_frames_retry_then_data():
    cache = PriceCache(); cache.update("AAPL", 190.0)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)
    retry, data = await take(gen, 2)
    assert retry == "retry: 1000\n\n"
    payload = json.loads(data.removeprefix("data: "))
    assert payload["AAPL"]["price"] == 190.0 and len(payload["AAPL"]) == 7


async def test_empty_cache_emits_empty_object():          # T0.3
    cache = PriceCache(); cache.update("AAPL", 190.0)
    gen = _generate_events(cache, FakeRequest(), interval=0.01)
    await take(gen, 2)
    cache.remove("AAPL")                                   # relies on T0.2
    frame = await asyncio.wait_for(gen.__anext__(), 1)
    assert frame == "data: {}\n\n"


async def test_never_populated_cache_still_sends_initial_frame():
    gen = _generate_events(PriceCache(), FakeRequest(), interval=0.01)
    assert (await take(gen, 2))[1] == "data: {}\n\n"


async def test_stops_on_disconnect():
    req = FakeRequest()
    gen = _generate_events(PriceCache(), req, interval=0.01)
    await take(gen, 2)
    req.disconnected = True
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), 1)


def test_factory_returns_fresh_router_each_call():           # T0.4
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert r1 is not r2
    assert [r.path for r in r1.routes] == ["/api/stream/prices"]
    assert [r.path for r in r2.routes] == ["/api/stream/prices"]


def test_two_apps_do_not_share_routes():
    a, b = FastAPI(), FastAPI()
    a.include_router(create_stream_router(PriceCache()))
    b.include_router(create_stream_router(PriceCache()))
    assert sum(r.path == "/api/stream/prices" for r in a.routes) == 1
```

An end-to-end smoke test over HTTP (`httpx.AsyncClient` + `ASGITransport`, reading two lines and then closing) is optional. Streaming responses with in-process transports can hang on disconnect, so the generator-level tests above are the required coverage.

### 14.6 Manual verification

```bash
cd backend
uv run --extra dev pytest -q
uv run --extra dev ruff check app/ tests/
uv run market_data_demo.py          # live Rich dashboard (simulator)
```

With a real key, verify the fix from §7.1 in one shot:

```bash
cd backend
MASSIVE_API_KEY=... uv run python -c "
import asyncio; from app.market import PriceCache; from app.market.massive_client import MassiveDataSource
async def main():
    c = PriceCache(); s = MassiveDataSource('$MASSIVE_API_KEY', c)
    await s.start(['AAPL','MSFT']); print({t: u.to_dict() for t, u in c.get_all().items()}); await s.stop()
asyncio.run(main())"
```

Expected output: two entries with real prices and plausible Unix-second timestamps. Before the fix, it prints `{}`.

---

## 15. Edge Cases and Failure Modes

| Situation | Behaviour |
|---|---|
| Empty watchlist and no positions at startup | `start([])` works. Simulator steps nothing. Massive skips the API call. SSE sends `data: {}` once, then keep-alives. |
| Last ticker removed | Cache empty, version bumped (T0.2), SSE sends `data: {}` (T0.3), frontend clears its map. |
| Remove watchlist ticker that is still held | `release_if_unused` sees it in `positions` and keeps it tracked. Prices keep streaming. |
| Trade on ticker with no cached price | `400 "Price not yet available for <T>"`. Only possible in Massive mode right after an add, or with an invalid symbol. |
| Massive returns no data for a symbol (e.g. delisted) | Never cached. Watchlist shows `price: null`. Trades get 400. |
| Massive 401/403/429/network error | Logged at ERROR, retried next interval, previous prices remain in cache. |
| Massive poll slower than interval | Impossible to overlap: the loop awaits each poll before sleeping. |
| Ticker removed during an in-flight poll | Dropped by the tracked re-check (§7.3). |
| Market closed (Massive) | Prices static, frames still sent each poll with `direction: "flat"`. |
| Simulator step raises (shouldn't happen) | Logged with traceback, loop continues next tick. |
| `add_ticker` before `start` | Simulator queues it. Massive appends to its list. Both pick it up at start. |
| Client disconnects from SSE | Generator exits at the next 0.5 s check. No server-side state to clean. |
| Server restart | Simulator restarts from **seed prices** (prices aren't persisted). Positions keep `avg_cost`, so P&L can jump. This is acceptable for a simulated demo. |
| Multiple uvicorn workers | **Unsupported.** Each worker would run its own simulator with different prices (`PLAN.md` §11). |

---

## 16. Configuration Reference

| Parameter | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | empty | Non-empty selects Massive |
| `poll_interval` | `MassiveDataSource(...)` | `15.0` s | Free tier: ≥ 12 s. Paid: 2–5 s |
| RESTClient timeouts / retries | `massive` defaults | 10 s / 10 s / 3 | Not overridden |
| `update_interval` | `SimulatorDataSource(...)` | `0.5` s | |
| `event_probability` | `SimulatorDataSource(...)` → `GBMSimulator` | `0.001` | Per ticker per tick |
| `dt` | `GBMSimulator(...)` | `0.5 / 5_896_800` | Keep in sync with `update_interval` |
| Unknown-ticker seed range | `GBMSimulator._add_ticker_internal` | `$50–$300` | |
| SSE check interval | `create_stream_router(interval=)` | `0.5` s | |
| SSE keep-alive | `create_stream_router(keepalive_seconds=)` | `15` s | Comment frame |
| SSE retry | `_generate_events` | `1000` ms | |
| Price precision | `PriceCache.update` | 2 dp | |

---

## 17. M0 Change Checklist

Work through these in order. Each line names the file and the test that proves it.

| # | Change | File | Test |
|---|---|---|---|
| T0.1 | Add `normalize_ticker()`. Use it in `start/add_ticker/remove_ticker` of both sources. De-duplicate in `start` | `interface.py`, `simulator.py`, `massive_client.py` | §14.2 |
| T0.2 | `remove()` bumps `_version` when the ticker was present. `version` read under lock | `cache.py` | §14.1 |
| T0.3 | Drop the `if prices:` guard. Always emit the (possibly empty) map on version change | `stream.py` | §14.5 `test_empty_cache_emits_empty_object` |
| T0.4 | Create `APIRouter` inside `create_stream_router()`. Delete the module-level `router` | `stream.py` | §14.5 `test_factory_returns_fresh_router_each_call` |
| T0.5 *(new)* | Massive: read `last_trade.sip_timestamp` (ns), add the `extract_price()` fallbacks, pass a ticker copy to the thread, apply the tracked re-check. Switch the tests to real `TickerSnapshot` objects | `massive_client.py`, `test_massive.py` | §14.4 |
| — | Re-raise `CancelledError` in the SSE generator. Add keep-alive comments | `stream.py` | §14.5 |
| — | Export `normalize_ticker` from `app.market` | `__init__.py` | import in tests |
| — | Record T0.5 in `PLAN.md` §6 Known Gaps. Update `MARKET_DATA_SUMMARY.md` (status, test count) and `backend/CLAUDE.md` (`version` bumps on remove; `data: {}`) | docs | — |

**Done when:** `uv run --extra dev pytest` is green, including every test above. `ruff check app/ tests/` is clean. The `MARKET_DATA_SUMMARY.md` status line says "Task 0 complete".
