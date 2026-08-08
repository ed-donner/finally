# Market Data Interface

The unified Python interface FinAlly uses to retrieve stock prices. It hides
whether prices come from the **Massive API** (when `MASSIVE_API_KEY` is set) or
the **built-in simulator** (the default). All downstream code — SSE streaming,
portfolio valuation, the frontend — reads prices from one in-memory cache and
never knows or cares about the source.

See `MASSIVE_API.md` for the live-data endpoints and `MARKET_SIMULATOR.md` for
the simulator internals.

---

## 1. Design goals

- **One interface, two implementations.** A single small abstraction
  (`MarketDataSource`) that both the simulator and the Massive poller satisfy.
- **Source selection by environment variable.** `MASSIVE_API_KEY` present and
  non-empty selects Massive; otherwise the simulator. Decided once at startup.
- **A shared in-memory price cache.** One background task writes to it; SSE and
  REST endpoints read from it. This is the single source of truth for "current
  price" and cleanly supports future multi-user scenarios.
- **Decoupled cadences.** The source is polled at its own natural interval
  (~500ms simulator, ~15s Massive), while SSE pushes to clients at a steady
  ~500ms straight from the cache. The two rates are independent.
- **Simple and non-defensive.** Minimal types, no speculative abstraction. Just
  enough to swap sources without touching consumers.

---

## 2. Core data types

```python
# backend/market/types.py
from dataclasses import dataclass
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True)
class PriceTick:
    """A single ticker's latest price as held in the cache and pushed over SSE."""
    ticker: str
    price: float
    previous_price: float
    direction: Direction
    timestamp: str  # ISO 8601 UTC
```

The cache stores one `PriceTick` per ticker. `previous_price` and `direction`
are computed by the feed loop each time a new price arrives, so consumers get
the up/down flash information for free.

---

## 3. The interface

A source's only job is: given a set of tickers, produce their latest prices. It
does not touch the cache, compute directions, or manage timing — the feed loop
owns all of that. This keeps each source tiny.

```python
# backend/market/source.py
from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Produces the latest price for a set of tickers. One method to implement."""

    #: How often the feed loop should ask this source for fresh prices.
    poll_interval_seconds: float

    @abstractmethod
    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        """Return {ticker: price} for as many of the requested tickers as
        available. Missing tickers are simply omitted (the feed keeps their
        last cached value)."""
        ...

    async def aclose(self) -> None:
        """Release any resources (HTTP client, etc.). Default: no-op."""
        return None
```

That is the entire contract. Both implementations below satisfy it.

### Simulator source

The simulator holds internal GBM state and advances it one step per call. It
ignores `tickers` beyond ensuring state exists for each (it can price any symbol
by lazily seeding it). Full details in `MARKET_SIMULATOR.md`.

```python
# backend/market/simulator.py
from .source import MarketDataSource
from .gbm import SimEngine


class SimulatedSource(MarketDataSource):
    poll_interval_seconds = 0.5

    def __init__(self) -> None:
        self._engine = SimEngine()

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        return self._engine.step(tickers)  # advance GBM one tick, return prices
```

### Massive source

Wraps the snapshot endpoint from `MASSIVE_API.md` — one HTTP request returns
every watched ticker.

```python
# backend/market/massive.py
import httpx
from .source import MarketDataSource

BASE = "https://api.massive.com"


class MassiveSource(MarketDataSource):
    poll_interval_seconds = 15.0  # free tier: 5 req/min

    def __init__(self, api_key: str) -> None:
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._client = httpx.AsyncClient(base_url=BASE, timeout=10.0)

    async def get_prices(self, tickers: list[str]) -> dict[str, float]:
        resp = await self._client.get(
            "/v2/snapshot/locale/us/markets/stocks/tickers",
            params={"tickers": ",".join(tickers)},
            headers=self._headers,
        )
        resp.raise_for_status()
        prices: dict[str, float] = {}
        for item in resp.json().get("tickers", []):
            last = item.get("lastTrade", {}).get("p")
            day = item.get("day", {}).get("c")
            prev = item.get("prevDay", {}).get("c")
            price = last or day or prev
            if price:
                prices[item["ticker"]] = float(price)
        return prices

    async def aclose(self) -> None:
        await self._client.aclose()
```

---

## 4. The price cache

A thin in-memory dict of `PriceTick`, guarded for concurrent access. Written by
the feed loop, read by SSE and REST handlers.

```python
# backend/market/cache.py
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
            return dict(self._ticks)
```

`snapshot()` gives portfolio valuation and the SSE stream a consistent view of
all current prices in one call.

---

## 5. The feed loop (background task)

The single writer. It repeatedly asks the source for prices, computes
`previous_price` and `direction`, and updates the cache. The set of tickers it
requests is the current watchlist (read fresh each cycle so watchlist edits take
effect immediately).

```python
# backend/market/feed.py
import asyncio
from datetime import datetime, timezone
from .source import MarketDataSource
from .cache import PriceCache
from .types import PriceTick


class MarketFeed:
    def __init__(self, source: MarketDataSource, cache: PriceCache,
                 get_watchlist) -> None:
        self._source = source
        self._cache = cache
        self._get_watchlist = get_watchlist   # callable -> list[str]
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self._source.aclose()

    async def _run(self) -> None:
        while True:
            tickers = self._get_watchlist()
            if tickers:
                try:
                    prices = await self._source.get_prices(tickers)
                except Exception:
                    prices = {}  # transient failure: keep last cached values
                now = datetime.now(timezone.utc).isoformat()
                for ticker, price in prices.items():
                    prev = await self._cache.get(ticker)
                    previous = prev.price if prev else price
                    direction = (
                        "up" if price > previous
                        else "down" if price < previous
                        else "flat"
                    )
                    await self._cache.update(PriceTick(
                        ticker=ticker, price=price, previous_price=previous,
                        direction=direction, timestamp=now,
                    ))
            await asyncio.sleep(self._source.poll_interval_seconds)
```

Note the cadence: the feed sleeps for the *source's* interval (500ms sim / 15s
Massive). The SSE endpoint independently pushes the cache to clients every
~500ms, so even with slow Massive polling the UI stays smooth (prices simply
hold between polls).

---

## 6. Source selection (factory)

The only place the environment variable is read.

```python
# backend/market/factory.py
import os
from .source import MarketDataSource
from .simulator import SimulatedSource
from .massive import MassiveSource


def make_source() -> MarketDataSource:
    """Massive if MASSIVE_API_KEY is set and non-empty, else the simulator."""
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveSource(api_key)
    return SimulatedSource()
```

---

## 7. Wiring into FastAPI

Created once on startup via the lifespan handler, stopped on shutdown. The cache
and feed live on `app.state` so route handlers and the SSE endpoint can reach
them.

```python
# backend/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from market.cache import PriceCache
from market.feed import MarketFeed
from market.factory import make_source


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = make_source()
    feed = MarketFeed(source, cache, get_watchlist=load_watchlist_tickers)
    feed.start()
    app.state.price_cache = cache
    app.state.market_feed = feed
    try:
        yield
    finally:
        await feed.stop()


app = FastAPI(lifespan=lifespan)
```

`load_watchlist_tickers` reads the current watchlist from SQLite (see the schema
in `PLAN.md`). Because the feed calls it every cycle, adding or removing a ticker
via the API or the AI chat is picked up on the next poll with no restart.

---

## 8. How consumers use it

**SSE streaming** (`GET /api/stream/prices`) reads `app.state.price_cache`,
snapshots it every ~500ms, and emits one event per changed ticker:

```python
async def price_stream(cache: PriceCache):
    while True:
        for tick in (await cache.snapshot()).values():
            yield f"data: {json.dumps(asdict(tick))}\n\n"
        await asyncio.sleep(0.5)
```

**Portfolio valuation** (`GET /api/portfolio`) reads the same cache to price each
position at the current market price — no source-specific code.

---

## 9. Summary

```
                         env: MASSIVE_API_KEY?
                                  |
              set --------------- + --------------- unset
               |                                     |
        MassiveSource                          SimulatedSource
        (httpx snapshot,                       (GBM engine,
         poll 15s)                              poll 0.5s)
               \                                     /
                \                                   /
                 ---> MarketDataSource.get_prices <-
                                  |
                          MarketFeed (single writer:
                          computes prev price + direction)
                                  |
                             PriceCache  (in-memory, one PriceTick/ticker)
                                  |
                 +----------------+----------------+
                 |                                 |
            SSE /api/stream/prices          /api/portfolio valuation
```

Swapping data sources is a one-line change in `make_source`. Everything below
the cache is identical regardless of source. Adding a third source later (e.g. a
different vendor) means writing one class with a single `get_prices` method.
