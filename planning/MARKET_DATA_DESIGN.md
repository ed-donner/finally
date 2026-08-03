# Market Data Backend — Detailed Design

The implementation-level design for `backend/app/market/`: the subsystem that produces live
prices for FinAlly, whether they come from the built-in simulator or the Massive REST API, and
publishes them to every consumer through one cache and one SSE stream.

This document is the buildable form of three companion documents — `MARKET_INTERFACE.md` (the
contract), `MARKET_SIMULATOR.md` (the default provider), `MASSIVE_API.md` (the real provider) —
plus sections 6 and 13 of `PLAN.md`. Where they describe *what* and *why*, this describes *what
the code is*. Every snippet below is intended to be typed into the repository as written.

**Status.** Most of this module already exists and is tested (73 tests, 84% coverage — see
`MARKET_DATA_SUMMARY.md`). What is missing is the session baseline, sparkline history, and a
handful of correctness fixes. Section 15 is the precise delta from the code currently on disk;
if you are implementing rather than reading, start there and use sections 4–13 as the target
state.

---

## Table of Contents

1. [Scope and responsibilities](#1-scope-and-responsibilities)
2. [Architecture](#2-architecture)
3. [Module map](#3-module-map)
4. [`models.py` — PriceUpdate](#4-modelspy--priceupdate)
5. [`cache.py` — PriceCache](#5-cachepy--pricecache)
6. [`tickers.py` — validation](#6-tickerspy--validation)
7. [`interface.py` — MarketDataSource](#7-interfacepy--marketdatasource)
8. [`seed_prices.py` — parameters](#8-seed_pricespy--parameters)
9. [`simulator.py` — the default source](#9-simulatorpy--the-default-source)
10. [`massive_client.py` — the real source](#10-massive_clientpy--the-real-source)
11. [`factory.py` — the switch](#11-factorypy--the-switch)
12. [`stream.py` — SSE](#12-streampy--sse)
13. [Wiring into FastAPI](#13-wiring-into-fastapi)
14. [Consumer recipes](#14-consumer-recipes)
15. [Delta from the current implementation](#15-delta-from-the-current-implementation)
16. [Testing](#16-testing)
17. [Failure modes](#17-failure-modes)

---

## 1. Scope and responsibilities

**In scope for `app/market/`:**

- Producing a current price for every tracked ticker, from one of two sources
- Holding those prices, their session baselines, and ~60 points of recent history
- Validating ticker symbols (one rule, shared by every caller)
- Streaming price changes to browsers over SSE
- Deciding which source runs, from one environment variable

**Explicitly out of scope** — these belong to the portfolio, watchlist, and chat routers:

- Persistence. Nothing in this module touches SQLite. Prices are process state and are meant to
  be lost on restart.
- The watchlist itself. The database owns the list of tickers; this module is *told* which
  tickers to track, at startup and on change.
- Trade execution, valuation, P&L. Those read the cache; the cache does not know they exist.

The load-bearing rule: **no code outside `app/market/` knows which source is running.** A
consumer holds a `PriceCache` and reads it. It cannot tell a 500ms simulator from a 15-second
poller, and it has no API through which to ask.

---

## 2. Architecture

```
                       MASSIVE_API_KEY set?
                                │
              ┌─────────────────┴─────────────────┐
              │ yes                            no │
              ▼                                   ▼
      MassiveDataSource                   SimulatorDataSource
   REST snapshot poll, 15s               GBM step, 500ms, in-process
   asyncio.to_thread (SDK is sync)       pure numpy, no I/O
              │                                   │
              └─────────────────┬─────────────────┘
                                │  cache.update(ticker, price, timestamp)
                                ▼
                    ┌───────────────────────┐
                    │      PriceCache       │   latest / previous / open price
                    │  threading.Lock       │   version counter
                    │  version: int         │   deque(maxlen=60) history
                    └───────────┬───────────┘
                                │  read-only
        ┌──────────────┬────────┴────────┬──────────────────┐
        ▼              ▼                 ▼                  ▼
   SSE stream     portfolio          trade fill        /api/watchlist
 /api/stream/…    valuation         price lookup       price + history
```

Three properties fall out of this shape, and each is worth stating because breaking any one of
them breaks the module:

**Producers push, consumers pull.** The source never returns a price to a caller; it writes to
the cache on its own schedule. That inversion is what makes a 500ms producer and a 15-second
producer interchangeable — the reader always finds a current price and never learns how old it
is or where it came from.

**One writer at a time, many readers, always.** The lock is a `threading.Lock` rather than an
`asyncio.Lock` because the Massive poller writes from a worker thread (`asyncio.to_thread`)
while the simulator writes from the event loop. An `asyncio.Lock` would be silently wrong for
the threaded path.

**Change is a number, not an event.** The cache carries a monotonic `version`. The SSE generator
remembers the version it last sent. No pub/sub, no per-client queues, no fan-out bookkeeping —
adding a client costs one integer comparison every 500ms.

---

## 3. Module map

```
backend/app/market/
├── __init__.py          Public API re-exports
├── models.py            PriceUpdate                                    ~70 lines
├── cache.py             PriceCache, wait_for_price                    ~150 lines
├── tickers.py           TICKER_PATTERN, normalize_ticker                ~25 lines
├── interface.py         MarketDataSource (ABC)                         ~70 lines
├── seed_prices.py       Seed prices, GBM params, groups, synthesize    ~90 lines
├── simulator.py         GBMSimulator + SimulatorDataSource            ~230 lines
├── massive_client.py    MassiveDataSource                             ~200 lines
├── factory.py           create_market_data_source()                    ~35 lines
└── stream.py            create_stream_router()                         ~90 lines
```

```python
# app/market/__init__.py
"""Market data subsystem for FinAlly."""

from .cache import PriceCache, wait_for_price
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router
from .tickers import TICKER_PATTERN, normalize_ticker

__all__ = [
    "MarketDataSource",
    "PriceCache",
    "PriceUpdate",
    "TICKER_PATTERN",
    "create_market_data_source",
    "create_stream_router",
    "normalize_ticker",
    "wait_for_price",
]
```

Concrete source classes (`SimulatorDataSource`, `MassiveDataSource`, `GBMSimulator`) are
deliberately *not* re-exported. Application code should never name them — it calls the factory
and holds a `MarketDataSource`. Tests import them from their modules directly.

---

## 4. `models.py` — PriceUpdate

One ticker, one moment, immutable.

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    Constructed only by PriceCache.update(), which supplies previous_price and
    open_price from the entry it is replacing. Sources never build one directly.
    """

    ticker: str
    price: float
    previous_price: float
    open_price: float
    timestamp: float = field(default_factory=time.time)  # Unix epoch seconds

    # --- Derived: tick over tick ---

    @property
    def change(self) -> float:
        """Absolute price change since the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percent change since the previous tick. Drives the flash animation only."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down' or 'flat' since the previous tick."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    # --- Derived: against the session baseline ---

    @property
    def change_from_open(self) -> float:
        """Absolute price change since the session open."""
        return round(self.price - self.open_price, 4)

    @property
    def change_from_open_percent(self) -> float:
        """Percent change since the session open. This is the user-facing 'change %'."""
        if self.open_price == 0:
            return 0.0
        return round((self.price - self.open_price) / self.open_price * 100, 4)

    def to_dict(self) -> dict:
        """Serialise for JSON / SSE. Keys match the payload in PLAN.md section 6."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "open_price": self.open_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "change_from_open_percent": self.change_from_open_percent,
            "direction": self.direction,
        }
```

### Two "change" numbers, and which one the UI uses

`change_percent` compares against the previous tick. At 500ms that number flickers around zero
and is meaningless as a daily-change column — it reports how the last half-second went.

`open_price` is the **session baseline**: the first price seen for a ticker after process start,
or after the ticker was added to a running system. `change_from_open_percent` measures against
it, which is the number a user recognises as "how is it doing today".

| Consumer | Field |
|---|---|
| Watchlist "change %" column | `change_from_open_percent` |
| Price colour (green/red text) | `change_from_open_percent` |
| Flash animation on tick | `direction` / `change_percent` |
| Main chart, sparkline | `price` |

The baseline survives page reloads and SSE reconnects because it lives on the server, and resets
on container restart. For a simulation that is the right trade: no persistence, no market
calendar, no timezone logic.

### Why frozen and slotted

A `PriceUpdate` is handed to JSON serialisation on one task and to portfolio arithmetic on
another, concurrently. Nothing should be able to mutate one after the cache published it —
`frozen=True` makes that a `FrozenInstanceError` instead of a heisenbug. `slots=True` drops the
per-instance `__dict__`, which matters when 10–50 of these are allocated twice a second for the
life of the container.

### Rounding convention

`price` and `previous_price` and `open_price` are rounded to 2dp **by the cache** before the
dataclass is built, so every consumer sees the same cent value and no consumer needs to round.
Derived percentages round to 4dp — enough to show `+0.687%` without a wall of float noise.

---

## 5. `cache.py` — PriceCache

The single point of truth. Everything else in the app reads prices from here.

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from threading import Lock

from .models import PriceUpdate

HISTORY_POINTS = 60          # Points retained per ticker for sparklines
HISTORY_INTERVAL_SECONDS = 60.0   # Minimum spacing between recorded points


class PriceCache:
    """Latest price, session baseline and recent history for each tracked ticker.

    Writers: exactly one MarketDataSource (simulator or Massive poller).
    Readers: SSE stream, portfolio valuation, trade execution, watchlist.
    """

    def __init__(
        self,
        history_points: int = HISTORY_POINTS,
        history_interval: float = HISTORY_INTERVAL_SECONDS,
    ) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[float]] = {}
        self._history_at: dict[str, float] = {}
        self._history_points = history_points
        self._history_interval = history_interval
        self._lock = Lock()
        self._version = 0

    # --- Writing ---

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. Returns the stored PriceUpdate.

        Derives previous_price and open_price from the entry being replaced, so
        sources stay dumb: they produce a number, the cache supplies the meaning.
        The first update for a ticker pins its session baseline —
        previous_price == open_price == price, direction 'flat', both changes 0.
        """
        with self._lock:
            ts = time.time() if timestamp is None else timestamp
            price = round(price, 2)
            previous = self._prices.get(ticker)

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous.price if previous else price,
                open_price=previous.open_price if previous else price,
                timestamp=ts,
            )
            self._prices[ticker] = update

            # Version tracks *visible* change. A repeated price refreshes the
            # timestamp (so /api/health still sees a live feed) without waking
            # every SSE client to re-send an identical payload.
            if previous is None or previous.price != price:
                self._version += 1

            self._record_history(ticker, update)
            return update

    def seed_history(
        self, ticker: str, prices: list[float], timestamp: float | None = None
    ) -> None:
        """Install backfilled history for a ticker, replacing anything present.

        Called by a source at startup and when a ticker is added, so sparklines
        are populated on first paint rather than filling in over 30 seconds.
        """
        with self._lock:
            self._history[ticker] = deque(
                (round(p, 2) for p in prices[-self._history_points :]),
                maxlen=self._history_points,
            )
            self._history_at[ticker] = time.time() if timestamp is None else timestamp

    def remove(self, ticker: str) -> None:
        """Forget a ticker entirely — price, baseline and history."""
        with self._lock:
            self._prices.pop(ticker, None)
            self._history.pop(ticker, None)
            self._history_at.pop(ticker, None)

    # --- Reading ---

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of every current price. Safe to iterate without the lock."""
        with self._lock:
            return dict(self._prices)

    def get_history(self, ticker: str) -> list[float]:
        """Recent prices, oldest first, up to history_points. Empty if unknown."""
        with self._lock:
            history = self._history.get(ticker)
            return list(history) if history else []

    def newest_timestamp(self) -> float | None:
        """Timestamp of the most recently written price, for /api/health."""
        with self._lock:
            if not self._prices:
                return None
            return max(update.timestamp for update in self._prices.values())

    @property
    def version(self) -> int:
        """Monotonic counter, bumped whenever a price actually changes."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices

    # --- Internal (callers already hold the lock) ---

    def _record_history(self, ticker: str, update: PriceUpdate) -> None:
        history = self._history.get(ticker)
        if history is None:
            history = self._history[ticker] = deque(maxlen=self._history_points)

        last_at = self._history_at.get(ticker)
        if last_at is None or update.timestamp - last_at >= self._history_interval:
            history.append(update.price)
            self._history_at[ticker] = update.timestamp
```

### Why `update()` computes the derived fields

Callers pass a raw float. The cache looks up the previous entry, carries `open_price` forward,
and constructs the `PriceUpdate`. Sources stay dumb — they fetch or generate a number and hand
it over — and all the semantics live in exactly one function. The alternative, sources building
their own `PriceUpdate`s, means the simulator and the Massive client each have their own copy of
the baseline rule and they drift within a month.

### Why the history cadence is one point per minute

The sparkline holds 60 points. If every 500ms tick appended one, the entire series would span 30
seconds and would overwrite the backfill within half a minute — the sparkline would show noise
instead of shape, and the backfill would have been pointless.

`HISTORY_INTERVAL_SECONDS = 60.0` matches the cadence the simulator's backfill generates (see
§9), so the seeded points and the live points are on the same time axis and a 60-point series
always means "the last hour". The frontend extends its own copy live from the SSE stream, which
is what makes the sparkline move between reloads; the server's copy exists only so a fresh page
paints something real.

Tests pass `history_interval=0.0` to record every update.

### Why `version` only moves on a real change

`PLAN.md` requires that a quiet market produce no price events. Off-hours on the Massive path,
every 15-second poll returns the same last trade; bumping the version on those writes would push
an identical payload to every client four times a minute and make the "emit only on change"
guarantee a lie. Comparing the rounded price is the cheapest honest test — sub-cent simulator
jitter on a low-volatility name like V is also correctly treated as no change.

The timestamp is still refreshed on a repeated price, so `/api/health` can distinguish "the feed
is running and the price is flat" from "the feed died twenty minutes ago".

### `wait_for_price` — the one asynchronous helper

A just-added ticker has no price for a few hundred milliseconds. Failing the trade would be a
poor experience for the headline demo ("ask the AI to buy something it just added"), so trade
execution waits briefly instead.

```python
async def wait_for_price(cache: PriceCache, ticker: str, timeout: float = 2.0) -> float:
    """Return the current price, waiting up to `timeout` for a first tick.

    Raises ValueError with a user-facing message if no price arrives. Callers
    translate that into a 400 (PLAN.md section 8).
    """
    deadline = time.monotonic() + timeout
    while True:
        price = cache.get_price(ticker)
        if price is not None:
            return price
        if time.monotonic() >= deadline:
            raise ValueError(f"No price available for {ticker} yet, please try again")
        await asyncio.sleep(0.2)
```

On the simulator path this never expires: `add_ticker()` seeds the cache synchronously before it
returns. On the Massive path with a 15-second poll it genuinely can, which is why the message is
retryable rather than an error about an unknown symbol.

---

## 6. `tickers.py` — validation

Manual adds, LLM-driven adds, watchlist deletes and trades all funnel through one function.

```python
"""Ticker symbol validation — one rule, shared by every caller."""

from __future__ import annotations

import re

TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}$")


def normalize_ticker(raw: str) -> str:
    """Uppercase, strip, and validate a ticker symbol.

    Raises ValueError if the symbol is not 1-5 A-Z characters. Callers turn
    that into a 400 with the message shown to the user verbatim.
    """
    ticker = raw.strip().upper()
    if not TICKER_PATTERN.match(ticker):
        raise ValueError(f"Invalid ticker symbol: {raw!r}")
    return ticker
```

Uppercase first, then match — so `aapl` is accepted and normalised, while `hello world`, `12345`
and `""` are rejected. One function, one regex, one error message, so the manual path and the LLM
path cannot drift apart.

This validates **shape, not existence**. `ZZZZZ` is accepted and gets a synthesised price (§8).
The simulator has no universe of real symbols and could not do otherwise; rejecting unknown
symbols would dead-end "ask the AI to watch a new stock" on the first plausible thing anyone
tries.

---

## 7. `interface.py` — MarketDataSource

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push prices into a shared PriceCache on their own schedule.
    Downstream code never asks a source for a price — it reads the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])   # cache populated on return
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        await source.stop()                          # idempotent
    """

    @property
    @abstractmethod
    def source_name(self) -> str:
        """Short identifier for logs and /api/health: 'simulator' or 'massive'."""

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing prices for `tickers`.

        Must populate the cache (prices and seeded history) before returning, so
        the first HTTP request never sees an empty cache. Called exactly once.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Idempotent, and never writes afterwards."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker. No-op if already tracked. Seeds price and history."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and remove it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers. Synchronous — called from request handlers."""
```

Five methods and one property. Notice what is missing: **there is no `get_price()`**. A source
cannot be asked for a price. Omitting the read method is what enforces the architecture — a
consumer that tries to bypass the cache finds there is no way to.

`add_ticker` and `remove_ticker` are `async` because the Massive implementation awaits a history
backfill request; the simulator's are trivially async. `get_tickers` is synchronous because it
reads local state from inside request handlers.

`source_name` exists so `/api/health` can report which provider is running without an `isinstance`
check leaking concrete classes into the router — and because "why are prices not moving" is the
most likely support question in this project.

---

## 8. `seed_prices.py` — parameters

```python
"""Seed prices, GBM parameters and correlation groups for the simulator."""

from __future__ import annotations

import hashlib

# Recognisable rather than current. This is a simulation with pretend money.
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

# sigma: annualised volatility. mu: annualised drift.
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

# Correlation groups. TSLA is deliberately in neither: nominally tech, famously
# does its own thing, and its independence gives the watchlist texture.
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR = 0.6      # Tech names move together
INTRA_FINANCE_CORR = 0.5   # Finance names move together
CROSS_GROUP_CORR = 0.3     # Across sectors, TSLA, and synthesised tickers


def synthesize_params(ticker: str) -> tuple[float, dict[str, float]]:
    """Derive a stable seed price and GBM parameters from the symbol itself.

    Deterministic by construction: SHA-256 of the symbol, never random. A user
    holding 10 shares of PYPL bought at $73 must not restart the container and
    find PYPL trading at $412 — their P&L would be nonsense.

    Ranges are chosen so every synthesised ticker looks like an ordinary
    large-cap: $20-$500, sigma 0.15-0.50, mu 0.02-0.08.
    """
    digest = hashlib.sha256(ticker.encode()).digest()
    price = 20.0 + (int.from_bytes(digest[0:4], "big") % 48_000) / 100.0
    sigma = 0.15 + (digest[4] / 255.0) * 0.35
    mu = 0.02 + (digest[5] / 255.0) * 0.06
    return round(price, 2), {"sigma": round(sigma, 4), "mu": round(mu, 4)}


def params_for(ticker: str) -> tuple[float, dict[str, float]]:
    """Seed price and GBM parameters for any well-formed ticker."""
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker], dict(TICKER_PARAMS[ticker])
    return synthesize_params(ticker)
```

### The spread in sigma is the point

TSLA at 0.50 against V at 0.17 means TSLA visibly jumps while V barely moves — the watchlist has
texture instead of ten lines doing the same thing. NVDA carries the strongest drift so something
in the portfolio tends to trend upward, which makes the P&L chart more interesting than a random
walk around zero.

### Positive definiteness

`np.linalg.cholesky` raises on a matrix that is not positive definite, and correlations assigned
pairwise by ad-hoc rules are not guaranteed to be. The structure above is safe, and it is worth
recording why so that a future edit can check itself.

With TSLA excluded from both groups, the matrix decomposes as

```
C = 0.3·J  +  0.3·J_tech  +  0.2·J_finance  +  diag(residual)
```

where `J` is the all-ones matrix and `J_tech` / `J_finance` are all-ones on their block. Each
term is positive semi-definite, and every residual on the diagonal is strictly positive
(0.4 for tech, 0.5 for finance, 0.7 for TSLA and synthesised names), so `C` is positive definite.

The invariant to preserve: **within-group correlation ≥ cross-group correlation, and the sum of
the block values on any diagonal entry < 1.** Anything more elaborate — high correlations, more
groups, exceptions layered on exceptions — needs either a check or a nearest-positive-definite
repair. §9 keeps a defensive fallback for the day someone tries.

---

## 9. `simulator.py` — the default source

The simulator is not a fallback for people without an API key. It is the better demo: prices
always move, there are no market hours, no latency and no signup. A student running the app at
21:00 on a Sunday against real data sees ten flat prices and reasonably concludes it is broken.

The file splits into the maths (pure, synchronous, testable without a clock) and the plumbing
(asyncio, cache writes, no maths).

### 9.1 `GBMSimulator` — the maths

```
S(t+dt) = S(t) · exp( (mu − sigma²/2)·dt + sigma·√dt·Z )
```

The `−sigma²/2` correction is not cosmetic. Without it `mu` is the drift of *log* price and the
expected price grows faster than `mu`, so prices inflate visibly over a long session.

`dt` must be a fraction of a trading year because `mu` and `sigma` are annualised. A trading year
is 252 days of 6.5 hours; a 500ms tick is therefore ~8.48e-8 of one. That tiny `dt` is what makes
the output look right: a $190 stock at sigma 0.22 moves on the order of a cent per tick and
wanders a few tenths of a percent over a minute. Using `dt = 0.5` (half a *year* per tick) sends
prices to five figures inside a minute — the classic failure here.

```python
"""GBM-based market simulator."""

from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import HISTORY_POINTS, PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    params_for,
)

logger = logging.getLogger(__name__)

TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8 for a 500ms tick
HISTORY_STEP_TICKS = 120                      # One backfill point per minute


class GBMSimulator:
    """Correlated geometric Brownian motion over a set of tickers.

    Pure and synchronous: holds prices, parameters and the Cholesky factor, and
    knows nothing about the cache, asyncio or FastAPI. Tests drive step()
    directly with a large dt instead of sleeping.
    """

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
            self._add_internal(ticker)
        self._rebuild_cholesky()

    # --- Public API ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: rounded price}.

        The hot path: called every 500ms forever. All n normals are drawn in one
        numpy call and correlated with one matrix multiply rather than looping.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        prices: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # Random shock: GBM alone is smooth, real markets jump. ~0.1% per
            # tick per ticker is an event every ~50s across ten tickers — often
            # enough to see in a minute, rare enough to stay an event.
            if random.random() < self._event_prob:
                magnitude = random.uniform(0.02, 0.05)
                sign = random.choice([-1, 1])
                self._prices[ticker] *= 1 + magnitude * sign
                logger.debug(
                    "Shock event on %s: %.1f%% %s",
                    ticker,
                    magnitude * 100,
                    "up" if sign > 0 else "down",
                )

            prices[ticker] = round(self._prices[ticker], 2)

        return prices

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        price = self._prices.get(ticker)
        return round(price, 2) if price is not None else None

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def backfill_history(self, ticker: str, points: int = HISTORY_POINTS) -> list[float]:
        """Manufacture plausible prior history ending at the current price.

        Runs the GBM recurrence *backwards* — dividing rather than multiplying —
        so the series ends at the live price and joins the stream continuously.
        The coarser dt gives a per-minute cadence, so 60 points is an hour of
        price action with visible shape rather than 30 seconds of flat line.
        """
        params = self._params.get(ticker)
        if params is None or points < 1:
            return []

        mu, sigma = params["mu"], params["sigma"]
        dt = self._dt * HISTORY_STEP_TICKS
        drift = (mu - 0.5 * sigma**2) * dt
        scale = sigma * math.sqrt(dt)

        price = self._prices[ticker]
        history = [round(price, 2)]
        for z in np.random.standard_normal(points - 1):
            price /= math.exp(drift + scale * z)
            history.append(round(price, 2))

        history.reverse()  # Oldest first, ending at the current price
        return history

    # --- Internals ---

    def _add_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky, for batch initialisation."""
        if ticker in self._prices:
            return
        price, params = params_for(ticker)
        self._tickers.append(ticker)
        self._prices[ticker] = price
        self._params[ticker] = params

    def _rebuild_cholesky(self) -> None:
        """Refactor the correlation matrix. O(n^3), called only on add/remove."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Should be unreachable with the block structure in seed_prices.py
            # (see section 8). Degrade to independent draws rather than taking
            # the price feed down over a correlation constant.
            logger.error("Correlation matrix not positive definite; using independent draws")
            self._cholesky = None

    @staticmethod
    def _pairwise_correlation(a: str, b: str) -> float:
        """Sector-based correlation: tech 0.6, finance 0.5, everything else 0.3."""
        if a in CORRELATION_GROUPS["tech"] and b in CORRELATION_GROUPS["tech"]:
            return INTRA_TECH_CORR
        if a in CORRELATION_GROUPS["finance"] and b in CORRELATION_GROUPS["finance"]:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR
```

Independent random walks look wrong. Real markets move together — when tech sells off, it sells
off broadly. Ten independently wandering lines read as noise; correlated ones read as a market.
Cholesky is how that is imposed: given `C = L·Lᵀ` and independent standard normals `z`, the
product `L @ z` has exactly the correlation structure of `C`.

The shock line is the single highest-value line in the simulator for demo purposes. It is what
makes the flash animations fire and the P&L chart do something worth looking at.

### 9.2 `SimulatorDataSource` — the plumbing

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by the GBM simulator.

    Owns one asyncio task that steps the simulation every `update_interval`
    seconds and writes each price to the cache. No maths, no I/O.
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

    @property
    def source_name(self) -> str:
        return "simulator"

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers, event_probability=self._event_prob)

        # Populate the cache *before* the loop starts, so start() returns with
        # prices and sparklines already available and the first HTTP request
        # never sees an empty cache.
        for ticker in tickers:
            self._seed(ticker)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started: %d tickers, %.0fms interval",
                    len(tickers), self._interval * 1000)

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
        if self._sim is None:
            return
        if ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        self._seed(ticker)
        logger.info("Simulator: tracking %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim is not None:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        logger.info("Simulator: dropped %s", ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    # --- Internals ---

    def _seed(self, ticker: str) -> None:
        """Backfill history then publish the first price. Order matters: the
        history must be in place before the price the sparkline ends at."""
        assert self._sim is not None
        price = self._sim.get_price(ticker)
        if price is None:
            return
        self._cache.seed_history(ticker, self._sim.backfill_history(ticker))
        self._cache.update(ticker, price)

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim is not None:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker, price)
            except Exception:
                # A background task that raises dies silently and takes the whole
                # price feed with it, leaving a UI that looks connected and
                # frozen. Log and take the next tick.
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Two behaviours carry weight:

**The cache is seeded before the task starts.** `start()` and `add_ticker()` both return with a
price already published, which is what stops `wait_for_price` from ever mattering on the
simulator path.

**The loop catches and continues.** It gets another chance in 500ms; dying is never the better
option.

---

## 10. `massive_client.py` — the real source

Massive (formerly Polygon.io) rebranded on 30 October 2025; existing keys and `api.polygon.io`
URLs still work. The SDK is the `massive` PyPI package, **synchronous urllib3 under the hood**.

Three constraints shape this file:

1. **The free tier allows 5 requests/minute.** One request per ticker is not viable. The poller
   fetches every watched ticker in a *single* snapshot request and defaults to a 15-second
   interval (4 requests/minute, leaving headroom).
2. **The SDK blocks.** Every call goes through `asyncio.to_thread`. A blocking HTTP call on the
   event loop freezes every SSE connection and every API request for its duration.
3. **A market data failure must never take down the app.** The loop catches broadly, logs, backs
   off, and lives to poll again. Stale prices are strictly better than a 500 on every request.

```python
"""Massive (Polygon.io) REST client for real market data."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

from .cache import HISTORY_POINTS, PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

MAX_BACKOFF_MULTIPLIER = 8.0
BACKFILL_LOOKBACK_DAYS = 7


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive REST API.

    Polls /v2/snapshot/locale/us/markets/stocks/tickers for the union of watched
    tickers in one request, then writes each result to the PriceCache.

    Rate limits:
      Basic (free): 5 req/min  -> poll_interval 15.0 (default)
      Paid tiers:   unlimited  -> poll_interval 2.0-5.0
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        backfill_history: bool = True,
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._backfill_enabled = backfill_history
        self._tickers: list[str] = []
        self._client: RESTClient | None = None
        self._task: asyncio.Task | None = None
        self._backfill_task: asyncio.Task | None = None
        self._backoff = 1.0

    @property
    def source_name(self) -> str:
        return "massive"

    # --- Lifecycle ---

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = [t.upper() for t in tickers]

        await self._log_market_status()
        await self._poll_once()  # Cache has prices before start() returns

        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        if self._backfill_enabled:
            self._backfill_task = asyncio.create_task(
                self._backfill_all(list(self._tickers)), name="massive-backfill"
            )
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval", len(self._tickers), self._interval
        )

    async def stop(self) -> None:
        for task in (self._task, self._backfill_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._task = self._backfill_task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        logger.info("Massive: tracking %s (price arrives on the next poll)", ticker)
        if self._backfill_enabled:
            await self._backfill_one(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._cache.remove(ticker)
        logger.info("Massive: dropped %s", ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- Polling ---

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval * self._backoff)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """One poll cycle. Never raises — the loop must survive every failure."""
        if not self._tickers or self._client is None:
            return

        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
        except Exception as exc:
            # 401 bad key, 403 not in plan, 429 rate limited, network, timeout.
            self._backoff = min(self._backoff * 2, MAX_BACKOFF_MULTIPLIER)
            logger.error(
                "Massive poll failed (%s); backing off to %.1fs",
                exc,
                self._interval * self._backoff,
            )
            return

        self._backoff = 1.0
        updated = 0
        for snap in snapshots:
            quote = self._extract_quote(snap)
            if quote is None:
                logger.warning("No usable price in snapshot for %s", getattr(snap, "ticker", "???"))
                continue
            price, timestamp = quote
            self._cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
            updated += 1

        logger.debug("Massive poll: updated %d/%d tickers", updated, len(self._tickers))

    def _fetch_snapshots(self) -> list:
        """Synchronous SDK call. Runs on a worker thread."""
        assert self._client is not None
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )

    @staticmethod
    def _extract_quote(snap) -> tuple[float, float] | None:
        """Best available price and its Unix-seconds timestamp, or None.

        TIMESTAMP UNITS ARE NOT UNIFORM IN THIS API:
            last_trade.timestamp, last_quote.timestamp, snapshot.updated -> NANOseconds
            Agg.timestamp (aggregate bars), min.timestamp               -> MILLIseconds

        Massive's own sample lastTrade.t of 1605195918306274000 is 2020-11-12
        when divided by 1e9, and out of range for any other unit. Dividing by
        1e3 puts every price roughly 50,000 years in the future, silently.
        """
        trade = getattr(snap, "last_trade", None)
        if trade is not None and getattr(trade, "price", None):
            return float(trade.price), float(trade.timestamp) / 1_000_000_000.0

        minute = getattr(snap, "min", None)
        if minute is not None and getattr(minute, "close", None):
            return float(minute.close), float(minute.timestamp) / 1_000.0

        day = getattr(snap, "day", None)
        if day is not None and getattr(day, "close", None):
            return float(day.close), time.time()

        return None

    # --- History backfill ---

    async def _backfill_all(self, tickers: list[str]) -> None:
        """Seed sparkline history, one ticker at a time, spaced to respect the
        rate limit. Off the poll loop: once at startup, once per added ticker.

        On the free tier this shares a 5 req/min budget with the poller, so
        sparklines fill in over the first minutes rather than instantly. That is
        the correct trade — a burst of ten requests at startup earns a 429 and
        no history at all.
        """
        for ticker in tickers:
            await self._backfill_one(ticker)
            await asyncio.sleep(self._interval)

    async def _backfill_one(self, ticker: str) -> None:
        if self._client is None:
            return
        try:
            history = await asyncio.to_thread(self._fetch_history, ticker)
        except Exception as exc:
            logger.warning("History backfill failed for %s: %s", ticker, exc)
            return
        if history:
            self._cache.seed_history(ticker, history)
            logger.debug("Backfilled %d history points for %s", len(history), ticker)

    def _fetch_history(self, ticker: str) -> list[float]:
        """Most recent ~60 one-minute closes. Synchronous; runs on a thread."""
        assert self._client is not None
        today = dt.date.today()
        bars = self._client.get_aggs(
            ticker=ticker,
            multiplier=1,
            timespan="minute",
            from_=(today - dt.timedelta(days=BACKFILL_LOOKBACK_DAYS)).isoformat(),
            to=today.isoformat(),
            limit=HISTORY_POINTS,
            sort="desc",
        )
        return [float(bar.close) for bar in reversed(list(bars))]  # Oldest first

    # --- Diagnostics ---

    async def _log_market_status(self) -> None:
        """Log whether the market is open. 'Prices are not moving' is the most
        likely support question on this path, and this line is the answer."""
        if self._client is None:
            return
        try:
            status = await asyncio.to_thread(self._client.get_market_status)
            logger.info("Massive market status: %s", getattr(status, "market", "unknown"))
        except Exception as exc:
            logger.warning("Could not read market status: %s", exc)
```

### Behaviour outside market hours is not a bug

US equities trade 09:30–16:00 ET on weekdays. Outside that window `last_trade` holds the final
trade of the previous session and never changes. Consequently:

- Every price is flat and no flash animation fires
- `change_from_open_percent` sits at zero
- The cache version never advances, so the SSE stream emits only heartbeats

This is correct, and it is the main reason the simulator stays the recommended default.
`/api/health` reporting `market_source` and `newest_price_age_seconds` is what lets a user tell
this apart from a dead backend.

Free-tier data is additionally 15 minutes delayed, and real-time snapshot access requires Starter
or above; on the free tier the aggregate endpoints are the dependable ones.

### Optional: a real daily open on the Massive path

The cache's session baseline is "the first price seen after start", which is uniform across both
sources and needs no market calendar. On real data a truer baseline is available —
`snap.day.open` — and can be adopted without disturbing the abstraction by adding one write-once
method to the cache:

```python
class PriceCache:
    def __init__(self, ...):
        ...
        self._pending_opens: dict[str, float] = {}   # additional field

    def seed_open_price(self, ticker: str, open_price: float) -> None:
        """Supply a real session open. Write-once, before the first update()."""
        with self._lock:
            if ticker in self._prices:
                return  # Baseline already pinned; do not move it under the UI
            self._pending_opens[ticker] = round(open_price, 2)

    # and inside update(), for a ticker with no previous entry:
    #   open_price = self._pending_opens.pop(ticker, price)
```

The Massive poller would then call `cache.seed_open_price(snap.ticker, snap.day.open)` on its
first sight of each ticker. This is a refinement, not part of the core build — the change % it produces is more meaningful, and it is
the only place where the two sources would differ in behaviour rather than in timing.

---

## 11. `factory.py` — the switch

```python
"""Factory selecting the market data source from the environment."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Return an UNSTARTED market data source.

    MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real data)
    otherwise                         -> SimulatorDataSource (GBM simulation)

    The caller owns the lifecycle, which keeps this function synchronous and
    trivially testable.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

`.strip()` carries real weight: `.env` files routinely contain `MASSIVE_API_KEY=` with nothing
after it, and an empty or whitespace-only value must mean "use the simulator", not "authenticate
with an empty key". The log line is not decoration either — it is the first thing to look at when
someone reports that prices are not moving.

---

## 12. `stream.py` — SSE

```python
"""SSE endpoint for live price updates."""

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

POLL_INTERVAL = 0.5      # How often the generator looks at the cache
HEARTBEAT_INTERVAL = 15.0  # Comment frame cadence, price activity or not


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """Build the /api/stream router bound to a specific cache.

    The router is created inside the factory, not at module level, so calling
    this twice (an app plus a test app) does not register the route twice.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        """Live price stream for the browser's EventSource."""
        return StreamingResponse(
            _generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                # Stop nginx buffering the stream into uselessness if proxied
                "X-Accel-Buffering": "no",
            },
        )

    return router


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = POLL_INTERVAL,
    heartbeat: float = HEARTBEAT_INTERVAL,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames until the client disconnects.

    Emits one data event carrying EVERY tracked ticker, keyed by symbol, and
    only when the cache version has moved. A heartbeat comment goes out every
    `heartbeat` seconds regardless, so silence is legible to the frontend.
    """
    client = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client)

    # EventSource reconnects on its own; tell it how long to wait.
    yield "retry: 1000\n\n"

    last_version = -1
    last_beat = time.monotonic()

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client)
                break

            version = price_cache.version
            if version != last_version:
                last_version = version
                prices = price_cache.get_all()
                if prices:
                    payload = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(payload)}\n\n"

            now = time.monotonic()
            if now - last_beat >= heartbeat:
                yield ": ping\n\n"
                last_beat = now

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled: %s", client)
        raise
```

### Wire format

```
retry: 1000

data: {"AAPL": {"ticker":"AAPL","price":190.50,"previous_price":190.40,
                "open_price":189.20,"timestamp":1753401234.5,
                "change":0.10,"change_percent":0.052,
                "change_from_open_percent":0.687,"direction":"up"},
       "GOOGL": {...}}

: ping

```

**One event for all tickers, not one per ticker.** Ten tickers at 2Hz as separate events would be
20 messages per second per client, each needing its own parse and its own React state update. One
keyed object is one parse and one batched update — and it lets the frontend recompute the
portfolio total exactly once per frame (`PLAN.md` §10).

**Emit only on change.** Overnight on real data the version never advances and the stream costs
nothing but heartbeats.

**The heartbeat is what makes silence legible.** Without it the frontend cannot tell "connected,
market quiet" from "backend stalled" — both look like no data. With it, the connection dot can be
honest: green when a price event or heartbeat arrived within 30s, yellow when the stream is open
but silent for longer or `readyState === CONNECTING`, red when `CLOSED`. A comment frame (`:` and
no field name) is ignored by `EventSource` handlers but still resets the frontend's liveness
timer, which is exactly the semantic wanted.

**Timestamps here are epoch seconds** — the SSE payload is the *only* place in FinAlly that uses
them. Every REST timestamp and every `*_at` column is an ISO 8601 UTC string. The two never mix
inside one payload.

---

## 13. Wiring into FastAPI

```python
# app/main.py
from __future__ import annotations

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from app.market import PriceCache, create_market_data_source, create_stream_router

# Genuinely process-global state with a single lifetime.
price_cache = PriceCache()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()                                   # lazy init + seed
    tickers = load_watchlist_tickers()                # from SQLite

    source = create_market_data_source(price_cache)
    await source.start(tickers)                       # cache is populated on return

    app.state.price_cache = price_cache
    app.state.market_source = source
    snapshot_task = start_snapshot_task(app)          # portfolio snapshots, PLAN.md section 7
    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)

app.include_router(create_stream_router(price_cache))
app.include_router(portfolio_router)
app.include_router(watchlist_router)
app.include_router(chat_router)
app.include_router(health_router)

# MUST be last. Mounted before the routers it shadows every /api/* path and
# each endpoint 404s while the UI still appears to work — the most common way
# this architecture breaks (PLAN.md section 11).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
```

The source is stashed on `app.state` so the watchlist router can call `add_ticker` /
`remove_ticker`. The cache is a module-level singleton rather than app state because
`create_stream_router` needs it at import time and there is exactly one of it per process; it is
also exposed on `app.state` so handlers can reach it through `Request` without importing
`main`.

### `/api/health`

```python
@router.get("/api/health")
async def health(request: Request) -> dict:
    cache: PriceCache = request.app.state.price_cache
    source: MarketDataSource = request.app.state.market_source
    newest = cache.newest_timestamp()
    return {
        "status": "ok",
        "market_source": source.source_name,          # "simulator" | "massive"
        "tickers_cached": len(cache),
        "newest_price_age_seconds": (
            round(time.time() - newest, 3) if newest is not None else None
        ),
    }
```

Four fields chosen to answer "is the stream alive?" in one request. A simulator run shows an age
under a second; a Massive run overnight shows an age of many hours with `market_source:
"massive"`, which explains a flat UI without anyone having to read logs.

---

## 14. Consumer recipes

Everything below lives *outside* `app/market/`. It is included so the routers built next are
written against the real shapes.

### `GET /api/watchlist` — price plus sparkline

```python
@router.get("/api/watchlist")
async def get_watchlist(request: Request) -> dict:
    cache: PriceCache = request.app.state.price_cache
    tickers = []
    for ticker in list_watchlist_tickers():           # from SQLite, ordered
        update = cache.get(ticker)
        tickers.append(
            {
                "ticker": ticker,
                "price": update.price if update else None,
                "open_price": update.open_price if update else None,
                "change_from_open_percent": (
                    update.change_from_open_percent if update else 0.0
                ),
                "history": cache.get_history(ticker),   # ~60 points, oldest first
            }
        )
    return {"tickers": tickers}
```

### `POST /api/watchlist` — add

```python
ticker = normalize_ticker(body.ticker)                # ValueError -> 400
insert_watchlist_row(ticker)                          # UNIQUE(user_id, ticker)
await request.app.state.market_source.add_ticker(ticker)
```

Order matters: persist first, then tell the source. If the insert fails on the unique
constraint, the source is never asked to track a duplicate.

### `DELETE /api/watchlist/{ticker}` — remove

```python
ticker = normalize_ticker(ticker)
if not in_watchlist(ticker):
    raise HTTPException(404, f"{ticker} is not on the watchlist")
if has_open_position(ticker):
    raise HTTPException(409, f"Cannot remove {ticker} while you hold a position in it")
delete_watchlist_row(ticker)
await request.app.state.market_source.remove_ticker(ticker)
```

The 409 plus the "trades auto-add their ticker" rule together hold the invariant that every
position has a live price feed. Without it a held position could lose its price and portfolio
valuation would silently drop a line.

### `POST /api/portfolio/trade` — fill at the server's price

```python
ticker = normalize_ticker(body.ticker)
if not in_watchlist(ticker):
    insert_watchlist_row(ticker)
    await request.app.state.market_source.add_ticker(ticker)

try:
    fill_price = await wait_for_price(cache, ticker)   # up to 2s
except ValueError as exc:
    raise HTTPException(400, str(exc)) from exc
```

The client's displayed price is advisory. The fill is whatever is in the cache when the request
lands, and the response returns it as `fill_price` so the UI shows the fill it got rather than the
price that was clicked.

### Portfolio valuation

```python
def total_value(cache: PriceCache, cash: float, positions: list[Position]) -> float:
    return cash + sum(p.quantity * (cache.get_price(p.ticker) or p.avg_cost) for p in positions)
```

Falling back to `avg_cost` when a price is missing keeps the total finite during the sub-second
window after a restart. The frontend does the same arithmetic on every SSE frame from its own copy
of cash and positions — there is no portfolio SSE channel and no polling loop.

---

## 15. Delta from the current implementation

What is on disk today versus the design above. Nothing here is a rewrite; it is roughly 200 lines
of change across seven files, plus one new file.

| # | File | Change | Why |
|---|---|---|---|
| 1 | `models.py` | Add `open_price` field, `change_from_open` / `change_from_open_percent` properties, both new keys in `to_dict()` | Session baseline — the whole frontend change % column depends on it |
| 2 | `cache.py:30` | `ts = time.time() if timestamp is None else timestamp` | `timestamp or time.time()` discards a legitimate `0.0` |
| 3 | `cache.py` | Carry `open_price` forward in `update()` | First write pins the baseline |
| 4 | `cache.py` | Add `_history` / `_history_at` deques, `seed_history()`, `get_history()`, `newest_timestamp()`; clear them in `remove()` | Sparklines on first paint; `/api/health` |
| 5 | `cache.py` | Bump `version` only when the rounded price changes | "Emit only on change" is currently untrue on the Massive path |
| 6 | `seed_prices.py:151` (used by `simulator.py`) | Replace `random.uniform(50.0, 300.0)` with `synthesize_params()` / `params_for()` | A restart currently reprices PYPL at random and makes the user's P&L nonsense |
| 7 | `seed_prices.py` | Drop `TSLA` from the tech group, drop `TSLA_CORR` and `DEFAULT_PARAMS` | The carve-out in `_pairwise_correlation` says the same thing twice; removing it makes positive definiteness provable (§8). Existing tests still pass — TSLA pairs stay at 0.3 |
| 8 | `simulator.py` | Add `backfill_history()`; seed history in `start()` and `add_ticker()` | 60 points at a one-minute cadence, ending at the live price |
| 9 | `simulator.py` | Guard `np.linalg.cholesky` with `LinAlgError` -> independent draws | A correlation constant should never take the feed down |
| 10 | `massive_client.py:103` | `/ 1_000_000_000.0`, not `/ 1000.0` | Nanoseconds. Every real-data price is currently stamped ~50,000 years in the future |
| 11 | `massive_client.py` | `_extract_quote()` fallback chain (last_trade -> min -> day) | `last_trade` can be absent off-hours; a missing attribute should not drop the ticker |
| 12 | `massive_client.py` | Startup + per-ticker history backfill via `get_aggs`, spaced by the poll interval | Sparklines on the real-data path without blowing the 5 req/min budget |
| 13 | `massive_client.py` | Exponential backoff on poll failure; market-status log line | 429s should widen the interval, not repeat every 15s |
| 14 | `interface.py` | Add abstract `source_name` property (`"simulator"` / `"massive"`) | `/api/health` needs it without an `isinstance` check |
| 15 | `stream.py:17` | Move `APIRouter(...)` inside `create_stream_router` | A module-level router double-registers `/prices` when the factory is called twice |
| 16 | `stream.py` | 15-second heartbeat comment frame | The connection dot cannot otherwise tell "quiet" from "stalled" |
| 17 | **new** `tickers.py` | `TICKER_PATTERN`, `normalize_ticker()`, exported from `__init__` | One validation rule for the manual and LLM paths |
| 18 | `__init__.py` | Export `wait_for_price`, `normalize_ticker`, `TICKER_PATTERN` | Trade and watchlist routers need them |
| 19 | `pyproject.toml` | Pin `massive==2.2.0` (currently `>=1.0.0`) | `MASSIVE_API.md` documents the 2.2.0 model shapes; a 1.x resolve would not match |
| 20 | `market_data_demo.py` | Show `change_from_open_percent`; read `cache.get_history()` instead of its own deque | Keeps the demo an honest preview of what the UI shows |

Ordering note: items 1–5 are one commit (the model and cache change together and the tests move
with them); 6–9 and 10–13 are independent of each other; 14–18 are trivial and can ride along with
either.

---

## 16. Testing

The whole point of the interface is that none of this needs a network, an API key, a browser, or
real elapsed time. Any test that sleeps for more than a few hundred milliseconds is doing it
wrong.

### 16.1 `PriceCache`

```python
def test_first_update_pins_the_session_baseline():
    cache = PriceCache()
    update = cache.update("AAPL", 190.00)
    assert update.open_price == 190.00
    assert update.previous_price == 190.00
    assert update.direction == "flat"
    assert update.change_from_open_percent == 0.0


def test_open_price_survives_later_updates():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    cache.update("AAPL", 191.00)
    update = cache.update("AAPL", 192.00)
    assert update.open_price == 190.00           # still the first price seen
    assert update.previous_price == 191.00       # but previous tracks the tick
    assert update.change_from_open_percent == pytest.approx(1.0526, abs=1e-3)


def test_repeated_price_does_not_bump_version():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    version = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == version              # SSE stays quiet
    assert cache.get("AAPL").timestamp > 0       # but the feed still looks alive


def test_history_is_bounded_and_ordered():
    cache = PriceCache(history_points=5, history_interval=0.0)
    for price in range(100, 110):
        cache.update("AAPL", float(price))
    assert cache.get_history("AAPL") == [105.0, 106.0, 107.0, 108.0, 109.0]


def test_seed_history_then_remove_clears_everything():
    cache = PriceCache()
    cache.seed_history("AAPL", [1.0, 2.0, 3.0])
    cache.update("AAPL", 4.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert cache.get_history("AAPL") == []
```

### 16.2 `GBMSimulator`

Seed both RNGs in a fixture; keep statistical tolerances generous, because a test that fails once
a week is worse than no test.

| Target | Assertion |
|---|---|
| `step()` at default `dt` | All prices positive; every move under 1% (absent a shock) |
| Drift | Over 100k seeded steps, mean log-return ≈ `(mu − sigma²/2)·dt` |
| Volatility | Sample std of log-returns ≈ `sigma·√dt` |
| Correlation | Tech/tech log-return correlation exceeds tech/finance over many steps |
| Cholesky | Rebuilt on add and remove; matrix stays factorable at n = 1, 2, 50 |
| Shocks | With `event_probability=1.0`, every tick moves 2–5% |
| Unknown ticker | `synthesize_params("PYPL")` is identical across calls *and* processes |
| Backfill | Exactly 60 points, oldest first, last element == current price |

```python
def test_synthesize_params_is_deterministic():
    # Not just stable within a process — stable across them. Hard-code the value.
    price, params = synthesize_params("PYPL")
    assert (price, params) == synthesize_params("PYPL")
    assert 20.0 <= price <= 500.0
    assert 0.15 <= params["sigma"] <= 0.50


def test_backfill_ends_at_the_current_price():
    sim = GBMSimulator(["AAPL"])
    history = sim.backfill_history("AAPL")
    assert len(history) == 60
    assert history[-1] == sim.get_price("AAPL")


def test_step_moves_are_realistic():
    sim = GBMSimulator(["AAPL", "TSLA"], event_probability=0.0)
    before = {t: sim.get_price(t) for t in sim.get_tickers()}
    after = sim.step()
    for ticker, price in after.items():
        assert price > 0
        assert abs(price - before[ticker]) / before[ticker] < 0.01
```

### 16.3 Conformance — one suite, both implementations

Anything only one source passes is a leak in the abstraction, so run the lifecycle contract
against both.

```python
@pytest.fixture(params=["simulator", "massive"])
def source_and_cache(request):
    cache = PriceCache()
    if request.param == "simulator":
        yield SimulatorDataSource(cache, update_interval=0.05), cache
    else:
        source = MassiveDataSource("test-key", cache, poll_interval=60.0,
                                   backfill_history=False)
        with patch.object(source, "_fetch_snapshots", return_value=[
            _snapshot("AAPL", 190.50), _snapshot("GOOGL", 175.25),
        ]), patch.object(source, "_log_market_status", new=AsyncMock()), \
             patch("app.market.massive_client.RESTClient"):
            yield source, cache


async def test_start_populates_cache_before_returning(source_and_cache):
    source, cache = source_and_cache
    await source.start(["AAPL", "GOOGL"])
    assert cache.get_price("AAPL") is not None      # no sleep, no polling
    await source.stop()


async def test_stop_is_idempotent(source_and_cache):
    source, cache = source_and_cache
    await source.start(["AAPL"])
    await source.stop()
    await source.stop()                              # must not raise


async def test_remove_ticker_clears_the_cache(source_and_cache):
    source, cache = source_and_cache
    await source.start(["AAPL", "GOOGL"])
    await source.remove_ticker("AAPL")
    assert "AAPL" not in cache
    assert "AAPL" not in source.get_tickers()
    await source.stop()
```

### 16.4 `MassiveDataSource`

Mock the SDK entirely. The two assertions that matter are the nanosecond conversion and that an
exception inside a poll never escapes the loop.

```python
def _snapshot(ticker: str, price: float, ts_ns: int = 1605195918306274000):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = ts_ns
    return snap


async def test_nanosecond_timestamps_convert_to_seconds():
    cache = PriceCache()
    source = MassiveDataSource("k", cache, poll_interval=60.0)
    source._tickers, source._client = ["AAPL"], MagicMock()

    with patch.object(source, "_fetch_snapshots", return_value=[_snapshot("AAPL", 190.5)]):
        await source._poll_once()

    # 1605195918306274000 ns == 2020-11-12T15:45:18Z, not the year 52,000
    assert cache.get("AAPL").timestamp == pytest.approx(1605195918.306274)


async def test_poll_failure_is_swallowed_and_backs_off():
    cache = PriceCache()
    source = MassiveDataSource("k", cache, poll_interval=15.0)
    source._tickers, source._client = ["AAPL"], MagicMock()

    with patch.object(source, "_fetch_snapshots", side_effect=RuntimeError("429")):
        await source._poll_once()                    # must not raise

    assert source._backoff == 2.0
    assert len(cache) == 0
```

### 16.5 SSE

Drive `_generate_events` directly against a hand-fed cache and a stub request — no ASGI server,
no browser.

```python
class _StubRequest:
    client = None
    def __init__(self, disconnect_after: int):
        self._calls, self._limit = 0, disconnect_after
    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > self._limit


async def test_no_event_when_the_version_is_unchanged():
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    frames = [f async for f in _generate_events(cache, _StubRequest(3), interval=0.0)]
    data_frames = [f for f in frames if f.startswith("data:")]
    assert len(data_frames) == 1                     # one payload, then silence


async def test_heartbeat_arrives_in_a_quiet_market():
    cache = PriceCache()
    frames = [f async for f in _generate_events(
        cache, _StubRequest(3), interval=0.0, heartbeat=0.0)]
    assert ": ping\n\n" in frames


async def test_payload_is_keyed_by_ticker_and_carries_the_baseline():
    cache = PriceCache()
    cache.update("AAPL", 189.20)
    cache.update("AAPL", 190.50)
    frames = [f async for f in _generate_events(cache, _StubRequest(1), interval=0.0)]
    payload = json.loads(frames[1].removeprefix("data: "))
    assert payload["AAPL"]["open_price"] == 189.20
    assert payload["AAPL"]["change_from_open_percent"] == pytest.approx(0.687, abs=1e-3)
```

### 16.6 Factory

```python
@pytest.mark.parametrize("value,expected", [
    (None, SimulatorDataSource),
    ("", SimulatorDataSource),
    ("   ", SimulatorDataSource),      # a bare `MASSIVE_API_KEY=` in .env
    ("real-key", MassiveDataSource),
])
def test_factory_selection(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("MASSIVE_API_KEY", value)
    assert isinstance(create_market_data_source(PriceCache()), expected)
```

---

## 17. Failure modes

| Situation | Behaviour | Rationale |
|---|---|---|
| Empty watchlist at startup | `start([])` succeeds; `step()` returns `{}`; SSE sends heartbeats only | A user can delete every ticker; that is not an error |
| Trade on a just-added ticker | `wait_for_price` polls for up to 2s at 200ms, then 400 with a retryable message | Never happens on the simulator; can happen on a 15s Massive poll |
| Invalid `MASSIVE_API_KEY` | 401 logged each poll, backoff widens to 2 minutes, app serves stale/empty prices | A bad key must not prevent the app from starting |
| 429 rate limit | Backoff doubles to a ceiling of 8× the interval, resets on the first success | Hammering a rate limit makes it worse |
| Market closed (Massive) | Prices flat, version frozen, stream emits heartbeats only, `/api/health` shows a large `newest_price_age_seconds` | Correct behaviour; the health payload is how a user tells it apart from a stall |
| Simulator task raises | Exception logged, loop continues on the next tick | A dead task leaves a UI that looks connected and frozen — the worst outcome |
| Cholesky not factorable | Logged, falls back to independent draws | Losing correlation is a cosmetic regression; losing the feed is not |
| Ticker removed mid-stream | `cache.remove()` drops price and history; the next SSE payload omits it | The frontend keys on the payload, so the row disappears cleanly |
| Two `start()` calls | Undefined; the second overwrites the simulator and orphans the first task | Guarded by the single call site in `lifespan`, not by defensive code |
| Price at exactly `0.0` | `change_percent` and `change_from_open_percent` return `0.0` rather than dividing | Cannot arise from GBM (always positive) but a real feed can send a zero |

---

## Appendix — configuration

| Variable | Default | Effect on this module |
|---|---|---|
| `MASSIVE_API_KEY` | unset | Non-empty after `.strip()` selects `MassiveDataSource`; otherwise the simulator |

| Constant | Value | Where | Meaning |
|---|---|---|---|
| `HISTORY_POINTS` | 60 | `cache.py` | Sparkline points retained per ticker |
| `HISTORY_INTERVAL_SECONDS` | 60.0 | `cache.py` | Minimum spacing between recorded history points |
| `TRADING_SECONDS_PER_YEAR` | 5,896,800 | `simulator.py` | 252 days × 6.5h × 3600 |
| `DEFAULT_DT` | ~8.48e-8 | `simulator.py` | 500ms as a fraction of a trading year |
| `HISTORY_STEP_TICKS` | 120 | `simulator.py` | One backfill point per simulated minute |
| `update_interval` | 0.5 | `SimulatorDataSource` | Simulator tick |
| `event_probability` | 0.001 | `SimulatorDataSource` | Shock chance per tick per ticker |
| `poll_interval` | 15.0 | `MassiveDataSource` | REST poll cadence (free tier safe) |
| `MAX_BACKOFF_MULTIPLIER` | 8.0 | `massive_client.py` | Backoff ceiling — 2 minutes at the default interval |
| `POLL_INTERVAL` | 0.5 | `stream.py` | How often the SSE generator reads the cache |
| `HEARTBEAT_INTERVAL` | 15.0 | `stream.py` | Comment frame cadence |
