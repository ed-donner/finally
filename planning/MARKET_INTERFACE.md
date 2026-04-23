# Market Data Interface

Design document for the abstraction layer that sits between "where prices come from" (simulator or Massive API) and "what consumes prices" (SSE streaming, portfolio valuation, chat context). The interface exists so the rest of the app never knows or cares which source is active.

**Status:** 2026-04-22. The interface described here is implemented in `backend/app/market/` and is close to — but not exactly — what was in the archived design doc. This document describes the intended shape and flags the gaps.

---

## Goals

1. **Pluggable data sources.** Adding a new source (IEX, Alpaca, a different simulator) should not touch any code outside `backend/app/market/`.
2. **Consumer-agnostic.** SSE streaming, the portfolio service, and the chat context builder all read from the same in-memory cache. None of them talks to a data source directly.
3. **Non-blocking.** Producers run in background tasks; consumers read without waiting for the network.
4. **Change-driven, not time-driven.** SSE pushes to clients when prices change, not on a fixed heartbeat. The version counter on the cache drives this.
5. **Graceful degradation.** No API key → simulator. Transient API errors → keep serving stale prices, keep polling.

---

## Topology

```
┌────────────────────────┐         ┌────────────────────┐
│  SimulatorDataSource   │         │  MassiveDataSource │
│  (GBM, ~500ms)         │         │  (REST, ~15s)      │
└───────────┬────────────┘         └─────────┬──────────┘
            │                                │
            │     writes PriceUpdate         │
            └───────────┬───────────┬────────┘
                        │           │
                        ▼           ▼
                   ┌─────────────────────┐
                   │     PriceCache      │
                   │  (thread-safe,      │
                   │   versioned)        │
                   └──────┬──────────────┘
                          │  reads
          ┌───────────────┼───────────────────┐
          │               │                   │
          ▼               ▼                   ▼
    SSE stream     Portfolio service    Chat context
   (/api/stream    (P&L, total value)   (LLM prompt)
    /prices)
```

Only one data source runs at a time. The factory picks at startup based on env vars. The cache is a singleton attached to application state.

---

## The Data Model: `PriceUpdate`

`backend/app/market/models.py` — immutable frozen dataclass with `__slots__`.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float       # UNIX seconds (float)

    @property
    def change(self) -> float: ...           # price - previous_price
    @property
    def change_percent(self) -> float: ...   # 0 if previous_price == 0
    @property
    def direction(self) -> str: ...          # "up" | "down" | "flat"

    def to_dict(self) -> dict: ...
```

### Invariants

- `previous_price` is the prior value recorded in the cache, **not** the prior-session close. Day-over-day comparison is out of scope for v1 (see MASSIVE_API.md — it's available in the snapshot response but we don't wire it through yet).
- `change_percent` is **tick-to-tick**, matching the SSE payload contract in PLAN.md §8.
- `timestamp` is the source's reported time, not the cache insert time — the cache does not overwrite timestamps.

### Why frozen + slots

Frozen guarantees that once a `PriceUpdate` is handed to a consumer (SSE generator, portfolio snapshotter), no one can mutate it. Slots knock ~30% off memory for the many millions of these we'll create over a session.

---

## The Abstract Source: `MarketDataSource`

`backend/app/market/interface.py` — an `abc.ABC` with five methods.

```python
class MarketDataSource(abc.ABC):
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

### Contract (what every source must guarantee)

- `start(tickers)` — idempotent. Initializes internal state, spawns the background task. Returns only when the source is ready to accept `add_ticker` / `remove_ticker`.
- `stop()` — cancels the background task and returns after clean shutdown. Safe to call on an already-stopped source.
- `add_ticker(ticker)` — uppercase-normalizes, no-op if already present. New tickers must be reflected in the cache on the next cycle at latest.
- `remove_ticker(ticker)` — removes from the internal set **and** from the cache. No-op if absent.
- `get_tickers()` — returns a snapshot list; callers may mutate the returned list without affecting internal state.
- **Push model:** sources push to the cache. Consumers never call the source.
- **Exception discipline:** the background task must not die on transient errors. Log and continue.

### What the contract deliberately does not say

- Cadence. The simulator ticks every 500ms; Massive polls every 15s. Consumers should not assume either.
- Delivery guarantees. A source may skip an update if the upstream didn't change. Consumers should be idempotent.
- Ordering across tickers within one tick. The cache is the source of truth; the tick boundary is the cache's version bump.

---

## The Price Cache

`backend/app/market/cache.py` — thread-safe in-memory store, keyed by ticker.

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate: ...
    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...
    def get_price(self, ticker: str) -> float | None: ...
    def remove(self, ticker: str) -> None: ...
    @property
    def version(self) -> int: ...
    def __len__(self) -> int: ...
    def __contains__(self, ticker: str) -> bool: ...
```

### Concurrency model

- **One lock for everything.** A single `threading.Lock` guards all reads and writes. Contention is not an issue at our scale (~10 tickers, one writer, a handful of readers).
- **Works for both producer modes.** The simulator's writes come from an asyncio task (but we still take the lock — no-GIL Python compatibility, and it's nearly free). Massive's writes come from a worker thread launched via `asyncio.to_thread`.
- **Readers get consistent snapshots.** `get_all()` returns a shallow copy taken under the lock; iterating the result is lock-free.

### Version counter

Every successful `update()` increments a monotonic `version: int`. SSE generators poll `cache.version` every 500ms and only serialize + emit when it has advanced. This turns a timer-driven loop into an effectively event-driven one without adding a real pub/sub layer.

**Known hygiene item:** the current code reads `_version` outside the lock in the property getter. CPython's GIL makes this safe in practice, but no-GIL builds (3.13t+) would race. Put the read under the lock.

### Rounding

Prices are rounded to 2 decimals on write. This is a deliberate UX choice — we don't want `$191.4237` flickering in the UI — but it also means math done downstream (P&L) inherits the rounding. That's fine for a simulated trading UI; it would not be fine for actual execution.

---

## The Factory

`backend/app/market/factory.py` — one function, no classes.

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    if api_key:
        from .massive_client import MassiveDataSource  # lazy import
        return MassiveDataSource(api_key, price_cache)
    return SimulatorDataSource(price_cache)
```

### Why lazy-import the Massive client

The `polygon` / `massive` SDK is an optional dependency. Users running simulator-only should not have to install it. The lazy import inside the factory keeps that promise. A consequence is that test mocking is awkward — `unittest.mock.patch` with `create=True` or module-level patching are both workable.

### Why env var over config file

One process, one source, one env var. The simulator is the default because "works with zero config" is a design goal. This matches the Docker-run-and-go UX.

---

## SSE Streaming

`backend/app/market/stream.py` — a FastAPI router with one endpoint.

```
GET /api/stream/prices
```

### Generator contract

1. On connect, emit `retry: 1000\n\n` so browsers reconnect after 1s on drop.
2. Track `last_version = cache.version` at connect time.
3. Loop every 500ms:
   - If `cache.version > last_version`: serialize `cache.get_all()` as a JSON array of `PriceUpdate.to_dict()` entries, emit as `data: <json>\n\n`, update `last_version`.
   - Else: no-op.
   - On client disconnect: break.

### Event payload

```json
{
  "ticker": "AAPL",
  "price": 191.42,
  "previous_price": 191.05,
  "timestamp": 1776321000.12,
  "change": 0.37,
  "change_percent": 0.1937,
  "direction": "up"
}
```

This exactly matches PLAN.md §8. The frontend's `EventSource` handler can forward each entry to the price-state reducer as-is.

### Why 500ms poll on the generator side

The generator loop runs at the simulator's cadence. Massive's poll is slower (15s), so in that mode the generator frequently finds no version change and emits nothing — which is what we want. The 500ms is a cap on latency, not a data cadence.

### Known hygiene items

- `_generate_events` is annotated `-> None` but yields strings. Should be `AsyncGenerator[str, None]`.
- A module-level `router` variable is created when `create_stream_router` is called; calling it twice would re-register routes on the same object. Switch to a fresh `APIRouter` inside the factory.

---

## Lifecycle in the FastAPI app

On startup:

1. Create `PriceCache` singleton and stash on `app.state`.
2. Call `create_market_data_source(cache)` → source instance.
3. Load default watchlist from DB (or use the seed set).
4. `await source.start(watchlist_tickers)`.
5. Register the SSE router with the cache injected.

On shutdown:

1. `await source.stop()` — background task cancelled cleanly.
2. Let the cache be garbage-collected with the process.

Adding/removing tickers through the watchlist API calls `source.add_ticker(...)` / `remove_ticker(...)` and persists to the DB. The order (cache first, then DB, vs. DB first, then cache) is a small correctness question: if the process dies between, we could have a ticker in the cache that isn't in the DB, or vice versa. **DB first, cache second** is the right order — a ticker in the DB but not yet in the cache is recoverable on next startup; a ticker in the cache but not the DB vanishes on restart, which is confusing.

---

## What's Deliberately Out of Scope

- **Historical bars / charting data.** The main chart uses frontend-accumulated SSE ticks (PLAN.md §2, §10). No bar-fetching endpoint in v1.
- **Multi-user price isolation.** The cache is global; every user watches the same union of tickers. PLAN.md notes this is a deferred concern.
- **Source failover.** If Massive keeps 429'ing, we do not auto-flip to the simulator. The user would either stop hitting the limit or switch keys.
- **WebSocket upstream.** Massive's WebSocket is Advanced-tier-only ($199/mo). REST polling covers v1.

---

## Testing Strategy

- **Simulator source:** full async lifecycle, add/remove, seed-determinism (when `SIMULATOR_SEED` is set), exception resilience. Covered in `backend/tests/market/test_simulator*.py`.
- **Massive source:** mock the `polygon.RESTClient.get_snapshot_all` return value and assert cache updates. The lazy-import pattern makes this awkward; use `patch("backend.app.market.massive_client.RESTClient", create=True)`.
- **Cache:** update/get/version/remove; concurrent write test (spawn two threads writing different tickers, assert no corruption).
- **SSE:** integration test with a FastAPI `TestClient`. Seed a price, connect, assert the first event arrives; bump a price, assert a second event arrives; don't bump, assert no third event for N seconds.

Current coverage has gaps: SSE end-to-end and cache concurrency both lack tests. Worth addressing in the next pass.

---

## Summary of Known Deltas (Code vs. This Doc)

| Item | File | Status |
|---|---|---|
| `_generate_events` return type annotation | `stream.py:54` | Says `-> None`, should be `AsyncGenerator[str, None]` |
| `version` property lock | `cache.py:65-67` | Read outside lock; fine on CPython, races on no-GIL |
| `get_tickers()` reaches into `_sim._tickers` | `simulator.py:258` | Expose a public `get_tickers()` on the GBM simulator |
| Module-level `router` in factory | `stream.py:16` | Create fresh `APIRouter` inside the factory |
| Day-over-day `prevDay.c` not extracted | `massive_client.py` | Available in snapshot response; not wired to cache |
| Timestamp divisor assumes ms | `massive_client.py:103` | Snapshot timestamps are ns; verify and fix |

None of these break the current behavior. They are next-pass hygiene.
