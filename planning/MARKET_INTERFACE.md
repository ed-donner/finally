# Market Data Interface

The unified Python API for retrieving stock prices in FinAlly. One interface,
two implementations: the **Massive API** when `MASSIVE_API_KEY` is set, the
**GBM simulator** otherwise.

Companion documents: `MASSIVE_API.md` (the real API), `MARKET_SIMULATOR.md`
(the simulation model).

---

## 1. Design Goal

Every consumer of price data in FinAlly — the SSE stream, portfolio valuation,
trade execution, the LLM's context builder — must be **completely unaware of
where prices come from**. Swapping a simulated feed for a live one is an
environment-variable change, not a code change.

This is achieved with two ideas:

1. **A producer interface** (`MarketDataSource`) that both backends implement.
2. **A shared cache** (`PriceCache`) that decouples producers from consumers.

```
        ┌──────────────────────────────────────────┐
        │            MarketDataSource (ABC)        │
        │  start / stop / add_ticker /             │
        │  remove_ticker / get_tickers             │
        └──────────────────────────────────────────┘
                  ▲                      ▲
                  │                      │
      ┌───────────┴────────┐   ┌─────────┴──────────┐
      │ SimulatorDataSource│   │ MassiveDataSource  │
      │ GBM, 500ms ticks   │   │ REST poll, 15s     │
      └───────────┬────────┘   └─────────┬──────────┘
                  │                      │
                  └──────── writes ──────┘
                              │
                              ▼
                  ┌───────────────────────┐
                  │      PriceCache       │
                  │  thread-safe, in-mem  │
                  │  + version counter    │
                  └───────────┬───────────┘
                              │ reads
             ┌────────────────┼────────────────┐
             ▼                ▼                ▼
      SSE /api/stream    Portfolio        Trade execution
        /prices          valuation        & LLM context
```

**The cache is the only contract consumers depend on.** No consumer ever holds
a reference to a `MarketDataSource`.

---

## 2. Module Layout

Located at `backend/app/market/`:

| Module | Responsibility |
|---|---|
| `models.py` | `PriceUpdate` — the immutable unit of price data |
| `interface.py` | `MarketDataSource` — the abstract producer contract |
| `cache.py` | `PriceCache` — thread-safe store, versioned |
| `factory.py` | `create_market_data_source()` — environment-driven selection |
| `simulator.py` | `GBMSimulator` + `SimulatorDataSource` |
| `massive_client.py` | `MassiveDataSource` — REST poller |
| `seed_prices.py` | Seed prices, GBM params, correlation groups |
| `stream.py` | `create_stream_router()` — SSE endpoint factory |

Public surface, re-exported from `app.market`:

```python
from app.market import (
    PriceUpdate,
    PriceCache,
    MarketDataSource,
    create_market_data_source,
    create_stream_router,
)
```

---

## 3. `PriceUpdate` — The Data Unit

An immutable, frozen, slotted dataclass. Immutability matters: instances are
handed to SSE serializers and portfolio math concurrently, and nothing should
be able to mutate a price after the fact.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float: ...          # price - previous_price
    @property
    def change_percent(self) -> float: ...  # % change, 0.0 if previous == 0
    @property
    def direction(self) -> str: ...         # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...          # JSON/SSE serialization
```

Design decisions:

- **Derived values are properties, not fields.** `change`, `change_percent`,
  and `direction` cannot drift out of sync with the prices they derive from.
- **`previous_price` is the previous *tick*, not the previous *close*.** This
  drives the frontend's green/red flash animation. Day-over-day change is a
  separate concern computed by the portfolio layer.
- **`timestamp` is Unix seconds (float)**, normalized regardless of source.
  Massive's nanosecond timestamps are converted at the boundary.
- **Division-by-zero guard** on `change_percent` returns `0.0` rather than
  raising — a price feed glitch must not propagate an exception into the SSE
  generator.

`to_dict()` output — this is the wire format the frontend consumes:

```json
{
  "ticker": "AAPL",
  "price": 190.42,
  "previous_price": 190.35,
  "timestamp": 1785432000.123,
  "change": 0.07,
  "change_percent": 0.0368,
  "direction": "up"
}
```

---

## 4. `MarketDataSource` — The Producer Contract

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

### Contract semantics

| Method | Guarantee |
|---|---|
| `start(tickers)` | Begins a background task writing to the cache. Called exactly once. Populates the cache with initial prices **before returning**, so the first SSE client never sees an empty payload. |
| `stop()` | Cancels the background task and releases resources. Idempotent — safe to call multiple times, including when never started. After `stop()`, the source never writes to the cache again. |
| `add_ticker(t)` | Idempotent. Ticker appears in the next update cycle (or immediately, for the simulator). |
| `remove_ticker(t)` | Idempotent. **Also removes the ticker from the cache**, so a de-watchlisted symbol stops appearing in the SSE payload. |
| `get_tickers()` | Synchronous (no I/O). Returns a copy — callers cannot mutate internal state. |

### Why this shape

- **Async lifecycle, sync accessor.** `start`/`stop`/`add`/`remove` are async
  because the Massive implementation performs network I/O and both manage
  asyncio tasks. `get_tickers()` is a pure in-memory read, so forcing `await`
  on it would be noise.
- **No `get_price()` on the interface.** Deliberately absent. If sources
  exposed price reads, consumers would couple to the source and the cache
  would become an implementation detail rather than the contract. Prices are
  read from `PriceCache`, always.
- **Push, not pull.** Sources write to the cache on their own schedule.
  Consumers never trigger a fetch, so a slow API can never block a request.

---

## 5. `PriceCache` — The Consumer Contract

```python
class PriceCache:
    def update(self, ticker: str, price: float,
               timestamp: float | None = None) -> PriceUpdate: ...
    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_price(self, ticker: str) -> float | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...
    def remove(self, ticker: str) -> None: ...

    @property
    def version(self) -> int: ...

    def __len__(self) -> int: ...
    def __contains__(self, ticker: str) -> bool: ...
```

### Thread safety

Guarded by a `threading.Lock`, not an `asyncio.Lock`. This is deliberate: the
Massive client is synchronous and runs inside `asyncio.to_thread(...)`, so
writes genuinely originate from a worker thread while reads happen on the event
loop. An asyncio lock would not protect that boundary.

Every method holds the lock for the shortest possible span. `get_all()` returns
a **shallow copy** of the dict — safe to iterate outside the lock because
`PriceUpdate` is frozen.

### The version counter

`version` is a monotonic integer incremented on every `update()`. It exists so
the SSE generator can answer "has anything changed since I last sent?" in O(1)
without diffing dicts or comparing timestamps.

```python
last_version = -1
while True:
    if price_cache.version != last_version:
        last_version = price_cache.version
        yield f"data: {json.dumps(...)}\n\n"
    await asyncio.sleep(0.5)
```

This keeps idle connections silent — when the simulator is stopped or the
Massive poll fails, no redundant frames are pushed.

### First-update semantics

On the first `update()` for a ticker, `previous_price` is set equal to `price`,
so `direction` is `"flat"` and `change` is `0.0`. The frontend therefore never
flashes a spurious green/red on page load.

Prices are rounded to 2 decimal places on write. Rounding at the cache boundary
means every consumer sees identical values — no display/execution mismatch
where the UI shows $190.42 but a trade fills at $190.41999999.

---

## 6. `create_market_data_source()` — Selection

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

Selection rules:

| `MASSIVE_API_KEY` | Result |
|---|---|
| Unset | Simulator |
| Empty string | Simulator |
| Whitespace only | Simulator (`.strip()` handles this) |
| Non-empty | Massive |

The `.strip()` matters in practice: a `.env` file containing `MASSIVE_API_KEY=`
followed by a trailing space would otherwise select the real API with a garbage
key, and the app would silently show no prices.

The factory returns an **unstarted** source — the caller owns the lifecycle.
This keeps the factory synchronous and makes it trivial to unit-test selection
logic without spawning background tasks.

---

## 7. Application Wiring

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    cache = PriceCache()
    source = create_market_data_source(cache)

    tickers = load_watchlist_tickers()          # from SQLite
    await source.start(tickers)

    app.state.price_cache = cache
    app.state.market_source = source
    try:
        yield
    finally:
        await source.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(app.state.price_cache))
```

Both the cache and the source live on `app.state` rather than in module-level
globals, which keeps tests isolated — each test app gets its own cache.

### Keeping the source in sync with the watchlist

Watchlist mutations must update the database *and* the data source:

```python
@router.post("/api/watchlist")
async def add_to_watchlist(body: TickerBody, request: Request):
    ticker = body.ticker.upper().strip()
    db_add_ticker(ticker)
    await request.app.state.market_source.add_ticker(ticker)
    return {"ok": True}


@router.delete("/api/watchlist/{ticker}")
async def remove_from_watchlist(ticker: str, request: Request):
    ticker = ticker.upper().strip()
    db_remove_ticker(ticker)
    await request.app.state.market_source.remove_ticker(ticker)
    return {"ok": True}
```

Ticker normalization (`.upper().strip()`) happens at the API boundary *and*
defensively inside both sources, since the cache is keyed by exact string.

---

## 8. Reading Prices — Consumer Patterns

```python
cache: PriceCache = request.app.state.price_cache

# Single price for trade execution
price = cache.get_price("AAPL")
if price is None:
    raise HTTPException(400, "No price available for AAPL")

# Full update with direction, for display
update = cache.get("AAPL")

# Portfolio valuation across all positions
prices = cache.get_all()
total = cash + sum(
    pos.quantity * prices[pos.ticker].price
    for pos in positions
    if pos.ticker in prices
)
```

**Always handle `None`.** A ticker can be absent from the cache when it was
just added and the first poll has not landed, or when a Massive poll failed
before any successful fetch. Trade execution must reject rather than assume a
price.

---

## 9. SSE Streaming Layer

`create_stream_router(cache)` returns a FastAPI `APIRouter` exposing
`GET /api/stream/prices`.

```
retry: 1000

data: {"AAPL": {"ticker":"AAPL","price":190.42,...}, "GOOGL": {...}}

data: {...}
```

Behaviour:

- Emits a `retry: 1000` directive first, so `EventSource` reconnects after 1s.
- Sends **all** tracked tickers in each frame, keyed by ticker. Simpler for the
  client than per-ticker events, and at ~10 tickers the payload is trivial.
- Polls the cache every 500ms and only emits when `version` changed.
- Detects disconnect via `await request.is_disconnected()` and exits the loop.
- Sets `X-Accel-Buffering: no` and `Cache-Control: no-cache` so the stream is
  not buffered by an intermediate proxy.

The 500ms stream cadence is independent of the source cadence. With the
simulator (500ms ticks) the client sees a frame per tick. With Massive (15s
polls) the version is unchanged between polls, so frames are emitted only when
new data actually arrives — no wasted bandwidth.

---

## 10. Implementation Comparison

| Aspect | `SimulatorDataSource` | `MassiveDataSource` |
|---|---|---|
| Update cadence | 500ms | 15s (configurable) |
| Mechanism | In-process GBM step | REST poll, batched |
| Blocking I/O | None | Yes — wrapped in `asyncio.to_thread` |
| New ticker latency | Immediate (seeded on add) | Next poll cycle |
| Failure mode | Logs and continues | Logs and continues; cache goes stale |
| External dependency | None | Network + API key + paid plan |
| Cost | Free | Snapshot endpoints need Starter+ |

Both share the same failure philosophy: **the loop never dies**. Exceptions are
caught, logged, and the next cycle proceeds. A market data problem degrades to
stale prices, never to a 500 on the SSE endpoint.

---

## 11. Testing Strategy

The interface makes each layer independently testable:

- **`PriceCache`** — pure unit tests: update/get/remove, version increments,
  first-update flat semantics, rounding, concurrent writes from threads.
- **`PriceUpdate`** — property math, zero-division guard, `to_dict()` shape.
- **Factory** — `monkeypatch.setenv` across unset/empty/whitespace/valid,
  asserting the returned type. No network, no tasks.
- **`SimulatorDataSource`** — start, let a few ticks elapse, assert the cache
  populated and prices moved; assert `stop()` halts writes.
- **`MassiveDataSource`** — mock `RESTClient.get_snapshot_all` with realistic
  `TickerSnapshot` objects built via `TickerSnapshot.from_dict(raw_json)`.
  Building from raw JSON rather than hand-constructing the dataclass is what
  catches field-name errors like `sip_timestamp` vs `timestamp`.
- **Consumers** — inject a `PriceCache` pre-populated via `update()`. No data
  source needed at all to test portfolio math or trade execution.

A conformance test parametrized over both implementations asserts they satisfy
the contract identically (idempotent `stop()`, `remove_ticker` clears the
cache, `get_tickers()` reflects mutations).

---

## 12. Extending to a New Provider

To add, say, an Alpaca or Finnhub feed:

1. Implement `MarketDataSource` in a new module.
2. Convert that provider's price and timestamp into Unix-seconds floats and
   call `cache.update(...)`.
3. Add a branch to `create_market_data_source()`.
4. Add it to the conformance test parametrization.

No consumer changes. That property is the entire point of the design.
