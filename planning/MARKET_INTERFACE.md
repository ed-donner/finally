# Market Data Interface

The unified Python API FinAlly uses to retrieve stock prices. One interface,
two implementations: the Massive REST client when `MASSIVE_API_KEY` is set,
the built-in simulator otherwise. Everything downstream — the price cache,
the SSE stream, the portfolio valuation, the frontend — is agnostic to which
one is running.

Companion documents: `MASSIVE_API.md` (the upstream API) and
`MARKET_SIMULATOR.md` (the simulator's internals).

---

## 1. Shape of the Thing

```
                  create_provider()
                         |
          +--------------+--------------+
          |                             |
  SimulatorProvider              MassiveProvider
   (no API key)                  (MASSIVE_API_KEY set)
          |                             |
          +--------------+--------------+
                         |
                MarketDataProvider
                   .fetch(tickers) -> {ticker: Quote}
                         |
                    PriceEngine          background task, one per process
                         |               polls on provider.poll_interval
                         v
                    PriceCache           in-memory, latest tick per ticker
                         |
          +--------------+--------------+
          |                             |
  GET /api/stream/prices        portfolio valuation,
  (SSE, ~500ms)                 chat context, watchlist
```

Three deliberate splits:

- **The provider does not own a timer.** It answers `fetch()` when asked.
  Timing belongs to the engine, so both implementations stay trivial to
  test and neither spawns tasks of its own.
- **The engine's cadence is separate from the SSE cadence.** The engine
  polls as fast as the provider allows (500ms simulated, 15s on a free
  Massive key). SSE pushes from the cache every ~500ms regardless. A slow
  upstream never stalls the stream; it just repeats the last known price.
- **The cache is the single source of truth for "current price."** Trade
  execution, portfolio valuation and the LLM's context all read it, so a
  trade always fills at exactly the price the user was looking at.

---

## 2. Module Layout

```
backend/market/
├── __init__.py      # public exports, create_provider()
├── types.py         # Quote, PriceTick
├── provider.py      # MarketDataProvider abstract base class
├── simulator.py     # SimulatorProvider
├── massive.py       # MassiveProvider
├── cache.py         # PriceCache
└── engine.py        # PriceEngine background task
```

Import boundary: the rest of the backend imports from `backend.market` only.
Nothing outside this package imports `simulator` or `massive` directly, so
swapping or adding a provider touches one file.

---

## 3. Types

```python
"""Value types shared by every market data provider."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

Direction = Literal["up", "down", "flat"]


@dataclass(frozen=True, slots=True)
class Quote:
    """A price observation for one ticker, as reported by a provider."""

    ticker: str
    price: float
    previous_close: float
    timestamp: datetime

    @property
    def change(self) -> float:
        """Absolute move since the previous session's close."""
        return self.price - self.previous_close

    @property
    def change_percent(self) -> float:
        """Percentage move since the previous session's close."""
        if self.previous_close == 0:
            return 0.0
        return (self.price - self.previous_close) / self.previous_close * 100


@dataclass(frozen=True, slots=True)
class PriceTick:
    """A cached quote enriched with tick-over-tick movement for the UI."""

    ticker: str
    price: float
    previous_price: float
    previous_close: float
    timestamp: datetime
    direction: Direction

    @property
    def change(self) -> float:
        return self.price - self.previous_close

    @property
    def change_percent(self) -> float:
        if self.previous_close == 0:
            return 0.0
        return (self.price - self.previous_close) / self.previous_close * 100
```

Two "previous" values, because the UI needs two different things:

- `previous_price` is the immediately preceding tick. It drives `direction`,
  which drives the green/red flash animation.
- `previous_close` is the prior session's close. It drives the daily change
  percentage in the watchlist and positions table.

Providers only produce `Quote`. `PriceTick` is assembled by the cache, which
is the only component that knows what the previous tick was.

---

## 4. The Provider Interface

```python
"""Abstract interface every market data source implements."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from .types import Quote


class MarketDataProvider(ABC):
    """A pull-based source of current prices for a set of tickers.

    Implementations are cheap to construct and hold no background tasks.
    The caller decides when to fetch; `poll_interval` is the provider's
    advice on how often that should be.
    """

    name: str
    poll_interval: float

    @abstractmethod
    async def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Return current quotes keyed by ticker.

        Tickers with no available price are omitted rather than reported as
        errors. Raises only on failures affecting the whole request, such as
        a network error or an invalid API key.
        """

    async def aclose(self) -> None:
        """Release any held resources. Safe to call more than once."""
```

Design notes:

- **One method.** Both backends are pull-based, so `fetch()` covers both. No
  `subscribe`/`unsubscribe` pair to keep in sync with the watchlist.
- **`tickers` is passed per call, not held as state.** The watchlist changes
  at runtime; passing it in each call means there is nothing to
  resubscribe. The engine reads the current watchlist and hands it over.
- **Partial results are normal.** A ticker the user typed by mistake simply
  does not appear in the returned dict. The interface makes that the
  expected case rather than an exception.
- **`poll_interval` is mutable.** `MassiveProvider` widens it after
  discovering the key's tier (section 6), and the engine re-reads it each
  iteration.

---

## 5. Selecting the Provider

```python
"""Provider selection driven by environment variables."""

import os

from .massive import MassiveProvider
from .provider import MarketDataProvider
from .simulator import SimulatorProvider


def create_provider() -> MarketDataProvider:
    """Return the Massive provider if an API key is configured, else the simulator."""
    api_key = os.getenv("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveProvider(api_key=api_key)
    return SimulatorProvider()
```

`.strip()` matters: `MASSIVE_API_KEY=` in a `.env` file yields an empty
string, not an unset variable, and `PLAN.md` specifies that absent-or-empty
selects the simulator.

Environment variables consumed by this package:

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | unset | Non-empty selects `MassiveProvider` |
| `MARKET_POLL_SECONDS` | provider's default | Overrides `poll_interval` |
| `SIMULATOR_SEED` | unset | Fixes the simulator's RNG for tests |

Log the choice once at startup. "Which data source am I looking at" is the
first question anyone asks when prices look wrong:

```
market: provider=simulator poll_interval=0.5s tickers=10
market: provider=massive mode=snapshot poll_interval=15.0s
```

---

## 6. The Massive Provider

```python
"""Massive REST implementation of MarketDataProvider."""

import logging
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

import httpx

from .provider import MarketDataProvider
from .types import Quote

logger = logging.getLogger(__name__)

BASE_URL = "https://api.massive.com"
SNAPSHOT_PATH = "/v2/snapshot/locale/us/markets/stocks/tickers"


class MassiveProvider(MarketDataProvider):
    """Fetches prices from Massive, degrading to free-tier endpoints as needed."""

    name = "massive"

    def __init__(self, api_key: str, poll_interval: float = 5.0):
        self.poll_interval = poll_interval
        self._mode = "snapshot"
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(10.0, connect=5.0),
        )

    async def fetch(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Return quotes for the given tickers, one HTTP call per fetch."""
        if not tickers:
            return {}
        if self._mode == "snapshot":
            try:
                return await self._fetch_snapshot(tickers)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code != 403:
                    raise
                self._downgrade()
        return await self._fetch_grouped(tickers)

    def _downgrade(self) -> None:
        """Switch to free-tier endpoints after a 403 on the snapshot endpoint."""
        self._mode = "grouped"
        self.poll_interval = max(self.poll_interval, 15.0)
        logger.warning(
            "massive: snapshot endpoint not available on this plan, "
            "falling back to end-of-day grouped bars at %.0fs",
            self.poll_interval,
        )

    async def _fetch_snapshot(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """Current prices via /v2/snapshot. Requires Starter plan or above."""
        response = await self._client.get(
            SNAPSHOT_PATH, params={"tickers": ",".join(tickers)}
        )
        response.raise_for_status()
        now = datetime.now(timezone.utc)
        quotes = {}
        for row in response.json().get("tickers", []):
            previous_close = float(row.get("prevDay", {}).get("c") or 0.0)
            price = _snapshot_price(row) or previous_close
            if price:
                quotes[row["ticker"]] = Quote(
                    ticker=row["ticker"],
                    price=price,
                    previous_close=previous_close or price,
                    timestamp=now,
                )
        return quotes

    async def _fetch_grouped(self, tickers: Sequence[str]) -> dict[str, Quote]:
        """End-of-day closes via grouped daily bars. Available on every plan."""
        wanted = set(tickers)
        today = datetime.now(timezone.utc).date()
        for offset in range(5):
            day = today - timedelta(days=offset)
            rows = await self._grouped_bars(day)
            if rows:
                stamp = datetime.now(timezone.utc)
                return {
                    row["T"]: Quote(
                        ticker=row["T"],
                        price=float(row["c"]),
                        previous_close=float(row["o"]),
                        timestamp=stamp,
                    )
                    for row in rows
                    if row["T"] in wanted
                }
        return {}

    async def _grouped_bars(self, day: date) -> list[dict]:
        """Grouped daily bars for one date. Empty list on a non-trading day."""
        response = await self._client.get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            params={"adjusted": "true"},
        )
        response.raise_for_status()
        return response.json().get("results") or []

    async def aclose(self) -> None:
        await self._client.aclose()


def _snapshot_price(row: dict) -> float | None:
    """Best available current price from a snapshot row, most reliable first."""
    for value in (
        row.get("lastTrade", {}).get("p"),
        row.get("min", {}).get("c"),
        row.get("day", {}).get("c"),
    ):
        if value:
            return float(value)
    return None
```

Points worth calling out:

- **One HTTP call per fetch, in either mode.** Snapshot takes a `tickers=`
  filter; grouped daily returns the whole market and is filtered
  client-side. Watchlist size never changes the call count, which is what
  keeps a free key inside 5 calls/min.
- **The 403 downgrade is discovered, not configured.** There is no reliable
  way to ask an API key what tier it is on, so the provider tries the good
  endpoint once and remembers the answer. No `MASSIVE_PLAN` env var to get
  wrong.
- **Grouped mode uses the session open as `previous_close`.** In that mode
  both values come from the same bar, so the change percentage reflects that
  day's move. It is the honest reading of the only data a free key has.
- **`httpx` rather than the official `massive` client.** The official client
  is synchronous urllib3 and would block the event loop; this provider needs
  exactly two endpoints. See `MASSIVE_API.md` section 6.2 for the client
  library if that trade-off is ever revisited.

Add the dependency with `uv add httpx`.

---

## 7. The Price Cache

```python
"""In-memory store of the latest tick per ticker."""

from collections.abc import Iterable

from .types import Direction, PriceTick, Quote


class PriceCache:
    """Latest known price per ticker, plus tick-over-tick direction.

    Single-writer (the engine), many-reader (SSE, routes). Reads return
    snapshots so callers never observe a half-applied update.
    """

    def __init__(self) -> None:
        self._ticks: dict[str, PriceTick] = {}

    def update(self, quotes: Iterable[Quote]) -> list[PriceTick]:
        """Apply quotes and return the ticks that changed."""
        changed = []
        for quote in quotes:
            existing = self._ticks.get(quote.ticker)
            previous_price = existing.price if existing else quote.price
            tick = PriceTick(
                ticker=quote.ticker,
                price=quote.price,
                previous_price=previous_price,
                previous_close=quote.previous_close,
                timestamp=quote.timestamp,
                direction=_direction(previous_price, quote.price),
            )
            self._ticks[quote.ticker] = tick
            changed.append(tick)
        return changed

    def get(self, ticker: str) -> PriceTick | None:
        """Latest tick for one ticker, or None if never seen."""
        return self._ticks.get(ticker)

    def snapshot(self) -> dict[str, PriceTick]:
        """A copy of every known tick."""
        return dict(self._ticks)

    def prune(self, keep: Iterable[str]) -> None:
        """Drop tickers no longer on any watchlist."""
        keeping = set(keep)
        self._ticks = {t: v for t, v in self._ticks.items() if t in keeping}


def _direction(previous: float, current: float) -> Direction:
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"
```

The first observation of a ticker sets `previous_price == price`, so
`direction` is `"flat"` and the UI does not flash on page load.

No lock is needed. Everything runs in one asyncio event loop, the engine is
the only writer, and `dict` assignment does not yield.

---

## 8. The Price Engine

```python
"""Background task that keeps the price cache current."""

import asyncio
import logging
from collections.abc import Callable, Sequence

from .cache import PriceCache
from .provider import MarketDataProvider

logger = logging.getLogger(__name__)


class PriceEngine:
    """Polls a provider on its advised interval and writes into the cache."""

    def __init__(
        self,
        provider: MarketDataProvider,
        cache: PriceCache,
        tickers: Callable[[], Sequence[str]],
    ):
        self._provider = provider
        self._cache = cache
        self._tickers = tickers
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin polling. Idempotent."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="price-engine")

    async def stop(self) -> None:
        """Cancel the poll loop and release the provider."""
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        await self._provider.aclose()

    async def _run(self) -> None:
        while True:
            try:
                tickers = list(self._tickers())
                if tickers:
                    quotes = await self._provider.fetch(tickers)
                    self._cache.update(quotes.values())
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("price engine poll failed, keeping last prices")
            await asyncio.sleep(self._provider.poll_interval)
```

The `tickers` callable is what makes watchlist changes take effect without a
restart. In the app it reads the watchlist table; in tests it is a lambda
returning a fixed list.

The broad `except Exception` is one of the few places defensive code earns
its place. A transient upstream failure must not kill the only task feeding
every connected SSE client. The cache keeps its last values, prices go
stale rather than blank, and the next poll recovers on its own.

---

## 9. Wiring into FastAPI

```python
"""Application wiring for market data."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.market import PriceCache, PriceEngine, create_provider


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start the price engine for the lifetime of the app."""
    cache = PriceCache()
    provider = create_provider()
    engine = PriceEngine(provider, cache, tickers=all_watched_tickers)

    app.state.prices = cache
    engine.start()
    try:
        yield
    finally:
        await engine.stop()


app = FastAPI(lifespan=lifespan)
```

Reading a price anywhere else in the backend:

```python
def current_price(request: Request, ticker: str) -> float:
    """Price a trade fills at, or raise if the ticker has no price yet."""
    tick = request.app.state.prices.get(ticker)
    if tick is None:
        raise HTTPException(422, f"No price available for {ticker}")
    return tick.price
```

The SSE endpoint reads the same cache on its own clock:

```python
async def stream_prices(request: Request):
    """Push every known price to the client roughly twice a second."""
    cache = request.app.state.prices
    while not await request.is_disconnected():
        for tick in cache.snapshot().values():
            yield {"event": "price", "data": json.dumps(_serialize(tick))}
        await asyncio.sleep(0.5)
```

---

## 10. Contract Tests

`PLAN.md` calls for verifying that both implementations conform to the
abstract interface. Parametrize one suite over both providers so a new
provider inherits the whole thing:

```python
import pytest

from backend.market import MassiveProvider, SimulatorProvider

TICKERS = ["AAPL", "GOOGL", "MSFT"]


@pytest.fixture(params=["simulator", "massive"])
def provider(request, httpx_mock):
    if request.param == "simulator":
        return SimulatorProvider(seed=42)
    _mock_massive_snapshot(httpx_mock, TICKERS)
    return MassiveProvider(api_key="test-key")


async def test_fetch_returns_quotes_keyed_by_ticker(provider):
    quotes = await provider.fetch(TICKERS)
    assert set(quotes) <= set(TICKERS)
    assert all(quotes[t].ticker == t for t in quotes)


async def test_prices_are_positive(provider):
    quotes = await provider.fetch(TICKERS)
    assert all(q.price > 0 and q.previous_close > 0 for q in quotes.values())


async def test_empty_ticker_list_makes_no_request(provider):
    assert await provider.fetch([]) == {}


async def test_unknown_ticker_is_omitted_not_raised(provider):
    quotes = await provider.fetch(["NOTATICKER"])
    assert "NOTATICKER" not in quotes
```

Provider-specific tests then cover only what is genuinely specific: the 403
downgrade path and grouped-bar parsing for Massive, distributional and
determinism properties for the simulator.

For route and E2E tests, a third implementation keeps things deterministic
without touching the network or the RNG:

```python
class FixedProvider(MarketDataProvider):
    """Returns preset prices. For tests that need a known portfolio value."""

    name = "fixed"
    poll_interval = 0.05

    def __init__(self, prices: dict[str, float]):
        self._prices = prices

    async def fetch(self, tickers):
        now = datetime.now(timezone.utc)
        return {
            t: Quote(t, self._prices[t], self._prices[t], now)
            for t in tickers
            if t in self._prices
        }
```

---

## 11. Deliberately Not in the Interface

Kept out to hold the surface to what the build actually needs:

- **Historical bars.** `PLAN.md` builds sparklines and charts from SSE data
  accumulated since page load, so no provider needs a history method. If
  pre-seeded charts are wanted later, add a separate optional protocol
  (`HistoricalDataProvider` with `fetch_bars(ticker, timespan, start, end)`)
  and feature-detect it with `isinstance`. That leaves the simulator, which
  has no real history, free not to implement it.
- **WebSocket streaming.** Massive offers it on paid tiers, but it is a
  second transport with its own reconnect logic for no user-visible gain
  over a few-second REST poll. `PLAN.md` chose REST polling.
- **Per-provider rate limiters.** `poll_interval` already bounds the call
  rate, and every fetch is a single HTTP call.
- **Quotes, order books, volume analytics.** Market orders fill at the last
  price. Nothing else is needed.
