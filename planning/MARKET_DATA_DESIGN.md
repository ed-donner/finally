# Market Data Backend — Detailed Design

> **Status.** The core subsystem below (`backend/app/market/`) is **built, tested, and reviewed**
> — see `planning/MARKET_DATA_SUMMARY.md`. This document has two jobs:
> 1. Describe that shipped code accurately, with real snippets (not the pre-build sketch archived
>    at `planning/archive/MARKET_DATA_DESIGN.md`, which has since drifted from what was actually
>    implemented).
> 2. Give an implementation-ready design for the pieces PLAN.md §6 and the Design Decisions Log
>    call for that are **not** in the shipped code yet: the day-change **anchor**, ticker-format
>    **validation**, the SSE **keepalive**, and the **watchlist ∪ positions** tracked-ticker
>    invariant. These are called out explicitly wherever they touch already-shipped, already-tested
>    files — this is a modification of reviewed code, not a greenfield add.
>
> Every section states which category it's in.

---

## Table of Contents

1. [Architecture Recap](#1-architecture-recap)
2. [File Structure](#2-file-structure)
3. [Data Model — `models.py`](#3-data-model--modelspy)
4. [Price Cache — `cache.py`](#4-price-cache--cachepy)
5. [Abstract Interface — `interface.py`](#5-abstract-interface--interfacepy)
6. [Ticker Validation — `validation.py` (new)](#6-ticker-validation--validationpy-new)
7. [Seed Prices & Correlation — `seed_prices.py`](#7-seed-prices--correlation--seed_pricespy)
8. [GBM Simulator — `simulator.py`](#8-gbm-simulator--simulatorpy)
9. [Massive API Client — `massive_client.py`](#9-massive-api-client--massive_clientpy)
10. [Factory — `factory.py`](#10-factory--factorypy)
11. [SSE Streaming Endpoint — `stream.py`](#11-sse-streaming-endpoint--streampy)
12. [FastAPI Lifespan Integration](#12-fastapi-lifespan-integration)
13. [Watchlist ↔ Positions Reconciliation](#13-watchlist--positions-reconciliation)
14. [Concurrency & Deployment Constraints](#14-concurrency--deployment-constraints)
15. [Testing Plan for the New Pieces](#15-testing-plan-for-the-new-pieces)
16. [Error Handling & Edge Cases](#16-error-handling--edge-cases)
17. [Configuration Summary](#17-configuration-summary)

---

## 1. Architecture Recap

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no API key needed)
└── MassiveDataSource    →  Polygon.io REST poller (when MASSIVE_API_KEY set)
        │
        ▼
   PriceCache (thread-safe, in-memory, holds price + day-change anchor)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)  — pushes anchor + keepalive
        ├──→ Portfolio valuation
        └──→ Trade execution
```

Both data sources implement one interface (`MarketDataSource`) and write into one shared
`PriceCache`; everything downstream (SSE, portfolio, trades) is source-agnostic and only ever
touches the cache, never the source directly (except to call `add_ticker` / `remove_ticker`).

---

## 2. File Structure

```
backend/
  app/
    market/
      __init__.py             # Re-exports the public API
      models.py                # PriceUpdate dataclass                              [MODIFY: + anchor]
      cache.py                  # PriceCache (thread-safe in-memory store)            [MODIFY: + anchor map]
      interface.py               # MarketDataSource ABC                                [unchanged]
      validation.py               # validate_ticker()                                  [NEW]
      seed_prices.py                # SEED_PRICES, TICKER_PARAMS, correlation constants  [unchanged]
      simulator.py                   # GBMSimulator + SimulatorDataSource                 [unchanged]
      massive_client.py                # MassiveDataSource                                   [MODIFY: + anchor]
      factory.py                         # create_market_data_source()                         [unchanged]
      stream.py                            # SSE endpoint (FastAPI router)                        [MODIFY: + keepalive, anchor]
      reconcile.py                          # reconcile_tracked_tickers() helper                    [NEW — thin, platform-facing]
```

`reconcile.py` is placed in `app/market/` (rather than a future `app/portfolio/` or
`app/watchlist/` module) because it only orchestrates calls into `MarketDataSource` — it has no
DB code of its own, just a documented call contract the platform/watchlist/trade routes use. See
§13.

---

## 3. Data Model — `models.py`

**Currently shipped** (`backend/app/market/models.py`):

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float: ...
    @property
    def change_percent(self) -> float: ...
    @property
    def direction(self) -> str: ...  # "up" | "down" | "flat"
    def to_dict(self) -> dict: ...
```

No `anchor` field exists. `change`/`change_percent`/`direction` are all **tick-to-tick** (vs. the
immediately preceding price), not day-over-day. PLAN.md §6 / Decision #1 need a separate
**day-change anchor** — the previous close (Massive) or first-observed price (simulator) — so the
UI can show "day change %" that doesn't reset every 500ms.

### 3.1 Design: add `anchor` [MODIFY]

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
    anchor: float                                          # NEW — day-change baseline
    timestamp: float = field(default_factory=time.time)     # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from the previous tick."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from the previous tick."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat' — tick-to-tick, drives the flash animation."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    @property
    def day_change(self) -> float:
        """Absolute change vs. the day-change anchor (previous close / first observed)."""
        return round(self.price - self.anchor, 4)

    @property
    def day_change_percent(self) -> float:
        """Percentage change vs. the day-change anchor. This is the '% change' shown
        next to each ticker in the watchlist — NOT change_percent, which is per-tick."""
        if self.anchor == 0:
            return 0.0
        return round((self.price - self.anchor) / self.anchor * 100, 4)

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "anchor": self.anchor,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
            "day_change": self.day_change,
            "day_change_percent": self.day_change_percent,
        }
```

**Why a required field, not `Optional[float] = None`:** every `PriceUpdate` the cache ever
constructs has an anchor by construction (§4.1 — the cache captures it on first write). Making it
required means every consumer (frontend, tests) can rely on it always being a number, never
`null`.

**Naming note:** keep `change` / `change_percent` (tick-to-tick, drives the flash) distinct from
`day_change` / `day_change_percent` (vs. anchor, drives the watchlist % column). The frontend
needs both — flashing on every tick uses `direction`; the persistent "% change" label uses
`day_change_percent`. Don't collapse these into one field.

### 3.2 Timestamp format — explicit exception to Decision #19 [DOCUMENT]

Decision #19 says "UTC ISO-8601 with `Z` everywhere," but the market layer has always used **Unix
epoch seconds** (`float`) end to end, and this document keeps it that way rather than converting
at the SSE boundary:

- It's what `time.time()` and Massive's `last_trade.timestamp / 1000.0` naturally produce.
- The frontend plots timestamps on a numeric x-axis (Recharts) — epoch floats need no parsing;
  ISO strings would require `Date.parse()` on every one of ~2 points/sec/ticker.
- This is a price-stream-only exception. Every *persisted* timestamp (`trades.executed_at`,
  `portfolio_snapshots.recorded_at`, `chat_messages.created_at`, …) is still UTC ISO-8601 with
  `Z`, per §7. Only the live SSE payload and in-memory `PriceUpdate.timestamp` are epoch seconds.

---

## 4. Price Cache — `cache.py`

**Currently shipped**: `PriceCache` stores `dict[str, PriceUpdate]` behind a `threading.Lock`,
with `update() / get() / get_all() / get_price() / remove()` and a `version` counter for SSE
change detection. Full current source:

```python
class PriceCache:
    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
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
            self._version += 1
            return update
    # get / get_all / get_price / remove / version / __len__ / __contains__
```

There is **no** concept of an anchor anywhere in this file.

### 4.1 Design: anchor capture [MODIFY]

The cache is the natural owner of the anchor because it already owns the "have we seen this
ticker before?" check (`prev = self._prices.get(ticker)`). Add a parallel `_anchors` map, captured
once per ticker and never mutated by the tick loop:

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
        self._anchors: dict[str, float] = {}          # NEW — ticker -> day-change baseline
        self._lock = Lock()
        self._version: int = 0

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        anchor: float | None = None,                    # NEW
    ) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        `anchor`, when given, is the day-change baseline for this ticker (e.g. Massive's
        previous close). It is captured only on the *first* update seen for a ticker — later
        calls ignore the argument and keep whatever anchor was captured first, so day-change
        stays stable across a session (Decision #1 / #11: re-anchors only on process restart,
        because the cache itself is rebuilt then).

        If no anchor is given (simulator mode, or Massive's previous close is briefly
        unavailable), the anchor defaults to this call's `price` — "first observed price."
        """
        with self._lock:
            ts = timestamp or time.time()
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price

            if ticker not in self._anchors:
                self._anchors[ticker] = round(anchor if anchor is not None else price, 2)

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(previous_price, 2),
                anchor=self._anchors[ticker],
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get_anchor(self, ticker: str) -> float | None:              # NEW
        """The captured day-change baseline for a ticker, or None if untracked."""
        with self._lock:
            return self._anchors.get(ticker)

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist).

        Also drops its anchor: if the ticker is re-tracked later (re-added to the watchlist,
        or a new position reopens it), it re-anchors fresh from that moment — consistent with
        "first observed price after tracking started" (Decision #1).
        """
        with self._lock:
            self._prices.pop(ticker, None)
            self._anchors.pop(ticker, None)              # NEW

    # get / get_all / get_price / version / __len__ / __contains__ — unchanged
```

`get / get_all / get_price / version / __len__ / __contains__` are untouched — they already
return `PriceUpdate` objects, which now simply carry the extra `anchor` field for free.

### 4.2 Test impact

`cache.update(...)` gains one optional kwarg; every existing call site (simulator, Massive,
all 13 tests in `test_cache.py`) keeps working unchanged because `anchor` defaults to `None` →
first-observed-price behavior, which is exactly what the simulator needs. New tests are listed in
§15.

---

## 5. Abstract Interface — `interface.py`

**Unchanged.** `MarketDataSource` stays exactly as shipped — `start / stop / add_ticker /
remove_ticker / get_tickers`. The anchor is a `PriceCache` concern; sources don't need new methods,
they just optionally pass `anchor=` into `cache.update()` (Massive does, simulator doesn't).

```python
class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: list[str]) -> None: ...
    @abstractmethod
    async def stop(self) -> None: ...
    @abstractmethod
    async def add_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None: ...
    @abstractmethod
    def get_tickers(self) -> list[str]: ...
```

---

## 6. Ticker Validation — `validation.py` (new)

PLAN.md §6 / Decision #3: "Tickers are validated as 1–5 uppercase letters before being accepted."
This does not exist anywhere in the shipped market code today — `GBMSimulator` happily accepts any
string as a ticker (`SEED_PRICES.get(ticker, random.uniform(50, 300))` works for `"nvda"`,
`"NOT-A-TICKER"`, `""`, anything), and `MassiveDataSource.add_ticker` just
`.upper().strip()`s its input with no rejection.

Per §1.5 of `planning/REVIEW.md`, this is correctly scoped as **API-boundary validation**, not a
simulator change — the simulator's "synthesize a seed price for anything" behavior is already
correct and should stay permissive internally (defense in depth is cheap, but the single source of
truth for the format rule belongs at the edge, called from every write path once).

```python
"""Ticker symbol validation shared by every write path that accepts a ticker."""

from __future__ import annotations

import re

_TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


class InvalidTickerError(ValueError):
    """Raised when a ticker does not match the 1-5 uppercase letter format."""


def validate_ticker(raw: str) -> str:
    """Normalize and validate a ticker symbol.

    Uppercases and strips whitespace, then requires 1-5 letters A-Z (Decision #3).
    Returns the normalized ticker on success; raises InvalidTickerError otherwise.

    Callers (all four write paths, per REVIEW.md §5.13):
        - POST /api/watchlist              (manual watchlist add)
        - POST /api/portfolio/trade         (manual trade)
        - LLM `trades[].ticker`             (chat-initiated trade)
        - LLM `watchlist_changes[].ticker`  (chat-initiated watchlist change)
    """
    ticker = raw.strip().upper()
    if not _TICKER_RE.match(ticker):
        raise InvalidTickerError(
            f"Invalid ticker '{raw}': must be 1-5 letters (A-Z)."
        )
    return ticker
```

### Usage at the API boundary

```python
from fastapi import APIRouter, HTTPException
from app.market.validation import InvalidTickerError, validate_ticker

router = APIRouter(prefix="/api")


@router.post("/watchlist")
async def add_to_watchlist(payload: WatchlistAdd):
    try:
        ticker = validate_ticker(payload.ticker)
    except InvalidTickerError as e:
        raise HTTPException(status_code=400, detail={"error": str(e)}) from e
    # ... insert `ticker` into the watchlist table, call reconcile helper (§13) ...
```

The LLM-triggered paths reuse the *same* `validate_ticker` call inside trade/watchlist execution
— per §8 / Decision #16, a chat-initiated failure is **not** an HTTP error, it's folded into the
chat response's per-action result:

```python
try:
    ticker = validate_ticker(trade_spec.ticker)
except InvalidTickerError as e:
    results.append({"ticker": trade_spec.ticker, "status": "error", "error": str(e)})
    continue
```

---

## 7. Seed Prices & Correlation — `seed_prices.py`

**Unchanged — shipped and correct.** Pure constants, no logic:

```python
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

These 10 tickers must stay the single source of truth for the DB's default watchlist seed too
(REVIEW.md §6.6) — when the backend platform agent writes the schema seed logic, import
`SEED_PRICES.keys()` (or a small `DEFAULT_WATCHLIST: list[str]` re-export) rather than
hardcoding the 10 symbols a second time in SQL/seed code.

---

## 8. GBM Simulator — `simulator.py`

**Unchanged — shipped and correct**, including the unknown-ticker fallback. Full design rationale
(already implemented, not a new build):

### 8.1 The math

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

`dt` is 500ms expressed as a fraction of a trading year (`0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8`),
producing sub-cent moves per tick that accumulate naturally. `Z` is a **correlated** standard
normal, generated once per tick for all tickers via `Cholesky(corr_matrix) @ independent_normals`
— tech names at ρ=0.6, finance at ρ=0.5, everything else (including TSLA, deliberately excluded
from its own tech bucket) at ρ=0.3.

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8

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
        n = len(self._tickers)
        if n == 0:
            return {}
        z_independent = np.random.standard_normal(n)
        z_correlated = self._cholesky @ z_independent if self._cholesky is not None else z_independent

        result = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma ** 2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            if random.random() < self._event_prob:              # ~0.1%/tick/ticker
                shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
                self._prices[ticker] *= 1 + shock

            result[ticker] = round(self._prices[ticker], 2)
        return result
```

### 8.2 Unknown-ticker synthesis (already built)

```python
def _add_ticker_internal(self, ticker: str) -> None:
    if ticker in self._prices:
        return
    self._tickers.append(ticker)
    self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
    self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

Any ticker not in `SEED_PRICES` gets a random seed in `[$50, $300]` and `DEFAULT_PARAMS`
(`sigma=0.25, mu=0.05`) with correlation `CROSS_GROUP_CORR` (0.3) to everything else. This already
satisfies Decision #3's "simulator synthesizes a seed price + default GBM params" clause — the
**only** missing piece was format validation, which now lives one layer up (§6), not in the
simulator. The simulator intentionally stays permissive; `validate_ticker()` at the API boundary
is what actually enforces "1-5 uppercase letters."

### 8.3 `SimulatorDataSource` — async wrapper (unchanged)

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001):
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        for ticker in tickers:                                   # seed cache immediately
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)   # anchor defaults to this price
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
                self._cache.update(ticker=ticker, price=price)   # re-anchors fresh here

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)                                # drops anchor too (§4.1)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)   # no anchor= passed
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

No changes needed here for the anchor work: the simulator never passes `anchor=` — every call
relies on `PriceCache.update()`'s default ("first observed price"), which is exactly the
simulator's documented anchor semantics (Decision #1).

### 8.4 Concurrency note (existing constraint, not a change)

`add_ticker` / `remove_ticker` mutate `self._tickers` / `self._params` and rebuild the Cholesky
factor with **no lock**, while `step()` iterates the same structures every 500ms on the event-loop
task. This is safe only if every caller of `add_ticker`/`remove_ticker` runs on the event loop
thread — i.e., watchlist and trade routes that call `source.add_ticker()` **must be `async def`**,
never a sync route FastAPI would dispatch to its thread pool. See §14.

---

## 9. Massive API Client — `massive_client.py`

**Shipped, needs one addition**: plumbing the previous-close anchor through to the cache.

### 9.1 Current shape (unchanged parts)

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0):
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None

    async def start(self, tickers: list[str]) -> None:
        self._client = RESTClient(api_key=self._api_key)
        self._tickers = list(tickers)
        await self._poll_once()                                   # immediate first poll
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")

    async def stop(self) -> None: ...       # cancel task, clear client — unchanged
    async def add_ticker(self, ticker: str) -> None: ...   # append to list — unchanged
    async def remove_ticker(self, ticker: str) -> None: ...  # remove + cache.remove — unchanged
    def get_tickers(self) -> list[str]: ...

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    def _fetch_snapshots(self) -> list:
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### 9.2 Design: anchor plumbing in `_poll_once` [MODIFY]

The Massive/Polygon snapshot object exposes `snap.day.previous_close` alongside
`snap.last_trade.price` (confirmed against the `massive` package's snapshot schema — see
`planning/archive/MASSIVE_API.md` §1 for the full response shape). Read it defensively, since a
malformed or partial snapshot must not break price updates — the price is more important than the
anchor:

```python
async def _poll_once(self) -> None:
    """Execute one poll cycle: fetch snapshots, update cache."""
    if not self._tickers or not self._client:
        return

    try:
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
        processed = 0
        for snap in snapshots:
            try:
                price = snap.last_trade.price
                timestamp = snap.last_trade.timestamp / 1000.0   # ms -> seconds

                # Best-effort previous-close anchor. If the field is missing/None on this
                # snapshot (pre-market, a thin plan tier, a transient partial response),
                # fall back silently to PriceCache's own "first observed" default by
                # passing anchor=None — never let a missing anchor drop the price update.
                anchor = getattr(getattr(snap, "day", None), "previous_close", None)

                self._cache.update(
                    ticker=snap.ticker,
                    price=price,
                    timestamp=timestamp,
                    anchor=anchor,
                )
                processed += 1
            except (AttributeError, TypeError) as e:
                logger.warning("Skipping snapshot for %s: %s", getattr(snap, "ticker", "???"), e)
        logger.debug("Massive poll: updated %d/%d tickers", processed, len(self._tickers))

    except Exception as e:
        logger.error("Massive poll failed: %s", e)
        # Don't re-raise — retried on the next interval.
```

Recall `PriceCache.update()` only *uses* the `anchor` argument on a ticker's first write (§4.1);
every poll after that passes `anchor=snap.day.previous_close` again but it's a no-op — the cache
already locked in the value. This matters because Polygon's `day` object resets at market open, so
by mid-session `previous_close` is stable and matches what was captured on the first poll anyway.

**Outside market hours** (§6 of PLAN.md: "prices are the last known close and simply stop
changing"): `last_trade.price` continues to reflect the last trade (which may be during
after-hours or literally the prior session's close), and the anchor stays whatever was captured on
the first poll of this process's lifetime — consistent with Decision #11 (re-anchor only on
restart).

### 9.3 Lazy import — no longer applicable

The archived pre-build design (`planning/archive/MARKET_DATA_DESIGN.md` §7) described a lazy
`from massive import RESTClient` inside `start()`. The shipped code imports `massive` at module
top level instead (see `planning/MARKET_DATA_SUMMARY.md`, review fix #2) because `massive` is a
core dependency of `pyproject.toml`, not optional — this document reflects the shipped choice, not
the archived one.

---

## 10. Factory — `factory.py`

**Unchanged.**

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MASSIVE_API_KEY set and non-empty -> MassiveDataSource; otherwise -> SimulatorDataSource."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```

```python
price_cache = PriceCache()
source = create_market_data_source(price_cache)
await source.start(initial_tickers)   # e.g. watchlist ∪ open positions, see §13
```

---

## 11. SSE Streaming Endpoint — `stream.py`

### 11.1 Shipped behavior

A single FastAPI route (`GET /api/stream/prices`) returns a `StreamingResponse` over an async
generator. Every ~500ms it checks `PriceCache.version`; if it changed since the last send, it
serializes **all** tracked tickers into one JSON object keyed by ticker and yields one `data:`
line. There is currently **no** periodic keepalive — the loop only ever yields when the version
changes.

```python
async def _generate_events(price_cache, request, interval: float = 0.5) -> AsyncGenerator[str, None]:
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

Wire format (unchanged by this design — this envelope shape is correct and stays):

```
data: {"AAPL":{"ticker":"AAPL","price":190.50,"previous_price":190.42,"anchor":188.30,"timestamp":1707580800.5,"change":0.08,"change_percent":0.042,"direction":"up","day_change":2.20,"day_change_percent":1.168},"GOOGL":{...}}

```

It's a **whole-snapshot object per event**, not one event per ticker — this is what the frontend
agent's `EventSource.onmessage` handler must parse (`JSON.parse(event.data)` → `{ticker: {...}}`).

### 11.2 Design: keepalive [MODIFY]

PLAN.md §6 / Decision #14: "The server emits a `: keepalive` comment every ~15s so the client can
distinguish an idle stream from a dropped connection." In Massive mode with a 15s free-tier poll
interval, or outside market hours when prices "simply stop changing," `PriceCache.version` can go
tens of seconds without changing — the loop above would then send nothing at all, and the client
has no way to tell "idle" from "connection silently died."

Add a wall-clock timer, independent of `version`, that fires a comment line (SSE comments start
with `:` and are ignored by `EventSource.onmessage`, but keep the underlying TCP stream alive and
observable):

```python
import time

KEEPALIVE_INTERVAL = 15.0  # seconds


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
    keepalive_interval: float = KEEPALIVE_INTERVAL,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"

    last_version = -1
    last_send = time.monotonic()          # NEW — tracks the last time *anything* was yielded
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            now = time.monotonic()
            current_version = price_cache.version

            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    data = {ticker: update.to_dict() for ticker, update in prices.items()}
                    yield f"data: {json.dumps(data)}\n\n"
                    last_send = now
            elif now - last_send >= keepalive_interval:            # NEW
                yield ": keepalive\n\n"
                last_send = now

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

`last_send` is updated on **both** a real data event and a keepalive, so the two never compound —
worst case the client goes `keepalive_interval` (15s) between any bytes at all, whether that's
because the market genuinely hasn't moved or Massive's poll hasn't landed yet. The frontend
connection-status dot (green/yellow/red, §2) should treat "no bytes, including no keepalive, for
> ~2x keepalive_interval" as the "reconnecting" signal, and rely on `EventSource`'s own `onerror`
/ readyState for the hard-disconnect case.

### 11.3 `to_dict()` now includes the anchor automatically

No change needed in `stream.py` beyond the keepalive — `update.to_dict()` (§3.1) already emits
`anchor`, `day_change`, `day_change_percent` once `models.py` is updated, so the SSE payload gets
the new fields for free.

### 11.4 `request.is_disconnected()` reliability [NOTE, not a required change]

`REVIEW.md` §4.1 flags that polling `request.is_disconnected()` can be unreliable behind some
proxies/uvicorn versions and can leak generator tasks. Low priority for the local Docker demo (no
proxy in front of uvicorn per PLAN.md §11); worth revisiting only if the optional cloud deployment
(§11's App Runner stretch goal) is pursued behind a reverse proxy.

---

## 12. FastAPI Lifespan Integration

Not shipped yet (no `app/main.py` exists) — this is the forward-looking contract the backend
platform agent implements against. Ordering matters (REVIEW.md §2.2): DB must be ready and the
tracked-ticker set computed **before** `source.start()` is called, and the SSE router must only be
mounted once the cache exists.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router
from app.market.reconcile import get_tracked_tickers   # see §13


@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- STARTUP, in order ---

    # 1. DB: open, create schema if missing, seed defaults (unconditionally — not "lazy on
    #    first request", since the market task and snapshot writer both need it immediately;
    #    see REVIEW.md §2.1).
    await init_db()

    # 2. Create the shared price cache.
    price_cache = PriceCache()
    app.state.price_cache = price_cache

    # 3. Compute the tracked set = watchlist ∪ open positions (§13) and start the source with
    #    it — never just the watchlist, so an open position is never left unpriced even before
    #    the first watchlist mutation happens.
    initial_tickers = await get_tracked_tickers(db)
    source = create_market_data_source(price_cache)
    app.state.market_source = source
    await source.start(initial_tickers)

    # 4. Mount the SSE router.
    app.include_router(create_stream_router(price_cache))

    # 5. Start the portfolio-snapshot / housekeeping background task (owned by the platform
    #    layer, not this module — mentioned here only for ordering: it must start after the
    #    cache has data, so the first snapshot isn't valuing against an empty cache).
    app.state.housekeeping_task = asyncio.create_task(housekeeping_loop(app.state))

    yield  # App is running

    # --- SHUTDOWN, in order ---
    app.state.housekeeping_task.cancel()
    await source.stop()


app = FastAPI(title="FinAlly", lifespan=lifespan)
```

### Dependency injection for routes

```python
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

```python
@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    price_cache: PriceCache = Depends(get_price_cache),
):
    current_price = price_cache.get_price(trade.ticker)
    if current_price is None:
        raise HTTPException(status_code=400, detail={"error": f"No price available for {trade.ticker}"})
    # ... execute at current_price ...
```

---

## 13. Watchlist ↔ Positions Reconciliation

**New — not shipped.** REVIEW.md §3.1 flags this as a [BLOCKER]: nothing in the shipped market
code enforces "the tracked set is always `watchlist ∪ open positions`" (PLAN.md §6, Decision #4).
Both `SimulatorDataSource.remove_ticker` and `MassiveDataSource.remove_ticker` unconditionally
evict from the cache — the invariant is **entirely the caller's responsibility**. This section
specs the one helper that all three call sites (startup, watchlist routes, trade routes) share, so
the rule is enforced in exactly one place.

### 13.1 The helper

```python
"""Keeps the market data source's tracked ticker set in sync with
watchlist ∪ open positions (PLAN.md §6, Decision #4)."""

from __future__ import annotations

from .interface import MarketDataSource


async def get_tracked_tickers(db) -> list[str]:
    """The tracked set at any point in time: every watchlist ticker, plus every ticker
    with a non-zero open position, deduplicated. Used at startup and anywhere the full
    set needs recomputing from scratch."""
    watchlist = await db.get_watchlist_tickers()          # e.g. SELECT ticker FROM watchlist
    positions = await db.get_open_position_tickers()      # e.g. SELECT ticker FROM positions WHERE quantity > 0
    return sorted(set(watchlist) | set(positions))


async def on_watchlist_add(source: MarketDataSource, ticker: str) -> None:
    """Call after inserting a new watchlist row. Idempotent — add_ticker() on both
    sources is already a no-op if the ticker is already tracked (e.g. via an open position)."""
    await source.add_ticker(ticker)


async def on_watchlist_remove(source: MarketDataSource, db, ticker: str) -> None:
    """Call after deleting a watchlist row. Only stops tracking if there is no open
    position for this ticker — an open position keeps it priced even off the watchlist
    (PLAN.md §8: "the ticker stays priced while the position is open")."""
    position = await db.get_position(ticker)
    if position is None or position.quantity == 0:
        await source.remove_ticker(ticker)


async def on_trade_executed(source: MarketDataSource, db, ticker: str) -> None:
    """Call after every trade commits (buy or sell), regardless of whether the trade
    succeeded on a ticker already tracked. Covers two edge cases the plan states as an
    invariant but never assigns an owner for (REVIEW.md §3.1):

    1. A buy opens a *new* ticker not on the watchlist -> it must start being tracked.
    2. A sell reduces a ticker's quantity to 0, and that ticker had already been removed
       from the watchlist earlier while the position was still open -> now that nothing
       references it, stop tracking it.
    """
    position = await db.get_position(ticker)
    on_watchlist = await db.is_on_watchlist(ticker)

    if position and position.quantity > 0:
        await source.add_ticker(ticker)          # covers case 1; no-op if already tracked
    elif not on_watchlist:
        await source.remove_ticker(ticker)        # covers case 2
```

### 13.2 Call sites

```python
@router.post("/watchlist")
async def add_to_watchlist(
    payload: WatchlistAdd,
    source: MarketDataSource = Depends(get_market_source),
):
    ticker = validate_ticker(payload.ticker)         # §6 — reject bad format before touching DB
    await db.insert_watchlist_row(ticker)             # persist first
    await on_watchlist_add(source, ticker)              # then reconcile the tracked set
    return {"ticker": ticker, "price": price_cache.get_price(ticker)}


@router.delete("/watchlist/{ticker}")
async def remove_from_watchlist(
    ticker: str,
    source: MarketDataSource = Depends(get_market_source),
):
    await db.delete_watchlist_row(ticker)              # persist first
    await on_watchlist_remove(source, db, ticker)       # then reconcile (checks open position)
    return {"status": "ok"}


@router.post("/portfolio/trade")
async def execute_trade(
    trade: TradeRequest,
    source: MarketDataSource = Depends(get_market_source),
):
    # ... validate, run the trade in one DB transaction (§7/§8/Decision #15) ...
    await on_trade_executed(source, db, trade.ticker)   # then reconcile
    return result
```

**Ordering rule, stated once:** in every write path, the **DB transaction commits first**, then
the market-source reconciliation call runs. If the reconciliation call fails or the process
crashes between the two, the tracked set self-heals on the next `get_tracked_tickers()` call —
which only happens at startup today. This is an acceptable v1 gap (a ticker might go briefly
unpriced until restart) but is worth a one-line note in the platform agent's error handling: a
failed `add_ticker`/`remove_ticker` call should be logged, not raised, so it never rolls back an
already-committed trade or watchlist change.

---

## 14. Concurrency & Deployment Constraints

These aren't new market-data *code*, but they're constraints the market layer's design depends on
and that the platform/Docker agents must honor, so they're recorded here where the reasoning lives
(REVIEW.md §1.7, §3.4).

1. **Single uvicorn worker.** Everything hinges on one in-process `PriceCache` and one
   simulator/poller task. Multiple workers means N independent simulators producing divergent
   prices for the same tickers, N snapshot writers, N `PriceCache`s that never agree. The
   Dockerfile's `CMD` must pin `--workers 1`:
   ```
   CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
   ```

2. **Watchlist/trade routes that touch the market source must be `async def`, not sync.** FastAPI
   dispatches `def` (non-async) route handlers to a thread pool. `GBMSimulator.add_ticker` /
   `remove_ticker` mutate shared state with no lock (§8.4), assuming everything runs on the event
   loop. A sync route calling `source.add_ticker()` (itself `async def`, called via
   `asyncio.run_coroutine_threadsafe` or similar) would race with the simulator's own `step()`
   task. Keep every route in §13.2 `async def` end to end.

3. **The market data task must be running before the housekeeping/snapshot task starts** (§12,
   step 3 before step 5) — the snapshot writer values the portfolio using
   `price_cache.get_price()`, and an empty cache would either write a wrong first snapshot or
   need special-casing. Starting the source first avoids that entirely.

---

## 15. Testing Plan for the New Pieces

The 73 existing tests in `backend/tests/market/` (§ summary above) cover the shipped code and stay
green — `anchor` is purely additive with a defaulting kwarg. New tests needed for this design:

### 15.1 `test_cache.py` — anchor behavior

```python
class TestPriceCacheAnchor:
    def test_first_update_sets_anchor_to_price(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.00)
        assert update.anchor == 190.00

    def test_explicit_anchor_used_on_first_update(self):
        cache = PriceCache()
        update = cache.update("AAPL", 190.00, anchor=185.50)
        assert update.anchor == 185.50

    def test_anchor_is_sticky_across_updates(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00, anchor=185.50)
        update = cache.update("AAPL", 192.00, anchor=999.00)  # later anchor arg ignored
        assert update.anchor == 185.50

    def test_day_change_percent(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00, anchor=100.00)
        update = cache.update("AAPL", 200.00)
        assert update.day_change == 100.00
        assert update.day_change_percent == 100.0

    def test_remove_drops_anchor(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00, anchor=185.50)
        cache.remove("AAPL")
        update = cache.update("AAPL", 300.00)   # re-tracked later
        assert update.anchor == 300.00           # re-anchored fresh, not 185.50

    def test_get_anchor(self):
        cache = PriceCache()
        cache.update("AAPL", 190.00, anchor=185.50)
        assert cache.get_anchor("AAPL") == 185.50
        assert cache.get_anchor("NOPE") is None
```

### 15.2 `test_models.py` — `PriceUpdate` day-change properties

```python
def test_day_change_percent_zero_anchor_is_safe():
    update = PriceUpdate(ticker="X", price=10.0, previous_price=10.0, anchor=0.0)
    assert update.day_change_percent == 0.0   # no ZeroDivisionError

def test_to_dict_includes_anchor_fields():
    update = PriceUpdate(ticker="AAPL", price=200.0, previous_price=198.0, anchor=190.0)
    d = update.to_dict()
    assert d["anchor"] == 190.0
    assert d["day_change"] == 10.0
    assert d["day_change_percent"] == pytest.approx(5.2632, rel=1e-3)
```

### 15.3 `test_massive.py` — previous-close plumbing

```python
def _make_snapshot(ticker, price, timestamp_ms, previous_close=None):
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    snap.day.previous_close = previous_close
    return snap

async def test_poll_captures_previous_close_as_anchor(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    snap = _make_snapshot("AAPL", 190.50, 1707580800000, previous_close=185.00)
    with patch.object(source, "_fetch_snapshots", return_value=[snap]):
        await source._poll_once()
    assert cache.get_anchor("AAPL") == 185.00

async def test_missing_previous_close_falls_back_to_first_observed(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL"]
    snap = _make_snapshot("AAPL", 190.50, 1707580800000, previous_close=None)
    with patch.object(source, "_fetch_snapshots", return_value=[snap]):
        await source._poll_once()
    assert cache.get_anchor("AAPL") == 190.50   # falls back to price itself
```

### 15.4 `test_validation.py` — new file

```python
import pytest
from app.market.validation import InvalidTickerError, validate_ticker

@pytest.mark.parametrize("raw,expected", [
    ("aapl", "AAPL"),
    (" TSLA ", "TSLA"),
    ("V", "V"),
    ("GOOGL", "GOOGL"),
])
def test_valid_tickers_normalize(raw, expected):
    assert validate_ticker(raw) == expected

@pytest.mark.parametrize("raw", ["", "TOOLONG", "AB3", "AB-C", "aapl$", "123", "A B"])
def test_invalid_tickers_raise(raw):
    with pytest.raises(InvalidTickerError):
        validate_ticker(raw)
```

### 15.5 `test_stream.py` — keepalive (new file, or added to an existing stream test module)

```python
@pytest.mark.asyncio
async def test_keepalive_sent_when_cache_idle(monkeypatch):
    cache = PriceCache()
    cache.update("AAPL", 190.0)   # one real event, then silence

    request = FakeRequest(disconnect_after=3)  # helper: is_disconnected() False for N calls
    events = [
        e async for e in _generate_events(cache, request, interval=0.01, keepalive_interval=0.02)
    ]
    assert any(e.startswith(": keepalive") for e in events)
```

### 15.6 `test_reconcile.py` — new file, against a fake DB/source

```python
@pytest.mark.asyncio
async def test_remove_watchlist_keeps_open_position(fake_db, fake_source):
    fake_db.set_position("AAPL", quantity=10)
    await on_watchlist_remove(fake_source, fake_db, "AAPL")
    assert not fake_source.remove_called

@pytest.mark.asyncio
async def test_remove_watchlist_no_position_removes(fake_db, fake_source):
    fake_db.set_position("AAPL", quantity=0)
    await on_watchlist_remove(fake_source, fake_db, "AAPL")
    assert fake_source.remove_called

@pytest.mark.asyncio
async def test_trade_opens_new_ticker_not_on_watchlist(fake_db, fake_source):
    fake_db.set_position("PYPL", quantity=5)
    fake_db.set_on_watchlist("PYPL", False)
    await on_trade_executed(fake_source, fake_db, "PYPL")
    assert fake_source.add_called_with == "PYPL"

@pytest.mark.asyncio
async def test_sell_to_zero_off_watchlist_removes(fake_db, fake_source):
    fake_db.set_position("PYPL", quantity=0)
    fake_db.set_on_watchlist("PYPL", False)
    await on_trade_executed(fake_source, fake_db, "PYPL")
    assert fake_source.remove_called_with == "PYPL"
```

---

## 16. Error Handling & Edge Cases

| Scenario | Behavior |
|---|---|
| Empty watchlist at startup | `get_tracked_tickers()` returns `[]`; both sources handle an empty ticker list gracefully (simulator produces no prices, Massive skips its poll). SSE sends nothing until a ticker is added. |
| Trade on a ticker with no cached price yet | `price_cache.get_price(ticker)` returns `None` → trade route responds `400 {"error": "Price not yet available for X"}`. The simulator avoids this by seeding synchronously in `add_ticker()`; Massive may have a brief gap between `add_ticker()` (appends to the poll list) and the next poll landing. |
| Massive API key invalid | First poll 401s, logged, poller keeps retrying every `poll_interval`. Cache stays empty for those tickers; SSE streams (with keepalives) but no data for them. Not currently escalated to `/api/health` — see REVIEW.md §2.6 if a `market_data: "stale"` health field is added later. |
| Massive snapshot missing `day.previous_close` | Anchor falls back to first-observed price (§9.2) — the *price* update still succeeds; only the anchor degrades. Never let an anchor problem drop a price. |
| Ticker removed from watchlist while a position is open | Stays tracked and priced (§13); its anchor is untouched (no `remove()` call happens). |
| Ticker re-added after being fully evicted (`remove()` called, position and watchlist both empty) | Re-anchors fresh from whatever price it starts at when re-tracked — this is correct per Decision #1's "first observed price after tracking started," not a bug. |
| SSE client behind a slow network | `request.is_disconnected()` may lag; worst case a generator keeps running slightly past actual disconnect, self-terminating on the next check. Acceptable for local Docker; see §11.4 for the cloud-deploy caveat. |
| Ticker format rejected (`validate_ticker` raises) | Manual API paths: `400 {"error": "..."}`. Chat-initiated: folded into that action's `status: "error"` entry in the chat response, per Decision #16 — never an HTTP error for an LLM-issued action. |

---

## 17. Configuration Summary

| Parameter | Location | Default | Description |
|---|---|---|---|
| `MASSIVE_API_KEY` | Environment variable | `""` (empty) | If set, use Massive API; otherwise use simulator. |
| `update_interval` | `SimulatorDataSource.__init__` | `0.5`s | Time between simulator ticks. |
| `poll_interval` | `MassiveDataSource.__init__` | `15.0`s | Time between Massive API polls (free tier). |
| `event_probability` | `GBMSimulator.__init__` | `0.001` | Chance of a random shock event per ticker per tick. |
| `dt` | `GBMSimulator.__init__` | `~8.5e-8` | GBM time step (fraction of a trading year). |
| SSE push interval | `_generate_events()` | `0.5`s | Cache poll cadence inside the SSE loop. |
| SSE keepalive interval | `_generate_events()` | `15.0`s (**new**) | Max gap between bytes sent to an idle SSE client. |
| SSE retry directive | `_generate_events()` | `1000`ms | Browser `EventSource` reconnection delay. |
| Ticker format | `validate_ticker()` (**new**) | `^[A-Z]{1,5}$` | Enforced at every write path (§6), not inside the simulator. |
| Uvicorn workers | Dockerfile `CMD` | `1` (**required**) | One `PriceCache` / one source per process — see §14. |

### `__init__.py` — public exports (add the two new modules)

```python
"""Market data subsystem for FinAlly."""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .reconcile import get_tracked_tickers, on_trade_executed, on_watchlist_add, on_watchlist_remove
from .stream import create_stream_router
from .validation import InvalidTickerError, validate_ticker

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
    "get_tracked_tickers",
    "on_watchlist_add",
    "on_watchlist_remove",
    "on_trade_executed",
    "validate_ticker",
    "InvalidTickerError",
]
```
