# Market Data Interface — Unified Python API

How FinAlly gets stock prices: one interface, two implementations, picked by `MASSIVE_API_KEY`. Downstream code (SSE, portfolio valuation, trade execution, LLM context) never knows which source is running.

This describes the design implemented in `backend/app/market/`, plus the changes the Massive research (`MASSIVE_API.md`) shows are needed. Items marked **[CHANGE]** are not in the code yet.

Related: `MASSIVE_API.md` (provider reference), `MARKET_SIMULATOR.md` (simulator design).

---

## 1. Shape of the design

```
                 create_market_data_source(cache)
                   │  reads MASSIVE_API_KEY
         ┌─────────┴──────────┐
         ▼                    ▼
 SimulatorDataSource    MassiveDataSource
 (GBM, 500 ms ticks)    (REST poll: snapshot or EOD mode)
         │                    │
         └──── write ────┬────┘
                         ▼
                    PriceCache  ◄── single source of truth, thread-safe
                         │ read
     ┌───────────────┬───┴───────────┬─────────────────┐
     ▼               ▼               ▼                 ▼
 SSE /api/stream  GET /api/portfolio  POST trade     LLM chat context
```

Principles:

1. **Push, don't pull.** Sources write to the cache on their own schedule. Consumers read only from the cache — nobody calls a data source for a price.
2. **Strategy pattern.** Both sources implement the `MarketDataSource` ABC. Swapping sources changes nothing downstream.
3. **Async lifecycle, sync reads.** `start/stop/add/remove` are `async` (they manage background tasks); cache reads are cheap synchronous calls that can be used anywhere, including in sync code and threads.
4. **The source owns the ticker set; the database owns the watchlist.** Routes that change the watchlist update the DB *and* call `source.add_ticker/remove_ticker`.

---

## 2. Public API

```python
from app.market import (
    PriceUpdate,               # immutable price snapshot
    PriceCache,                # thread-safe latest-price store
    MarketDataSource,          # ABC
    create_market_data_source, # factory
    create_stream_router,      # FastAPI SSE router factory
)
```

### 2.1 `PriceUpdate` (models.py)

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float              # rounded to 2 dp
    previous_price: float     # price from the previous update (== price on first update)
    timestamp: float          # Unix seconds

    change: float             # property: price - previous_price
    change_percent: float     # property
    direction: str            # property: "up" | "down" | "flat"
    def to_dict(self) -> dict # JSON/SSE payload
```

**[CHANGE] Add a daily reference price.** PLAN §10 wants "daily change %" in the watchlist, but `change_percent` is tick-to-tick (meaningless over a 500 ms tick). Add:

```python
    reference_price: float | None = None   # previous close (Massive) / session start price (simulator)

    @property
    def day_change_percent(self) -> float | None:
        if not self.reference_price:
            return None
        return round((self.price - self.reference_price) / self.reference_price * 100, 2)
```

and include `reference_price` and `day_change_percent` in `to_dict()`. Sources supply it (see §2.2); the frontend uses `day_change_percent` for the watchlist column and `direction` for flashes.

### 2.2 `PriceCache` (cache.py)

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]        # shallow copy
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int                            # bumped on every update
    def __len__(self) -> int
    def __contains__(self, ticker: str) -> bool
```

- Guarded by a `threading.Lock`, so it's safe from both the event loop and `asyncio.to_thread` workers.
- `previous_price` and `direction` are computed **inside** the cache from the prior entry, so sources only ever report "ticker X is now $Y".
- `version` lets the SSE generator skip sending when nothing changed (important in Massive EOD mode where prices change once a day).

**[CHANGE]** Extend `update(..., reference_price: float | None = None)`. When omitted, carry forward the previous entry's `reference_price`. Sources set it:
- Simulator: seed price at `start()` / `add_ticker()` ("session open").
- Massive snapshot mode: `prev_day.close`.
- Massive EOD mode: the close of the trading day *before* the one being shown (or `None` if not fetched; frontend shows "—").

### 2.3 `MarketDataSource` (interface.py)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None   # call once; seeds cache, launches background task
    async def stop(self) -> None                        # idempotent; cancels task
    async def add_ticker(self, ticker: str) -> None     # no-op if present
    async def remove_ticker(self, ticker: str) -> None  # no-op if absent; also evicts from cache
    def get_tickers(self) -> list[str]
```

Contract every implementation must satisfy (and the shared conformance tests check):

| Rule | Why |
|---|---|
| After `start()` returns, the cache holds a price for every valid starting ticker (simulator) or has attempted one fetch (Massive) | First SSE event / first `/api/portfolio` call has data |
| Tickers are normalised to `ticker.strip().upper()` | DB, cache and source agree on keys |
| `add_ticker` makes a price available by the next update cycle (simulator: immediately) | Watchlist add shows a price quickly |
| `remove_ticker` evicts from the cache | SSE stops sending it |
| Background failures are logged, never raised out of the task | One bad poll must not kill the stream |
| `stop()` is safe to call twice and before `start()` | Simple shutdown in lifespan |
| No method blocks the event loop | Blocking SDK calls go through `asyncio.to_thread` |

**[CHANGE]** Normalise case in `SimulatorDataSource.add_ticker/remove_ticker` too (only the Massive source does it today).

**[CHANGE] Tickers held as positions.** A position whose ticker was removed from the watchlist still needs a price for valuation. The watchlist DELETE route should only call `source.remove_ticker` if the user holds no position in it; the trade route should call `source.add_ticker` for any ticker being bought. In other words the source tracks `watchlist ∪ held tickers`.

### 2.4 `create_market_data_source` (factory.py)

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache,
                                 poll_interval=float(os.environ.get("MASSIVE_POLL_INTERVAL", 15)))  # [CHANGE] env override
    return SimulatorDataSource(price_cache=price_cache)
```

Returns an **unstarted** source. Empty or whitespace-only key → simulator.

### 2.5 `create_stream_router` (stream.py)

`GET /api/stream/prices` → `text/event-stream`. Sends `retry: 1000` first, then every 500 ms, *if* `cache.version` changed, one event with all prices:

```
data: {"AAPL": {"ticker":"AAPL","price":190.52,"previous_price":190.48,"timestamp":1762876800.5,
                "change":0.04,"change_percent":0.021,"direction":"up"}, "MSFT": {...}}
```

**[CHANGE]** The router is created via a module-level `router` object; calling `create_stream_router` twice (e.g. in tests) registers the route twice. Create the `APIRouter` inside the factory.

---

## 3. `MassiveDataSource` — two modes

Research finding: the snapshot endpoint the current code uses is **not available on the free Basic plan** (5 req/min, end-of-day only). A free key currently produces a 403 on every poll and an empty watchlist. The source must pick a mode.

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache,
                 poll_interval: float = 15.0,       # snapshot mode
                 eod_poll_interval: float = 300.0): # EOD mode
        ...
        self._mode: Literal["snapshot", "eod"] | None = None

    async def start(self, tickers):
        self._client = RESTClient(api_key=self._api_key, retries=0)   # [CHANGE] no 429 retry storm
        self._tickers = [t.strip().upper() for t in tickers]
        self._mode = await asyncio.to_thread(self._detect_mode)        # [CHANGE]
        await self._poll_once()
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
```

### 3.1 Mode detection [CHANGE]

At `start()`, make one snapshot call for the first ticker. `BadResponse` whose text contains `NOT_AUTHORIZED` / "not entitled" → `"eod"`; success → `"snapshot"`. Log the chosen mode at INFO so the user sees why prices aren't moving. An invalid key (`401`/"Unknown API Key") → log an ERROR; keep polling at the EOD interval (don't silently fall back to the simulator — the user asked for real data).

### 3.2 Snapshot mode (paid)

```python
def _fetch_snapshot(self) -> list[tuple[str, float, float, float | None]]:
    out = []
    for s in self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=self._tickers):
        price, ts = _snapshot_price(s)
        if price is None:
            continue
        ref = s.prev_day.close if s.prev_day and s.prev_day.close else None
        out.append((s.ticker, price, ts, ref))
    return out

def _snapshot_price(s) -> tuple[float | None, float]:
    lt = s.last_trade
    if lt and lt.price:                      return lt.price, lt.sip_timestamp / 1e9   # ns
    if s.min and s.min.close:                return s.min.close, s.min.timestamp / 1e3 # ms
    if s.day and s.day.close:                return s.day.close, (s.updated or 0) / 1e9
    if s.prev_day and s.prev_day.close:      return s.prev_day.close, time.time()
    return None, 0.0
```

> **[CHANGE] Bug in current code:** `massive_client.py` reads `snap.last_trade.timestamp / 1000.0`. The SDK's `LastTrade` has no `timestamp` attribute (it's `sip_timestamp`, nanoseconds), so every snapshot raises `AttributeError`, is logged as "Skipping snapshot", and **no prices ever reach the cache**. The unit tests mock `last_trade.timestamp`, which is why they pass. Fix the code and the mocks together; use `_snapshot_price` above (it also handles plans without trade data and the overnight reset).

### 3.3 EOD mode (free)

```python
def _fetch_eod(self) -> list[tuple[str, float, float, float | None]]:
    d = self._last_good_date or (date.today() - timedelta(days=1))
    for _ in range(4):                                     # weekends / holidays
        aggs = self._client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        if aggs:
            self._last_good_date = d
            wanted = set(self._tickers)
            return [(a.ticker, a.close, a.timestamp / 1e3, None)
                    for a in aggs if a.ticker in wanted and a.close]
        d -= timedelta(days=1)
    return []
```

- One request per poll for any number of tickers (whole-market response, filtered locally).
- Poll every 300 s; re-check for a newer trading day after ~5 PM ET.
- `reference_price` can come from a second grouped-daily call for the previous day, done once per new trading day (2 calls/day — well within quota).

### 3.4 Back-off [CHANGE]

On a 429 (`"exceeded the maximum requests"` in the `BadResponse` text): double the current interval (cap 60 s in snapshot mode), reset to the configured interval after a successful poll.

### 3.5 Unknown tickers

In snapshot mode, a requested ticker missing from the response is probably invalid. Log a WARNING once per ticker. The watchlist API should not wait for this — PLAN says adding is immediate; the ticker just shows no price until data arrives.

---

## 4. `SimulatorDataSource`

Summary only; see `MARKET_SIMULATOR.md`.

- Wraps `GBMSimulator`; a background task calls `step()` every 0.5 s and writes all prices to the cache.
- `start()` and `add_ticker()` seed the cache immediately so new tickers have a price at once.
- `reference_price` = the ticker's starting price, so "day change %" = change since app start.

---

## 5. Wiring into FastAPI

```python
# backend/app/main.py
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router
from app.db import get_watchlist_tickers, get_position_tickers   # backend agent provides these

@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)
    tickers = sorted(set(get_watchlist_tickers()) | set(get_position_tickers()))
    await source.start(tickers)
    app.state.price_cache = cache
    app.state.market = source
    app.include_router(create_stream_router(cache))
    try:
        yield
    finally:
        await source.stop()

app = FastAPI(lifespan=lifespan)
```

Using it from routes:

```python
from fastapi import Request, HTTPException

def current_price(request: Request, ticker: str) -> float:
    price = request.app.state.price_cache.get_price(ticker.upper())
    if price is None:
        raise HTTPException(400, f"No price available for {ticker}")
    return price

@router.post("/api/watchlist")
async def add_to_watchlist(body: AddTicker, request: Request):
    ticker = body.ticker.strip().upper()
    db.add_watchlist(ticker)
    await request.app.state.market.add_ticker(ticker)
    return {"ticker": ticker, "price": request.app.state.price_cache.get_price(ticker)}
```

Trades always execute at `cache.get_price(ticker)`. If that's `None` (ticker never priced), reject with 400.

---

## 6. Configuration

| Variable | Default | Effect |
|---|---|---|
| `MASSIVE_API_KEY` | empty | Non-empty → `MassiveDataSource`; else simulator |
| `MASSIVE_POLL_INTERVAL` **[CHANGE]** | `15` | Seconds between snapshot polls (paid tiers can use `2`–`5`) |

The free-vs-paid mode is detected automatically; no extra variable.

---

## 7. Testing

- **Conformance** (parametrize over both sources, Massive with a fake client): start seeds/attempts, add/remove are idempotent, remove evicts from cache, `stop()` twice is safe, case normalisation.
- **Massive parsing:** build `TickerSnapshot.from_dict(...)` from the real JSON samples in `MASSIVE_API.md` instead of hand-built `MagicMock`s, so attribute names are checked against the real SDK (this would have caught the `sip_timestamp` bug). Cover: missing `lastTrade`, overnight-reset zeros, ns vs ms timestamps.
- **Mode detection:** fake client raising `BadResponse('{"status":"NOT_AUTHORIZED",...}')` → EOD mode; weekend → walks back to Friday.
- **Factory:** empty / whitespace / set key.
- **SSE:** version unchanged → no event sent.

---

## 8. Change checklist

| # | Change | File |
|---|---|---|
| 1 | Fix `last_trade.timestamp` → `sip_timestamp / 1e9`, add price fallback chain | `massive_client.py`, `tests/market/test_massive.py` |
| 2 | Free-plan EOD mode with auto-detection | `massive_client.py` |
| 3 | `RESTClient(retries=0)` + 429 back-off | `massive_client.py` |
| 4 | `reference_price` / `day_change_percent` | `models.py`, `cache.py`, both sources |
| 5 | Upper-case tickers in simulator source | `simulator.py` |
| 6 | Router created inside factory | `stream.py` |
| 7 | `MASSIVE_POLL_INTERVAL` env var | `factory.py` |
| 8 | Track watchlist ∪ held tickers | backend routes (watchlist/trade) |
