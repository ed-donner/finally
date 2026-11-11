# Market Data Interface

The unified Python API for stock prices. Code in `backend/app/market/` selects a source at startup: Massive if `MASSIVE_API_KEY` is set and non-empty, otherwise the simulator. Everything downstream reads from one in-memory cache and never knows which source is active. Massive endpoint details are in `MASSIVE_API.md`; simulator internals are in `MARKET_SIMULATOR.md`.

## Design

```
create_market_data_source(cache)
   ├── SimulatorDataSource   (no key)   -- GBM step every 0.5s
   └── MassiveDataSource     (key set)  -- snapshot poll every 15s (configurable)
              │  writes
              ▼
          PriceCache  (thread-safe, latest PriceUpdate per ticker, version counter)
              │  reads
              ├── SSE  GET /api/stream/prices
              ├── portfolio valuation (GET /api/portfolio)
              ├── trade execution (fill price)
              └── watchlist (GET /api/watchlist)
```

Principles:
- Push into a cache, pull from the cache. Consumers never call a source for a price.
- One abstract class, two implementations (strategy pattern). The factory is the only place that reads the env var.
- Sources are started once, in the FastAPI lifespan, and stopped on shutdown. Exactly one worker process (the cache is in memory).

## Public API (`from app.market import ...`)

### `PriceUpdate` (frozen dataclass)

| Field / property | Meaning |
|---|---|
| `ticker`, `price`, `previous_price` | Latest and prior price (2 decimals) |
| `timestamp` | Unix **seconds** (float) |
| `change`, `change_percent` | Versus `previous_price` (tick-to-tick, not daily) |
| `direction` | `"up"`, `"down"` or `"flat"` |
| `to_dict()` | JSON form used by SSE |

### `PriceCache`

```python
cache = PriceCache()
cache.update("AAPL", 190.5)            # -> PriceUpdate; computes previous_price/direction
cache.get("AAPL")                       # PriceUpdate | None
cache.get_price("AAPL")                 # float | None
cache.get_all()                         # dict[str, PriceUpdate] (copy)
cache.remove("AAPL")
cache.version                           # int, bumps on every update (SSE change detection)
"AAPL" in cache; len(cache)
```

### `MarketDataSource` (ABC)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None   # once; first prices are in the cache on return
    async def stop(self) -> None                         # idempotent
    async def add_ticker(self, ticker: str) -> None      # no-op if present
    async def remove_ticker(self, ticker: str) -> None   # also drops it from the cache
    def get_tickers(self) -> list[str]
```

### Factory

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    key = os.environ.get("MASSIVE_API_KEY", "").strip()
    return MassiveDataSource(api_key=key, price_cache=price_cache) if key \
        else SimulatorDataSource(price_cache=price_cache)
```

### Lifecycle in the app

```python
@asynccontextmanager
async def lifespan(app):
    cache = PriceCache()
    source = create_market_data_source(cache)
    await source.start(tracked_tickers())      # watchlist union open positions
    app.state.cache, app.state.source = cache, source
    yield
    await source.stop()

app.include_router(create_stream_router(cache))   # GET /api/stream/prices
```

Usage by services:

```python
price = request.app.state.cache.get_price(ticker)   # trade fill / valuation
await request.app.state.source.add_ticker(ticker)   # watchlist add or first buy
```

## SSE payload

One event per change of `cache.version`, polled every 0.5s, with a `retry: 1000` directive:

```
data: {"AAPL": {"ticker": "AAPL", "price": 190.5, "previous_price": 190.4,
                "timestamp": 1731200000.1, "change": 0.1, "change_percent": 0.0525,
                "direction": "up"}, "GOOGL": {...}}
```

## Source behaviour differences

| | Simulator | Massive |
|---|---|---|
| Cadence | 0.5s | 15s default; 2-5s on paid plans |
| Any ticker | Yes (random seed price 50-300, default params) | Only tickers with a quote |
| New ticker price | Immediately on `add_ticker` | Next poll (up to one interval) |
| Offline/market closed | Runs 24/7 | Stale; UI shows "market closed" if no update in 60s |
| Tick-to-tick direction | Every tick | Between polls; `flat` when price unchanged |

## Gaps between the code and PLAN.md

Found while reading the existing code against PLAN.md and the installed `massive` package. These are the work items for the next market-data increment, in priority order.

1. **Bug: Massive source never updates the cache.** `massive_client.py` reads `snap.last_trade.timestamp`, but `LastTrade` has no `timestamp` attribute (verified: `hasattr(...) == False`). The `AttributeError` is caught by the per-snapshot handler, which logs a warning and skips every ticker. Fix: use `snap.last_trade.sip_timestamp / 1e9` (nanoseconds to seconds), falling back to `snap.min.close`/`snap.day.close` when `last_trade` is `None` (plans without trades access). The existing Massive tests mock the snapshot with a `timestamp` attribute, so they hide this; build the mocks from real `TickerSnapshot.from_dict(...)` instead.
2. **Daily change % baseline missing.** PLAN.md requires the cache to hold a baseline (previous close for Massive, seed price for the simulator) and the SSE to send daily change %. Today `PriceUpdate.change_percent` is tick-to-tick. Add `baseline: float` to `PriceUpdate` and `PriceCache.update(..., baseline=None)`; the first update for a ticker sets it. Massive supplies `snap.prev_day.close`; the simulator supplies the seed price. Expose `daily_change_percent` in `to_dict()`.
3. **Ticker validation.** PLAN.md requires rejecting a ticker with no quote on Massive (404 `unknown_ticker`). Add `async def validate_ticker(ticker) -> bool` to the ABC. Simulator: always true. Massive: true if already in the cache, otherwise one `get_snapshot_ticker` call (catch the exception, return false).
4. **Tracked set must be the union of watchlist and positions.** `remove_ticker` currently drops the price from the cache, which would break a held position when the user removes it from the watchlist. The service layer must only call `remove_ticker` when the ticker is on neither list. Keep the source API as it is and put this rule in the watchlist service.
5. **Stale detection for "market closed".** Add `PriceCache.last_update_age() -> float | None` or expose `timestamp` per ticker (already present) and let the SSE/health endpoint compute staleness. Note the cache stamps the Massive trade time, so a stale price has an old timestamp automatically once item 1 is fixed.
6. **Poller on a bad key.** On 401/403 the poller logs an error every interval forever. Acceptable for now; log once at error level and continue at debug thereafter if noisy.

## Testing approach

- Cache: update, direction, version, remove, concurrent writers.
- Simulator: see `MARKET_SIMULATOR.md`.
- Massive: build `TickerSnapshot` objects with `TickerSnapshot.from_dict` using the sample JSON in `MASSIVE_API.md`, patch `RESTClient.get_snapshot_all`, assert cache contents, including the no-`lastTrade` case and a failed poll.
- Factory: env var unset, empty, whitespace, set.
- Contract test parameterised over both sources: `start` fills the cache, `add_ticker`/`remove_ticker` behave as documented, `stop` is idempotent.
