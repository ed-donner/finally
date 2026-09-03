# Market Data Backend — Implementation Design

**Status:** Implementation-ready design. Consolidates and supersedes the market-data portion
of [`PLAN.md`](PLAN.md) §6, and turns the three research documents into code you can type in.

| Document | Role | Relationship to this one |
|---|---|---|
| [`MARKET_INTERFACE.md`](MARKET_INTERFACE.md) | Architecture: three sources, one interface | This document is its implementation |
| [`MARKET_SIMULATOR.md`](MARKET_SIMULATOR.md) | Measured review of the GBM model | Supplies §7's calibration constants |
| [`MASSIVE_API.md`](MASSIVE_API.md) | Verified API/entitlement research | Supplies §6 and §9's endpoint facts |

Everything here targets the code already in `backend/app/market/`. Sections are ordered so
that each one only depends on the ones above it — implement top to bottom and the tree
compiles at every step.

---

## 1. What This Design Delivers

Three data sources behind one interface, selected by what the running key can *actually do*
rather than by whether an environment variable is non-empty:

```
                        MASSIVE_API_KEY set?
                               │
               ┌───────────────┴────────────────┐
              no                               yes
               │                                │
               │                   probe_capabilities()   ← 2 calls, once, at startup
               │                                │
               │          ┌─────────────────────┼──────────────────────┐
               │     realtime/delayed       end-of-day               invalid
               │          │                     │                      │
               ▼          ▼                     ▼                      ▼
       SimulatorDataSource  MassiveDataSource  AnchoredSimulator  SimulatorDataSource
       (synthetic seeds)    (real prices)      (real levels,      (+ reason in /health)
                                                synthetic motion)
               │          │                     │                      │
               └──────────┴──────────┬──────────┴──────────────────────┘
                                     ▼
                                 PriceCache          ← single read path, source-agnostic
                                     │
             ┌───────────────────────┼───────────────────────┐
        SSE /api/stream/prices  portfolio valuation     trade pricing
```

### Non-negotiable invariants

1. **The app always boots into a moving terminal.** Every branch of §7's factory returns a
   source that produces ticking prices. A missing, free, or revoked key degrades the *source*;
   it never blanks the screen.
2. **Simulated prices are never presented as live.** `SourceStatus.live` is `False` for both
   simulator flavours, `/api/health` says so in words, and the frontend badges it.
3. **One API call per cycle, never one per ticker.** The free tier's budget is 5 calls/minute
   (`MASSIVE_API.md` §7); per-ticker fetching cannot price a 10-symbol watchlist even once.
4. **Everything downstream of `PriceCache` is source-agnostic.** No route, no valuation, no
   SSE handler ever imports `MassiveDataSource` or `SimulatorDataSource`.
5. **The SDK is synchronous.** Every Massive call is wrapped in `asyncio.to_thread(...)`, or
   it stalls the event loop and freezes SSE for every connected client.

---

## 2. Module Layout and Change Map

```
backend/app/market/
├── __init__.py           MODIFIED  export SourceStatus, PricePoint, capabilities
├── models.py             MODIFIED  + open_price, tick_direction, PricePoint, SourceStatus, time helpers
├── cache.py              MODIFIED  + open_price parameter (one behavioural change)
├── interface.py          MODIFIED  + describe(), + get_history() with a default
├── capabilities.py       NEW       probe_capabilities() / MassiveCapabilities
├── factory.py            REWRITTEN async, capability-driven, five key states
├── seed_prices.py        MODIFIED  refreshed closes, LOW_PRICE_SIGMA_FLOOR
├── simulator.py          MODIFIED  decaying shocks, deterministic seeds, history, describe()
├── anchored.py           NEW       AnchoredSimulatorDataSource
├── massive_client.py     REWRITTEN ns timestamps, open_price, error classification, backoff
└── stream.py             MODIFIED  + heartbeat
```

Effort estimate: `models`/`cache`/`interface` are an hour; `simulator.py` is the largest
single change; `massive_client.py` is a rewrite but a small one; `anchored.py` is ~120 lines.

---

## 3. Shared Models — `app/market/models.py`

Three additions to the shipped file: the `open_price` baseline (`PLAN.md` §13.1 item 1), the
`PricePoint`/`SourceStatus` records the interface needs, and the ISO-8601 wire conversion
(`PLAN.md` §13.1 item 6).

```python
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

# --- Time conversion -------------------------------------------------------
# Massive is inconsistent: aggregate bars carry milliseconds, snapshots and
# last-trade prints carry NANOseconds (MASSIVE_API.md §6). Convert once, here,
# at the boundary. Internally everything is float epoch seconds; on the wire
# everything is ISO 8601 UTC.

_UNIT_DIVISOR = {"s": 1.0, "ms": 1e3, "us": 1e6, "ns": 1e9}


def to_epoch_seconds(value: int | float, unit: str) -> float:
    """Convert a Massive timestamp to float epoch seconds. `unit` is explicit on purpose."""
    return float(value) / _UNIT_DIVISOR[unit]


def epoch_to_iso(epoch_seconds: float) -> str:
    """Float epoch seconds -> '2026-09-03T17:42:11.413700Z'."""
    return (
        datetime.fromtimestamp(epoch_seconds, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class PricePoint:
    """One point of a historical series, as served by MarketDataSource.get_history()."""

    timestamp: str  # ISO 8601 UTC — already wire-format
    price: float

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "price": self.price}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Introspection for GET /api/health. Produced by MarketDataSource.describe()."""

    name: str  # "simulator" | "massive" | "anchored-simulator"
    live: bool  # True only when prices reflect the real current market
    detail: str  # human-readable, surfaced verbatim in /api/health
    tickers: int
    cache_populated: bool

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "live": self.live,
            "detail": self.detail,
            "tickers": self.tickers,
            "cache_populated": self.cache_populated,
        }


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time.

    Two distinct baselines, deliberately named so they cannot be confused
    (PLAN.md §13.1 item 1):

      previous_price -> the PREVIOUS TICK. Drives the flash animation only.
      open_price     -> the session open / anchor, fixed for the session.
                        Drives the "daily change %" column.
    """

    ticker: str
    price: float
    previous_price: float
    open_price: float
    timestamp: float = field(default_factory=time.time)  # epoch seconds, internal only

    # --- tick-scale: flash animation ---
    @property
    def tick_direction(self) -> str:
        """'up' | 'down' | 'flat' — the CSS flash class."""
        if self.price > self.previous_price:
            return "up"
        if self.price < self.previous_price:
            return "down"
        return "flat"

    # --- session-scale: the watchlist's daily column ---
    @property
    def change_today(self) -> float:
        return round(self.price - self.open_price, 4)

    @property
    def change_percent_today(self) -> float:
        if self.open_price == 0:
            return 0.0
        return round((self.price - self.open_price) / self.open_price * 100, 4)

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE. Timestamp becomes ISO 8601 UTC here."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "open_price": self.open_price,
            "timestamp": epoch_to_iso(self.timestamp),
            "tick_direction": self.tick_direction,
            "change_today": self.change_today,
            "change_percent_today": self.change_percent_today,
        }
```

### Wire format

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

### Where `open_price` comes from

| Source | `open_price` |
|---|---|
| `MassiveDataSource` | `snap.prev_day.close` (real previous close) |
| `AnchoredSimulatorDataSource` | the real close fetched once at startup |
| `SimulatorDataSource` | the seed price the simulation started from |

Fixed for the session in all three, so the daily % accumulates over minutes and hours
instead of resetting every 500 ms.

### Breaking changes to announce

`direction` → `tick_direction`, and `change`/`change_percent` (tick-scale) are **removed** in
favour of `change_today`/`change_percent_today` (session-scale). Nothing but tests consumes
them today; the frontend has not been written yet, so this is the moment to make the change.

---

## 4. Price Cache — `app/market/cache.py`

The shipped cache is correct and mostly untouched: keep the `threading.Lock` (writers are
`asyncio.to_thread` worker threads, so a threading lock is right, not an asyncio one) and
keep the monotonic `version` counter that drives SSE change detection.

One behavioural change — `open_price` is **sticky**:

```python
    def update(
        self,
        ticker: str,
        price: float,
        *,
        open_price: float | None = None,
        timestamp: float | None = None,
    ) -> PriceUpdate:
        """Record a new price. Returns the created PriceUpdate.

        open_price semantics (sticky):
          - first write for a ticker : open_price or price
          - later writes, None       : keep whatever the ticker already had
          - later writes, a value    : overwrite (re-anchoring, new session)

        This is what lets a 500ms tick loop pass open_price=None forever while
        the daily-change baseline stays fixed for the whole session.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            prev = self._prices.get(ticker)

            previous_price = prev.price if prev else round(price, 2)
            if open_price is not None:
                resolved_open = round(open_price, 2)
            elif prev is not None:
                resolved_open = prev.open_price
            else:
                resolved_open = round(price, 2)

            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=previous_price,
                open_price=resolved_open,
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update
```

`timestamp` and `open_price` are keyword-only so no existing positional call can silently
land in the wrong slot. Note `timestamp if timestamp is not None` rather than the shipped
`timestamp or time.time()` — the latter treats a legitimate `0.0` as absent.

Rounding rule (`PLAN.md` §13.4 item 16): **full float precision in the model, rounded to 2dp
at the cache boundary.** The simulator keeps its unrounded price internally and never rounds
in place, or a drift bias accumulates over tens of thousands of ticks.

`get`, `get_all`, `get_price`, `remove`, `version`, `__len__`, `__contains__` are unchanged.

---

## 5. The Interface — `app/market/interface.py`

Keep the ABC exactly as shipped and add two members.

```python
class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.
    """

    # --- lifecycle: unchanged from the shipped interface ---
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
        """Introspection for GET /api/health.

        MUST NOT raise and MUST NOT do I/O — it is called from a request handler
        and its entire job is to still work when the source is broken.
        """

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        """Historical series for a chart's first paint.

        Concrete default rather than an abstractmethod: a source that has no
        history is a working source. Returning [] makes the frontend fall back
        to accumulating from SSE, exactly as PLAN.md §10 describes.
        """
        return []
```

`get_history` on the interface is the better form of the ring buffer that `PLAN.md` §13.2
item 15 recommends: the simulator serves its own deque, Massive-backed sources serve genuine
intraday minute bars, and the frontend calls one endpoint without knowing which it got.

---

## 6. Capability Probe — `app/market/capabilities.py` (new)

The single most consequential research finding (`MASSIVE_API.md` §4): **a free Massive key
authenticates successfully and then refuses to return any live price.** Snapshots, last
trade, and anything dated today are all `NOT_AUTHORIZED` on the Basic tier. So the presence
of `MASSIVE_API_KEY` tells you almost nothing:

| Key state | What actually works | Source chosen (§7) |
|---|---|---|
| Absent | nothing | `SimulatorDataSource` |
| Present, Basic (free) | historical bars, yesterday's closes; **no live price** | `AnchoredSimulatorDataSource` |
| Present, Starter/Developer | snapshots, 15-minute delayed | `MassiveDataSource` (15 s poll) |
| Present, Advanced | snapshots, real time | `MassiveDataSource` (5 s poll) |
| Present, invalid/revoked | nothing | `SimulatorDataSource` + reason |

Establish this once at startup, with two calls, instead of discovering it through a silent
stream of swallowed exceptions.

```python
"""Detect what a Massive API key is actually entitled to.

Two calls, run once from the factory at startup. Costs 2 of the free tier's
5-per-minute budget and is immutable for the process lifetime.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import urllib3.exceptions
from massive import RESTClient
from massive.exceptions import AuthError, BadResponse

from .models import to_epoch_seconds

logger = logging.getLogger(__name__)

# A snapshot print older than this means the plan serves delayed data.
DELAYED_THRESHOLD_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class MassiveCapabilities:
    """What a given API key is actually allowed to do."""

    valid: bool  # the key authenticates at all
    realtime: bool  # snapshot / last-trade endpoints are entitled
    end_of_day: bool  # aggregate endpoints are entitled
    detail: str  # human-readable, ends up in /api/health
    delay_seconds: float | None = None  # observed quote age when realtime is True

    @property
    def poll_interval(self) -> float:
        """Snapshot poll cadence implied by the plan (MARKET_INTERFACE.md §5).

        Delayed plans have unlimited call budgets but 15-minute-old data, so
        polling faster than 15s buys nothing.
        """
        if self.delay_seconds and self.delay_seconds > DELAYED_THRESHOLD_SECONDS:
            return 15.0
        return 5.0


def probe_capabilities(api_key: str) -> MassiveCapabilities:
    """Classify a key into one of the five states above. Never raises.

    SYNCHRONOUS — the Massive SDK is urllib3-based. Call it as
    `await asyncio.to_thread(probe_capabilities, api_key)`.
    """
    try:
        # retries=0 is essential: the SDK's default retries=3 backs off at
        # 0.0/0.2/0.4s, i.e. three more requests inside the same 60s window
        # that just rejected us (MASSIVE_API.md §7).
        client = RESTClient(api_key=api_key, retries=0, read_timeout=5.0)
    except AuthError:
        return MassiveCapabilities(False, False, False, "no API key configured")

    # 1. Cheapest possible entitlement test for live data.
    try:
        snapshots = client.get_snapshot_all(market_type="stocks", tickers=["AAPL"])
        delay = _observed_delay(snapshots)
        if delay is not None and delay > DELAYED_THRESHOLD_SECONDS:
            detail = f"delayed snapshots entitled (~{delay / 60:.0f} min behind)"
        else:
            detail = "real-time snapshots entitled"
        return MassiveCapabilities(True, True, True, detail, delay_seconds=delay)
    except BadResponse as e:
        if "NOT_AUTHORIZED" not in str(e):
            return MassiveCapabilities(False, False, False, f"unexpected response: {e}")
        # fall through — entitlement, not a bad key
    except urllib3.exceptions.MaxRetryError as e:
        # Rate limited or unreachable. MaxRetryError is NOT a massive.exceptions
        # subclass, so `except BadResponse` alone misses it entirely.
        return MassiveCapabilities(False, False, False, f"unreachable or rate-limited: {e}")
    except Exception as e:  # noqa: BLE001 - the probe must never take the app down
        return MassiveCapabilities(False, False, False, f"probe failed: {e}")

    # 2. Snapshots refused. Valid key on a lower plan, or a dead key?
    try:
        client.get_previous_close_agg("AAPL")
        return MassiveCapabilities(True, False, True, "end-of-day only (Basic tier)")
    except Exception as e:  # noqa: BLE001
        return MassiveCapabilities(False, False, False, f"key rejected: {e}")


def _observed_delay(snapshots) -> float | None:
    """Age of the newest last-trade print, in seconds. None if unreadable.

    Distinguishes Advanced (real time) from Starter/Developer (15 min delayed)
    without a second API call. sip_timestamp is NANOseconds (MASSIVE_API.md §6).
    """
    try:
        newest = max(
            to_epoch_seconds(s.last_trade.sip_timestamp, "ns")
            for s in snapshots
            if getattr(s, "last_trade", None) is not None
        )
    except (AttributeError, TypeError, ValueError):
        return None
    return max(0.0, time.time() - newest)
```

Two rejection messages exist and the difference is diagnostic gold (`MASSIVE_API.md` §4):
*"You are not entitled to this data"* means the **endpoint** is out of plan; *"Your plan
doesn't include this data timeframe"* means the endpoint is fine but the **date** is too
recent. Both contain `NOT_AUTHORIZED`, which is why the probe matches on that substring.

---

## 7. Source Selection — `app/market/factory.py` (rewritten)

```python
"""Factory for creating market data sources."""

from __future__ import annotations

import asyncio
import logging
import os

from .anchored import AnchoredSimulatorDataSource
from .cache import PriceCache
from .capabilities import probe_capabilities
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


async def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Select a market data source. Never raises; always returns a working source.

    Async because the capability probe makes real network calls. Called exactly
    once, from the FastAPI lifespan handler. Returns an UNSTARTED source — the
    caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if not api_key:
        logger.info("No MASSIVE_API_KEY — using the GBM simulator")
        return SimulatorDataSource(price_cache)

    caps = await asyncio.to_thread(probe_capabilities, api_key)

    if caps.realtime:
        logger.info("Massive: %s — polling every %.0fs", caps.detail, caps.poll_interval)
        return MassiveDataSource(
            api_key=api_key,
            price_cache=price_cache,
            poll_interval=caps.poll_interval,
            capability_detail=caps.detail,
        )

    if caps.end_of_day:
        logger.warning(
            "Massive key is end-of-day only (Basic tier). Anchoring the simulator to "
            "real closing prices — displayed prices are SIMULATED, not live."
        )
        return AnchoredSimulatorDataSource(api_key=api_key, price_cache=price_cache)

    logger.error("MASSIVE_API_KEY unusable (%s) — falling back to the simulator", caps.detail)
    return SimulatorDataSource(price_cache, status_detail=f"Massive key unusable: {caps.detail}")
```

Three rules encoded here, in order of importance:

1. **Never boot into a broken state.** Every branch produces moving prices.
2. **Never silently mislead.** Simulated prices carry `live=False` all the way to
   `/api/health` and the frontend's "SIMULATED" badge.
3. **Probe once.** Two calls at startup, not two per poll.

### Poll intervals

| Source | Cadence | Why |
|---|---|---|
| `SimulatorDataSource` | 500 ms | local computation, no budget (`PLAN.md` §6) |
| `AnchoredSimulatorDataSource` | 500 ms tick, **1** anchor fetch at startup (+ hourly re-anchor) | GBM is local; anchors are one grouped-daily call |
| `MassiveDataSource`, Advanced | 5 s | unlimited calls; 5 s is plenty for a terminal |
| `MassiveDataSource`, Starter/Developer | 15 s | the data is 15 minutes old anyway |

---

## 8. The Simulator — `app/market/simulator.py`

For nearly every student running this project the simulator **is** the product: no key and a
free key both land here. `MARKET_SIMULATOR.md` measured the shipped implementation; this
section is the corrected code.

### 8.1 The model, and why the calibration is already right

```
S(t+Δt) = S(t) · exp[ (μ − σ²/2)·Δt + σ·√Δt·Z ]
```

`Δt = 0.5 / (252 × 6.5 × 3600) ≈ 8.48 × 10⁻⁸`. One trading day is 46,800 ticks and
46,800 × Δt = 1/252 exactly, so a simulated day reproduces the target daily volatility by
construction — verified at `1.3425%` realised against a `1.3859%` target over 200 one-day
AAPL runs (`MARKET_SIMULATOR.md` §2). **Do not touch `DEFAULT_DT` or the Itô `−σ²/2` term.**

### 8.2 The one real bug: shocks are permanent level shifts

Shipped code applies `self._prices[ticker] *= 1 + shock` at `p = 0.001` per ticker per tick.
That is 7.2 permanent 2–5% jumps per ticker per hour — a random walk of shocks layered on
GBM, which inflates realised volatility ~20× and makes every per-ticker σ decorative
(measured: 10.02% one-hour return sd against a 0.505% pure-GBM baseline).

The fix is a **decaying overlay**: the underlying price keeps following calibrated GBM, and
the shock is a separate transient component added on top of the *displayed* price.

```python
@dataclass
class Shock:
    """A transient, mean-reverting price dislocation.

    Real intraday spikes substantially revert. Modelling the shock as a decaying
    overlay keeps the drama visible on the chart while leaving the long-run
    distribution governed by sigma.
    """

    magnitude: float  # signed, e.g. -0.03
    decay: float = 0.985  # per-tick multiplier -> half-life ~46 ticks (~23s)
```

```python
    def step(self) -> dict[str, float]:
        """Advance every ticker one time step. Returns {ticker: displayed_price}.

        Hot path — runs every 500ms. One vectorised normal draw for all tickers,
        not one per ticker.
        """
        n = len(self._tickers)
        if n == 0:
            return {}

        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        result: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            params = self._params[ticker]
            mu, sigma = params["mu"], params["sigma"]

            # 1. GBM evolves the underlying price. Full precision, never rounded
            #    in place — rounding here accumulates a drift bias over 10k+ ticks.
            drift = (mu - 0.5 * sigma**2) * self._dt
            diffusion = sigma * math.sqrt(self._dt) * z[i]
            self._prices[ticker] *= math.exp(drift + diffusion)

            # 2. Shocks are a separate, decaying overlay on the DISPLAYED price.
            if random.random() < self._event_prob:
                self._shocks[ticker] = Shock(
                    magnitude=random.uniform(0.015, 0.04) * random.choice([-1, 1])
                )
                logger.debug("Shock on %s: %+.2f%%", ticker, self._shocks[ticker].magnitude * 100)

            shock = self._shocks.get(ticker)
            if shock is not None:
                displayed = self._prices[ticker] * (1 + shock.magnitude)
                shock.magnitude *= shock.decay
                if abs(shock.magnitude) < 1e-4:
                    del self._shocks[ticker]
            else:
                displayed = self._prices[ticker]

            result[ticker] = round(displayed, 2)

        return result
```

Paired with `event_probability = 1e-4` (down from `1e-3`), a 10-ticker watchlist sees about
**1.2 events per 10-minute demo** — enough that something happens while the user is watching,
without a shock every 50 seconds.

### 8.3 Deterministic seeds for unknown tickers

`PLAN.md` §13.2 item 8 asks what happens when a user — or the LLM's `watchlist_changes` —
requests a symbol the simulator has never heard of. **Answer: synthesise, never reject.** The
demo must not dead-end on a typo. But the shipped `random.uniform(50.0, 300.0)` gives `ZZZZ`
a different price on every restart, so a held position's cost basis jumps between container
restarts and the P&L chart lies.

```python
def _synthesize_seed(ticker: str) -> float:
    """Stable pseudo-price for an unknown symbol. Same ticker, same price, always."""
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    return round(20.0 + (h % 48_000) / 100.0, 2)  # $20.00 - $500.00
```

Seed resolution order: **`seed_overrides` → `SEED_PRICES` → `_synthesize_seed`.** Unknown
tickers take `DEFAULT_PARAMS` (σ = 0.25, μ = 0.05) and cross-group correlation.

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8

    def __init__(
        self,
        tickers: list[str],
        dt: float = DEFAULT_DT,
        event_probability: float = 1e-4,  # was 1e-3 — see §8.2
        seed_overrides: dict[str, float] | None = None,  # real closes when available
    ) -> None:
        self._dt = dt
        self._event_prob = event_probability
        self._seed_overrides = {k.upper(): v for k, v in (seed_overrides or {}).items()}
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._shocks: dict[str, Shock] = {}
        self._retired: dict[str, float] = {}  # price kept across remove/re-add
        self._cholesky: np.ndarray | None = None

        for ticker in tickers:
            self._add_ticker_internal(ticker)
        self._rebuild_cholesky()

    def _add_ticker_internal(self, ticker: str) -> None:
        """Add without rebuilding Cholesky — used for batch initialisation."""
        if ticker in self._prices:
            return
        seed = (
            self._retired.pop(ticker, None)  # re-added: resume where it left off
            or self._seed_overrides.get(ticker)
            or SEED_PRICES.get(ticker)
            or _synthesize_seed(ticker)
        )
        self._tickers.append(ticker)
        self._prices[ticker] = seed
        self._params[ticker] = self._resolve_params(ticker, seed)

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        # Remember the price: a position you still hold must not jump when its
        # watchlist row is toggled off and back on.
        self._retired[ticker] = self._prices.pop(ticker)
        self._tickers.remove(ticker)
        self._params.pop(ticker, None)
        self._shocks.pop(ticker, None)
        self._rebuild_cholesky()
```

Sub-$20 names are the one liveness cliff: per-tick sd is `S·σ·√Δt`, so below roughly $20 a
tick rounds to no visible change. NFLX at 82.73 is the measured floor case at 58% visible
ticks — fine. Floor σ for anything cheaper:

```python
# seed_prices.py
LOW_PRICE_THRESHOLD = 20.0   # below this, a 2dp display quantum eats most ticks
LOW_PRICE_SIGMA_FLOOR = 0.35
```

```python
    def _resolve_params(self, ticker: str, seed: float) -> dict[str, float]:
        params = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))
        if seed < LOW_PRICE_THRESHOLD:
            params["sigma"] = max(params["sigma"], LOW_PRICE_SIGMA_FLOOR)
        return params
```

### 8.4 Correlation, and the 500 that is waiting to happen

Independent tickers read as fake within seconds. The correlation matrix (tech ∩ tech 0.6,
finance ∩ finance 0.5, TSLA and cross-sector 0.3) is applied via Cholesky: draw `n`
independent normals, multiply by `L` where `L Lᵀ = C`.

The structure is a block matrix of equicorrelated groups, so its minimum eigenvalue is pinned
at `1 − ρ_max = 0.4` regardless of size — verified positive-definite to n = 110. But
`np.linalg.cholesky` raises `LinAlgError` on a non-PD matrix and `_rebuild_cholesky` is
called from `add_ticker`, so the failure mode is a **user-facing 500 on a watchlist add**.
Three lines make that impossible:

```python
    def _rebuild_cholesky(self) -> None:
        """Rebuild the Cholesky factor of the correlation matrix.

        O(n^2), called only on watchlist edits — never in the tick loop.
        """
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return

        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho

        try:
            self._cholesky = np.linalg.cholesky(corr)
        except np.linalg.LinAlgError:
            # Degrade to uncorrelated moves rather than 500 a watchlist add.
            logger.warning("Correlation matrix not positive-definite at n=%d — using identity", n)
            self._cholesky = None
```

### 8.5 History: ring buffer plus prefill

`PLAN.md` §13.2 item 15 — charts are empty on first paint and a refresh throws away
everything accumulated from SSE. A bounded deque fixes it for ~480 KB at 50 tickers.

```python
class SimulatorDataSource(MarketDataSource):
    HISTORY_POINTS = 600  # 600 ticks x 500ms = 5 minutes per ticker

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 1e-4,
        seed_overrides: dict[str, float] | None = None,
        status_detail: str | None = None,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        self._event_prob = event_probability
        self._seed_overrides = {k.upper(): v for k, v in (seed_overrides or {}).items()}
        self._status_detail = status_detail
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None
        self._opens: dict[str, float] = {}  # session anchors, fixed after start
        self._history: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.HISTORY_POINTS)
        )
```

**Prefill so the first paint is a chart, not a dot.** Step the model forward
`HISTORY_POINTS` times with no sleeping, back-date the timestamps, and keep the final price
as the current one — so the history joins the live series continuously instead of jumping:

```python
    def _prefill_history(self) -> None:
        """Generate ~5 minutes of plausible history before the first tick.

        Costs about half a second of CPU. Timestamps are back-dated so the
        prefilled path ends exactly at the current price — no discontinuity
        where synthetic history meets the live stream.
        """
        assert self._sim is not None
        now = time.time()
        start = now - self.HISTORY_POINTS * self._interval
        for i in range(self.HISTORY_POINTS):
            ts = start + i * self._interval
            for ticker, price in self._sim.step().items():
                self._history[ticker].append((ts, price))

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(
            tickers=tickers,
            event_probability=self._event_prob,
            seed_overrides=self._seed_overrides,
        )
        # The seed price IS the session open — capture it before any stepping.
        self._opens = {t: p for t in tickers if (p := self._sim.get_price(t)) is not None}

        self._prefill_history()

        # Seed the cache so SSE has data on the very first connection.
        for ticker, open_price in self._opens.items():
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price, open_price=open_price)

        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(tickers))

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        series = self._history.get(ticker.upper(), ())
        return [PricePoint(epoch_to_iso(t), p) for t, p in list(series)[-points:]]
```

### 8.6 The tick loop

```python
    async def _run_loop(self) -> None:
        """Step the simulation, write to the cache, record history, sleep."""
        while True:
            try:
                if self._sim:
                    now = time.time()
                    for ticker, price in self._sim.step().items():
                        # open_price=None -> the cache keeps the sticky anchor (§4)
                        self._cache.update(ticker=ticker, price=price, timestamp=now)
                        self._history[ticker].append((now, price))
            except Exception:
                # NEVER let one bad tick kill the task: a raise here would end the
                # background loop and freeze every price with no error path.
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

`asyncio.sleep(interval)` *after* the work means the true period is `interval + work`, so
ticks drift slightly slower than 500 ms. This is invisible on screen — do not add a
compensating scheduler.

### 8.7 `add_ticker`, `remove_ticker`, `describe`

```python
    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if not self._sim:
            return
        self._sim.add_ticker(ticker)  # synthesises a seed if unknown — never dead-ends
        price = self._sim.get_price(ticker)
        if price is not None:
            self._opens.setdefault(ticker, price)
            self._cache.update(ticker=ticker, price=price, open_price=self._opens[ticker])
        logger.info("Simulator: added ticker %s", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if self._sim:
            self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)
        # History is deliberately KEPT: re-adding a ticker restores its chart.
        logger.info("Simulator: removed ticker %s", ticker)

    def describe(self) -> SourceStatus:
        return SourceStatus(
            name="simulator",
            live=False,
            detail=self._status_detail or "GBM simulation from static seed prices",
            tickers=len(self._sim.get_tickers()) if self._sim else 0,
            cache_populated=len(self._cache) > 0,
        )
```

`GBMSimulator.remove_ticker` should retain the price in a `_retired` dict so that
remove-then-add restores the price the ticker had rather than resetting to seed — a position
you still hold must not jump when its watchlist row is toggled.

### 8.8 Keep the two-class split

`GBMSimulator` (pure, synchronous, no I/O, no cache reference) and `SimulatorDataSource` (the
async adapter) is the best structural decision in the shipped code. It is what lets the
statistical tests in §15 run tens of thousands of steps per second with no event loop. **Keep
the model free of the transport.**

### 8.9 Refresh the static seed table

`SEED_PRICES` has rotted badly — NVDA seeded at 800.00 against a real 224.41 (3.6× high, post
splits), NFLX at 600.00 against 82.73 (7.3×). Replace with the verified 2026-09-02 closes
from `MASSIVE_API.md` §5.1, and note in a comment that this table is now only the **no-key
fallback**; §9's anchoring removes the maintenance burden whenever a key is present.

```python
# Real closes, 2026-09-02 (MASSIVE_API.md §5.1). Fallback only — any Massive key
# anchors to live closes instead (see anchored.py).
SEED_PRICES: dict[str, float] = {
    "AAPL": 324.96, "GOOGL": 337.12, "MSFT": 496.82, "AMZN": 254.98, "TSLA": 357.01,
    "NVDA": 224.41, "META": 592.85, "JPM": 356.22, "V": 378.40, "NFLX": 82.73,
}
```

---

## 9. Anchored Simulator — `app/market/anchored.py` (new)

The source that makes a free key useful. It fetches **real closing prices** with the one
free-tier call that prices the entire market, then runs the GBM simulator forward from those
anchors.

A student with a free key gets real price levels, real relative valuations, a watchlist that
ticks, and a portfolio that changes — the whole demo works, for one API call at startup, well
inside a 5-per-minute budget.

```python
"""GBM simulation anchored to real Massive closing prices."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from massive import RESTClient

from .cache import PriceCache
from .interface import MarketDataSource
from .models import PricePoint, SourceStatus, epoch_to_iso, to_epoch_seconds
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)

RE_ANCHOR_INTERVAL_SECONDS = 3600.0  # 24 calls/day out of 7,200 — cheap honesty
MAX_ANCHOR_LOOKBACK_DAYS = 7  # covers a long holiday weekend


class AnchoredSimulatorDataSource(MarketDataSource):
    """Real price LEVELS from Massive, synthetic price MOTION from the simulator.

    Bridges the Basic-tier gap: one free-tier call prices every ticker, and the
    GBM simulator supplies the movement the snapshot endpoints would have given
    us on a paid plan. Prices are explicitly NOT live — describe() says so and
    the frontend badges it SIMULATED.
    """

    def __init__(self, api_key: str, price_cache: PriceCache) -> None:
        self._client = RESTClient(api_key=api_key, retries=0, read_timeout=10.0)
        self._cache = price_cache
        self._sim: SimulatorDataSource | None = None
        self._anchors: dict[str, float] = {}
        self._anchor_date: str | None = None
        self._reanchor_task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        tickers = [t.upper().strip() for t in tickers]
        self._anchors, self._anchor_date = await asyncio.to_thread(self._fetch_anchors, tickers)

        # Real closes where we have them; the static seed table covers the rest.
        self._sim = SimulatorDataSource(
            self._cache,
            seed_overrides=self._anchors,
            status_detail=self._detail(),
        )
        await self._sim.start(tickers)
        self._reanchor_task = asyncio.create_task(self._reanchor_loop(), name="re-anchor")

    async def stop(self) -> None:
        for task in (self._reanchor_task,):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._reanchor_task = None
        if self._sim:
            await self._sim.stop()

    async def add_ticker(self, ticker: str) -> None:
        # Delegates to the simulator, which synthesises a deterministic seed for
        # any symbol the anchor set does not cover (PLAN.md §13.2 item 8).
        if self._sim:
            await self._sim.add_ticker(ticker)

    async def remove_ticker(self, ticker: str) -> None:
        if self._sim:
            await self._sim.remove_ticker(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers() if self._sim else []

    def describe(self) -> SourceStatus:
        return SourceStatus(
            name="anchored-simulator",
            live=False,
            detail=self._detail(),
            tickers=len(self.get_tickers()),
            cache_populated=len(self._cache) > 0,
        )

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        """Real minute bars from the last completed session — free-tier allowed.

        Genuinely better than synthetic backfill: the chart opens on the shape
        the market actually traded. Falls back to the simulator's ring buffer.
        """
        if not self._anchor_date:
            return await self._sim.get_history(ticker, points) if self._sim else []
        try:
            bars = await asyncio.to_thread(
                self._client.get_aggs,
                ticker.upper().strip(),
                1,
                "minute",
                self._anchor_date,
                self._anchor_date,
                limit=50_000,
            )
        except Exception as e:  # noqa: BLE001 - history is best-effort
            logger.warning("Minute-bar history failed for %s: %s", ticker, e)
            return await self._sim.get_history(ticker, points) if self._sim else []
        # Aggregate timestamps are MILLIseconds (MASSIVE_API.md §6).
        return [
            PricePoint(epoch_to_iso(to_epoch_seconds(b.timestamp, "ms")), b.close)
            for b in bars[-points:]
        ]

    # --- Internals ---

    def _fetch_anchors(self, tickers: list[str]) -> tuple[dict[str, float], str | None]:
        """ONE API call prices every ticker. Walks back over weekends and holidays.

        Synchronous — always call via asyncio.to_thread.
        """
        wanted = set(tickers)
        day = date.today()
        for _ in range(MAX_ANCHOR_LOOKBACK_DAYS):
            day -= timedelta(days=1)
            iso = day.isoformat()
            try:
                bars = self._client.get_grouped_daily_aggs(iso, adjusted=True)
            except Exception as e:  # noqa: BLE001 - try the previous day
                logger.warning("Anchor fetch failed for %s: %s", iso, e)
                continue
            if not bars:
                continue  # weekend or holiday: resultsCount 0, not an error
            found = {b.ticker: b.close for b in bars if b.ticker in wanted}
            logger.info("Anchored %d/%d tickers to %s closes", len(found), len(wanted), iso)
            return found, iso
        logger.warning("No anchors available — falling back to the static seed table")
        return {}, None

    async def _reanchor_loop(self) -> None:
        """Re-fetch closes hourly so an overnight container picks up the new session."""
        while True:
            await asyncio.sleep(RE_ANCHOR_INTERVAL_SECONDS)
            try:
                anchors, anchor_date = await asyncio.to_thread(
                    self._fetch_anchors, self.get_tickers()
                )
            except Exception:  # noqa: BLE001
                logger.exception("Re-anchor failed")
                continue
            if not anchors or anchor_date == self._anchor_date:
                continue
            # A NEW session's closes: reset the daily baseline, but do not jump
            # the live price — the simulated path continues from where it is.
            self._anchors, self._anchor_date = anchors, anchor_date
            for ticker, close in anchors.items():
                current = self._cache.get_price(ticker)
                if current is not None:
                    self._cache.update(ticker=ticker, price=current, open_price=close)
            logger.info("Re-anchored daily baselines to %s closes", anchor_date)

    def _detail(self) -> str:
        if self._anchor_date:
            return (
                f"simulated from real {self._anchor_date} closes "
                f"({len(self._anchors)} tickers anchored)"
            )
        return "simulated from static seed prices (anchor fetch failed)"
```

**`adjusted=True` matters.** Unadjusted series show a false −90% cliff on a 10:1 split; the
gap between the repo's 800.00 NVDA seed and the real 224.41 is mostly splits, not a crash.

**Honesty note.** Showing real levels with synthetic motion, badged SIMULATED, is a
deliberate choice over showing yesterday's frozen closes — which is more truthful and
completely lifeless. `MARKET_INTERFACE.md` §12 flags it for sign-off; this design proceeds
with the badged simulation.

---

## 10. Massive Source — `app/market/massive_client.py` (rewritten)

The shipped client has four defects beyond the entitlement gate, all of which fail
*invisibly* because `except Exception` swallows them once per poll, forever.

| # | Defect | Consequence |
|---|---|---|
| 1 | `snap.last_trade.timestamp / 1000.0` | wrong attribute (`AttributeError`, swallowed) **and** wrong unit — the field is `sip_timestamp` in **nanoseconds** |
| 2 | `open_price` never captured | the daily-% column has no baseline, though `prev_day.close` arrives free with every poll |
| 3 | `except Exception` around everything | rate limiting logs at error level forever with no user-facing signal |
| 4 | `retries=3` (SDK default) | 0.0/0.2/0.4 s backoff burns three more requests inside the same 60 s window that just rejected you |

```python
"""Massive (Polygon.io) API client for real market data."""

from __future__ import annotations

import asyncio
import logging
import time

import urllib3.exceptions
from massive import RESTClient
from massive.exceptions import AuthError, BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .models import PricePoint, SourceStatus, epoch_to_iso, to_epoch_seconds

logger = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 120.0
STALE_AFTER_MISSED_POLLS = 3  # no successful poll in 3 intervals -> not live
# A quote older than this is not "the current market" on any plan, delayed
# included. It is how a weekend or a halted symbol stops rendering as live.
MAX_QUOTE_AGE_SECONDS = 1800.0


class MassiveDataSource(MarketDataSource):
    """MarketDataSource backed by the Massive REST snapshot endpoint.

    One call per poll for ALL watched tickers (never one call per ticker — see
    MASSIVE_API.md §7). Requires a snapshot-entitled key; the factory only
    constructs this after probe_capabilities() confirms entitlement.
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        capability_detail: str = "snapshots entitled",
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._detail = capability_detail
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        self._client: RESTClient | None = None
        # Health state, all read by describe()
        self._last_success: float | None = None
        self._newest_quote: float | None = None  # freshest sip_timestamp seen
        self._consecutive_failures = 0
        self._last_error: str | None = None
        self._unpriced: set[str] = set()  # requested but absent from the response

    async def start(self, tickers: list[str]) -> None:
        # retries=0: the SDK's urllib3 retry raises MaxRetryError (NOT a
        # massive.exceptions type) and its sub-second backoff is useless against
        # a 60s rolling window. Back off at the poll-loop level instead.
        self._client = RESTClient(api_key=self._api_key, retries=0, read_timeout=10.0)
        self._tickers = [t.upper().strip() for t in tickers]

        await self._poll_once()  # populate the cache before the first SSE connection
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info(
            "Massive poller started: %d tickers, %.1fs interval", len(self._tickers), self._interval
        )

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._client = None
        logger.info("Massive poller stopped")

    async def add_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        if ticker not in self._tickers:
            self._tickers.append(ticker)
            logger.info("Massive: added %s (priced on the next poll)", ticker)

    async def remove_ticker(self, ticker: str) -> None:
        ticker = ticker.upper().strip()
        self._tickers = [t for t in self._tickers if t != ticker]
        self._unpriced.discard(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def describe(self) -> SourceStatus:
        """Never raises, never does I/O — it must work when everything else is broken."""
        now = time.time()
        poll_stale = (
            self._last_success is None
            or now - self._last_success > self._interval * STALE_AFTER_MISSED_POLLS
        )
        # Polls can succeed all weekend while returning Friday's prints, so
        # freshness is judged on the QUOTE age, not just on the poll succeeding.
        quote_stale = self._newest_quote is None or now - self._newest_quote > MAX_QUOTE_AGE_SECONDS
        stale = poll_stale or quote_stale

        detail = self._detail
        if poll_stale:
            age = "never" if self._last_success is None else f"{now - self._last_success:.0f}s ago"
            detail = f"STALE — last successful poll {age}"
            if self._last_error:
                detail += f" ({self._last_error})"
        elif quote_stale:
            mins = (now - self._newest_quote) / 60 if self._newest_quote else 0
            detail = f"{self._detail}; market closed or halted — newest quote {mins:.0f} min old"
        elif self._unpriced:
            detail = f"{self._detail}; no data for {', '.join(sorted(self._unpriced))}"
        return SourceStatus(
            name="massive",
            live=not stale,  # a frozen quote must never render as live
            detail=detail,
            tickers=len(self._tickers),
            cache_populated=len(self._cache) > 0,
        )

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        """Real minute bars for today (paid keys) or the last session."""
        if not self._client:
            return []
        today = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            bars = await asyncio.to_thread(
                self._client.get_aggs, ticker.upper().strip(), 1, "minute", today, today,
                limit=50_000,
            )
        except Exception as e:  # noqa: BLE001 - history is best-effort
            logger.warning("History failed for %s: %s", ticker, e)
            return []
        return [
            PricePoint(epoch_to_iso(to_epoch_seconds(b.timestamp, "ms")), b.close)
            for b in bars[-points:]
        ]

    # --- Internals ---

    async def _poll_loop(self) -> None:
        """Poll on interval, backing off exponentially while failing."""
        while True:
            delay = self._interval
            if self._consecutive_failures:
                delay = min(self._interval * 2**self._consecutive_failures, MAX_BACKOFF_SECONDS)
            await asyncio.sleep(delay)
            await self._poll_once()

    async def _poll_once(self) -> None:
        if not self._tickers or not self._client:
            return
        try:
            snapshots = await asyncio.to_thread(self._fetch_snapshots)
        except AuthError as e:
            self._fail(f"authentication failed: {e}", level=logging.ERROR)
            return
        except BadResponse as e:
            if "NOT_AUTHORIZED" in str(e):
                # Permanent: the probe said we were entitled and the plan changed
                # under us. Back off hard rather than burning the budget.
                self._fail(f"not entitled: {e}", level=logging.ERROR)
            else:
                self._fail(f"bad response: {e}", level=logging.WARNING)
            return
        except urllib3.exceptions.MaxRetryError as e:
            # Rate limited or unreachable — this is what a 429 looks like.
            self._fail(f"rate limited or unreachable: {e}", level=logging.WARNING)
            return
        except Exception as e:  # noqa: BLE001 - the loop must survive anything
            self._fail(f"unexpected: {e}", level=logging.WARNING)
            return

        seen: set[str] = set()
        for snap in snapshots:
            price, ts = self._extract_price(snap)
            if price is None:
                continue
            self._cache.update(
                ticker=snap.ticker,
                price=price,
                # The real previous close — the daily-% baseline, free with every poll.
                open_price=snap.prev_day.close if getattr(snap, "prev_day", None) else None,
                timestamp=ts,
            )
            seen.add(snap.ticker)
            if ts is not None and (self._newest_quote is None or ts > self._newest_quote):
                self._newest_quote = ts

        # Symbols the API simply omitted (unknown/delisted) are recorded rather
        # than vanishing silently, so /api/watchlist can flag them.
        self._unpriced = {t for t in self._tickers if t not in seen}
        if seen:
            self._last_success = time.time()
            self._consecutive_failures = 0
            self._last_error = None
        else:
            self._fail("poll returned no usable snapshots", level=logging.WARNING)
        logger.debug("Massive poll: priced %d/%d tickers", len(seen), len(self._tickers))

    @staticmethod
    def _extract_price(snap) -> tuple[float | None, float | None]:
        """Last trade price and its timestamp in epoch seconds.

        sip_timestamp is NANOseconds. The shipped code read `.timestamp` (wrong
        attribute) and divided by 1e3 (wrong unit by a factor of a million,
        landing the quote in the year 52000).
        """
        trade = getattr(snap, "last_trade", None)
        if trade is None or trade.price is None:
            return None, None
        raw = getattr(trade, "sip_timestamp", None)
        ts = to_epoch_seconds(raw, "ns") if raw else None
        return float(trade.price), ts

    def _fail(self, message: str, level: int) -> None:
        self._consecutive_failures += 1
        self._last_error = message
        logger.log(level, "Massive poll failed (%d consecutive): %s", self._consecutive_failures, message)

    def _fetch_snapshots(self) -> list:
        """Synchronous SDK call — always runs in a worker thread."""
        return self._client.get_snapshot_all(
            market_type=SnapshotMarketType.STOCKS,
            tickers=self._tickers,
        )
```

### Two kinds of stale

`describe()` distinguishes them because they need different words in `/api/health`:

- **Poll stale** — no successful call in three intervals. Network, rate limit, or a revoked
  key. `_last_error` names it.
- **Quote stale** — polls succeed but the newest `sip_timestamp` is over 30 minutes old.
  This is the closed-market case (`PLAN.md` §13.2 item 14): all weekend the snapshot endpoint
  cheerfully returns Friday's prints, and without the quote-age check the terminal would
  present a two-day-old price as live.

Either flips `live` to `False`. The app keeps running and keeps showing the last known
prices — it just stops claiming they are current. A deployment that expects to sit through
weekends should prefer `AnchoredSimulatorDataSource`, which stays alive by design.

---

## 11. SSE Streaming — `app/market/stream.py`

Three properties of the shipped stream are deliberate and stay:

- **The full price map on every change, not a delta.** At 10–50 tickers the payload is a
  couple of KB; a delta protocol would need reconnection-resync logic for no measurable gain
  (`PLAN.md` §13.5 item 40). **Do not optimise this into a delta.**
- **The `version` counter** for change detection — cheap, and it means an idle cache costs
  one integer comparison per client per interval.
- **The `create_stream_router(cache)` factory**, which injects the cache without globals.

The one addition is the heartbeat (`PLAN.md` §13.1 item 7). It barely matters at a 500 ms
simulator cadence and matters enormously in Massive mode: at a 15 s poll — or on a closed
market, where the version never changes at all — the connection sits silent long enough for
an intermediate proxy or a sleeping laptop to drop it with neither side noticing.

```python
HEARTBEAT_SECONDS = 10.0


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yield SSE frames: the full price map whenever it changes, else a heartbeat."""
    yield "retry: 1000\n\n"  # EventSource reconnect hint

    last_version = -1
    last_sent = time.monotonic()
    client_ip = request.client.host if request.client else "unknown"
    logger.info("SSE client connected: %s", client_ip)

    try:
        while True:
            if await request.is_disconnected():
                logger.info("SSE client disconnected: %s", client_ip)
                break

            current_version = price_cache.version
            if current_version != last_version:
                last_version = current_version
                prices = price_cache.get_all()
                if prices:
                    payload = json.dumps({t: u.to_dict() for t, u in prices.items()})
                    yield f"data: {payload}\n\n"
                    last_sent = time.monotonic()
            elif time.monotonic() - last_sent > HEARTBEAT_SECONDS:
                # An SSE comment: keeps the connection warm, ignored by EventSource.
                yield ": ping\n\n"
                last_sent = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

SSE frame on the wire:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 325.41, "previous_price": 325.38, "open_price": 324.96, "timestamp": "2026-09-03T17:42:11.413700Z", "tick_direction": "up", "change_today": 0.45, "change_percent_today": 0.1385}, "GOOGL": { ... }}

: ping
```

---

## 12. HTTP Surface

Two endpoints the market subsystem owns, beyond the SSE stream.

### `GET /api/prices/{ticker}/history?points=120`

First paint for the main chart and the sparklines. The frontend does not know or care whether
it receives simulated history or real minute bars.

```python
@router.get("/api/prices/{ticker}/history")
async def price_history(ticker: str, request: Request, points: int = 120) -> dict:
    source: MarketDataSource = request.app.state.market
    series = await source.get_history(ticker.upper().strip(), points=min(points, 600))
    return {
        "ticker": ticker.upper().strip(),
        "source": source.describe().name,
        "points": [p.to_dict() for p in series],
    }
```

```json
{
  "ticker": "AAPL",
  "source": "anchored-simulator",
  "points": [
    {"timestamp": "2026-09-03T17:37:11.413700Z", "price": 324.91},
    {"timestamp": "2026-09-03T17:37:11.913700Z", "price": 324.94}
  ]
}
```

An empty `points` array is a valid answer, not an error — the frontend falls back to
accumulating from SSE.

### `GET /api/health`

`PLAN.md` §13.5 item 38 asks health to report the active source and whether the cache is
populated. `describe()` hands both over, plus the sentence that ends the debugging session.

```python
@router.get("/api/health")
async def health(request: Request) -> dict:
    source: MarketDataSource = request.app.state.market
    return {"status": "ok", "market_data": source.describe().to_dict()}
```

```json
{
  "status": "ok",
  "market_data": {
    "name": "anchored-simulator",
    "live": false,
    "detail": "simulated from real 2026-09-02 closes (10 tickers anchored)",
    "tickers": 10,
    "cache_populated": true
  }
}
```

`live: false` is the frontend's cue to render a **SIMULATED** badge beside the connection dot.

---

## 13. Lifespan Wiring

Resolves `PLAN.md` §13.1 item 5: initialise the database **at startup, not on first request**,
because the market source needs the seeded watchlist before any request arrives. One code
path, no request-time locking.

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_database()  # create + seed if empty
    app.state.price_cache = PriceCache()

    tickers = get_tracked_tickers()  # watchlist ∪ held positions
    app.state.market = await create_market_data_source(app.state.price_cache)
    await app.state.market.start(tickers)

    app.state.snapshots = asyncio.create_task(portfolio_snapshot_loop(app.state))
    try:
        yield
    finally:
        app.state.snapshots.cancel()
        await app.state.market.stop()
```

Two contracts this places on the backend agent:

**`get_tracked_tickers()` returns watchlist ∪ held positions** (`PLAN.md` §13.2 item 10).
Removing a ticker from the watchlist must not stop pricing a position you still hold, or the
heatmap tile, the positions row, and the total portfolio value all go stale.

```python
def get_tracked_tickers() -> list[str]:
    """Everything that needs a live price: the watchlist plus anything held."""
    with connect() as db:
        rows = db.execute(
            "SELECT ticker FROM watchlist WHERE user_id = ? "
            "UNION SELECT ticker FROM positions WHERE user_id = ? AND quantity > 1e-9",
            (DEFAULT_USER_ID, DEFAULT_USER_ID),
        ).fetchall()
    return [r["ticker"] for r in rows]
```

**Watchlist and trade routes keep the source in sync.** Adding a ticker must call
`source.add_ticker()`; removing one must only call `source.remove_ticker()` when no position
remains. Trading an off-watchlist symbol auto-adds it (`PLAN.md` §13.2 item 9) so the trade
is priceable:

```python
async def ensure_priced(app_state, ticker: str) -> float | None:
    """Make a ticker priceable, then return its price. Used by the trade path."""
    if ticker not in app_state.price_cache:
        await app_state.market.add_ticker(ticker)
        for _ in range(20):  # simulator prices instantly; Massive needs a poll
            price = app_state.price_cache.get_price(ticker)
            if price is not None:
                return price
            await asyncio.sleep(0.25)
    return app_state.price_cache.get_price(ticker)
```

**Portfolio valuation** falls back to `avg_cost` for any position with no cached price, and
the 30-second snapshot task skips its first run until the cache is populated — otherwise the
P&L chart opens with a garbage point at t=0 (`PLAN.md` §13.6 item 42).

---

## 14. Configuration

| Variable | Default | Effect on market data |
|---|---|---|
| `MASSIVE_API_KEY` | unset | unset → simulator; set → probed, then one of the three sources |

No other market-data environment variables. Poll cadence, event probability, and history
depth are constructor arguments with the defaults in this document — tests override them by
construction, not by environment (`MARKET_SIMULATOR.md` §9 depends on that).

`--env-file .env` supplies the container's environment; `python-dotenv` is a local-dev
convenience only (`PLAN.md` §13.4 item 33).

---

## 15. Testing

The existing 73 tests stay green apart from deliberate signature churn: `direction` →
`tick_direction`, `change`/`change_percent` → `change_today`/`change_percent_today`, the
keyword-only `open_price` on `PriceCache.update`, and `create_market_data_source` becoming a
coroutine. **Every Massive test stubs the client — no test touches the network**, because a
5-call-per-minute budget makes a real-network suite unrunnable in CI.

### 15.1 Statistical tests — the ones that would have caught the shock bug

Nothing in the current suite fails at `event_probability=0.001`, which is why a 20×
volatility inflation shipped. Fix that first:

```python
def test_realized_daily_volatility_matches_sigma():
    """Realized vol must match the parameter WITH SHOCKS AT THE PRODUCTION DEFAULT.

    This is the regression test for the decaying-shock design: it fails at the
    old permanent-shift shocks and at event_probability=1e-3.
    """
    random.seed(42)
    np.random.seed(42)

    runs, ticks_per_day = 200, 46_800
    sigma = TICKER_PARAMS["AAPL"]["sigma"]
    returns = []
    for _ in range(runs):
        sim = GBMSimulator(["AAPL"], event_probability=1e-4)
        start = sim.get_price("AAPL")
        for _ in range(ticks_per_day):
            sim.step()
        returns.append(math.log(sim.get_price("AAPL") / start))

    realized = statistics.stdev(returns)
    target = sigma / math.sqrt(252)
    stderr = target / math.sqrt(2 * (runs - 1))
    assert abs(realized - target) < 3 * stderr, f"{realized:.4%} vs {target:.4%}"
```

Also assert, over long runs: realised tech-vs-tech correlation ≈ 0.6 ± 0.05 and tech-vs-finance
≈ 0.3 ± 0.05 (proves the Cholesky is applied and not silently bypassed); mean log return
consistent with `(μ − σ²/2)·t` (catches a dropped Itô correction). Seed **both** `random` and
`np.random` — the simulator uses both.

### 15.2 Invariants

- Price strictly positive after 100,000 steps, for every σ in `TICKER_PARAMS`.
- No NaN or infinity anywhere in the price map.
- `step()` returns exactly the current ticker set, always.
- Cholesky survives 100 unknown tickers added **one at a time** (the incremental path, which
  rebuilds on every call — not the batch constructor).
- Visible-tick rate > 50% for every default ticker at anchored prices — the guard on the
  rounding cliff.
- `_synthesize_seed("ZZZZ")` is identical across two fresh instances.
- `remove_ticker` then `add_ticker` restores the price the ticker had, not a fresh seed.

### 15.3 Capability probe and factory

Stub the client for each of the five key states and assert both the classification and the
class the factory picks:

```python
class _StubClient:
    def __init__(self, snapshot_exc=None, prev_close_exc=None):
        self._snapshot_exc, self._prev_close_exc = snapshot_exc, prev_close_exc

    def get_snapshot_all(self, **_):
        if self._snapshot_exc:
            raise self._snapshot_exc
        return [_snapshot("AAPL", price=324.96, sip_timestamp=int(time.time() * 1e9))]

    def get_previous_close_agg(self, *_a, **_k):
        if self._prev_close_exc:
            raise self._prev_close_exc
        return [_prev_close("AAPL", close=324.96)]


def test_basic_tier_classifies_as_end_of_day(monkeypatch):
    """The critical case: NOT_AUTHORIZED snapshots + working aggregates is a
    VALID free key, not a dead one. Getting this wrong sends every student to
    the unanchored simulator."""
    monkeypatch.setattr(
        capabilities, "RESTClient",
        lambda **_: _StubClient(snapshot_exc=BadResponse("NOT_AUTHORIZED: not entitled")),
    )
    caps = probe_capabilities("k" * 32)
    assert (caps.valid, caps.realtime, caps.end_of_day) == (True, False, True)


async def test_factory_never_raises_and_always_returns_a_source(monkeypatch):
    for caps, expected in [
        (MassiveCapabilities(True, True, True, "rt"), MassiveDataSource),
        (MassiveCapabilities(True, False, True, "eod"), AnchoredSimulatorDataSource),
        (MassiveCapabilities(False, False, False, "rejected"), SimulatorDataSource),
    ]:
        monkeypatch.setattr(factory, "probe_capabilities", lambda _k, c=caps: c)
        monkeypatch.setenv("MASSIVE_API_KEY", "k" * 32)
        assert isinstance(await create_market_data_source(PriceCache()), expected)
```

Also: `MaxRetryError` (rate limited) classifies as invalid rather than crashing, and a
`get_snapshot_all` whose newest `sip_timestamp` is 15 minutes old yields
`poll_interval == 15.0`.

### 15.4 Massive client

```python
def test_sip_timestamp_is_nanoseconds():
    """The shipped bug: `.timestamp / 1000.0` put quotes in the year 52000."""
    price, ts = MassiveDataSource._extract_price(_snapshot("AAPL", 324.96, 1605192894630916600))
    assert price == 324.96
    assert abs(ts - 1605192894.63) < 0.01
```

Plus: `prev_day.close` lands in `PriceUpdate.open_price`; a poll returning no usable
snapshots increments `_consecutive_failures` and backs off; a ticker absent from the response
appears in `_unpriced` and in `describe().detail`; `describe()` returns `live=False` when the
newest quote is a day old; and `describe()` never raises when `start()` was never called.

### 15.5 Anchored simulator

`_fetch_anchors` walks back over a weekend — empty `bars` for Saturday and Sunday, populated
for Friday — and returns `({}, None)` cleanly after seven failures. A successful anchor sets
`seed_overrides`, so `AAPL` starts at the real close rather than the seed table's value.

### 15.6 Stream

`_generate_events` emits `": ping"` when the cache version is static for longer than the
heartbeat, and emits a `data:` frame within one interval of a cache update. This is the unit
test `PLAN.md` §13.4 item 31 wants **in place of** the Playwright disconnect/reconnect case,
which ultimately only verifies that the browser's built-in `EventSource` retry works.

---

## 16. Implementation Order

Each step leaves the tree importable and the suite runnable.

1. **`models.py`** — `PricePoint`, `SourceStatus`, time helpers, `open_price`,
   `tick_direction`. Update `test_models.py` for the renames.
2. **`cache.py`** — keyword-only sticky `open_price`. Update `test_cache.py`.
3. **`interface.py`** — `describe()` (abstract) and `get_history()` (default). Both existing
   sources fail to instantiate until step 4 — expected.
4. **`simulator.py` + `seed_prices.py`** — decaying shocks at `1e-4`, deterministic seeds,
   `seed_overrides`, `LinAlgError` fallback, history deque + prefill, `describe()`, refreshed
   seed table. Add the §15.1 statistical tests; confirm they **fail** at `event_probability=1e-3`
   before making them pass.
5. **`capabilities.py`** — probe plus its five-state test matrix.
6. **`massive_client.py`** — rewritten per §10.
7. **`anchored.py`** — new source.
8. **`factory.py`** — async, capability-driven. Update `test_factory.py` for the coroutine.
9. **`stream.py`** — heartbeat.
10. **`__init__.py`** — export `PricePoint`, `SourceStatus`, `MassiveCapabilities`,
    `probe_capabilities`; refresh `backend/CLAUDE.md`, whose documented `PriceUpdate` fields
    and synchronous factory both go stale at steps 1 and 8.

Steps 1–4 and 9 need no API key and no network. Steps 5–7 are fully stubbed in tests; run
`market_data_demo.py` against a real free key once to confirm the anchored path end to end.

---

## 17. Decisions This Document Closes

| `PLAN.md` item | Resolution |
|---|---|
| §13.1 #1 daily-change baseline | `open_price` on `PriceUpdate` and the cache; `tick_direction` vs `change_percent_today` (§3) |
| §13.1 #5 lazy DB init | Startup only, in the lifespan handler (§13) |
| §13.1 #6 timestamp formats | Epoch float internally, ISO 8601 UTC on the wire, converted in `to_dict()` (§3) |
| §13.1 #7 SSE heartbeat | `": ping"` every 10 s of silence (§11) |
| §13.2 #8 unknown tickers | Synthesise, never reject; deterministic SHA-256 seed (§8.3) |
| §13.2 #9 trading off-watchlist symbols | Auto-add via `ensure_priced()` (§13) |
| §13.2 #10 removing a held ticker | Tracked set is watchlist ∪ positions (§13) |
| §13.2 #14 closed market | Quote-age staleness flips `live` to `False`; anchored simulator stays alive by design (§10) |
| §13.2 #15 price history | `get_history()` on the interface: simulator ring buffer + prefill, real minute bars on Massive (§8.5, §12) |
| §13.5 #38 health reporting | `SourceStatus` verbatim in `/api/health` (§12) |
| §13.5 #39 two ticker lists | DB seed imports its list from `seed_prices.SEED_PRICES` |
| §13.5 #40 full-map SSE payload | Deliberate; do not convert to a delta (§11) |
| §13.6 #42 missing prices | Fall back to `avg_cost`; snapshot task waits for a populated cache (§13) |

### Still open

1. **Anchored-simulator honesty** (`MARKET_INTERFACE.md` §12). Real levels with synthetic
   motion, badged SIMULATED, versus yesterday's truthful but lifeless frozen closes. This
   design picks the badged simulation and wants an explicit sign-off.
2. **Delayed-plan badging.** A Starter/Developer key is real market data, just 15 minutes
   late. §6 reports it as `live: True` with the delay named in `detail`, rather than as
   SIMULATED. Confirm that is the frontend's intent.
3. **Re-anchor cadence.** Hourly, at 24 calls/day out of 7,200. Cheap, and it keeps a demo
   left open overnight honest.
4. **`get_history` depth.** 600 points (5 simulated minutes) is the ring buffer; Massive
   serves whatever the day holds. If the main chart wants hours rather than minutes, the
   simulator's deque needs to grow or thin its samples.
