# Market Data Interface Design

**Status:** Design proposal, 2026-11-19. Companion to [PLAN.md](PLAN.md) §6 and §10 and [API.md](API.md) (the wire contract is the authority; this document describes how the code is organized around it).
**Question answered:** How should FinAlly's market data be structured as an interface, from the price source on the server to the components on screen, so that the simulator and Massive are interchangeable and the UI never knows which one is running?

---

## 1. Principles

1. **One seam, on the server.** Everything downstream of the price source depends on one abstract interface, `PriceSource`. The simulator and the Massive client both implement it. Nothing else knows which is active.
2. **One wire contract.** The browser receives `snapshot`, `prices`, and `heartbeat` events (API.md). It never talks to Massive or the simulator directly, and it never receives an API key.
3. **State is replaced, then patched.** On connect, the client replaces its whole price state from `snapshot`. After that it applies `prices` deltas. There is no merge logic on reconnect, which avoids gaps.
4. **Pure logic is separate from React.** Valuation, bucketing, connection state, and direction are plain functions with unit tests. Components only render them.
5. **Rendering is cheap by design.** The data rate is tiny: about 2 events per second and at most ~30 tickers. The design avoids clever optimizations and keeps the update path simple.

---

## 2. Layers

```
Massive REST  ─┐
               ├─► PriceSource (ABC) ─► PriceCache ─► Broadcaster ─► GET /api/stream/prices (SSE)
Simulator    ─┘        tick()            (per-ticker     (fan-out to        │
                                         state, 600pt    SSE clients)       │
                                         history)                           ▼
                                                                  EventSource (browser)
                                                                            │
                                                                  market store (pure reducer)
                                                                            │
                          ┌──────────────┬──────────────┬─────────────┬─────┴──────┐
                     Watchlist     Main chart     Sparklines    Heatmap / P&L    Header total
                     (rows)        (LWC series)   (SVG)         (treemap / line) (cash + Σ qty×px)
```

Server side: §3–§4. Client side: §5–§7. Testing seams: §8.

---

## 3. Server interface

### 3.1 The `PriceSource` contract

Matches PLAN.md §6 exactly. Shown here as the Python shape the backend should use.

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass(frozen=True)
class PriceUpdate:
    ticker: str
    price: float           # latest trade/mark, dollars
    timestamp_ms: int      # UTC epoch milliseconds
    prev_close: float | None  # None until known (Massive, before baseline)

class UnknownTicker(Exception):
    """Raised by add_ticker when the source cannot price the symbol."""

class PriceSource(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def add_ticker(self, symbol: str) -> None:
        """Raise UnknownTicker if the source cannot price it."""

    @abstractmethod
    async def remove_ticker(self, symbol: str) -> None: ...

    @abstractmethod
    def tick(self) -> list[PriceUpdate]:
        """Updates produced since the previous call. Called once per cadence."""
```

Design notes:

- `tick()` is **pull**, not push. The cache's single background task calls it on a schedule. The simulator produces an update batch every 500 ms. The Massive client returns whatever the last poll produced, possibly an empty list. Downstream code therefore never cares about the source's internal timing.
- `prev_close` travels with the update so the cache can fill the baseline without a second call. The simulator sets it to the seed price (PLAN.md §6). Massive sets it from `prevDay.c` (see [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md) §5.1).
- Source-specific health (`source_status`, `consecutive_failures`) is **not** on the interface. It is exposed as a separate read-only property that the heartbeat reads. The simulator always reports `ok`. Keeping it off the core method keeps the interface small and the contract test simple.

```python
class SourceHealth(Protocol):
    source_name: str            # "simulator" | "massive"
    status: str                 # "ok" | "degraded"
    consecutive_failures: int
```

### 3.2 Price cache

The cache is the only writer of per-ticker state. It is a single in-process object, consistent with the one-worker rule (PLAN.md §3).

Per ticker it holds:

- `price`, `previous_price`, `timestamp_ms`, `prev_close`
- `direction` (computed here: `up` / `down` / `flat`, comparing `price` with `previous_price`)
- `history`: a `collections.deque(maxlen=600)` of `(timestamp_ms, price)`
- `dirty` flag: set when the price changed since the last broadcast

Rules:

- `previous_price` is the value before the latest update, at any cadence (PLAN.md §6, clarification 7).
- `prev_close` is set once per update when the source provides it. A `None` never overwrites a known value. This keeps the restart and stale-snapshot rules from PLAN.md §7 simple.
- The cache's tick loop: call `source.tick()`, apply each update, mark dirty, append to history, then hand the dirty set to the broadcaster. Nothing else mutates the cache.

Watched set: the cache subscribes the source to `watchlist ∪ held tickers`, so held-but-unwatched tickers keep pricing (PLAN.md §6). Watchlist and portfolio services call `cache.ensure(ticker)` and `cache.release(ticker)`. Release calls `source.remove_ticker` only when the ticker is neither watched nor held.

### 3.3 Broadcaster and SSE

- Each SSE connection gets an `asyncio.Queue` with a bounded size (for example, 100 events). The broadcaster puts events on every queue.
- **Slow client policy:** if a client's queue is full, drop that client's oldest `prices` event, not the connection. A `prices` event is a delta, so a dropped one leaves that client stale until the next event for the same ticker. To make recovery exact, a full queue should close the connection, and `EventSource` reconnects and gets a fresh `snapshot`. **Recommended:** close on overflow. It is simpler and keeps the reasoning in one place.
- On connect the server sends, in order: `retry: 1000`, then `snapshot` (the whole cache, including 600-point history), then the live loop.
- A heartbeat task sends `heartbeat` every 5 s on every connection, always (API.md). It reads `SourceHealth`.
- Serialization happens once per tick and is reused for every client, not once per client.

### 3.4 Source selection

A single factory reads the environment (PLAN.md §5) and returns one `PriceSource`:

```python
def make_price_source(env) -> PriceSource:
    if env.massive_api_key:           # non-empty
        return MassiveClient(api_key=env.massive_api_key, limiter=..., poll_interval=...)
    return SimulatorSource(seed=SIM_SEED, events_enabled=True)
```

The factory is the only place that branches on the source. Tests inject a `FakePriceSource` directly.

---

## 4. Server-side behaviour that the UI relies on

| Situation | What the server does | What the client sees |
|---|---|---|
| Fresh start, simulator | Seeds cache from `price_state` if present, else seed prices | `snapshot` with current prices and 600-point history |
| Ticker added to watchlist | `cache.ensure` → source `add_ticker` → first tick includes it | Ticker appears in `snapshot` on next connect; in `prices` once the source emits it |
| Unknown ticker (Massive) | `add_ticker` raises `UnknownTicker` → `404 unknown_ticker` | Watchlist add rejected, nothing streams |
| Massive poll fails | Keep last prices, count failure | `heartbeat` with `source_status: "degraded"` after 3 failures |
| Massive ticker before first poll | Cache has no price | `snapshot` lists it with `price: null` (API.md) |
| Held ticker with no price after restart (Massive) | Valued from `price_state`, marked stale | Portfolio response carries `stale: true` |

---

## 5. Client structure

Proposed module layout under `frontend/src/market/`. Names are suggestions; the frontend agent may adapt them, but the separation of pure logic from React should hold.

```
frontend/src/market/
├── types.ts          # Wire types: Snapshot, PriceEntry, Heartbeat, Direction, SourceStatus
├── events.ts         # Parsing + validation of SSE event payloads (pure)
├── store.ts          # Reducer: snapshot (replace) | prices (patch) | heartbeat (liveness) (pure)
├── connection.ts     # Connection state machine from clock + readyState + heartbeat (pure)
├── bucket.ts         # 500 ms → 1 point per integer second for LWC (pure)
├── valuation.ts      # cash + Σ qty × live price; heatmap colour; P&L (pure)
├── useMarketStream.ts  # React hook: owns EventSource, dispatches to reducer
└── format.ts         # Number and percent formatting
```

### 5.1 Types (from API.md)

```ts
export type Direction = "up" | "down" | "flat";
export type SourceStatus = "ok" | "degraded";

export interface PriceEntry {
  price: number | null;
  previous_price: number | null;
  prev_close: number | null;
  change_pct: number | null;
  direction: Direction | null;
  timestamp: string;             // ISO 8601 UTC
  history: [number, number][];   // [timestamp_ms, price]
}

export interface MarketState {
  source: "simulator" | "massive" | null;
  sourceStatus: SourceStatus;
  tickers: Record<string, PriceEntry>;
  lastEventAt: number | null;    // ms, client clock, any event
  lastHeartbeatAt: number | null;
}
```

### 5.2 Reducer

Three transitions only, matching the client rules in API.md:

- `snapshot` → **replace** `tickers`, `source`, `sourceStatus`. Do not merge. Set `lastEventAt`.
- `prices` → for each entry, **patch** that ticker. Keep `history` (append the new point, trimmed to 600). `direction` comes from the server, not from comparing numbers in the component (PLAN.md §10).
- `heartbeat` → set `lastHeartbeatAt`, `lastEventAt`, `sourceStatus`. No price changes.

The reducer is a pure function. Its tests are the cheapest and most valuable in the frontend suite.

### 5.3 Connection state

Pure function of `(now, readyState, lastEventAt, lastHeartbeatAt, sourceStatus, openSince)`, using the table in PLAN.md §10:

| State | Rule |
|---|---|
| Connected (green) | `OPEN`, a heartbeat, `prices`, or `snapshot` within 20 s, and `ok` |
| Degraded (yellow) | `OPEN`, fresh heartbeat, `sourceStatus === "degraded"` |
| Reconnecting (yellow) | `CONNECTING`, or `OPEN` with no event for 20 s |
| Disconnected (red) | not `OPEN` continuously for 30 s |

The hook drives `now` with a 1 s timer so the state changes even with no events arriving. Keeping the function pure makes the time-based transitions testable with a fake clock.

### 5.4 Bucketing for the main chart

Lightweight Charts expects times in increasing order without duplicates (PLAN.md §10). The stream produces 2 points per second, so `bucket.ts` keeps the last price in each integer second:

```ts
export function bucketToSeconds(points: [number, number][]): { time: number; value: number }[] {
  const out: { time: number; value: number }[] = [];
  for (const [tsMs, price] of points) {
    const t = Math.floor(tsMs / 1000);
    const last = out[out.length - 1];
    if (last && last.time === t) last.value = price;   // last price in the second wins
    else if (!last || t > last.time) out.push({ time: t, value: price });
    // t < last.time: out of order input; drop (should not happen from the server)
  }
  return out;
}
```

The same function serves the snapshot history on load and each live point. Live points use `series.update()` with the newest bucket; the full series is set once on load or ticker switch with `series.setData()`.

### 5.5 Valuation

`valuation.ts` computes the header total and the heatmap inputs as pure functions:

- `liveTotal(cashCents, positions, prices)` = `cash + Σ(quantity × price)`, in integer cents for the cash part and rounded once at the end for display. Must match the backend snapshot formula (PLAN.md §8, §10). A test uses a backend-computed fixture at the same prices.
- `heatColour(pctReturn)`: 0 → `#30363d`; ≥ +10% → gain; ≤ −10% → loss; linear in between. Size = market value.

If a ticker's price is `null`, valuation falls back to its last known value or excludes it, and the UI shows it as stale. It must not show zero.

### 5.6 Price flash

`useFlash(ticker)` returns a class name. On a `prices` entry with `direction` up or down, it adds `flash-up` or `flash-down` and removes it after 500 ms (CSS transition, PLAN.md §2). Direction is never recomputed in the component.

### 5.7 Rendering cost

- Two events per second, ~30 tickers, one batch per event. React state updates once per batch, not once per ticker.
- Sparklines: a single SVG `<polyline>` of up to 600 points per ticker. This is cheap; no downsampling is needed at this size. If profiling later shows a problem, downsample to one point per 2 s for the sparkline only.
- Main chart: `series.update()` for each second's new bucket. No full redraws during streaming.
- Treemap: a layout recomputed only when the set of positions or their market values changes by more than a rounding threshold, not on every tick.

---

## 6. Trade and watchlist flows through the same interface

Trades and watchlist changes do not talk to the price source. They go through REST (API.md) and the server updates the cache. The client's only obligations are:

- After a trade: re-fetch `GET /api/portfolio` (the backend is authoritative for cash, positions, and fills).
- After a watchlist add: the ticker appears in the next `prices` event. Until then it shows as pending, not as a price.
- After a watchlist remove: remove the row. If the ticker is still held, it stays in the portfolio table and in `snapshot`, which the API already specifies.

---

## 7. Decisions and recommendations

| # | Decision | Recommendation |
|---|---|---|
| D1 | Pull (`tick()`) vs push from sources | Pull. One scheduler owns timing. |
| D2 | Slow-client policy | Close the connection on queue overflow; client reconnects and gets a snapshot. |
| D3 | Where `source_status` lives | Separate `SourceHealth` protocol, not on `PriceSource`. |
| D4 | Client state model | Reducer with replace-on-snapshot; no merge logic. |
| D5 | State library | Plain `useReducer` plus context. The data is small; avoid a dependency. |
| D6 | Main chart library | Lightweight Charts v5 (canvas), as PLAN.md §10 specifies. Verify its exact time-ordering behaviour against the installed version in a unit test. |
| D7 | Sparklines | Inline SVG polyline (PLAN.md §10). |
| D8 | Key handling | Massive key stays server-side. The client never sees it. |

---

## 8. Testing seams

- **Server:** a `FakePriceSource` implementing `PriceSource` with scripted updates. Used for cache, broadcaster, and SSE route tests. The shared contract test (PLAN.md §12) runs against both real sources.
- **Server SSE:** test with an in-process ASGI client. Assert event order (`retry`, `snapshot`, then `prices`/`heartbeat`), that `snapshot` contains 600 points for a full cache, and that overflow closes the connection.
- **Client pure logic:** reducer, connection state (fake clock), bucketing (600 points at 500 ms → 300 strictly increasing integer seconds, last price kept), valuation (backend fixture), heat colour (0%, +10%, −10%).
- **E2E:** `LLM_MOCK=true` with the simulator (deterministic seed). For the degraded and quiet-stream scenarios in PLAN.md §12, the E2E harness swaps in a fake Massive server that returns errors or no changes on command.

---

## 9. Open questions

1. **Massive base URL and auth header.** Pending the spike in [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md) §8. The interface does not change either way; only the `MassiveClient` internals do.
2. **Delayed prices in Massive mode.** On Starter or Developer, `price` is 15 minutes old. The interface carries no flag for this. Should `source` read `"massive-delayed"`? This is a product wording decision and changes only the `snapshot`/`heartbeat` `source` string.
3. **Client-side jump or "stale" indicator.** If a Massive ticker has not changed for several polls, should the UI mark it stale? PLAN.md says flashes are infrequent by design, so this is optional.

---

**Related:** [PLAN.md](PLAN.md) §6, §10 · [API.md](API.md) · [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md) · [SIMULATOR_RESEARCH.md](SIMULATOR_RESEARCH.md)
