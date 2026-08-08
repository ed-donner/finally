# Market Data Backend — Implementation Design

This is the **build guide** for FinAlly's market data subsystem. It consolidates
the three reference documents into one implementation-ready design with complete,
copy-pasteable code:

- `MARKET_INTERFACE.md` — the unified `MarketDataSource` abstraction and cache.
- `MARKET_SIMULATOR.md` — the GBM price simulator (default, no key).
- `MASSIVE_API.md` — the Massive (Polygon.io) REST endpoints (live data).

Where those docs describe *what* and *why*, this document specifies *exactly how*
to build it: the module layout, every file's code, configuration, the SSE and
history endpoints that consume the cache, cache warm-up, resilience, and the
tests. It also fills three gaps the reference docs leave open:

1. **A configuration module** — one place to read the environment.
2. **Historical bars in the unified interface** — the detail chart needs a
   backfill path for *both* sources; the simulator gets a synthetic-history
   generator so the chart is populated on first click even with no vendor.
3. **The consumer endpoints themselves** — the SSE stream and the history route,
   written out in full, not just sketched.

---

## 1. Scope and responsibilities

The market data backend owns everything from "where does a price come from" up to
"the cache the rest of the app reads." Concretely it is responsible for:

- Selecting a data source at startup from `MASSIVE_API_KEY`.
- Running a single background task that keeps an in-memory price cache current.
- Computing per-tick `previous_price` and up/down/flat `direction`.
- Serving the SSE stream (`GET /api/stream/prices`) from the cache.
- Serving historical bars (`GET /api/history/{ticker}`) for the detail chart.
- Exposing a `snapshot()` the portfolio valuation reads to price positions.

It is **not** responsible for the database schema, trade execution, or the LLM —
it only exposes prices. Portfolio and chat code depend on the cache, never on a
specific source.

### Module map

```
backend/
├── pyproject.toml
├── main.py                      # FastAPI app + lifespan wiring
├── config.py                    # env-driven settings (single source of truth)
├── db/                          # schema + watchlist/position reads (other agents)
└── market/
    ├── __init__.py
    ├── types.py                 # PriceTick, Bar, Direction
    ├── source.py                # MarketDataSource ABC (the contract)
    ├── cache.py                 # PriceCache (in-memory, single source of truth)
    ├── feed.py                  # MarketFeed background writer
    ├── factory.py               # make_source(): env -> source
    ├── seeds.py                 # per-ticker GBM seed params
    ├── gbm.py                   # SimEngine (correlated GBM + synthetic history)
    ├── simulator.py             # SimulatedSource (implements the interface)
    ├── massive.py               # MassiveSource (implements the interface)
    └── routes.py                # /api/stream/prices, /api/history/{ticker}
```

All market code lives under `backend/market/`. The only files outside it that
this subsystem touches are `config.py`, `main.py` (lifespan wiring), and a single
read helper in `db/`.

---

## 2. Data types

Two immutable records flow through the system: a live `PriceTick` (what the cache
holds and SSE pushes) and a historical `Bar` (what the detail chart backfills
with).

```python
# backend/market/types.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class PriceTick:
    """A single ticker's latest price, as held in the cache and pushed over SSE."""
    ticker: str
    price: float
    previous_price: float
    direction: Direction
    timestamp: str  # ISO 8601 UTC, e.g. "2026-08-08T14:03:00.512000+00:00"


@dataclass(frozen=True)
class Bar:
    """One OHLC(V) candle for the historical detail chart."""
    t: int      # bar start, Unix milliseconds (matches Massive + frontend charts)
    o: float
    h: float
    low: float  # 'l' would shadow nothing but reads poorly; serialize back to "l"
    c: float
    v: float
```

`Bar` serializes to the exact shape both Massive and Lightweight-Charts expect
(`{t, o, h, l, c, v}`). Because `l` is an awkward attribute name, the field is
`low` in Python and mapped back to `"l"` at the JSON boundary (see `routes.py`).
The cache stores one `PriceTick` per ticker; `previous_price`/`direction` are
computed once by the feed so every consumer gets the flash information for free.

---

## 3. Configuration

One module reads the environment so nothing else calls `os.getenv` directly.
Selection logic, poll cadence, and mock flags all resolve here at import time.

```python
# backend/config.py
from __future__ import annotations

import os
from dataclasses import dataclass


def _clean(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass(frozen=True)
class Settings:
    # Market data
    massive_api_key: str
    massive_poll_seconds: float   # override poll cadence (paid tiers can go faster)
    sim_seed: int | None          # deterministic simulator for tests
    sse_push_seconds: float       # how often SSE flushes the cache to clients

    # LLM (documented here for completeness; owned by the chat agent)
    openrouter_api_key: str
    llm_mock: bool

    @property
    def use_massive(self) -> bool:
        return bool(self.massive_api_key)


def load_settings() -> Settings:
    return Settings(
        massive_api_key=_clean("MASSIVE_API_KEY"),
        massive_poll_seconds=float(_clean("MASSIVE_POLL_SECONDS", "15")),
        sim_seed=(int(_clean("SIM_SEED")) if _clean("SIM_SEED") else None),
        sse_push_seconds=float(_clean("SSE_PUSH_SECONDS", "0.5")),
        openrouter_api_key=_clean("OPENROUTER_API_KEY"),
        llm_mock=_clean("LLM_MOCK", "false").lower() == "true",
    )


settings = load_settings()
```

`.env` is loaded before this module runs — either by Docker (`--env-file .env`)
or, for local dev, by calling `dotenv.load_dotenv()` at the very top of `main.py`
before importing anything that reads `settings`. Only `MASSIVE_API_KEY` and
`OPENROUTER_API_KEY` are required to be meaningful; everything else has a sane
default matching `PLAN.md`.

---

## 4. The unified interface

A source's whole job: given tickers, produce their latest prices — and, for the
detail chart, produce historical bars. It never touches the cache, computes
directions, or manages timing; the feed owns all of that. This keeps each source
tiny and keeps consumers agnostic to the vendor.

```python
# backend/market/source.py
from __future__ import annotations

from abc import ABC, abstractmethod

from .types import Bar


class MarketDataSource(ABC):
    """Produces prices (and history) for tickers. Two methods to implement."""

    #: How often the feed loop should ask this source for fresh prices.
    poll_interval_seconds: float

    @abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Return {ticker: price} for as many requested tickers as available.
        Missing tickers are omitted; the feed keeps their last cached value."""
        ...

    @abstractmethod
    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        """Return up to `days` daily OHLC bars, oldest first, for the detail
        chart. Empty list if unavailable."""
        ...

    async def aclose(self) -> None:
        """Release resources (HTTP client, etc.). Default: no-op."""
        return None
```

That is the entire contract. `get_prices` powers the live cache; `get_history`
powers the backfill of the main chart when a ticker is selected. Both
implementations below satisfy it.

> **Design note — history in the interface.** `MARKET_INTERFACE.md` keeps the
> source to a single `get_prices` method. We add `get_history` here because the
> detail chart in `PLAN.md` §10 needs a backfill path, and it must work with *no
> vendor key* (the common case). Putting it on the interface lets the simulator
> synthesize history and Massive fetch it, with one route serving both.

---

## 5. The simulator (default source)

Used whenever `MASSIVE_API_KEY` is unset. Generates believable, dramatic,
correlated price action with zero external dependencies. Full rationale is in
`MARKET_SIMULATOR.md`; this section is the implementation.

### 5.1 Seed parameters

```python
# backend/market/seeds.py
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TickerSeed:
    price: float     # realistic starting price
    mu: float        # annual drift (expected return)
    sigma: float     # annual volatility (tech > financials)
    sector: str      # correlation grouping


SEEDS: dict[str, TickerSeed] = {
    "AAPL":  TickerSeed(190.0, 0.08, 0.28, "tech"),
    "GOOGL": TickerSeed(175.0, 0.10, 0.30, "tech"),
    "MSFT":  TickerSeed(420.0, 0.09, 0.26, "tech"),
    "AMZN":  TickerSeed(185.0, 0.11, 0.33, "tech"),
    "TSLA":  TickerSeed(250.0, 0.05, 0.55, "tech"),
    "NVDA":  TickerSeed(880.0, 0.15, 0.50, "tech"),
    "META":  TickerSeed(500.0, 0.10, 0.35, "tech"),
    "JPM":   TickerSeed(200.0, 0.06, 0.20, "financial"),
    "V":     TickerSeed(275.0, 0.07, 0.19, "financial"),
    "NFLX":  TickerSeed(630.0, 0.09, 0.40, "tech"),
}

DEFAULT_SEED = TickerSeed(100.0, 0.07, 0.30, "other")
```

These are the ten default watchlist tickers from `PLAN.md`. Any user-added ticker
not in `SEEDS` is lazily created from `DEFAULT_SEED` with a randomized starting
price so it is not always exactly $100.

### 5.2 The GBM engine

The engine owns per-ticker prices and advances them one 500ms step per `step()`
call, applying a one-factor-per-sector correlation model plus rare shock events.
It also synthesizes daily history on demand.

```python
# backend/market/gbm.py
from __future__ import annotations

import math
import random

from .seeds import DEFAULT_SEED, SEEDS, TickerSeed
from .types import Bar

SECONDS_PER_YEAR = 252 * 6.5 * 3600      # 252 trading days x 6.5h
DT = 0.5 / SECONDS_PER_YEAR              # one 500ms step as a fraction of a year
DT_DAY = 1.0 / 252                       # one trading day, for synthetic history
EVENT_PROBABILITY = 0.005               # ~0.5% chance of a shock per ticker/step

# Correlation weights: market + sector + idiosyncratic ~= unit variance.
W_MARKET, W_SECTOR = 0.5, 0.4
W_IDIO = math.sqrt(max(0.0, 1 - W_MARKET**2 - W_SECTOR**2))


class SimEngine:
    """Advances correlated GBM prices one 500ms step at a time."""

    def __init__(self, seed: int | None = None) -> None:
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._seeds: dict[str, TickerSeed] = {}

    # -- seeding -----------------------------------------------------------

    def _ensure(self, ticker: str) -> None:
        if ticker not in self._prices:
            base = SEEDS.get(ticker)
            if base is None:
                jitter = self._rng.uniform(0.5, 2.0)
                base = TickerSeed(
                    DEFAULT_SEED.price * jitter,
                    DEFAULT_SEED.mu, DEFAULT_SEED.sigma, DEFAULT_SEED.sector,
                )
            self._seeds[ticker] = base
            self._prices[ticker] = base.price

    # -- live stepping -----------------------------------------------------

    def step(self, tickers: list[str]) -> dict[str, float]:
        for t in tickers:
            self._ensure(t)

        z_market = self._rng.gauss(0, 1)
        sector_factors: dict[str, float] = {}

        out: dict[str, float] = {}
        for t in tickers:
            seed = self._seeds[t]
            if seed.sector not in sector_factors:
                sector_factors[seed.sector] = self._rng.gauss(0, 1)
            z = (W_MARKET * z_market
                 + W_SECTOR * sector_factors[seed.sector]
                 + W_IDIO * self._rng.gauss(0, 1))

            drift = (seed.mu - 0.5 * seed.sigma**2) * DT
            diffusion = seed.sigma * math.sqrt(DT) * z
            price = self._prices[t] * math.exp(drift + diffusion)

            if self._rng.random() < EVENT_PROBABILITY:
                shock = self._rng.uniform(0.02, 0.05)
                price *= (1 + shock) if self._rng.random() < 0.5 else (1 - shock)

            price = round(max(price, 0.01), 2)
            self._prices[t] = price
            out[t] = price
        return out

    # -- synthetic history -------------------------------------------------

    def history(self, ticker: str, days: int, end_ms: int) -> list[Bar]:
        """Deterministic daily OHLC ending near the ticker's current price.

        Walks GBM *backwards* from the live price so the last bar's close lines
        up with what the stream is currently showing. Uses a per-ticker RNG so a
        given symbol always renders the same past (stable across chart reopens).
        """
        self._ensure(ticker)
        seed = self._seeds[ticker]
        rng = random.Random(hash((ticker, days)) & 0xFFFFFFFF)

        close = self._prices[ticker]
        rows: list[tuple[float, float, float, float]] = []  # o,h,l,c per day
        for _ in range(days):
            z = rng.gauss(0, 1)
            drift = (seed.mu - 0.5 * seed.sigma**2) * DT_DAY
            diffusion = seed.sigma * math.sqrt(DT_DAY) * z
            prev_close = close / math.exp(drift + diffusion)  # step backward
            o, c = prev_close, close
            hi = max(o, c) * (1 + abs(rng.gauss(0, 0.4)) * seed.sigma * math.sqrt(DT_DAY))
            lo = min(o, c) * (1 - abs(rng.gauss(0, 0.4)) * seed.sigma * math.sqrt(DT_DAY))
            rows.append((round(o, 2), round(hi, 2), round(lo, 2), round(c, 2)))
            close = prev_close

        rows.reverse()  # oldest first
        ms_per_day = 86_400_000
        return [
            Bar(t=end_ms - (days - 1 - i) * ms_per_day,
                o=o, h=h, low=low, c=c,
                v=round(rng.uniform(1e6, 5e7)))
            for i, (o, h, low, c) in enumerate(rows)
        ]
```

Key properties (all covered by tests in §11):

- **State persists** across `step()` calls — prices are a continuous walk.
- **Lazy seeding** handles user-added tickers with no special case.
- **Deterministic** when constructed with a fixed `seed` (E2E via `SIM_SEED`).
- **History lines up with live price** — the last synthetic bar's close equals
  the current streamed price, so the chart doesn't "jump" when the stream takes
  over. History is deterministic per ticker so reopening the chart is stable.

### 5.3 The simulator source

A thin adapter that satisfies the interface.

```python
# backend/market/simulator.py
from __future__ import annotations

import time

from .gbm import SimEngine
from .source import MarketDataSource
from .types import Bar


class SimulatedSource(MarketDataSource):
    poll_interval_seconds = 0.5

    def __init__(self, seed: int | None = None) -> None:
        self._engine = SimEngine(seed)

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        return self._engine.step(tickers)  # advance one tick, return prices

    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        end_ms = int(time.time() * 1000)
        return self._engine.history(ticker, days, end_ms)
```

`time.time()` is the one non-deterministic input to history (the end timestamp);
the *shape* of the series is fully deterministic per ticker.

---

## 6. The Massive source (optional, live data)

Used when `MASSIVE_API_KEY` is set. Wraps the endpoints from `MASSIVE_API.md`:
the full-market snapshot for live prices (one request covers the whole
watchlist), and custom aggregate bars for history.

```python
# backend/market/massive.py
from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

import httpx

from .source import MarketDataSource
from .types import Bar

log = logging.getLogger("finally.market.massive")
BASE = "https://api.massive.com"


class MassiveSource(MarketDataSource):
    def __init__(self, api_key: str, poll_interval_seconds: float = 15.0) -> None:
        self.poll_interval_seconds = poll_interval_seconds  # free tier: 5 req/min
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(base_url=BASE, timeout=10.0)

    # -- live prices: one snapshot request for all tickers -----------------

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        if not tickers:
            return {}
        try:
            resp = await self._client.get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": ",".join(tickers)},
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # 429: rate limited (free tier) -> back off implicitly, keep cache.
            # 401/403: bad key. Log once; do not crash the feed.
            log.warning("Massive snapshot failed: %s", e.response.status_code)
            return {}
        except httpx.HTTPError as e:
            log.warning("Massive snapshot transport error: %s", e)
            return {}

        prices: dict[str, float] = {}
        for item in resp.json().get("tickers", []):
            last = item.get("lastTrade", {}).get("p")
            day = item.get("day", {}).get("c")
            prev = item.get("prevDay", {}).get("c")
            price = last or day or prev  # prefer last trade; fall back when closed
            if price:
                prices[item["ticker"]] = float(price)
        return prices

    # -- history: daily aggregate bars for the detail chart ----------------

    async def get_history(self, ticker: str, days: int = 90) -> list[Bar]:
        end = date.today()
        start = end - timedelta(days=int(days * 1.5) + 5)  # pad for weekends/holidays
        try:
            resp = await self._client.get(
                f"/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}",
                params={"adjusted": "true", "sort": "asc", "limit": 5000},
                headers=self._headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("Massive history failed for %s: %s", ticker, e)
            return []

        results = resp.json().get("results", [])[-days:]
        return [
            Bar(t=int(r["t"]), o=float(r["o"]), h=float(r["h"]),
                low=float(r["l"]), c=float(r["c"]), v=float(r.get("v", 0.0)))
            for r in results
        ]

    async def aclose(self) -> None:
        await self._client.aclose()
```

Notes tied to `MASSIVE_API.md` §8:

- **Current price selection:** `lastTrade.p` first, then `day.c`, then
  `prevDay.c` — correct when the market is closed or on the delayed free tier.
- **Errors never crash the feed.** Any HTTP error returns `{}` (or `[]` for
  history); the feed keeps the last cached values. A 429 simply means the next
  poll is skipped — with a 15s interval we stay within the 5-req/min budget.
- **One request per cycle**, regardless of watchlist size, via the snapshot
  endpoint's `tickers` CSV.
- **Poll cadence is configurable** via `MASSIVE_POLL_SECONDS` so paid tiers can
  drop to 2-5s without touching code.

---

## 7. The price cache

A thin, lock-guarded in-memory dict of `PriceTick`. Written only by the feed;
read by SSE, the history route, and portfolio valuation.

```python
# backend/market/cache.py
from __future__ import annotations

import asyncio

from .types import PriceTick


class PriceCache:
    def __init__(self) -> None:
        self._ticks: dict[str, PriceTick] = {}
        self._lock = asyncio.Lock()

    async def update(self, tick: PriceTick) -> None:
        async with self._lock:
            self._ticks[tick.ticker] = tick

    async def get(self, ticker: str) -> PriceTick | None:
        async with self._lock:
            return self._ticks.get(ticker)

    async def snapshot(self) -> dict[str, PriceTick]:
        async with self._lock:
            return dict(self._ticks)  # shallow copy: consistent view, no tearing
```

`snapshot()` gives SSE and portfolio valuation a consistent view of every current
price in one call. `PriceTick` is frozen, so handing out references from a copied
dict is safe.

---

## 8. The market feed (single writer)

The one task that writes the cache. Each cycle it asks the source for the active
tickers' prices, computes `previous_price` and `direction`, and updates the
cache. The active-ticker set is read fresh every cycle so watchlist edits (manual
or via AI chat) take effect on the next poll with no restart.

```python
# backend/market/feed.py
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, timezone

from .cache import PriceCache
from .source import MarketDataSource
from .types import Direction, PriceTick

log = logging.getLogger("finally.market.feed")


def _direction(new: float, prev: float) -> Direction:
    if new > prev:
        return "up"
    if new < prev:
        return "down"
    return "flat"


class MarketFeed:
    def __init__(
        self,
        source: MarketDataSource,
        cache: PriceCache,
        get_active_tickers: Callable[[], list[str]],
    ) -> None:
        self._source = source
        self._cache = cache
        self._get_active_tickers = get_active_tickers
        self._task: asyncio.Task | None = None

    async def prime(self) -> None:
        """Populate the cache once before serving so the first SSE/portfolio
        request has data even on a slow (15s) Massive interval."""
        await self._tick_once()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._source.aclose()

    async def _tick_once(self) -> None:
        tickers = self._get_active_tickers()
        if not tickers:
            return
        try:
            prices = await self._source.get_prices(tickers)
        except Exception:  # source is defensive already; belt-and-suspenders
            log.exception("source.get_prices raised; keeping cached values")
            return
        now = datetime.now(timezone.utc).isoformat()
        for ticker, price in prices.items():
            prev = await self._cache.get(ticker)
            previous = prev.price if prev else price
            await self._cache.update(PriceTick(
                ticker=ticker,
                price=price,
                previous_price=previous,
                direction=_direction(price, previous),
                timestamp=now,
            ))

    async def _run(self) -> None:
        while True:
            await self._tick_once()
            await asyncio.sleep(self._source.poll_interval_seconds)
```

**Cadence decoupling (the key design point).** The feed sleeps for the *source's*
interval — 500ms for the simulator, 15s for Massive. The SSE endpoint (§10)
independently flushes the cache to clients every `SSE_PUSH_SECONDS` (~500ms), so
even with slow Massive polling the UI stays smooth: prices simply hold steady
between polls instead of the connection stalling.

**Active tickers = watchlist ∪ held positions.** `get_active_tickers` returns the
union so that a position in a ticker the user removed from the watchlist is still
priced for portfolio valuation. See §9.

---

## 9. Source selection and the active-ticker loader

The factory is the only place the environment decides the source.

```python
# backend/market/factory.py
from __future__ import annotations

from config import settings

from .massive import MassiveSource
from .simulator import SimulatedSource
from .source import MarketDataSource


def make_source() -> MarketDataSource:
    """Massive if MASSIVE_API_KEY is set and non-empty, else the simulator."""
    if settings.use_massive:
        return MassiveSource(
            settings.massive_api_key,
            poll_interval_seconds=settings.massive_poll_seconds,
        )
    return SimulatedSource(seed=settings.sim_seed)
```

The active-ticker loader reads SQLite each call. It is deliberately a plain
function (not a class) so the feed can hold a reference to it without coupling to
the DB layer. The `db` module is owned by another agent; the market subsystem
only needs this one read.

```python
# backend/db/reads.py  (market subsystem depends on this single helper)
from __future__ import annotations

from .connection import get_connection  # provided by the db agent


def load_active_tickers(user_id: str = "default") -> list[str]:
    """Union of watchlist tickers and tickers with an open position, so both
    the stream and portfolio valuation always have prices."""
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT ticker FROM watchlist WHERE user_id = ?
        UNION
        SELECT ticker FROM positions WHERE user_id = ? AND quantity > 0
        """,
        (user_id, user_id),
    ).fetchall()
    return [r[0] for r in rows]
```

---

## 10. FastAPI wiring and consumer endpoints

### 10.1 Lifespan wiring

Everything is created once on startup and cleaned up on shutdown. The cache and
feed live on `app.state` so routes can reach them.

```python
# backend/main.py
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()  # populate os.environ from .env BEFORE importing config/settings

from fastapi import FastAPI  # noqa: E402

from db.reads import load_active_tickers  # noqa: E402
from market.cache import PriceCache  # noqa: E402
from market.factory import make_source  # noqa: E402
from market.feed import MarketFeed  # noqa: E402
from market.routes import router as market_router  # noqa: E402


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = make_source()
    feed = MarketFeed(source, cache, get_active_tickers=load_active_tickers)

    app.state.price_cache = cache
    app.state.market_source = source
    app.state.market_feed = feed

    await feed.prime()   # one synchronous fill so the first request has data
    feed.start()         # then the background loop takes over
    try:
        yield
    finally:
        await feed.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(market_router)
```

### 10.2 The SSE stream and history route

The SSE endpoint snapshots the cache on a fixed cadence and emits **only ticks
that changed** since the client last saw them — far less bandwidth than
re-sending all ten tickers 500ms — with an initial full snapshot on connect and
a periodic comment heartbeat to keep proxies from closing an idle connection.

```python
# backend/market/routes.py
from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from config import settings
from .cache import PriceCache
from .types import Bar, PriceTick

router = APIRouter(prefix="/api")


def _tick_json(tick: PriceTick) -> str:
    return json.dumps(asdict(tick))


async def _price_events(request: Request, cache: PriceCache):
    # Tell EventSource to retry after 2s if the connection drops.
    yield "retry: 2000\n\n"

    last_sent: dict[str, str] = {}          # ticker -> last ISO timestamp emitted
    heartbeat_every = max(1, int(5 / settings.sse_push_seconds))
    cycles = 0

    while True:
        if await request.is_disconnected():
            break

        for ticker, tick in (await cache.snapshot()).items():
            if last_sent.get(ticker) != tick.timestamp:
                last_sent[ticker] = tick.timestamp
                yield f"data: {_tick_json(tick)}\n\n"

        cycles += 1
        if cycles % heartbeat_every == 0:
            yield ": keepalive\n\n"       # SSE comment; ignored by EventSource

        await asyncio.sleep(settings.sse_push_seconds)


@router.get("/stream/prices")
async def stream_prices(request: Request):
    cache: PriceCache = request.app.state.price_cache
    return StreamingResponse(
        _price_events(request, cache),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable proxy buffering (nginx)
        },
    )


@router.get("/history/{ticker}")
async def history(ticker: str, request: Request, days: int = 90):
    source = request.app.state.market_source
    bars: list[Bar] = await source.get_history(ticker.upper(), days=days)
    # Map Bar.low -> "l" so the payload is the {t,o,h,l,c,v} charts expect.
    return {
        "ticker": ticker.upper(),
        "bars": [
            {"t": b.t, "o": b.o, "h": b.h, "l": b.low, "c": b.c, "v": b.v}
            for b in bars
        ],
    }
```

Each SSE `data:` line is exactly the `PriceTick` shape from `PLAN.md` §6 —
`{ticker, price, previous_price, direction, timestamp}` — which the frontend uses
directly for the price cell, the green/red flash, and sparkline accumulation.

> **Endpoint note.** `PLAN.md` §8 lists only `/api/stream/prices` under Market
> Data. `GET /api/history/{ticker}` is added here to back the detail chart
> (`PLAN.md` §10, "Main chart area"), consistent with the historical-bars section
> of `MASSIVE_API.md`. It works identically for both sources: Massive fetches
> real bars, the simulator synthesizes them.

### 10.3 How portfolio valuation consumes the cache

Portfolio code (owned by another agent) prices positions straight from the same
cache — no source-specific logic:

```python
# in the portfolio route
snapshot = await request.app.state.price_cache.snapshot()
for pos in positions:
    tick = snapshot.get(pos.ticker)
    current = tick.price if tick else pos.avg_cost   # fall back if not yet priced
    pos.market_value = current * pos.quantity
    pos.unrealized_pnl = (current - pos.avg_cost) * pos.quantity
```

Because `prime()` runs before the app serves traffic and the loader includes
held-position tickers, valuation always finds a price.

---

## 11. Resilience and edge cases

| Case | Behavior |
|------|----------|
| Massive 429 (rate limited) | `get_prices` returns `{}`; cache holds last values; next poll is 15s+ away, staying within budget. |
| Massive 401/403 (bad key) | Logged once at WARNING; feed keeps running on stale data rather than crashing. |
| Unknown/delisted ticker | Snapshot omits it; feed keeps its last cached tick; simulator lazily seeds any symbol so it always prices. |
| Empty watchlist | Feed cycle is a no-op; SSE emits nothing until a ticker is added. |
| Client disconnect | `request.is_disconnected()` ends the generator; no leaked tasks. |
| Slow source (15s) vs UI | SSE pushes every 500ms from cache; prices hold flat between polls — smooth, never stalled. |
| First request before first poll | `feed.prime()` fills the cache synchronously during lifespan startup. |
| Position in de-watchlisted ticker | `load_active_tickers` unions positions in, so it stays priced. |
| Simulator determinism for tests | `SIM_SEED` env → fixed RNG → reproducible sequences and history. |

---

## 12. Testing

Pure-function and async tests, no network, matching `PLAN.md` §12. Massive is
tested against a mocked transport so no key or live calls are needed.

### 12.1 Simulator (pure, fast)

```python
# backend/tests/test_simulator.py
import pytest

from market.gbm import SimEngine


def test_determinism():
    a = SimEngine(seed=42).step(["AAPL", "MSFT"])
    b = SimEngine(seed=42).step(["AAPL", "MSFT"])
    assert a == b


def test_prices_stay_positive_over_many_steps():
    eng = SimEngine(seed=1)
    for _ in range(10_000):
        for price in eng.step(["TSLA", "NVDA"]).values():
            assert price > 0


def test_lazy_seeding_of_unknown_ticker():
    eng = SimEngine(seed=7)
    p1 = eng.step(["FOO"])["FOO"]
    p2 = eng.step(["FOO"])["FOO"]
    assert p1 > 0 and p2 > 0            # priced, and continues from p1


def test_history_last_close_matches_live_price():
    eng = SimEngine(seed=3)
    eng.step(["AAPL"])                   # establish a live price
    bars = eng.history("AAPL", days=30, end_ms=1_700_000_000_000)
    assert len(bars) == 30
    assert bars[-1].c == pytest.approx(eng._prices["AAPL"], rel=1e-9)
    assert all(b.h >= b.o and b.h >= b.c for b in bars)   # OHLC sanity
    assert all(b.low <= b.o and b.low <= b.c for b in bars)


def test_same_sector_moves_correlate():
    eng = SimEngine(seed=99)
    same = cross = 0
    prev = eng.step(["AAPL", "MSFT", "JPM"])
    for _ in range(500):
        nxt = eng.step(["AAPL", "MSFT", "JPM"])
        d = {k: nxt[k] - prev[k] for k in nxt}
        same += (d["AAPL"] > 0) == (d["MSFT"] > 0)       # both tech
        cross += (d["AAPL"] > 0) == (d["JPM"] > 0)        # tech vs financial
        prev = nxt
    assert same > cross                                   # tech co-moves more
```

### 12.2 Massive (mocked transport)

```python
# backend/tests/test_massive.py
import httpx
import pytest

from market.massive import MassiveSource

SNAPSHOT = {
    "tickers": [
        {"ticker": "AAPL", "lastTrade": {"p": 191.2},
         "day": {"c": 190.0}, "prevDay": {"c": 189.0}},
        {"ticker": "MSFT", "lastTrade": {},          # closed: fall back to day.c
         "day": {"c": 421.5}, "prevDay": {"c": 420.0}},
    ]
}


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler),
                             base_url="https://api.massive.com")


@pytest.mark.asyncio
async def test_snapshot_parsing_and_fallback():
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(200, json=SNAPSHOT))
    prices = await src.get_prices(["AAPL", "MSFT"])
    assert prices == {"AAPL": 191.2, "MSFT": 421.5}      # last trade, then day.c
    await src.aclose()


@pytest.mark.asyncio
async def test_rate_limit_returns_empty_not_raises():
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(429, json={}))
    assert await src.get_prices(["AAPL"]) == {}          # feed keeps cached values
    await src.aclose()


@pytest.mark.asyncio
async def test_history_maps_to_bar_shape():
    aggs = {"results": [{"t": 1_700_000_000_000, "o": 1, "h": 2, "l": 0.5,
                         "c": 1.5, "v": 100}]}
    src = MassiveSource("key")
    src._client = _client(lambda req: httpx.Response(200, json=aggs))
    bars = await src.get_history("AAPL", days=1)
    assert bars[0].o == 1 and bars[0].low == 0.5 and bars[0].c == 1.5
    await src.aclose()
```

### 12.3 Feed + cache (async integration, no network)

```python
# backend/tests/test_feed.py
import pytest

from market.cache import PriceCache
from market.feed import MarketFeed
from market.simulator import SimulatedSource


@pytest.mark.asyncio
async def test_feed_populates_cache_and_computes_direction():
    cache = PriceCache()
    feed = MarketFeed(SimulatedSource(seed=5), cache,
                      get_active_tickers=lambda: ["AAPL"])
    await feed.prime()          # first fill: previous == price, direction flat
    first = await cache.get("AAPL")
    assert first is not None and first.direction == "flat"

    await feed._tick_once()     # second step: direction reflects the move
    second = await cache.get("AAPL")
    assert second.previous_price == first.price
    assert second.direction in ("up", "down", "flat")
```

Run with `uv run pytest`. All tests are deterministic (`seed=`), require no key,
and make no network calls (`httpx.MockTransport`).

---

## 13. Dependencies

Only two runtime libraries beyond FastAPI itself; the simulator needs nothing but
the standard library.

```toml
# backend/pyproject.toml  (market-data-relevant excerpt)
[project]
name = "finally-backend"
requires-python = ">=3.12"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",   # ASGI server; [standard] adds efficient SSE I/O
    "httpx>=0.27",               # async client for the Massive source
    "python-dotenv>=1.0",        # load .env in local dev
]

[dependency-groups]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
```

NumPy is intentionally omitted — at ten tickers, pure-Python `random.gauss` is
more than fast enough, and it keeps the image small (`MARKET_SIMULATOR.md` §1).

---

## 14. End-to-end data flow

```
              env: MASSIVE_API_KEY?  (read once in config.py)
                             |
        set ---------------- + ---------------- unset
         |                                        |
   MassiveSource                            SimulatedSource
   get_prices: 1 snapshot req               get_prices: GBM step
   get_history: aggregate bars              get_history: synthetic bars
   poll 15s (configurable)                  poll 0.5s
         \                                        /
          \                                      /
           +------> MarketDataSource contract <-+
                             |
        MarketFeed  (single writer; every poll_interval_seconds:
        reads watchlist ∪ positions, computes previous_price + direction)
                             |
                       PriceCache  (in-memory, one PriceTick/ticker, lock-guarded)
                             |
        +--------------------+--------------------+------------------+
        |                    |                    |                  |
  SSE /api/stream/prices   /api/portfolio   /api/history/{ticker}   (future
  (push changed ticks       (values positions  (source.get_history,   consumers)
   every ~0.5s)              from snapshot)      Massive or synthetic)
```

Swapping data sources is a one-line change in `make_source`. Everything from the
cache downward is identical regardless of source, and adding a third vendor later
means writing one class with two methods (`get_prices`, `get_history`).
