# Market Data Backend — Design

Implementation design for the FinAlly market data subsystem in `backend/app/market/`: the unified API, the GBM simulator, the Massive client, the price cache and the SSE stream. It builds on the existing code and closes the gaps against `PLAN.md` that are listed in `MARKET_INTERFACE.md` ("Gaps between the code and PLAN.md").

Related docs: `MARKET_INTERFACE.md` (public API summary), `MARKET_SIMULATOR.md` (simulator math), `MASSIVE_API.md` (endpoint reference). Written 2026-11-12, checked against the installed `massive` package in `backend/.venv`.

---

## 1. What changes versus the current code

| # | Change | Files | Why (PLAN.md) |
|---|---|---|---|
| 1 | Massive: read `last_trade.sip_timestamp` (ns) and fall back to `min`/`day` when there is no `last_trade` | `massive_client.py` | Bug: `LastTrade` has no `timestamp`, so every ticker is skipped today (verified) |
| 2 | Add a per-ticker `baseline` and `daily_change_percent` | `models.py`, `cache.py`, both sources | §6: the cache holds the baseline and SSE sends daily change % |
| 3 | Add `validate_ticker()` to the interface | `interface.py`, both sources | §6/§8: a Massive ticker with no quote is rejected with `unknown_ticker` (404) |
| 4 | "Market closed" detection: the cache tracks when any price last changed | `cache.py`, `stream.py` | §6: show the label when Massive data has not changed for more than 60s |
| 5 | Build the SSE router inside the factory, not at module level | `stream.py` | Calling the factory twice (in tests) registers `/prices` twice |
| 6 | The SSE payload becomes `{"prices": {...}, "market_closed": bool}` | `stream.py` | Carries item 4; the frontend is not built yet, so nothing breaks |
| 7 | `cache.update` treats `timestamp=0` correctly (`is None` check) | `cache.py` | Minor correctness |
| 8 | Rule for the tracked set (watchlist union positions) lives in the service layer | new `app/services/tracking.py` | §6: a held ticker removed from the watchlist keeps streaming |

Unchanged: the strategy pattern, `PriceCache` as the single source of truth, the GBM math, the Cholesky correlation, shock events, the factory env var rule, and the 0.5s SSE cadence.

---

## 2. Architecture

```
                 create_market_data_source(cache)        <- only place reading MASSIVE_API_KEY
                    |                         |
        SimulatorDataSource            MassiveDataSource
        GBM step every 0.5s            get_snapshot_all every 15s (asyncio.to_thread)
        baseline = seed price          baseline = prev_day.close
                    \                         /
                     \   cache.update(...)   /
                      v                     v
                 PriceCache  (threading.Lock, version counter, last_change_at)
                      |
       +--------------+---------------+------------------+
       |              |               |                  |
  SSE stream    portfolio value   trade fill price   watchlist GET
  /api/stream/prices
```

Rules:
- Sources push into the cache; consumers only read from the cache. Nothing asks a source for a price.
- Sources are started once in the FastAPI lifespan and stopped on shutdown. The app runs a single uvicorn worker, because the cache lives in memory.
- Tickers reaching the market layer are already normalised (uppercased, trimmed, validated by regex) at the API boundary.

File layout (unchanged apart from the new service):

```
backend/app/market/
  __init__.py        public re-exports
  models.py          PriceUpdate
  cache.py           PriceCache
  interface.py       MarketDataSource (ABC)
  seed_prices.py     seed prices, GBM params, correlation constants
  simulator.py       GBMSimulator + SimulatorDataSource
  massive_client.py  MassiveDataSource + parse_snapshot
  factory.py         create_market_data_source
  stream.py          create_stream_router (SSE)
backend/app/services/
  tracking.py        sync_tracking(): watchlist union positions -> source
```

---

## 3. Data model — `models.py`

`PriceUpdate` is the only type that leaves the market layer. Two fields are new: `baseline` (the reference price for daily change) and the derived `daily_change_percent`.

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of one ticker's price.

    baseline is the reference for daily change: the seed price (simulator)
    or the previous close (Massive).
    """

    ticker: str
    price: float
    previous_price: float
    baseline: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Tick-to-tick absolute change."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Tick-to-tick percent change."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def daily_change_percent(self) -> float:
        """Percent change versus the baseline (seed price or previous close)."""
        if self.baseline == 0:
            return 0.0
        return round((self.price - self.baseline) / self.baseline * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down' or 'flat' versus the previous price."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """JSON form used by SSE and REST responses."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "baseline": self.baseline,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "daily_change_percent": self.daily_change_percent,
            "direction": self.direction,
        }
```

Example:

```python
u = PriceUpdate("AAPL", price=191.6, previous_price=191.5, baseline=190.08, timestamp=1731200000.0)
u.direction             # "up"
u.change_percent        # 0.0522   (tick-to-tick)
u.daily_change_percent  # 0.7997   (vs previous close)
```

---

## 4. Price cache — `cache.py`

Changes:
- `update()` takes an optional `baseline`. When it is `None`, the existing baseline is kept, and on the first update the price itself becomes the baseline.
- `last_change_at` records the wall-clock time at which any ticker's price last changed value. This drives "market closed".
- `timestamp=0.0` is no longer replaced by "now".

```python
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Latest PriceUpdate per ticker.

    Writers: the active MarketDataSource (simulator loop or Massive poll thread).
    Readers: SSE, portfolio valuation, trade execution, watchlist.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0
        self._last_change_at = time.time()

    def update(
        self,
        ticker: str,
        price: float,
        timestamp: float | None = None,
        baseline: float | None = None,
    ) -> PriceUpdate:
        """Store a new price and return the resulting PriceUpdate.

        previous_price is the prior cached price (or the price itself on the
        first update). baseline defaults to the prior baseline, then to price.
        """
        price = round(price, 2)
        with self._lock:
            prev = self._prices.get(ticker)
            previous_price = prev.price if prev else price
            if baseline is None:
                baseline = prev.baseline if prev else price
            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                baseline=round(baseline, 2),
                timestamp=time.time() if timestamp is None else timestamp,
            )
            self._prices[ticker] = update
            self._version += 1
            if prev is None or prev.price != price:
                self._last_change_at = time.time()
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy of all prices."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._prices.pop(ticker, None)
            self._version += 1

    @property
    def version(self) -> int:
        """Bumps on every write; SSE sends only when it changes."""
        with self._lock:
            return self._version

    def seconds_since_change(self) -> float:
        """Seconds since any cached price last changed value."""
        with self._lock:
            return time.time() - self._last_change_at

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
```

Notes:
- `remove()` now bumps `version` too, so SSE clients see a removed ticker disappear on the next push.
- `threading.Lock`, not `asyncio.Lock`: the Massive poll writes from a worker thread (`asyncio.to_thread`).
- Why "price changed" rather than "trade timestamp is old" for staleness: on 15-minute-delayed Massive plans every trade timestamp is at least 15 minutes old, even while the market is open. A price that has not moved across four 15s polls is a reliable sign that the market is closed.

---

## 5. Unified interface — `interface.py`

One method is new: `validate_ticker`.

```python
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations write into a shared PriceCache on their own schedule.
    Consumers read prices from the cache, never from the source.
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Start the background task. Call once. First prices are cached on return."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task. Idempotent."""

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Track a ticker. No-op if already tracked."""

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Stop tracking a ticker and drop it from the cache. No-op if absent."""

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""

    @abstractmethod
    async def validate_ticker(self, ticker: str) -> bool:
        """True if the ticker has a price. A valid ticker is in the cache on return."""

    @property
    def is_live_market(self) -> bool:
        """True for real market data, where prices can go stale outside market hours."""
        return False
```

Contract for `validate_ticker`: when it returns `True`, `cache.get_price(ticker)` is not `None`. The trade and watchlist services rely on this, so a newly added Massive ticker can be traded straight away instead of waiting up to 15s for the next poll.

`is_live_market` lets the SSE stream show "market closed" for Massive only (PLAN §6: the simulator runs 24/7).

---

## 6. Simulator — `simulator.py`

The math is unchanged (see `MARKET_SIMULATOR.md`). The only changes:
- `GBMSimulator` remembers each ticker's seed price and exposes it through `get_seed()`.
- `SimulatorDataSource` passes the seed as `baseline`, and implements `validate_ticker`.

### 6.1 `GBMSimulator` changes

```python
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._seeds: dict[str, float] = {}          # new
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def get_seed(self, ticker: str) -> float | None:
        """Starting price for a ticker; the baseline for daily change %."""
        return self._seeds.get(ticker)

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        seed = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
        self._tickers.append(ticker)
        self._prices[ticker] = seed
        self._seeds[ticker] = seed
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._seeds[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()
```

`step()`, `_rebuild_cholesky()` and `_pairwise_correlation()` stay as they are. For reference, the core of `step()`:

```python
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z                       # correlated draws
        for i, ticker in enumerate(self._tickers):
            p = self._params[ticker]
            drift = (p["mu"] - 0.5 * p["sigma"] ** 2) * self._dt
            diffusion = p["sigma"] * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)
            if random.random() < self._event_prob:       # rare 2-5% shock
                self._prices[ticker] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
            result[ticker] = round(self._prices[ticker], 2)
```

### 6.2 `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    """MarketDataSource backed by GBMSimulator, stepping every update_interval seconds."""

    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._sim = GBMSimulator(tickers=[], event_probability=event_probability)
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        for ticker in tickers:
            self._sim.add_ticker(ticker)
            self._seed_cache(ticker)
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

    async def add_ticker(self, ticker: str) -> None:
        self._sim.add_ticker(ticker)
        self._seed_cache(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers()

    async def validate_ticker(self, ticker: str) -> bool:
        """Every well-formed ticker is valid; make sure it has a price."""
        if ticker not in self._cache:
            await self.add_ticker(ticker)
        return True

    def _seed_cache(self, ticker: str) -> None:
        """Write the current simulated price with the seed price as baseline."""
        if ticker not in self._cache:
            self._cache.update(ticker, self._sim.get_price(ticker),
                               baseline=self._sim.get_seed(ticker))

    async def _run_loop(self) -> None:
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker, price)    # baseline kept from seed
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

The simulator is built empty in `__init__` and filled in `start()`. This removes the `if self._sim:` checks, so `add_ticker` before `start` also works.

Example:

```python
cache = PriceCache()
src = SimulatorDataSource(cache)
await src.start(["AAPL", "NVDA"])
cache.get("AAPL").to_dict()
# {"ticker": "AAPL", "price": 190.0, "previous_price": 190.0, "baseline": 190.0,
#  "change": 0.0, "change_percent": 0.0, "daily_change_percent": 0.0, "direction": "flat", ...}
await src.validate_ticker("PYPL")     # True, PYPL now priced (random seed 50-300)
await src.stop()
```

---

## 7. Massive client — `massive_client.py`

Polls `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=...` with one call per poll, through the synchronous `massive.RESTClient` run inside `asyncio.to_thread`. Field names were checked against the installed package:

| Need | Attribute | Unit |
|---|---|---|
| Price (preferred) | `snap.last_trade.price` | USD |
| Trade time | `snap.last_trade.sip_timestamp` | **nanoseconds** |
| Price fallback 1 | `snap.min.close`, time `snap.min.timestamp` | USD, **milliseconds** |
| Price fallback 2 | `snap.day.close` | USD |
| Baseline | `snap.prev_day.close` | USD |

`last_trade` and `min` are `None` when the plan lacks trade access or before the open (verified with `TickerSnapshot.from_dict`).

### 7.1 Snapshot parsing (pure function, easy to test)

```python
from dataclasses import dataclass

from massive.rest.models import TickerSnapshot


@dataclass(frozen=True, slots=True)
class Quote:
    """Price fields extracted from one Massive snapshot."""

    ticker: str
    price: float
    timestamp: float | None   # Unix seconds; None means "now"
    baseline: float | None


def parse_snapshot(snap: TickerSnapshot) -> Quote | None:
    """Pick the best available price from a snapshot, or None if it has none."""
    baseline = snap.prev_day.close if snap.prev_day and snap.prev_day.close else None
    if snap.last_trade and snap.last_trade.price:
        ts = snap.last_trade.sip_timestamp
        return Quote(snap.ticker, snap.last_trade.price, ts / 1e9 if ts else None, baseline)
    if snap.min and snap.min.close:
        ts = snap.min.timestamp
        return Quote(snap.ticker, snap.min.close, ts / 1e3 if ts else None, baseline)
    if snap.day and snap.day.close:
        return Quote(snap.ticker, snap.day.close, None, baseline)
    return None
```

Example with the `MASSIVE_API.md` sample:

```python
snap = TickerSnapshot.from_dict({
    "ticker": "AAPL",
    "day": {"c": 191.6}, "prevDay": {"c": 190.08},
    "min": {"c": 191.6, "t": 1684428600000},
    "lastTrade": {"p": 191.6, "t": 1605192894630916600},
})
parse_snapshot(snap)
# Quote(ticker='AAPL', price=191.6, timestamp=1605192894.6309166, baseline=190.08)

parse_snapshot(TickerSnapshot.from_dict({"ticker": "X", "day": {"c": 10.0}, "prevDay": {"c": 9.0}}))
# Quote(ticker='X', price=10.0, timestamp=None, baseline=9.0)
```

### 7.2 `MassiveDataSource`

```python
class MassiveDataSource(MarketDataSource):
    """MarketDataSource polling the Massive full-market snapshot endpoint."""

    def __init__(self, api_key: str, price_cache: PriceCache,
                 poll_interval: float = 15.0) -> None:
        self._client = RESTClient(api_key=api_key)
        self._cache = price_cache
        self._interval = poll_interval
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._failing = False

    @property
    def is_live_market(self) -> bool:
        return True

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(dict.fromkeys(tickers))
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, %.1fs", len(self._tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        if ticker not in self._tickers:
            self._tickers.append(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            self._tickers.remove(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    async def validate_ticker(self, ticker: str) -> bool:
        """One single-ticker snapshot call for tickers not yet cached."""
        if ticker in self._cache:
            return True
        try:
            snap = await asyncio.to_thread(
                self._client.get_snapshot_ticker, SnapshotMarketType.STOCKS, ticker
            )
        except Exception as e:                       # 404 for unknown tickers
            logger.info("Massive: %s rejected: %s", ticker, e)
            return False
        quote = parse_snapshot(snap)
        if quote is None:
            return False
        self._write(quote)
        return True

    async def _poll_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._poll_once()

    async def _poll_once(self) -> None:
        """Fetch all tracked tickers in one call and write them to the cache."""
        if not self._tickers:
            return
        try:
            snaps = await asyncio.to_thread(
                self._client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=list(self._tickers),
            )
        except Exception as e:
            self._log_failure(e)
            return
        self._failing = False
        for snap in snaps:
            quote = parse_snapshot(snap)
            if quote is None:
                logger.warning("Massive: no price in snapshot for %s", snap.ticker)
                continue
            self._write(quote)

    def _write(self, quote: Quote) -> None:
        self._cache.update(quote.ticker, quote.price,
                           timestamp=quote.timestamp, baseline=quote.baseline)

    def _log_failure(self, e: Exception) -> None:
        """Log the first failure at error level, repeats at debug."""
        if self._failing:
            logger.debug("Massive poll still failing: %s", e)
        else:
            logger.error("Massive poll failed: %s", e)
        self._failing = True
```

Design notes:
- **Tickers removed mid-poll.** A snapshot can arrive for a ticker that `remove_ticker` dropped while the call was in flight, and writing it would re-add a stale entry. Guard against this in `_poll_once` with `if snap.ticker not in self._tickers: continue` before writing.
- **Rate budget.** On the free tier, polling every 15s uses 4 calls/min, which leaves about 1 call/min for `validate_ticker`. `RESTClient` retries 429/5xx up to 3 times internally.
- **Free-tier caveat.** `MASSIVE_API.md` notes that snapshot endpoints may return 403 on Stocks Basic. The poller then logs one error and the cache stays empty. That is acceptable: users without a paid plan should use the simulator. The grouped-daily fallback is out of scope.
- **No lazy import.** `massive` is a core dependency (`pyproject.toml`), so the imports stay at the top of the module.
- **Retries on start.** If the first poll in `start()` fails, the app still starts; prices appear on the first successful poll.

---

## 8. Factory — `factory.py`

Unchanged in behaviour:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """MassiveDataSource if MASSIVE_API_KEY is set and non-blank, else SimulatorDataSource."""
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

An optional `MASSIVE_POLL_INTERVAL` env var (float seconds, default 15) can be read here and passed as `poll_interval` for paid tiers. Leave it out unless someone needs it.

---

## 9. SSE stream — `stream.py`

Changes: the router is created inside the factory (no module-level state), the payload is wrapped to carry `market_closed`, and the factory takes the source so it can ask `is_live_market`.

```python
"""SSE endpoint streaming the price cache."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache
from .interface import MarketDataSource

logger = logging.getLogger(__name__)

STALE_AFTER_SECONDS = 60.0


def create_stream_router(cache: PriceCache, source: MarketDataSource,
                         interval: float = 0.5) -> APIRouter:
    """Router exposing GET /api/stream/prices."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            generate_events(cache, source, request, interval),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


def build_payload(cache: PriceCache, source: MarketDataSource) -> str:
    """One SSE data line holding every cached ticker."""
    market_closed = (source.is_live_market
                     and cache.seconds_since_change() > STALE_AFTER_SECONDS)
    prices = {t: u.to_dict() for t, u in cache.get_all().items()}
    return f"data: {json.dumps({'prices': prices, 'market_closed': market_closed})}\n\n"


async def generate_events(cache: PriceCache, source: MarketDataSource, request: Request,
                          interval: float) -> AsyncGenerator[str, None]:
    """Yield an event whenever the cache version changes; stop on disconnect."""
    yield "retry: 1000\n\n"
    last_version = -1
    while not await request.is_disconnected():
        version = cache.version
        if version != last_version:
            last_version = version
            yield build_payload(cache, source)
        await asyncio.sleep(interval)
```

Wire format (one event, one line, shown wrapped here):

```
retry: 1000

data: {"prices": {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.47,
       "baseline": 190.0, "timestamp": 1762950000.5, "change": 0.05, "change_percent": 0.0263,
       "daily_change_percent": 0.2737, "direction": "up"}, "GOOGL": {...}},
       "market_closed": false}
```

Frontend consumption:

```ts
const es = new EventSource("/api/stream/prices");
es.onmessage = (e) => {
  const { prices, market_closed } = JSON.parse(e.data);
  // prices: Record<string, {ticker, price, previous_price, baseline, timestamp,
  //                          change, change_percent, daily_change_percent, direction}>
};
// es.readyState: CONNECTING -> yellow dot, OPEN -> green, CLOSED -> red
```

Cadence: the simulator bumps `version` every 0.5s, so clients get about 2 events/s. Massive bumps it once per poll, so clients get one event per 15s. A removed ticker bumps `version`, so its removal reaches clients on the next push.

The stream has no list of its own: it sends whatever is in the cache. The cache holds exactly the tracked set (section 11), so this matches PLAN's "watchlist plus open positions".

---

## 10. Public API — `__init__.py`

```python
from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router

__all__ = ["PriceUpdate", "PriceCache", "MarketDataSource",
           "create_market_data_source", "create_stream_router"]
```

---

## 11. Integration with the rest of the backend

### 11.1 Lifespan (`app/main.py`)

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Init DB, start market data, stop it on shutdown."""
    db = init_db(DB_PATH)                          # creates schema + seed if needed
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(tracked_tickers(db))        # watchlist union open positions
    app.state.db, app.state.cache, app.state.source = db, cache, source
    app.include_router(create_stream_router(cache, source))
    snapshot_task = asyncio.create_task(record_snapshots_every(30, app.state))
    yield
    snapshot_task.cancel()
    await source.stop()
```

### 11.2 The tracked set rule (`app/services/tracking.py`)

PLAN §6/§8: the cache covers the union of watchlist and open positions. Removing a watchlist ticker that is still held must keep its price streaming. One function owns the rule, and every mutation calls it:

```python
def tracked_tickers(db) -> list[str]:
    """Watchlist tickers union open-position tickers, watchlist order first."""
    watch = [r["ticker"] for r in db.execute(
        "SELECT ticker FROM watchlist WHERE user_id = ? ORDER BY added_at", (DEFAULT_USER,))]
    held = [r["ticker"] for r in db.execute(
        "SELECT ticker FROM positions WHERE user_id = ?", (DEFAULT_USER,))]
    return list(dict.fromkeys(watch + held))


async def sync_tracking(db, source: MarketDataSource) -> None:
    """Make the source track exactly tracked_tickers(db)."""
    wanted = set(tracked_tickers(db))
    current = set(source.get_tickers())
    for ticker in wanted - current:
        await source.add_ticker(ticker)
    for ticker in current - wanted:
        await source.remove_ticker(ticker)
```

Call sites:

| Action | Sequence |
|---|---|
| `POST /api/watchlist` | `validate_ticker` -> 404 `unknown_ticker` if false -> insert row -> `sync_tracking` |
| `DELETE /api/watchlist/{t}` | delete row -> `sync_tracking` (keeps `t` if held) |
| `POST /api/portfolio/trade` | `validate_ticker` -> fill at `cache.get_price` -> transaction -> `sync_tracking` (a first buy adds the ticker; a full sell of a non-watchlist ticker drops it) |
| `POST /api/portfolio/reset` | reset tables -> `sync_tracking` (back to the 10 defaults) |
| Chat actions | same service functions as above |

### 11.3 Reading prices in services

```python
price = app.state.cache.get_price(ticker)          # trade fill, valuation
if price is None:                                   # cannot happen after validate_ticker
    raise ApiError(404, "unknown_ticker", f"No price for {ticker}")
```

Watchlist `GET` returns, for each row, `cache.get(ticker).to_dict()`, or `null` price fields if the ticker is not cached yet.

---

## 12. Testing

Existing tests in `backend/tests/market/` stay, with these updates and additions. Run them with `uv run --extra dev pytest -v`.

### 12.1 Models and cache

```python
def test_daily_change_percent_uses_baseline():
    u = PriceUpdate("AAPL", price=200.0, previous_price=199.0, baseline=190.0)
    assert u.daily_change_percent == round(10 / 190 * 100, 4)


def test_baseline_set_on_first_update_and_kept():
    cache = PriceCache()
    cache.update("AAPL", 190.0, baseline=188.0)
    assert cache.update("AAPL", 191.0).baseline == 188.0


def test_baseline_defaults_to_first_price():
    assert PriceCache().update("AAPL", 190.0).baseline == 190.0


def test_zero_timestamp_kept():
    assert PriceCache().update("AAPL", 1.0, timestamp=0.0).timestamp == 0.0


def test_unchanged_price_does_not_reset_change_clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("app.market.cache.time.time", lambda: now[0])
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    now[0] = 1100.0
    cache.update("AAPL", 190.0)                       # same price, 100s later
    assert cache.seconds_since_change() == 100.0
```

### 12.2 Simulator

```python
async def test_simulator_baseline_is_seed():
    cache = PriceCache()
    src = SimulatorDataSource(cache, update_interval=0.01)
    await src.start(["AAPL"])
    await asyncio.sleep(0.05)
    assert cache.get("AAPL").baseline == SEED_PRICES["AAPL"]
    await src.stop()


async def test_simulator_validate_any_ticker_prices_it():
    cache = PriceCache()
    src = SimulatorDataSource(cache)
    await src.start([])
    assert await src.validate_ticker("ZZZZ")
    assert 50 <= cache.get_price("ZZZZ") <= 300
    await src.stop()
```

### 12.3 Massive: real models, not MagicMock

Build the snapshots with `TickerSnapshot.from_dict`, so a wrong attribute name fails the test. The current mocks hid the `timestamp` bug.

```python
from massive.rest.models import TickerSnapshot

def snap(ticker, price=None, prev=None, trade_ns=None, minute=None, day=None):
    d = {"ticker": ticker}
    if price is not None:
        d["lastTrade"] = {"p": price, "t": trade_ns}
    if prev is not None:
        d["prevDay"] = {"c": prev}
    if minute is not None:
        d["min"] = {"c": minute, "t": 1684428600000}
    if day is not None:
        d["day"] = {"c": day}
    return TickerSnapshot.from_dict(d)


def test_parse_last_trade_ns_to_seconds():
    q = parse_snapshot(snap("AAPL", 191.6, 190.08, trade_ns=1605192894630916600))
    assert (q.price, q.baseline) == (191.6, 190.08)
    assert q.timestamp == pytest.approx(1605192894.63)


def test_parse_falls_back_to_minute_then_day():
    assert parse_snapshot(snap("A", minute=10.5, prev=10)).price == 10.5
    assert parse_snapshot(snap("A", day=11.0)).price == 11.0
    assert parse_snapshot(snap("A")) is None


async def test_poll_writes_cache(monkeypatch):
    cache = PriceCache()
    src = MassiveDataSource("k", cache, poll_interval=60)
    monkeypatch.setattr(src._client, "get_snapshot_all",
                        lambda **kw: [snap("AAPL", 191.6, 190.08, 1605192894630916600)])
    await src.start(["AAPL"])
    assert cache.get("AAPL").daily_change_percent == pytest.approx(0.7997, abs=1e-3)
    await src.stop()


async def test_poll_failure_keeps_last_prices(monkeypatch):
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    src = MassiveDataSource("k", cache)
    src._tickers = ["AAPL"]
    def boom(**kw): raise RuntimeError("403")
    monkeypatch.setattr(src._client, "get_snapshot_all", boom)
    await src._poll_once()
    assert cache.get_price("AAPL") == 190.0


async def test_validate_unknown_ticker_false(monkeypatch):
    src = MassiveDataSource("k", PriceCache())
    def not_found(*a, **kw): raise Exception("NOT_FOUND")
    monkeypatch.setattr(src._client, "get_snapshot_ticker", not_found)
    assert await src.validate_ticker("ZZZZZ") is False


async def test_validate_known_ticker_caches_price(monkeypatch):
    cache = PriceCache()
    src = MassiveDataSource("k", cache)
    monkeypatch.setattr(src._client, "get_snapshot_ticker", lambda *a, **kw: snap("PYPL", 70.0, 69.0))
    assert await src.validate_ticker("PYPL")
    assert cache.get_price("PYPL") == 70.0
```

### 12.4 Contract test over both sources

```python
@pytest.fixture(params=["sim", "massive"])
async def source(request, monkeypatch):
    cache = PriceCache()
    if request.param == "sim":
        src = SimulatorDataSource(cache, update_interval=0.01)
    else:
        src = MassiveDataSource("k", cache, poll_interval=60)
        monkeypatch.setattr(src._client, "get_snapshot_all",
                            lambda tickers, **kw: [snap(t, 100.0, 99.0) for t in tickers])
        monkeypatch.setattr(src._client, "get_snapshot_ticker",
                            lambda mt, t, **kw: snap(t, 50.0, 49.0))
    yield src, cache
    await src.stop()


async def test_contract(source):
    src, cache = source
    await src.start(["AAPL", "MSFT"])
    assert {"AAPL", "MSFT"} <= set(cache.get_all())
    assert await src.validate_ticker("PYPL") and cache.get_price("PYPL")
    await src.add_ticker("PYPL")
    await src.add_ticker("PYPL")
    assert src.get_tickers().count("PYPL") == 1
    await src.remove_ticker("MSFT")
    assert "MSFT" not in cache and "MSFT" not in src.get_tickers()
    await src.stop()
    await src.stop()                                  # idempotent
```

### 12.5 SSE

Test `build_payload` directly (no server needed), plus one generator test with a fake request:

```python
def test_payload_shape_and_market_closed(monkeypatch):
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    sim = SimulatorDataSource(cache)
    live = MassiveDataSource("k", cache)
    monkeypatch.setattr(cache, "seconds_since_change", lambda: 120.0)
    assert json.loads(build_payload(cache, sim)[6:])["market_closed"] is False
    body = json.loads(build_payload(cache, live)[6:])
    assert body["market_closed"] is True
    assert body["prices"]["AAPL"]["daily_change_percent"] == 0.0


async def test_generator_sends_retry_then_data():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    class Req:
        calls = 0
        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1
    events = [e async for e in generate_events(cache, SimulatorDataSource(cache), Req(), 0.01)]
    assert events[0] == "retry: 1000\n\n"
    assert events[1].startswith("data: ")
```

### 12.6 Tracking rule (with the DB, in `tests/services/`)

- Remove a watchlist ticker that is held: it stays in `source.get_tickers()` and in the cache.
- Fully sell a ticker that is not on the watchlist: it leaves the cache.
- Reset: the tracked set equals the 10 defaults.

---

## 13. Edge cases

| Case | Behaviour |
|---|---|
| Empty tracked set at start | Simulator steps nothing; Massive skips the call; SSE sends `{"prices": {}, ...}` once |
| Trade on a ticker not cached | Service calls `validate_ticker` first; on Massive an unknown ticker gives 404 `unknown_ticker` |
| Massive bad key / 403 plan | One error log, then debug logs; cache keeps its last prices; app keeps running |
| Massive 429 | Client retries internally; if it still fails, it is a poll failure as above |
| Market closed (Massive) | Prices stop changing; after 60s `market_closed: true`; trades still fill at the last price |
| Ticker removed during an in-flight poll | Snapshot is ignored because the ticker is not in `_tickers` |
| Restart | Simulator restarts from seed prices; Massive refetches; positions are valued once prices arrive |
| `previous_price == 0` or `baseline == 0` | Percentages are 0.0, no division error |

---

## 14. Configuration

| Setting | Where | Default |
|---|---|---|
| `MASSIVE_API_KEY` | env | empty -> simulator |
| `update_interval` | `SimulatorDataSource` | 0.5s |
| `event_probability` | `SimulatorDataSource` / `GBMSimulator` | 0.001 per ticker per tick |
| `poll_interval` | `MassiveDataSource` | 15s (free tier); 2-5s on paid tiers |
| SSE interval | `create_stream_router(interval=)` | 0.5s |
| SSE retry | `generate_events` | 1000ms |
| Stale threshold | `STALE_AFTER_SECONDS` | 60s |

---

## 15. Implementation order

Each step is small and ends green (`uv run --extra dev pytest`, `uv run --extra dev ruff check app/ tests/`).

1. **Massive timestamp fix.** Add `parse_snapshot` and use it in `_poll_once`. Rewrite the Massive tests with `TickerSnapshot.from_dict`. Prove the bug first: a `from_dict` test fails on the current code.
2. **Baseline.** `PriceUpdate.baseline` and `daily_change_percent`, `cache.update(baseline=)`, simulator seeds, Massive `prev_day.close`. Update the model/cache tests.
3. **`validate_ticker`** on the ABC and both sources, plus tests.
4. **Cache fixes.** `timestamp is None`, `remove()` bumps version, `seconds_since_change()`.
5. **SSE.** Router inside the factory, `build_payload` with `market_closed`, `is_live_market`, plus tests.
6. **Contract test** parameterised over both sources.
7. **Tracking service.** Done with the watchlist/portfolio increments (PLAN §11a steps 2-3), not in the market package.
8. Update `MARKET_INTERFACE.md`, `MARKET_DATA_SUMMARY.md` and `backend/CLAUDE.md` to the new API (baseline, `validate_ticker`, payload shape, `create_stream_router(cache, source)`).
