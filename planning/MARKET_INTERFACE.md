# Market Data Interface — Unified Design

Defines the single abstract interface (`MarketDataSource`, per PLAN.md §6) that both the Massive API client and the built-in simulator implement, so that price-cache and SSE code (and everything downstream) is agnostic to the source. Backed by research in `MASSIVE_API.md`; simulator internals are detailed in `MARKET_SIMULATOR.md`.

## 1. Selection Rule

```python
def build_market_data_source() -> "MarketDataSource":
    if os.environ.get("MASSIVE_API_KEY"):
        return MassiveMarketDataSource(api_key=os.environ["MASSIVE_API_KEY"])
    return SimulatorMarketDataSource()
```

- `MASSIVE_API_KEY` set and non-empty → `MassiveMarketDataSource`
- Absent/empty → `SimulatorMarketDataSource` (default, no external dependency)

This selection happens once at process startup; the chosen source runs as the single background task described in PLAN.md §6 ("Shared Price Cache").

## 2. Shared Data Model

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class PriceTick:
    ticker: str
    price: float          # latest trade price
    previous_close: float # prior session close, for daily % change
    timestamp: float       # unix seconds (epoch), UTC
```

Both implementations produce `PriceTick` objects in this exact shape — this is the contract the price cache and SSE stream depend on. Neither the simulator's GBM internals nor Massive's snapshot JSON shape leak past this boundary.

## 3. Abstract Interface

```python
from abc import ABC, abstractmethod
from typing import Iterable

class MarketDataSource(ABC):
    @abstractmethod
    async def start(self, tickers: Iterable[str]) -> None:
        """Begin producing price updates for the given tickers (initial watchlist)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background update loop and release any resources."""

    @abstractmethod
    def watch(self, ticker: str) -> None:
        """Add a ticker to the set of symbols being updated (e.g. added to watchlist, or a position opened on a delisted-from-watchlist ticker)."""

    @abstractmethod
    def unwatch(self, ticker: str) -> None:
        """Stop updating a ticker once nothing references it (removed from watchlist and no open position)."""

    @abstractmethod
    def latest(self, ticker: str) -> PriceTick | None:
        """Return the most recent PriceTick for a ticker, or None if not yet available."""

    @abstractmethod
    def subscribe(self) -> "AsyncIterator[PriceTick]":
        """Async stream of PriceTick updates as they occur, for the SSE endpoint to consume."""
```

Both `MassiveMarketDataSource` and `SimulatorMarketDataSource` (§4/§5 below) implement this exact interface.

## 4. `MassiveMarketDataSource`

- `start()` launches an `asyncio` polling loop calling the Massive **Full Market Snapshot** endpoint (`MASSIVE_API.md` §3.1) once per tick with the comma-joined set of currently-watched tickers.
- Poll interval: env-configurable (`MASSIVE_POLL_INTERVAL_SECONDS`, default `15` to respect the free-tier 5 req/min limit; lower for paid tiers).
- Each response is parsed into one `PriceTick` per ticker: `price = lastTrade.p`, `previous_close = prevDay.c`, `timestamp = updated / 1e9` (Massive returns nanoseconds).
- `watch()`/`unwatch()` mutate the in-memory ticker set consulted on the next tick — no immediate network call, to stay within rate limits. (Exception: consider an immediate single-ticker snapshot call, `MASSIVE_API.md` §3.2, when a brand-new ticker is added, so its first price isn't delayed a full poll interval — flagged here as a possible enhancement, not required for v1.)
- On HTTP error or 429: log and skip the tick, keep serving the last cached `PriceTick` per ticker (never fall back to the simulator mid-session — see `MASSIVE_API.md` §4).
- Unknown/invalid tickers: if a requested ticker is absent from the response, mark it `None` in `latest()` rather than raising.

```python
class MassiveMarketDataSource(MarketDataSource):
    def __init__(self, api_key: str, poll_interval: float = 15.0):
        self._api_key = api_key
        self._poll_interval = poll_interval
        self._tickers: set[str] = set()
        self._cache: dict[str, PriceTick] = {}
        self._subscribers: list[asyncio.Queue[PriceTick]] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers):
        self._tickers = set(tickers)
        self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self):
        async with httpx.AsyncClient(base_url="https://api.massive.com") as client:
            while True:
                if self._tickers:
                    await self._poll_once(client)
                await asyncio.sleep(self._poll_interval)

    async def _poll_once(self, client):
        try:
            resp = await client.get(
                "/v2/snapshot/locale/us/markets/stocks/tickers",
                params={"tickers": ",".join(sorted(self._tickers))},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
            resp.raise_for_status()
        except httpx.HTTPError:
            return  # skip this tick, keep last known prices
        for entry in resp.json().get("tickers", []):
            tick = PriceTick(
                ticker=entry["ticker"],
                price=entry["lastTrade"]["p"],
                previous_close=entry["prevDay"]["c"],
                timestamp=entry["updated"] / 1e9,
            )
            self._cache[tick.ticker] = tick
            self._publish(tick)
```

## 5. `SimulatorMarketDataSource`

- `start()` launches an `asyncio` loop ticking every ~500ms (PLAN.md §6), generating `PriceTick`s via GBM per `MARKET_SIMULATOR.md`.
- `watch()`/`unwatch()` add/remove a ticker from the simulated set immediately, seeding new tickers from `SEED_PRICES` (or a synthesized default if unrecognized — see `MARKET_SIMULATOR.md` §5).
- No network calls, no rate limits, no failure mode to handle.

## 6. Price Cache & SSE Integration

- Whichever `MarketDataSource` is active, the same cache/SSE code in the backend calls `latest()` for REST responses (`/api/watchlist`, `/api/portfolio`) and `subscribe()` to fan out `PriceTick`s to connected `/api/stream/prices` clients.
- SSE event payload is `PriceTick` serialized to JSON plus a derived `direction` field (`"up" | "down" | "flat"`, computed by comparing to the previously cached price before overwriting).

## 7. Watchlist / Position Lifecycle Hooks

Per the open question in `planning/REVIEW.md` about deleting a watched ticker with an open position: the backend calls `source.watch(ticker)` whenever a ticker is added to the watchlist **or** a position is opened, and `source.unwatch(ticker)` only when a ticker is both off the watchlist **and** has zero position quantity. This keeps the `MarketDataSource` implementations simple (they just track a flat ticker set) while the lifecycle policy lives in the backend layer that calls them.
