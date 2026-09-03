# Market Data Interface — Unified Design

**Status:** Design. Supersedes the market-data portion of PLAN.md §6.
Grounded in the entitlement research in [`MASSIVE_API.md`](MASSIVE_API.md).

---

## 1. The Problem This Design Solves

PLAN.md §6 specifies two implementations behind one interface, selected by whether
`MASSIVE_API_KEY` is set. That binary is wrong, and the code already in
`backend/app/market/` inherits the error.

The research finding: **a free Massive key authenticates successfully and then refuses to
return any live price.** Snapshots, last trade, and any bar dated today are all
`NOT_AUTHORIZED` on the Basic tier (verified — see `MASSIVE_API.md` §4). Real-time data
starts at $199/month.

So `MASSIVE_API_KEY` being present tells you almost nothing. A key can be:

| Key state | What works |
|---|---|
| Absent | nothing — simulate |
| Present, Basic (free) | historical bars and yesterday's closes; **no live prices** |
| Present, Starter/Developer | live-ish prices, 15 minutes delayed |
| Present, Advanced | live prices |
| Present, invalid/revoked | nothing |

Under the current `factory.py`, the second row — the one nearly every student will be in —
produces a running app whose watchlist never populates. Every poll raises, `_poll_once`
swallows it in a bare `except Exception`, and the cache stays empty in silence.

**The design goal is that all five rows produce a working, moving trading terminal.**

---

## 2. Three Sources, One Interface

```
                       MASSIVE_API_KEY set?
                              │
              ┌───────────────┴───────────────┐
              no                             yes
              │                               │
              │                    probe_capabilities()   ← 2 calls, once, at startup
              │                               │
              │              ┌────────────────┼────────────────┐
              │         realtime          end-of-day        invalid
              │              │                │                │
              ▼              ▼                ▼                ▼
      SimulatorDataSource  MassiveDataSource  AnchoredSimulator  SimulatorDataSource
      (synthetic seeds)    (real, streaming)  (real prices,      (+ warning in /health)
                                               synthetic motion)
                              │                │                │
                              └────────────────┴────────────────┘
                                               │
                                          PriceCache
                                               │
                        ┌──────────────────────┼──────────────────────┐
                   SSE /api/stream        portfolio valuation    trade pricing
```

Everything below `PriceCache` is source-agnostic. That boundary already exists in the repo
and is correct — this design keeps it untouched and only changes what sits above it.

### The third source is the important one

`AnchoredSimulatorDataSource` fetches **real closing prices** from Massive with the one
free-tier call that returns the whole market, then runs the GBM simulator forward from
those anchors.

The result: AAPL opens at its genuine 324.96 close rather than a hard-coded 190.00, and it
*moves*. A student with a free key gets real price levels, real relative valuations, a
watchlist that ticks, and a portfolio that changes — the entire demo works. One API call at
startup, well inside a 5/minute budget.

It also fixes a bug nobody has noticed yet: `backend/app/market/seed_prices.py` is badly
stale. NVDA is seeded at 800.00 and actually trades at 224.41; NFLX is seeded at 600.00 and
trades at 82.73 (both post-split). Anchoring to live data removes the maintenance burden
from that table entirely.

---

## 3. The Interface

`MarketDataSource` as it stands in `backend/app/market/interface.py` is sound. Keep the ABC,
add two members.

```python
class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    # --- existing, unchanged ---
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

    # --- new ---
    @abstractmethod
    def describe(self) -> SourceStatus:
        """Introspection for GET /api/health. Never raises."""

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        """Historical series for the chart's first paint.

        Default implementation returns [] — the frontend then accumulates from
        SSE as PLAN.md §10 describes. Massive-backed sources override this with
        real intraday bars.
        """
        return []
```

```python
@dataclass(frozen=True, slots=True)
class SourceStatus:
    name: str            # "simulator" | "massive" | "anchored-simulator"
    live: bool           # True only when prices reflect the real current market
    detail: str          # human-readable, surfaced verbatim in /api/health
    tickers: int
    cache_populated: bool
```

`describe()` answers PLAN.md §13.5 item 38 directly. `detail` should be specific enough to
end the debugging session: `"end-of-day only (Basic tier) — anchored to 2026-09-02 closes"`.

### Why `get_history` belongs on the interface

PLAN.md §13.2 item 15 asks whether the backend should keep a ring buffer so charts are not
blank on first paint, and recommends it. Putting `get_history` on the interface gets that
for free in a better form: the simulator can serve its own ring buffer, while
Massive-backed sources serve `get_aggs(ticker, 1, "minute", ...)` — a **genuine** intraday
series, available on the free tier. The frontend calls one endpoint and does not care which
it got.

---

## 4. `PriceUpdate` — adding the daily baseline

PLAN.md §13.1 item 1 identifies that "daily change %" has no baseline: `PriceUpdate` carries
only `previous_price` from the previous ~500 ms tick, so the column renders permanently flat
at ±0.05%. The fix is one field.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float          # previous TICK — drives the flash animation only
    open_price: float              # session open / anchor — drives the daily % column
    timestamp: float               # Unix seconds internally; ISO 8601 UTC on the wire

    @property
    def tick_direction(self) -> str:
        """'up' | 'down' | 'flat' — CSS flash class. Renamed from `direction`."""

    @property
    def change_today(self) -> float:
        return round(self.price - self.open_price, 4)

    @property
    def change_percent_today(self) -> float:
        if self.open_price == 0:
            return 0.0
        return round((self.price - self.open_price) / self.open_price * 100, 4)
```

Two clearly named fields, exactly as item 1 recommends. Where `open_price` comes from, per
source:

| Source | `open_price` |
|---|---|
| `MassiveDataSource` | `snapshot.prev_day.close` (real previous close) |
| `AnchoredSimulatorDataSource` | the anchor close fetched at startup |
| `SimulatorDataSource` | the seed price the simulation started from |

In all three cases it is fixed for the session, so the daily % accumulates over minutes and
hours instead of resetting every tick.

`PriceCache.update()` gains an `open_price: float | None` parameter; when `None` it keeps
whatever the ticker already had, and on first write defaults to `price`. That preserves the
existing call sites.

### Wire format

Per PLAN.md §13.1 item 6, JSON is **ISO 8601 UTC everywhere**. `PriceUpdate.timestamp` stays
a float internally and converts at `to_dict()`:

```json
{
  "ticker": "AAPL",
  "price": 325.41,
  "previous_price": 325.38,
  "open_price": 324.96,
  "timestamp": "2026-09-03T17:42:11.413700Z",
  "tick_direction": "up",
  "change_today": 0.45,
  "change_percent_today": 0.1385
}
```

---

## 5. Source Selection

Replaces `backend/app/market/factory.py`.

```python
async def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select a market data source. Never raises; always returns a working source.

    Async because the capability probe makes real network calls. Called once,
    from the FastAPI lifespan handler.
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if not api_key:
        logger.info("No MASSIVE_API_KEY — using GBM simulator")
        return SimulatorDataSource(price_cache)

    caps = await asyncio.to_thread(probe_capabilities, api_key)

    if caps.realtime:
        logger.info("Massive: real-time entitled — using live snapshots")
        return MassiveDataSource(api_key, price_cache, poll_interval=5.0)

    if caps.end_of_day:
        logger.warning(
            "Massive key is end-of-day only (Basic tier). Anchoring the simulator "
            "to real closing prices — displayed prices are simulated, not live."
        )
        return AnchoredSimulatorDataSource(api_key, price_cache)

    logger.error("MASSIVE_API_KEY rejected (%s) — falling back to simulator", caps.detail)
    return SimulatorDataSource(price_cache, status_detail=f"key rejected: {caps.detail}")
```

Three rules this encodes:

1. **Never boot into a broken state.** Every branch returns a source that produces moving
   prices. A bad key degrades; it does not blank the screen.
2. **Never silently mislead.** A simulated price is never presented as live —
   `SourceStatus.live` is `False` and the frontend shows a "SIMULATED" badge beside the
   connection dot.
3. **Probe once.** Two calls at startup, not two per poll. The result is immutable for the
   process lifetime.

### Poll intervals

| Source | Interval | Rationale |
|---|---|---|
| `SimulatorDataSource` | 500 ms | PLAN.md §6; local computation, no budget |
| `AnchoredSimulatorDataSource` | 500 ms tick; **one** anchor fetch at startup | GBM is local |
| `MassiveDataSource`, Advanced | 5 s | unlimited calls; 5 s is plenty for a terminal |
| `MassiveDataSource`, Starter/Developer | 15 s | data is 15 min delayed anyway |

`AnchoredSimulatorDataSource` optionally re-anchors once per hour (1 call) so a long-running
container picks up the next session's close. Cheap, and keeps a demo left open overnight
honest.

---

## 6. `AnchoredSimulatorDataSource`

```python
class AnchoredSimulatorDataSource(MarketDataSource):
    """GBM simulation seeded from real Massive closing prices.

    Bridges the gap for Basic-tier keys: real price *levels* from one
    free-tier API call, plus synthetic price *motion* so the terminal is alive.
    """

    def __init__(self, api_key: str, price_cache: PriceCache) -> None:
        self._client = RESTClient(api_key=api_key, retries=0)
        self._cache = price_cache
        self._sim: SimulatorDataSource | None = None
        self._anchors: dict[str, float] = {}
        self._anchor_date: str | None = None

    async def start(self, tickers: list[str]) -> None:
        self._anchors, self._anchor_date = await asyncio.to_thread(
            self._fetch_anchors, tickers
        )
        # Real closes where we have them; the static seed table covers the rest.
        self._sim = SimulatorDataSource(self._cache, seed_overrides=self._anchors)
        await self._sim.start(tickers)

    def _fetch_anchors(self, tickers: list[str]) -> tuple[dict[str, float], str | None]:
        """ONE API call prices every ticker. Walks back over weekends/holidays."""
        wanted = {t.upper() for t in tickers}
        day = date.today()
        for _ in range(7):                      # at most a week back
            day -= timedelta(days=1)
            iso = day.isoformat()
            try:
                bars = self._client.get_grouped_daily_aggs(iso, adjusted=True)
            except Exception as e:
                logger.warning("Anchor fetch failed for %s: %s", iso, e)
                continue
            if not bars:
                continue                        # weekend or holiday
            found = {b.ticker: b.close for b in bars if b.ticker in wanted}
            logger.info("Anchored %d/%d tickers to %s closes",
                        len(found), len(wanted), iso)
            return found, iso
        logger.warning("No anchors available — falling back to the static seed table")
        return {}, None

    def describe(self) -> SourceStatus:
        return SourceStatus(
            name="anchored-simulator",
            live=False,
            detail=(f"simulated from real {self._anchor_date} closes "
                    f"({len(self._anchors)} anchored)"
                    if self._anchor_date else
                    "simulated from static seed prices (anchor fetch failed)"),
            tickers=len(self._sim.get_tickers()) if self._sim else 0,
            cache_populated=len(self._cache) > 0,
        )

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        """Real minute bars from the last completed session (free tier allows this)."""
        if not self._anchor_date:
            return []
        bars = await asyncio.to_thread(
            self._client.get_aggs, ticker, 1, "minute",
            self._anchor_date, self._anchor_date, limit=50_000,
        )
        return [PricePoint(ms_to_iso(b.timestamp), b.close) for b in bars[-points:]]
```

Adding a ticker mid-session costs nothing: `add_ticker` delegates to the simulator, which
synthesises a seed if the anchor set has no entry — the answer to PLAN.md §13.2 item 8, and
the demo never dead-ends on an unknown symbol.

---

## 7. `MassiveDataSource` — corrections

The shipped implementation needs four fixes beyond the entitlement gate. All are verified
against the SDK and live API in `MASSIVE_API.md`.

1. **Wrong timestamp field and wrong unit.** The code reads `snap.last_trade.timestamp /
   1000.0`. The SDK field is `sip_timestamp`, and it is **nanoseconds**, not milliseconds.
   The `AttributeError` is currently swallowed by the surrounding handler, so this fails
   invisibly.
   ```python
   ts = snap.last_trade.sip_timestamp / 1e9        # ns → epoch seconds
   ```

2. **`open_price` is never captured**, so the daily % column has no baseline (§4).
   Snapshots hand it over for free:
   ```python
   self._cache.update(
       ticker=snap.ticker,
       price=snap.last_trade.price,
       open_price=snap.prev_day.close if snap.prev_day else None,
       timestamp=ts,
   )
   ```

3. **Rate limiting is invisible.** `except Exception` catches `MaxRetryError` and logs it at
   `error` level once per poll, forever, with no user-facing signal. Classify instead, and
   let `describe()` report a degraded state so `/api/health` and the frontend can show it.

4. **`retries=3` is actively harmful on a metered key.** The SDK's 0.0/0.2/0.4 s backoff
   burns three requests inside the same 60 s window as the failure. Set `retries=0` and back
   off at the poll-loop level.

Two smaller ones: a poll that returns zero usable snapshots after a run of consecutive
failures should flip `SourceStatus.live` to `False` rather than let the frontend keep
displaying a frozen price as live — which also covers PLAN.md §13.2 item 14, the weekend /
closed-market case. And tickers absent from a snapshot response (unknown symbols) should be
recorded so `/api/watchlist` can flag them, rather than vanishing silently.

---

## 8. What Does Not Change

Deliberately preserved from the shipped implementation:

- **`PriceCache`** — the threading `Lock`, the monotonic `version` counter, and the
  `dict[str, PriceUpdate]` shape are all correct. The `version` counter driving SSE change
  detection is a good design and stays.
- **The cache as the single read path.** Portfolio valuation, trade pricing, and SSE read
  the cache and never the source. This is what makes three sources cost nothing downstream.
- **The full-map SSE payload.** PLAN.md §13.5 item 40 asks for confirmation that sending
  every ticker on every change is deliberate rather than an oversight. It is deliberate: at
  10–50 tickers the payload is a couple of KB, and a delta protocol would need
  reconnection-resync logic for no measurable gain. **Do not optimise this into a delta.**
- **`create_stream_router`'s factory pattern** for injecting the cache without globals.

One change to `stream.py`: add the heartbeat from PLAN.md §13.1 item 7. It matters far more
now — in Massive mode at a 15 s poll interval, or on a closed market, the stream is silent
long enough for a proxy to drop it.

```python
last_sent = time.monotonic()
while True:
    ...
    if current_version != last_version:
        yield f"data: {payload}\n\n"
        last_sent = time.monotonic()
    elif time.monotonic() - last_sent > 10.0:
        yield ": ping\n\n"          # SSE comment — ignored by EventSource
        last_sent = time.monotonic()
```

---

## 9. Lifespan Wiring

Resolves PLAN.md §13.1 item 5 — DB init must happen at startup, not on first request,
because the market source needs the seeded watchlist before any request arrives.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()                                   # create + seed if empty
    app.state.price_cache = PriceCache()

    tickers = get_tracked_tickers()                   # watchlist ∪ held positions (item 10)
    app.state.market = await create_market_data_source(app.state.price_cache)
    await app.state.market.start(tickers)

    app.state.snapshots = asyncio.create_task(portfolio_snapshot_loop(app.state))
    yield
    app.state.snapshots.cancel()
    await app.state.market.stop()
```

`get_tracked_tickers()` returns **watchlist ∪ held positions**, per PLAN.md §13.2 item 10 —
removing a ticker from the watchlist must not stop pricing a position you still hold, or the
heatmap tile and total portfolio value go stale.

---

## 10. Module Layout

```
backend/app/market/
├── __init__.py           # public API (unchanged surface)
├── models.py             # PriceUpdate (+ open_price), PricePoint, SourceStatus
├── cache.py              # PriceCache — unchanged
├── interface.py          # MarketDataSource ABC (+ describe, get_history)
├── factory.py            # async create_market_data_source() — capability-driven
├── capabilities.py       # NEW — probe_capabilities(), MassiveCapabilities
├── simulator.py          # GBMSimulator + SimulatorDataSource (+ seed_overrides)
├── anchored.py           # NEW — AnchoredSimulatorDataSource
├── massive_client.py     # MassiveDataSource — corrected per §7
├── seed_prices.py        # fallback seeds; no longer the primary price source
└── stream.py             # SSE router (+ heartbeat)
```

## 11. Testing

The existing 73 tests stay green; `PriceUpdate.direction` → `tick_direction` and the new
`open_price` argument are the only signature churn.

New coverage:

- `probe_capabilities` against stubbed clients for all five key states in §1 — especially
  that a `NOT_AUTHORIZED` snapshot plus a successful `get_previous_close_agg` classifies as
  end-of-day rather than invalid.
- Factory selection: each of the five states returns the expected class and never raises.
- `AnchoredSimulatorDataSource._fetch_anchors` walks back over a weekend (empty `bars` for
  Saturday and Sunday, populated for Friday) and falls back cleanly after 7 failures.
- `MassiveDataSource` nanosecond conversion — assert a `sip_timestamp` of
  `1605192894630916600` yields `1605192894.63`, not a date in the year 52000.
- Rate-limit classification: `MaxRetryError` is handled distinctly from `BadResponse`.
- Heartbeat: `_generate_events` emits `": ping"` when the cache version is static for >10 s
  (this is the unit test PLAN.md §13.4 item 31 wants in place of the Playwright case).

Every Massive test stubs the client. No test hits the network — the free tier's 5 calls per
minute would make a real-network suite unrunnable in CI anyway.

---

## 12. Open Questions

1. **Is the anchored simulator honest enough?** It shows real price *levels* with synthetic
   *movement*, clearly badged "SIMULATED". The alternative — displaying yesterday's frozen
   closes — is more truthful and completely lifeless. This design picks the badged
   simulation. Worth an explicit sign-off.
2. **Re-anchor cadence.** Hourly is proposed. A container left running for days on a static
   anchor slowly drifts from reality; hourly re-anchoring costs 24 calls/day out of 7,200.
3. **Should a Starter/Developer key (15-min delay) be badged?** It is real market data, just
   late. Suggest `live: True` with `detail` naming the delay, rather than a "SIMULATED" badge.
