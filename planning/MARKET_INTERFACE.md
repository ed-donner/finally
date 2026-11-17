# Unified Market Data Interface

How FinAlly gets stock prices from **one Python API**. That API is backed by the **Massive REST API** when `MASSIVE_API_KEY` is set and by the **built-in simulator** otherwise. Downstream code (SSE, portfolio, trades, chat) never knows which source is running.

- Massive API details: `MASSIVE_API.md`
- Simulator internals: `MARKET_SIMULATOR.md`
- Code: `backend/app/market/`. Most of this design is already implemented. §8 lists what still has to change.

---

## 1. Design in one picture

```
                 create_market_data_source(cache)        ← reads MASSIVE_API_KEY
                               │
              ┌────────────────┴────────────────┐
   MASSIVE_API_KEY empty               MASSIVE_API_KEY set
              ▼                                  ▼
   SimulatorDataSource                  MassiveDataSource
   (GBM, tick every 0.5 s)              (REST poll every 15 s; snapshot → EOD fallback)
              │   writes                         │   writes
              └──────────────┬───────────────────┘
                             ▼
                    PriceCache  (thread-safe, in-memory, versioned)
                             │   reads
     ┌───────────────┬───────┴────────┬──────────────────┬───────────────┐
 SSE /api/stream   GET /api/portfolio  POST trade (fill)  GET /api/watchlist  chat context
```

The design rests on three rules:
1. **Producers push and consumers pull.** A source writes into `PriceCache` on its own schedule. Nobody asks a source for a price. Everybody reads the cache.
2. **One interface, two strategies.** `MarketDataSource` is an ABC with five methods. The factory chooses the strategy once, at startup.
3. **The cache owns derived fields.** `previous_price`, `direction`, `change`, `session_open` and `session_change_percent` are computed in the same way no matter which source produced the price.

---

## 2. Public API (`from app.market import ...`)

| Name | Kind | Purpose |
|---|---|---|
| `PriceUpdate` | frozen dataclass | One ticker's latest price plus derived fields |
| `PriceCache` | class | Thread-safe latest-price store. The only thing consumers read |
| `MarketDataSource` | ABC | Contract implemented by both sources |
| `create_market_data_source(cache)` | function | Factory that picks the implementation from the environment |
| `create_stream_router(cache)` | function | FastAPI router for `GET /api/stream/prices` |

### 2.1 `PriceUpdate`

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float              # rounded to 2 dp
    previous_price: float     # price at the previous cache write (== price on first write)
    timestamp: float          # Unix seconds (float)
    session_open: float       # first price the cache recorded for this ticker since server start

    @property
    def change(self) -> float: ...               # price - previous_price, 4 dp
    @property
    def change_percent(self) -> float: ...       # vs previous_price, 4 dp
    @property
    def direction(self) -> str: ...              # "up" | "down" | "flat"
    @property
    def session_change_percent(self) -> float: ...  # (price - session_open) / session_open * 100, 4 dp

    def to_dict(self) -> dict:
        return {
            "ticker": ..., "price": ..., "previous_price": ..., "timestamp": ...,
            "change": ..., "change_percent": ..., "direction": ...,
            "session_open": ..., "session_change_percent": ...,
        }
```

`to_dict()` is exactly the per-ticker object in the SSE payload (PLAN §6). It is also the source of the `price`, `previous_price` and `session_change_percent` fields in `GET /api/watchlist`.

### 2.2 `PriceCache`

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]          # shallow copy
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int                             # bumps on every update; SSE change detection
    def __contains__(self, ticker: str) -> bool
    def __len__(self) -> int
```

The semantics:
- `update()` takes the lock, reads the previous entry, and builds a new `PriceUpdate`. On the **first write** for a ticker it sets `session_open = price`. After that it copies `session_open` forward from the previous entry.
- `remove()` drops the ticker completely, `session_open` included. If the ticker is added again later, it gets a fresh session open. That is acceptable and simple.
- It uses a `threading.Lock`, not an `asyncio.Lock`, so it is safe whether writers run on the event loop (the simulator) or in a worker thread.

### 2.3 `MarketDataSource` (the contract)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None     # call once; seed cache if possible; spawn background task
    async def stop(self) -> None                          # idempotent; cancel task; never write again
    async def add_ticker(self, ticker: str) -> None       # idempotent; tracked from next cycle
    async def remove_ticker(self, ticker: str) -> None    # idempotent; also cache.remove(ticker)
    def get_tickers(self) -> list[str]                    # current tracked set
```

Behaviour both implementations **must** follow:

| Behaviour | Simulator | Massive |
|---|---|---|
| `start()` populates the cache before returning | ✅ seed prices written immediately | ✅ one synchronous poll before the loop starts (best effort) |
| `add_ticker()` makes a price available | **immediately** | on the **next poll** (up to `poll_interval` later) |
| A failure inside the loop | logged; loop continues | logged; loop continues; last prices stay in the cache |
| Ticker normalisation | caller passes uppercase | `.upper().strip()` defensively |
| Writes happen on | event loop | event loop (the HTTP call runs in `asyncio.to_thread`, then cache writes happen) |

Callers must treat "no price yet" as a normal state. That is why PLAN §9 says trades return `400 "No price available for XYZ yet"`.

### 2.4 Factory

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    return SimulatorDataSource(price_cache=price_cache)
```
A whitespace-only key counts as unset. The factory returns an **unstarted** source.

---

## 3. Massive implementation

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0): ...
```

**The poll cycle** does one HTTP request per cycle, whatever the number of tickers:

```python
async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return
    try:
        if self._mode == "snapshot":
            quotes = await asyncio.to_thread(self._fetch_snapshot_prices)   # list[(ticker, price, ts)]
        else:
            quotes = await asyncio.to_thread(self._fetch_eod_prices)
    except BadResponse as e:
        if self._mode == "snapshot" and _is_not_authorized(e):
            logger.warning("Massive plan has no snapshot access; switching to end-of-day prices")
            self._mode = "eod"
            return await self._poll_once()
        logger.error("Massive poll failed: %s", e); return
    except Exception as e:
        logger.error("Massive poll failed: %s", e); return
    for ticker, price, ts in quotes:
        self._cache.update(ticker, price, ts)
```

**Snapshot mode** (paid plans, real-time or 15-minute delayed):

```python
def _fetch_snapshot_prices(self) -> list[tuple[str, float, float]]:
    out = []
    for s in self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=self._tickers):
        lt = s.last_trade
        price = (lt.price if lt and lt.price else None) \
             or (s.min.close if s.min and s.min.close else None) \
             or (s.day.close if s.day and s.day.close else None) \
             or (s.prev_day.close if s.prev_day else None)
        if not price:
            continue
        ns = (lt.sip_timestamp if lt and lt.sip_timestamp else None) or s.updated
        out.append((s.ticker, float(price), ns / 1e9 if ns else time.time()))
    return out
```

**EOD mode** (free Basic plan, which cannot call snapshots). One grouped-daily call returns every ticker. Walk back over weekends and holidays (see `MASSIVE_API.md` §2.5):

```python
def _fetch_eod_prices(self) -> list[tuple[str, float, float]]:
    wanted = set(self._tickers)
    d = date.today() - timedelta(days=1)
    for _ in range(5):
        bars = self._client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        if bars:
            return [(b.ticker, float(b.close), b.timestamp / 1e3) for b in bars if b.ticker in wanted and b.close]
        d -= timedelta(days=1)
    return []
```
In EOD mode, prices change at most once a day. The cache's `version` only bumps when `update()` is called, and every poll does call `update()`, so the SSE stream keeps emitting and every ticker shows `direction: "flat"`. That is honest. Raise the interval in EOD mode, for example to 300 s, because nothing changes more often.

**Rate-limit budget:** free tier = 5 req/min, so `poll_interval ≥ 12 s`. The default is 15 s. Paid tiers can pass 2–5 s. The SDK already retries 429s three times internally, so never add a retry loop of our own on top of that.

---

## 4. Simulator implementation (summary)

`SimulatorDataSource(price_cache, update_interval=0.5, event_probability=0.001)` wraps a pure `GBMSimulator`:
- `start()` builds the simulator, writes every seed price into the cache, and spawns `_run_loop`.
- `_run_loop` runs `sim.step()`, writes each price to the cache, then waits with `asyncio.sleep(0.5)`.
- `add_ticker()` adds the ticker to the simulator (and rebuilds the correlation matrix) and **writes its starting price at once**, so a ticker added by the user or the AI can be traded immediately.

The full design is in `MARKET_SIMULATOR.md`.

---

## 5. Tracked tickers (watchlist ∪ positions)

The source tracks **every ticker that is on the watchlist or held in a position**. The source does not know about the database, so the app layer owns this rule. Put it in one helper (for example `app/services/tracking.py`) so routes and the chat flow all go through it:

```python
async def ensure_tracked(source: MarketDataSource, ticker: str) -> None:
    await source.add_ticker(ticker)                       # idempotent

async def maybe_untrack(db, source: MarketDataSource, ticker: str, user_id: str = "default") -> None:
    """Call after a watchlist removal or after a sell that closed a position."""
    if not (db.is_on_watchlist(user_id, ticker) or db.has_position(user_id, ticker)):
        await source.remove_ticker(ticker)                # also clears it from the cache
```

| Event | Call |
|---|---|
| Startup | `await source.start(sorted(watchlist ∪ position_tickers))` |
| `POST /api/watchlist` / chat "add" | insert row → `ensure_tracked` |
| Trade in a ticker not on the watchlist | add to watchlist → `ensure_tracked` → then attempt the trade (PLAN §9) |
| `DELETE /api/watchlist/{t}` / chat "remove" | delete row → `maybe_untrack` |
| Sell that deletes the position row | commit → `maybe_untrack` |

---

## 6. Wiring into FastAPI

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    db = init_database()                                   # PLAN §7: create tables + seed
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(db.tracked_tickers("default"))      # watchlist ∪ positions
    app.state.price_cache, app.state.market_source = cache, source
    snapshot_task = start_snapshot_task(db, cache)         # every 30 s (PLAN §7)
    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()

app = FastAPI(lifespan=lifespan)
```

Routers get the cache and the source through `request.app.state` (or a tiny `Depends` helper). `create_stream_router(cache)` needs the cache when the router is built, so either build the cache at module import time and pass it to both, or register the stream route inside the lifespan before `yield`. The first is simpler: create `cache = PriceCache()` at module level in `main.py`.

### Consumer examples

```python
# Trade fill price (PLAN §9)
price = cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"No price available for {ticker} yet")

# Portfolio valuation (fallback to avg_cost when not cached — PLAN §8)
current = cache.get_price(p.ticker) or p.avg_cost

# Watchlist row
u = cache.get(t)
row = {"ticker": t, "price": u.price if u else None,
       "previous_price": u.previous_price if u else None,
       "session_change_percent": u.session_change_percent if u else None,
       "added_at": added_at}
```

---

## 7. Testing the interface

- **Contract tests**, parametrised over both sources: run `start`, `add_ticker`, `remove_ticker` and `get_tickers`, call `stop` twice, and check that `remove_ticker` clears the cache.
- **Cache:** first write gives `direction == "flat"` and `session_open == price`. Later writes keep `session_open`. `remove` followed by a new write resets it.
- **Massive parsing:** build real SDK models with `TickerSnapshot.from_dict({...raw JSON...})` instead of `MagicMock`. A MagicMock will happily return a value for `last_trade.timestamp`, an attribute that does not exist on the real model, and that is how the bug in §8 got through the tests.
- **Massive fallback:** make the mock client raise `BadResponse('{"status":"NOT_AUTHORIZED",...}')` from `get_snapshot_all` and check that the grouped-daily path is used.
- Unit tests never make network calls.

---

## 8. Gap list: current code vs this design

| # | Item | File | Status |
|---|---|---|---|
| 1 | **Bug:** `snap.last_trade.timestamp` does not exist on the SDK's `LastTrade`. The field is `sip_timestamp` (ns). Every snapshot hits `AttributeError` and is skipped, so **Massive mode never populates the cache**. Tests mock it with `MagicMock`, which hides the bug | `massive_client.py:103`, `tests/market/test_massive.py:17` | **Fix** |
| 2 | Add `session_open` to `PriceUpdate`/`PriceCache`, plus `session_change_percent` in `to_dict()` | `models.py`, `cache.py` | To do (PLAN §6) |
| 3 | Price fallback chain (`lastTrade.p → min.c → day.c → prevDay.c`) | `massive_client.py` | To do |
| 4 | Free-tier EOD fallback on `NOT_AUTHORIZED` | `massive_client.py` | To do |
| 5 | `create_stream_router` attaches the route to a **module-level** `router`, so calling it twice registers `/prices` twice. Create the `APIRouter` inside the function | `stream.py:17` | Nice to have |
| 6 | Tracked-ticker helper (§5) | new, app layer | To do with the portfolio/watchlist routes |
