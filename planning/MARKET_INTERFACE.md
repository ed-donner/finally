# Unified Market Data Interface

The design of `backend/app/market/` — the one Python API the rest of FinAlly uses to get
stock prices, whether they come from the Massive API or the built-in simulator.

Companion documents: `MASSIVE_API.md` (the real data provider), `MARKET_SIMULATOR.md` (the
default provider).

## The Problem

FinAlly needs live prices for portfolio valuation, trade fills, watchlist display, and SSE
streaming. Prices come from one of two very different places:

- The **simulator**, which produces a new price for every ticker every 500ms, synchronously,
  in-process, for free, forever
- The **Massive API**, which is a synchronous HTTP call, rate-limited to 5 requests/minute on
  the free tier, 15 minutes delayed, and completely static overnight

If those differences leak upward, every consumer needs two code paths. The design goal is
that **no code outside `app/market/` knows or cares which source is running.**

## Shape of the Solution

```
                MASSIVE_API_KEY?
                       |
        +--------------+--------------+
        | set                         | unset
        v                             v
  MassiveDataSource            SimulatorDataSource
  (REST poll, 15s)             (GBM step, 500ms)
        |                             |
        +--------------+--------------+
                       |
                 writes into
                       v
                  PriceCache          <-- single source of truth
                       |
        +--------------+--------------+-----------------+
        v              v              v                 v
   SSE stream    portfolio       trade fill        watchlist
   /api/stream   valuation       price lookup      response
```

The key inversion: **consumers never call the data source.** They read the cache. The source
is a producer that runs on its own schedule and pushes in. This is what makes a 500ms
simulator and a 15-second poller interchangeable — the cache always has a current price, and
the reader does not know how old it is or where it came from.

## `PriceUpdate` — the unit of data

An immutable frozen dataclass in `models.py`. One ticker, one moment.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    open_price: float
    timestamp: float = field(default_factory=time.time)   # Unix seconds

    @property
    def change(self) -> float: ...
    @property
    def change_percent(self) -> float: ...              # vs previous tick
    @property
    def change_from_open_percent(self) -> float: ...    # vs session open
    @property
    def direction(self) -> str: ...                     # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...
```

### Two different "change" numbers, and why

`change_percent` compares against the previous tick. At 500ms intervals that number flickers
around zero and is meaningless as a "daily change" column — it says how the last half-second
went.

`open_price` is the **session baseline**: the first price seen for that ticker after process
start, or after the ticker was added to a running system. `change_from_open_percent` measures
against it, which is the number a user recognises as "how is it doing today".

The UI rule from `PLAN.md`: the watchlist change column and the price-flash colouring both use
`change_from_open_percent`. `change_percent` and `direction` drive only the momentary flash
animation.

The baseline survives page reloads and SSE reconnections because it lives in the server's
cache, and resets on container restart. For a simulation that is the right trade — it needs no
persistence and no market-calendar logic.

Frozen and slotted because a `PriceUpdate` is handed to SSE serialisation and to portfolio
maths concurrently; nothing should be able to mutate one after the cache published it.

## `PriceCache` — the single point of truth

Thread-safe in-memory store in `cache.py`. One writer (the active source), many readers.

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_price(self, ticker: str) -> float | None
    def get_all(self) -> dict[str, PriceUpdate]
    def get_history(self, ticker: str) -> list[float]
    def seed_history(self, ticker: str, prices: list[float]) -> None
    def remove(self, ticker: str) -> None

    @property
    def version(self) -> int
```

Design points:

**`update()` computes the derived fields.** Callers pass a raw price; the cache looks up the
previous entry, carries `open_price` forward, and constructs the `PriceUpdate`. Sources stay
dumb — they fetch or generate a number and hand it over. All the semantics live in one place.

**First update establishes the baseline.** When a ticker has no prior entry,
`previous_price == price == open_price`, so direction is `flat` and both change percentages
are zero. No special-casing at the call sites.

**A `threading.Lock`, not an asyncio lock.** The simulator writes from the event loop, but the
Massive poller writes from a worker thread via `asyncio.to_thread`. A `threading.Lock` is
correct for both; an `asyncio.Lock` would be silently unsafe for the threaded path.

**A monotonic `version` counter.** Incremented on every write. The SSE generator compares it
against the version it last sent and emits nothing when unchanged. This is what makes a quiet
market cost nothing: overnight on real data the version never advances, so the stream sends
only heartbeats instead of pushing identical payloads twice a second.

**Bounded per-ticker history.** A `deque(maxlen=60)` of recent prices per ticker, so
`/api/watchlist` can return populated sparklines on first paint rather than making the
frontend accumulate 30 seconds of data before drawing anything. `seed_history()` lets a source
backfill it at startup. Bounded so it cannot grow without limit over a long-running container.

## `MarketDataSource` — the provider contract

Abstract base class in `interface.py`. Five methods, all the surface there is:

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None
    async def stop(self) -> None
    async def add_ticker(self, ticker: str) -> None
    async def remove_ticker(self, ticker: str) -> None
    def get_tickers(self) -> list[str]
```

Notice what is **not** here: there is no `get_price()`. A source cannot be asked for a price.
It only pushes into the cache. Omitting the read method is what enforces the architecture —
a consumer that tries to bypass the cache finds there is no way to.

Lifecycle contract:

- `start()` is called exactly once, at app startup, and must populate the cache before
  returning so the first HTTP request already has prices
- `stop()` is idempotent and must not write to the cache afterwards
- `add_ticker()` / `remove_ticker()` are no-ops when the ticker is already present or absent
- `remove_ticker()` also clears the ticker from the cache
- Both are `async` because the Massive implementation may need to await a backfill request;
  the simulator's are trivially async

`get_tickers()` is deliberately synchronous — it reads local state and is called from request
handlers.

## The two implementations

### `SimulatorDataSource`

Wraps a `GBMSimulator` in an asyncio task that steps every 500ms and writes each price to the
cache. No I/O, no failure modes worth handling beyond logging. Full detail in
`MARKET_SIMULATOR.md`.

### `MassiveDataSource`

Polls the full-market-snapshot endpoint for the union of watched tickers in one request every
15 seconds, then writes each result to the cache.

Three things it must get right:

**Never block the event loop.** The `massive` SDK is synchronous urllib3. Every call goes
through `asyncio.to_thread`. A blocking HTTP call in the loop would freeze every SSE
connection and every API request for the duration.

**Convert nanoseconds, not milliseconds.** `snap.last_trade.timestamp` is in nanoseconds.

```python
timestamp = snap.last_trade.timestamp / 1_000_000_000.0
```

The current code divides by `1000.0`, putting every real-data price roughly 50,000 years in
the future. `MASSIVE_API.md` has the proof. This is the first fix required on the Massive
path.

**Never propagate a failure.** The poll loop catches broadly, logs, and continues. A 429, a
401, or a dropped connection leaves the last good prices in the cache and retries in 15
seconds. The app stays up with stale data rather than failing requests.

Poll interval is a constructor argument defaulting to 15.0 seconds — the free tier's 5
requests/minute allows one per 12 seconds, and 15 leaves headroom. Paid tiers can pass 2-5.

**Startup backfill.** On `start()`, one `get_aggs(..., timespan="minute", limit=60)` call per
ticker seeds `cache.seed_history()` so sparklines are populated. This is off the polling loop
— once at startup, and once per newly added ticker.

## `create_market_data_source()` — the switch

The whole environment-variable decision, in one function in `factory.py`:

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

`.strip()` matters: `.env` files routinely contain `MASSIVE_API_KEY=` with nothing after it,
and an empty string must mean "use the simulator", not "authenticate with an empty key".

Returns an **unstarted** source. The caller owns the lifecycle, which keeps the factory
synchronous and trivially testable.

The log line is not decoration. "Why are prices not moving" is the most likely support
question in this project, and the answer is usually visible in the first line of the log.

## Ticker Validation — one shared rule

Manual adds, LLM-driven adds, and trades all funnel through the same check:

```python
TICKER_PATTERN = re.compile(r"^[A-Z]{1,5}$")

def normalize_ticker(raw: str) -> str:
    """Uppercase and validate. Raises ValueError if malformed."""
    ticker = raw.strip().upper()
    if not TICKER_PATTERN.match(ticker):
        raise ValueError(f"Invalid ticker: {raw!r}")
    return ticker
```

Uppercase first, then match. One function, one regex, one error message, so the manual path
and the LLM path cannot drift apart. Callers translate the `ValueError` into a 400.

Note this validates *shape*, not *existence*. The simulator deliberately accepts any
well-formed symbol — see `MARKET_SIMULATOR.md`.

## Wiring into FastAPI

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

price_cache = PriceCache()

@asynccontextmanager
async def lifespan(app: FastAPI):
    source = create_market_data_source(price_cache)
    await source.start(load_watchlist_tickers())
    app.state.market_source = source
    yield
    await source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
# ... all other /api routers ...
# StaticFiles mount goes LAST - see PLAN.md section 11
```

The source is stashed on `app.state` so the watchlist router can call `add_ticker` /
`remove_ticker`. The cache is a module-level singleton because it is genuinely process-global
state with a single lifetime.

## SSE Streaming

`create_stream_router(price_cache)` returns a router exposing `GET /api/stream/prices`.

The generator:

1. Opens with `retry: 1000`, so the browser's `EventSource` reconnects after a second
2. Every 500ms, compares `cache.version` against the last sent version
3. On change, emits **one event carrying every tracked ticker**, keyed by symbol
4. Every 15 seconds regardless of price activity, emits a heartbeat comment `: ping\n\n`
5. Exits when `request.is_disconnected()`

```
data: {"AAPL": {"ticker":"AAPL","price":190.50,"previous_price":190.40,
                "open_price":189.20,"timestamp":1753401234.5,
                "change":0.10,"change_percent":0.052,
                "change_from_open_percent":0.687,"direction":"up"}, ...}
```

**One event for all tickers, not one per ticker.** Ten tickers at 2Hz would be 20 events per
second per client, each needing its own parse and its own React state update. One keyed
object is one parse and one batched update.

**Emit only on change** keeps an idle market silent, which is the overnight-real-data case.

**The heartbeat is what makes silence legible.** Without it the frontend cannot distinguish
"connected, market quiet" from "backend stalled" — both look like no data. With it, the
connection dot can be honest: green when a price event or heartbeat arrived in the last 30
seconds, yellow when open but silent longer than that or reconnecting, red when closed.

Response headers set `Cache-Control: no-cache` and `X-Accel-Buffering: no`, the latter to stop
nginx buffering the stream into uselessness if the app is ever proxied.

## Consuming the Cache

```python
from app.market import PriceCache

update = cache.get("AAPL")           # PriceUpdate | None
price = cache.get_price("AAPL")      # float | None
prices = cache.get_all()             # dict[str, PriceUpdate]
history = cache.get_history("AAPL")  # list[float], up to 60, oldest first
```

Trade execution needs one extra behaviour. A just-added ticker may have no price for a few
hundred milliseconds, and failing the trade would be a poor experience:

```python
async def wait_for_price(cache: PriceCache, ticker: str, timeout: float = 2.0) -> float:
    """Poll the cache for a first tick. Raises ValueError on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        price = cache.get_price(ticker)
        if price is not None:
            return price
        await asyncio.sleep(0.2)
    raise ValueError(f"No price available for {ticker} yet, try again")
```

The simulator publishes within 500ms so this effectively never expires. On the Massive path
with a 15-second poll it genuinely can, which is why it returns a retryable message rather
than a hard error.

## Testing Strategy

The interface is what makes the subsystem testable without network or waiting.

- **`PriceCache`** — pure synchronous logic. Assert `open_price` is pinned by the first
  update and carried through later ones; assert `version` increments; assert history is
  bounded at 60.
- **Conformance** — one parametrised suite runs the lifecycle contract against both
  implementations. Anything only one of them passes is a leak in the abstraction.
- **`create_market_data_source`** — `monkeypatch.setenv` over set, unset, empty, and
  whitespace-only keys; assert the returned type.
- **`MassiveDataSource`** — mock the SDK entirely. The valuable assertions are the
  nanosecond conversion and that a raised exception inside a poll does not escape the loop.
- **`SimulatorDataSource`** — inject a large `dt` to make moves observable without waiting.
- **SSE** — drive `_generate_events` directly with a hand-fed cache; assert no event is
  emitted when the version is unchanged, and that a heartbeat still arrives.

No test should need a real API key, and no test should need to sleep for real time.
