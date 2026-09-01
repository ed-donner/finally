# MARKET_DATA_DESIGN.md — Market Data Backend, Detailed Design

The implementation-level design for FinAlly's market data subsystem: one unified API, two
interchangeable sources (GBM simulator and the Massive REST API), a shared in-memory cache,
and the SSE stream that carries prices to the browser.

**Audience:** the agent (or human) implementing or extending `backend/app/market/`.
This document is meant to be read once, top to bottom, and then implemented from — every
snippet below is either the code that ships today or the code that should ship.

**Companion documents.** `PLAN.md` §6 is the frozen contract; `MARKET_INTERFACE.md`,
`MARKET_SIMULATOR.md`, and `MASSIVE_API.md` are the reference material this design draws on.
Where they disagree with this document, this document is the design and they are the background.

---

## 0. Status — what exists, what is missing

Verified by running the suite in `backend/` on 2026-09-01:

```
73 passed in 1.77s        TOTAL coverage 91%
app/market/cache.py           100%
app/market/models.py          100%
app/market/simulator.py        98%
app/market/massive_client.py   94%   <- high coverage, two live defects (§8.4)
app/market/stream.py           33%   <- the SSE generator is effectively untested
```

| Piece | State | Section |
|---|---|---|
| `PriceUpdate` wire model | Ships, frozen contract | §4 |
| `PriceCache` (latest price + version) | Ships | §5.1 |
| `PriceCache` rolling history | **Missing** | §5.2 |
| `MarketDataSource` ABC | Ships | §6 |
| `SimulatorDataSource` + `GBMSimulator` | Ships | §7 |
| `MassiveDataSource` | Ships but **writes nothing to the cache** | §8.4 |
| `create_market_data_source` | Ships | §9 |
| SSE `/api/stream/prices` | Ships | §10.1 |
| SSE keepalive | **Missing** | §10.2 |
| `GET /api/prices/{ticker}/history` | **Missing** | §11 |
| Lifespan wiring + tracked-set reconciliation | **Missing** | §12 |

Four gaps, all backend, all small. §15 orders them.

---

## 1. The shape of the design

Two sources with nothing in common — a 500ms in-process computation and a 15-second blocking
HTTP poll — must be interchangeable to everything downstream. The design achieves that with
**one indirection and one shared buffer**:

```
                       writes                       reads
  ┌──────────────────┐        ┌────────────┐               ┌──────────────────────┐
  │ SimulatorSource  │───┐    │            │───────────────│ SSE  /api/stream     │
  │   (500ms step)   │   ├───▶│ PriceCache │───────────────│ Portfolio valuation  │
  ├──────────────────┤   │    │  (in-mem,  │───────────────│ Trade execution      │
  │ MassiveSource    │───┘    │thread-safe)│───────────────│ Snapshot task        │
  │   (15s poll)     │        │            │───────────────│ /api/prices/history  │
  └──────────────────┘        └────────────┘               └──────────────────────┘
      MarketDataSource
     (abstract interface)
```

**The one invariant that makes this work: nothing downstream ever asks a source for a price.**
Sources are write-only from the application's point of view; readers only ever touch the cache.
That is why a 30× difference in update cadence is invisible to the rest of the app, and why a
`get_price()` on the interface would be a design error — under Massive it would turn every
portfolio valuation into a billed HTTP request.

### File structure

```
backend/app/market/
├── __init__.py          # public exports
├── models.py            # PriceUpdate — the unit of data
├── cache.py             # PriceCache — latest price + version + rolling history
├── interface.py         # MarketDataSource — the ABC
├── seed_prices.py       # simulator constants, no logic
├── simulator.py         # GBMSimulator (pure) + SimulatorDataSource (async)
├── massive_client.py    # MassiveDataSource
├── factory.py           # create_market_data_source
└── stream.py            # SSE router + history router
```

Public surface, unchanged by this design:

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

## 2. Vocabulary

| Term | Meaning |
|---|---|
| **tick** | One simulator step (500ms) or one Massive poll (15s) |
| **tracked set** | `watchlist ∪ {tickers with a non-zero position}` — §12.2 |
| **version** | Monotonic counter on `PriceCache`, bumped on every write; the SSE change signal |
| **seeding** | Writing an initial price into the cache so a ticker never renders as `—` unnecessarily |

---

## 3. Non-negotiable contracts

These are frozen because the frontend and the shipped module already depend on them. Everything
else in this document is open to reasonable change.

1. **SSE payload is a map keyed by ticker, one event carries every ticker.** Not one event per ticker.
2. **`timestamp` is Unix epoch seconds as a float.** Never ISO, never milliseconds. The frontend
   multiplies by 1000 for `Date`.
3. **`change_percent` is already in percent units.** `0.021` means 0.021%. This deliberately
   differs from REST responses elsewhere in the API, where percentages are fractions
   (`PLAN.md` §8). The inconsistency is real and preserved.
4. **A connecting client gets a full snapshot immediately**, including after a reconnect,
   because a fresh generator starts at `last_version = -1`.
5. **Tickers are uppercase everywhere**, normalized at the API boundary.

---

## 4. `PriceUpdate` — the unit of data

`backend/app/market/models.py`. Immutable, frozen, slotted. Both sources produce it; every
reader consumes it.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)   # Unix epoch SECONDS

    @property
    def change(self) -> float:
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

**`change`, `change_percent`, and `direction` are computed properties, not stored fields.**
They cannot drift out of sync with the prices they describe, and `to_dict()` cannot emit a
`direction` that contradicts its own `price`/`previous_price` pair.

**`previous_price` means the price at the previous update**, not the previous session's close.
On the first update for a ticker it equals `price`, so `direction` is `"flat"` and `change` is
`0.0` — a newly added ticker never flashes green or red on its first tick.

Example of the exact wire shape a client sees:

```json
{
  "ticker": "AAPL",
  "price": 190.52,
  "previous_price": 190.48,
  "timestamp": 1755873791.482,
  "change": 0.04,
  "change_percent": 0.021,
  "direction": "up"
}
```

---

## 5. `PriceCache` — the shared buffer

`backend/app/market/cache.py`.

### 5.1 What ships today

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate
    def get(self, ticker: str) -> PriceUpdate | None
    def get_all(self) -> dict[str, PriceUpdate]     # shallow copy
    def get_price(self, ticker: str) -> float | None
    def remove(self, ticker: str) -> None
    @property
    def version(self) -> int
    def __len__(self) -> int
    def __contains__(self, ticker: str) -> bool
```

Three design points that matter:

**`update()` derives `previous_price` itself.** Callers pass only the new price; the cache looks
up what it held and constructs the `PriceUpdate`. Neither source tracks prior state for the
purpose of computing a delta, so the two cannot implement it differently.

```python
def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
    with self._lock:
        ts = timestamp or time.time()
        prev = self._prices.get(ticker)
        previous_price = prev.price if prev else price

        update = PriceUpdate(
            ticker=ticker,
            price=round(price, 2),
            previous_price=round(previous_price, 2),
            timestamp=ts,
        )
        self._prices[ticker] = update
        self._version += 1
        return update
```

**A `threading.Lock`, not an `asyncio.Lock`.** `MassiveDataSource` writes from an
`asyncio.to_thread` worker, so a genuine cross-thread lock is required. The critical sections
are a few dict operations; contention is irrelevant.

**`version` is the SSE change-detection mechanism.** The stream compares an integer every 500ms
rather than diffing price maps. `get_all()` returns a shallow copy, and since `PriceUpdate` is
frozen, that copy is effectively deep and safe to iterate outside the lock.

### 5.2 Rolling price history — to implement

`PLAN.md` §6 requires the main chart to be populated the instant a ticker is clicked, rather
than drawing itself from scratch over the following minute. `PriceCache` gains a bounded
per-ticker deque of `(timestamp, price)`.

```python
from collections import deque

HISTORY_MAXLEN = 600      # ~5 minutes at the 500ms simulator cadence
```

Constructor:

```python
def __init__(self, history_maxlen: int = HISTORY_MAXLEN) -> None:
    self._prices: dict[str, PriceUpdate] = {}
    self._history: dict[str, deque[tuple[float, float]]] = {}
    self._history_maxlen = history_maxlen
    self._lock = Lock()
    self._version: int = 0
```

Appended inside `update()`, under the same lock, immediately after the price is stored:

```python
        self._prices[ticker] = update
        history = self._history.get(ticker)
        if history is None:
            history = deque(maxlen=self._history_maxlen)
            self._history[ticker] = history
        history.append((ts, update.price))
        self._version += 1
        return update
```

`remove()` must drop the deque too, or removed tickers leak memory and a re-added ticker
resurrects a stale chart:

```python
def remove(self, ticker: str) -> None:
    with self._lock:
        self._prices.pop(ticker, None)
        self._history.pop(ticker, None)
```

The reader, backing `GET /api/prices/{ticker}/history`:

```python
def get_history(self, ticker: str, limit: int = HISTORY_MAXLEN) -> list[tuple[float, float]]:
    """Oldest-first (timestamp, price) points. Empty list for an untracked ticker."""
    with self._lock:
        points = self._history.get(ticker)
        if not points:
            return []
        return list(points)[-limit:]
```

Four properties worth stating explicitly:

- **`deque(maxlen=600)` evicts the oldest point automatically** — there is no pruning logic to
  write, and no unbounded growth to worry about.
- **An untracked ticker returns `[]`, not a 404.** The chart draws nothing rather than erroring
  (`PLAN.md` §8).
- **Deliberately not persisted.** A restart clears it, which is the honest behavior for a
  simulator whose prices also reset to seed on restart.
- **Memory is negligible**: 600 points × 50 tickers × ~16 bytes ≈ 500KB.

Under Massive the deque fills at one point per 15-second poll, so five minutes of wall time is
20 points rather than 600. The chart is sparse but correct. Backfilling from `get_aggs`
(`MASSIVE_API.md` §5) is the eventual upgrade and is out of scope here.

---

## 6. `MarketDataSource` — the abstract contract

`backend/app/market/interface.py`.

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

Five methods, and every one is about **lifecycle and membership** — none returns a price. That
absence is the whole design (§1).

### Behavioral contract

Binding on both implementations. A test suite that passes against one should pass against the other.

| Method | Guarantee |
|---|---|
| `start(tickers)` | Begins a background task writing to the cache. **Seeds the cache before returning**, so the first SSE event is never empty. Called exactly once; calling twice is undefined. |
| `stop()` | Cancels the task and releases resources. **Idempotent.** No writes to the cache afterwards. |
| `add_ticker(t)` | Adds to the tracked set. No-op if present. Simulator seeds a price immediately; Massive picks it up on the next poll. |
| `remove_ticker(t)` | Removes from the tracked set **and from the cache** (price and history). No-op if absent. |
| `get_tickers()` | Current tracked set. Synchronous — reads local state only. |

Two asymmetries are permitted and must not be papered over:

- **Seeding latency.** `add_ticker` on the simulator makes a price available immediately; on
  Massive it takes up to one poll interval. The API contract already accommodates this —
  `GET /api/watchlist` returns `price: null` until the first tick, and the UI shows `—`.
- **Cadence.** 500ms versus 15s. Readers must never assume a minimum update rate. This is
  exactly what the SSE keepalive in §10.2 exists to handle.

### `remove_ticker` is destructive — and that is the trap

Both implementations call `self._cache.remove(ticker)`. Correct for the interface, but it means
removing a ticker whose position is still held silently freezes that position's valuation, P&L,
heatmap tile, and snapshot contribution. §12.2 is the rule that prevents it, and it is the single
most important piece of integration logic in this module because the failure mode is a wrong
number, not an error.

### Adding a third source

1. Subclass `MarketDataSource` and implement all five methods.
2. `start()` must **seed the cache before returning**.
3. Never write to the cache after `stop()`; make `stop()` idempotent.
4. `remove_ticker()` must call `cache.remove(ticker)`.
5. Convert timestamps to **Unix epoch seconds as a float** at the boundary.
6. Never let a fetch error kill the background loop — log and retry next cycle.
7. If the underlying client is synchronous, wrap **every** call in `asyncio.to_thread`.
8. Add a branch to `create_market_data_source` and a value to `market_source` in `/api/health`.

Point 7 is not optional: a blocking HTTP call inside `async def` stalls the event loop for the
whole round trip, which stops the SSE stream and every in-flight request.

---

## 7. The simulator — default source

`backend/app/market/simulator.py` and `seed_prices.py`. Two classes with a clean split:
**`GBMSimulator` is pure and synchronous; `SimulatorDataSource` owns the async lifecycle and
the cache.**

```
┌──────────────────────────────────────────────────────────┐
│ SimulatorDataSource(MarketDataSource)                    │
│   owns the asyncio task, writes to PriceCache            │
│   start / stop / add_ticker / remove_ticker / get_tickers│
│                        │                                 │
│                        ▼                                 │
│ GBMSimulator                                             │
│   pure math, no I/O, no async, no cache reference        │
│   step() -> {ticker: price}                              │
└──────────────────────────────────────────────────────────┘
                         │
                         ▼
                  seed_prices.py  (constants only)
```

The separation pays off in testing: `GBMSimulator` needs no event loop, no cache, and no mocks.

### 7.1 The model

```
S(t + dt) = S(t) · exp( (μ − σ²/2)·dt  +  σ·√dt·Z )
```

Three properties earn GBM its place:

**Prices cannot go negative.** The update is multiplicative — `exp(...)` is always positive.
No clamping, no `max(price, 0.01)` guard, no special case. An additive random walk needs all three.

**Returns scale correctly with time.** σ is annualized; `√dt` converts it to the tick. The 500ms
cadence is a display choice, not a modelling parameter.

**The `−σ²/2` term keeps the drift honest.** Without it, μ is not the expected return of the
price — a log-normal artefact. It costs one subtraction and makes the parameters mean what they say.

### 7.2 Sizing `dt`

`dt` is expressed against a **trading** year, not a calendar year. Markets are closed most of the
time; using 365×24h would understate per-tick moves by ~4.5×.

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.479e-8,  sqrt(dt) = 2.912e-4
```

What that produces per tick at the seed prices:

| Ticker | σ | Per-tick σ | Per-tick $ | Per-minute $ (120 ticks) |
|---|---|---|---|---|
| AAPL | 0.22 | 0.0064% | $0.012 | $0.13 |
| JPM | 0.18 | 0.0052% | $0.010 | $0.11 |
| NVDA | 0.40 | 0.0116% | $0.093 | $1.02 |
| TSLA | 0.50 | 0.0146% | $0.036 | $0.40 |

This is the number that decides whether the simulation looks right. A cent or two per tick on a
$200 stock means the price **rounds to a genuinely different value most ticks**, so the UI flashes
constantly, while a minute of drift stays in the tens of cents — what a real quote screen looks
like. Larger reads as a crash; smaller looks frozen.

### 7.3 Correlation via Cholesky

Independent draws would show tech stocks moving in opposite directions half the time. The eye
notices immediately. Standard fix: draw `n` independent normals, multiply by the Cholesky factor
`L` of the correlation matrix `C = L·Lᵀ`.

Constants live in `seed_prices.py`, not in the simulator:

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6    # tech stocks move together
INTRA_FINANCE_CORR = 0.5    # finance stocks move together
CROSS_GROUP_CORR   = 0.3    # between sectors, and for unknown tickers
TSLA_CORR          = 0.3    # TSLA does its own thing
```

Resolved pairwise, first match winning:

```python
@staticmethod
def _pairwise_correlation(t1: str, t2: str) -> float:
    tech = CORRELATION_GROUPS["tech"]
    finance = CORRELATION_GROUPS["finance"]

    # TSLA is in the tech set but behaves independently
    if t1 == "TSLA" or t2 == "TSLA":
        return TSLA_CORR

    if t1 in tech and t2 in tech:
        return INTRA_TECH_CORR
    if t1 in finance and t2 in finance:
        return INTRA_FINANCE_CORR

    return CROSS_GROUP_CORR
```

The TSLA clause is checked first on purpose: TSLA is a tech-set member for every other purpose,
but a demo where TSLA visibly decouples from the pack is more convincing than one where everything
moves in lockstep. `CROSS_GROUP_CORR` doubles as the default for any unknown symbol, which is what
makes §7.5 work.

```python
def _rebuild_cholesky(self) -> None:
    n = len(self._tickers)
    if n <= 1:
        self._cholesky = None      # a single ticker needs no correlation
        return

    corr = np.eye(n)
    for i in range(n):
        for j in range(i + 1, n):
            rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
            corr[i, j] = rho
            corr[j, i] = rho

    self._cholesky = np.linalg.cholesky(corr)
```

Rebuilt on every add and remove — `O(n²)` to build plus `O(n³)` to factor, on `n < 50`. That is
microseconds, and watchlist edits are human-speed, so caching it would be complexity without benefit.

> **Known risk.** `np.linalg.cholesky` raises `LinAlgError` on a matrix that is not positive
> definite, and the call is unguarded. The current block structure (0.6 / 0.5 / 0.3) was verified
> positive definite at 7, 20, and 40 tickers — but raising `INTRA_TECH_CORR` toward 1.0, or adding
> a group whose intra-group correlation is *below* the cross-group value, can break
> positive-definiteness and take down `add_ticker`. Anyone editing these constants must re-run the
> test in §14.2.

### 7.4 The tick

`step()` is the hot path — every 500ms, for every ticker.

```python
def step(self) -> dict[str, float]:
    """Advance all tickers by one time step. Returns {ticker: new_price}."""
    n = len(self._tickers)
    if n == 0:
        return {}

    z_independent = np.random.standard_normal(n)
    if self._cholesky is not None:
        z_correlated = self._cholesky @ z_independent
    else:
        z_correlated = z_independent

    result: dict[str, float] = {}
    for i, ticker in enumerate(self._tickers):
        params = self._params[ticker]
        mu, sigma = params["mu"], params["sigma"]

        drift = (mu - 0.5 * sigma**2) * self._dt
        diffusion = sigma * math.sqrt(self._dt) * z_correlated[i]
        self._prices[ticker] *= math.exp(drift + diffusion)

        if random.random() < self._event_prob:
            shock_magnitude = random.uniform(0.02, 0.05)
            shock_sign = random.choice([-1, 1])
            self._prices[ticker] *= 1 + shock_magnitude * shock_sign

        result[ticker] = round(self._prices[ticker], 2)

    return result
```

Two details worth pointing out:

**Full precision is kept internally; only the returned value is rounded.** Rounding the stored
state would accumulate quantization error into a slow systematic drift over thousands of ticks.

**One `standard_normal(n)` call per tick, not `n` calls.** A single vectorized draw feeding one
matrix multiply is why this stays negligible at 500ms.

**Random events** fire at `event_probability = 0.001` per ticker per tick. With 10 tickers at
2 ticks/second the expected wait is `1 / (10 × 2 × 0.001) = 50 seconds` — frequent enough that
something happens during a demo, rare enough that the series is not pure noise. The shock
multiplies the price directly rather than feeding through GBM, so it is a genuine discontinuity —
a gap, which is what real news does to a stock.

`_tickers` is an **ordered list** that indexes into the Cholesky matrix: row `i` corresponds to
`_tickers[i]`. That is why add and remove must both rebuild. `__init__` adds every ticker via
`_add_ticker_internal` and rebuilds **once** at the end — `O(n³)` instead of `O(n⁴)` on startup.

### 7.5 Unknown tickers

Any symbol passing the API-level pattern `^[A-Z][A-Z.]{0,5}$` works, with no allowlist. The AI
chat can add anything the user names, and it behaves plausibly.

```python
def _add_ticker_internal(self, ticker: str) -> None:
    if ticker in self._prices:
        return
    self._tickers.append(ticker)
    self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
    self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

- **Price**: `SEED_PRICES`, else uniform $50–$300 — where most large-cap US equities trade.
- **Parameters**: `TICKER_PARAMS`, else `DEFAULT_PARAMS` (σ=0.25, μ=0.05) — a mid-range large cap.
- **Correlation**: no sector membership, so `CROSS_GROUP_CORR` (0.3) against everything.

`dict(DEFAULT_PARAMS)` **copies** rather than sharing the module-level dict. Without the copy,
tuning one unknown ticker's σ would mutate the default for every unknown ticker at once.

This is a real advantage over the Massive path, where an unknown symbol never produces a price
and sits at `—` forever (§8.5).

### 7.6 `SimulatorDataSource` — the async wrapper

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001): ...

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
        # Seed the cache so the first SSE event carries real prices
        for ticker in tickers:
            price = self._sim.get_price(ticker)
            if price is not None:
                self._cache.update(ticker=ticker, price=price)
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
        while True:
            try:
                if self._sim:
                    for ticker, price in self._sim.step().items():
                        self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Three deliberate choices:

**Seed the cache in `start()` before creating the task.** The first SSE event then carries real
prices rather than an empty object, so the watchlist never renders as ten dashes on load.

**`add_ticker` seeds immediately.** The new ticker has a price on the very next SSE event, with no
wait for the following step — the reason adding a ticker feels instant.

**The `try` is inside the loop, around the step.** An exception logs and the loop continues on the
next interval. Wrapping the loop instead would let one bad tick kill the feed permanently. This is
the one place defensive handling is warranted: a background task has no caller to propagate to,
and a dead price feed is a dead app.

`stop()` cancels the task, awaits it, and swallows `CancelledError` — the normal shutdown path,
not an error.

### 7.7 Parameters

`seed_prices.py` holds constants only. Prices are realistic as of project creation; σ and μ are annualized.

| Ticker | Seed | σ | μ | Note |
|---|---|---|---|---|
| AAPL | $190 | 0.22 | 0.05 | |
| GOOGL | $175 | 0.25 | 0.05 | |
| MSFT | $420 | 0.20 | 0.05 | |
| AMZN | $185 | 0.28 | 0.05 | |
| TSLA | $250 | 0.50 | 0.03 | High volatility, decorrelated |
| NVDA | $800 | 0.40 | 0.08 | High volatility, strong drift |
| META | $500 | 0.30 | 0.05 | |
| JPM | $195 | 0.18 | 0.04 | Low volatility (bank) |
| V | $280 | 0.17 | 0.04 | Low volatility (payments) |
| NFLX | $600 | 0.35 | 0.05 | |
| *unknown* | $50–300 | 0.25 | 0.05 | `DEFAULT_PARAMS` |

The σ spread is what makes the watchlist readable at a glance: V and JPM barely move while NVDA
and TSLA jump, so the grid has texture instead of ten tickers twitching identically.

There is **no mean reversion and no session boundary.** Prices random-walk from their seed for as
long as the container runs. Over a demo that looks like a trading day; over a week of uptime a
ticker may wander far. That is correct GBM behavior and not worth correcting — state is in memory
only, so a restart returns everything to seed.

---

## 8. The Massive client — optional real data

`backend/app/market/massive_client.py`. Verified against the `massive` SDK **2.2.0** installed in
`backend/.venv`.

### 8.1 Why one snapshot endpoint, polled

The free tier allows **5 requests/minute** — one request every 12 seconds at best. Per-ticker
endpoints are therefore unusable: 10 watchlist tickers via `get_last_trade` would be 10 requests
per cycle, blowing the entire budget in one poll.

**The design must fetch all tickers in a single request.** That is
`GET /v2/snapshot/locale/us/markets/stocks/tickers`, one request returning the current state of
every ticker named:

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="YOUR_KEY")

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.todays_change_percent)
```

The SDK joins a list into a comma-separated string, so passing `list[str]` is correct.
Default poll interval is **15 seconds**, which leaves headroom under the free tier even if a poll
overruns. Paid tiers can drop to 2–5 seconds via `poll_interval`.

The v3 unified snapshot (`list_universal_snapshots`) is the alternative; it reports unknown
tickers explicitly with an `error` field instead of silently omitting them. FinAlly stays on v2:
a 10-ticker watchlist never approaches v3's 250-ticker limit, v2 is a single non-paginated
request, and per-ticker validation feedback is marginal when the simulator is the default path.
v3 is the right upgrade if that feedback is ever wanted.

### 8.2 `RESTClient` is synchronous — wrap every call

It is `urllib3`-based. Calling it from `async def` blocks the event loop for the whole HTTP round
trip, which in this app means visibly stuttering prices on the SSE stream.

```python
snapshots = await asyncio.to_thread(self._fetch_snapshots)
```

It also **retries 429 internally** (3 attempts, honoring `Retry-After`), so a rate-limited poll
blocks its worker thread rather than failing fast. That is fine — the worker is not the event loop.

### 8.3 Timestamp units — the trap

Massive uses three different time units across endpoints and the SDK passes them through unchanged.

| Source | Attribute | Unit | To Unix seconds |
|---|---|---|---|
| Snapshot `lastTrade` | `sip_timestamp` | **nanoseconds** | `/ 1_000_000_000` |
| Snapshot `lastQuote` | `sip_timestamp` | **nanoseconds** | `/ 1_000_000_000` |
| Snapshot top level | `updated` | **nanoseconds** | `/ 1_000_000_000` |
| Snapshot `min` | `timestamp` | **milliseconds** | `/ 1_000` |
| Aggregates (`Agg`, `PreviousCloseAgg`, grouped) | `timestamp` | **milliseconds** | `/ 1_000` |

And **attribute names never match JSON keys.** The wire format is single-letter (`p`, `s`, `t`,
`x`); `from_dict` maps those to readable attributes. `@modelclass` builds a plain dataclass with
no `__getattr__` fallback, so reading a key name raises `AttributeError`.

| `LastTrade` attribute | JSON key | Units |
|---|---|---|
| `price` | `p` | dollars |
| `size` | `s` | shares |
| `sip_timestamp` | `t` | **nanoseconds** |
| `exchange` | `x` | exchange ID |

### 8.4 Two defects in the shipped client — reproduced, not inferred

`_poll_once` currently reads:

```python
price = snap.last_trade.price
timestamp = snap.last_trade.timestamp / 1000.0    # AttributeError, then wrong unit
```

Reproduction against the installed SDK, run on 2026-09-01:

```python
from massive.rest.models.snapshot import TickerSnapshot

snap = TickerSnapshot.from_dict({
    "ticker": "AAPL",
    "lastTrade": {"p": 190.52, "s": 100, "t": 1755873791482000000, "x": 4},
})

snap.last_trade.price               # 190.52
snap.last_trade.sip_timestamp       # 1755873791482000000
hasattr(snap.last_trade, "timestamp")   # False
```

**Defect 1 — `last_trade.timestamp` does not exist, so the Massive path writes nothing at all.**
The loop wraps each snapshot in `except (AttributeError, TypeError)` and merely logs a warning, so
the exception is swallowed once per ticker on every poll. The symptom is not a crash: it is a
watchlist where every ticker shows `—` forever, with `Skipping snapshot for AAPL` in the logs.

**Defect 2 — the divisor is wrong by 10⁶.** Even with the attribute corrected, `/ 1000.0` treats
nanoseconds as milliseconds: `1755873791482000000 / 1000` ≈ 1.76 × 10¹⁵ seconds, roughly 55 million
years in the future. Any chart keyed on that timestamp is unusable.

**Why 94% coverage did not catch either.** `tests/market/test_massive.py` builds snapshots from
`MagicMock`, which answers to any attribute name:

```python
snap.last_trade.timestamp = timestamp_ms      # an attribute the real model does not have
```

`test_timestamp_conversion` then locks in the wrong unit as well. The lesson generalizes:
**mocking a third-party model tests your assumptions about the library, not the library.**
Parsing tests must go through the real `TickerSnapshot.from_dict` with a documented payload
(§14.4). That test needs no network and would have failed on its first run.

### 8.5 The corrected parse

```python
NANOS_PER_SECOND = 1_000_000_000

for snap in snapshots:
    trade = snap.last_trade
    if trade is None or trade.price is None:
        continue                      # no print yet today; leave the ticker showing "—"
    self._cache.update(
        ticker=snap.ticker,
        price=trade.price,
        timestamp=(
            trade.sip_timestamp / NANOS_PER_SECOND
            if trade.sip_timestamp
            else time.time()
        ),
    )
    processed += 1
```

**Guarding on `is None` rather than catching `AttributeError` is what makes the difference.**
A genuinely absent field is a normal condition to handle; a misspelled attribute is a bug that
should be loud. The existing blanket `except AttributeError` is precisely what hid defect 1.

### 8.6 Error handling in the poll loop

The SDK raises only two exception types (`massive/exceptions.py`): `AuthError` (empty or missing
key, raised at construction) and `BadResponse` (any non-200 surviving the retry policy).
`urllib3` raises its own for connection failures and timeouts.

```python
from massive.exceptions import AuthError, BadResponse

async def _poll_once(self) -> None:
    if not self._tickers or not self._client:
        return
    try:
        snapshots = await asyncio.to_thread(self._fetch_snapshots)
    except AuthError:
        logger.error("Massive API key rejected — the source does not fall back automatically")
        raise                       # unrecoverable: do not retry on a loop
    except BadResponse as e:
        logger.warning("Massive returned an error response: %s", e)
        return                      # transient: retry next interval
    except Exception:
        logger.exception("Massive poll failed")
        return
    ...  # the §8.5 parse
```

`start()` performs one poll synchronously **before** creating the task, so the cache is warm
before the first client connects:

```python
async def _poll_loop(self) -> None:
    """Poll on interval. The first poll already happened in start()."""
    while True:
        await asyncio.sleep(self._interval)
        await self._poll_once()
```

### 8.7 Behaviors to surface in the README

Properties of the data source, not bugs — users will otherwise report them as bugs:

- **Unknown symbols vanish silently.** The v2 snapshot omits tickers it does not recognize; there
  is no error entry. The ticker sits in the watchlist showing `—` indefinitely.
- **Prices freeze outside market hours.** Overnight, at weekends, and on holidays the snapshot
  returns the previous session's last trade. The UI looks broken but is correct. **This is the
  main reason the simulator is the default.**
- **Free-tier data is 15 minutes delayed**, so prices will not match any other quote source the
  user has open.
- **Snapshot data is cleared at midnight ET** and repopulates from about 4am ET. Between those
  times `last_trade` may be absent entirely — exactly the `None` case §8.5 guards.

`client.get_market_status()` is worth one call to explain a frozen feed rather than leaving the
user guessing.

### 8.8 Live verification

Run once a real key exists — it confirms auth, the multi-ticker snapshot, and unit conversion in
one pass:

```python
# backend/scripts/verify_massive.py
"""Smoke-test the Massive REST API against a live key."""

import os
from datetime import UTC, datetime

from massive import RESTClient
from massive.rest.models import SnapshotMarketType

NANOS_PER_SECOND = 1_000_000_000
TICKERS = ["AAPL", "GOOGL", "MSFT", "NVDA", "TSLA"]


def main() -> None:
    client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])

    print(f"market: {client.get_market_status().market}")

    snapshots = client.get_snapshot_all(SnapshotMarketType.STOCKS, TICKERS)
    print(f"requested {len(TICKERS)}, received {len(snapshots)}")

    for snap in snapshots:
        trade = snap.last_trade
        if trade is None or trade.price is None:
            print(f"{snap.ticker}: no trade data")
            continue
        when = datetime.fromtimestamp(trade.sip_timestamp / NANOS_PER_SECOND, UTC)
        print(f"{snap.ticker}: ${trade.price:.2f} at {when:%Y-%m-%d %H:%M:%S} UTC")

    missing = set(TICKERS) - {s.ticker for s in snapshots}
    if missing:
        print(f"absent from response (unknown or untraded): {sorted(missing)}")


if __name__ == "__main__":
    main()
```

```bash
cd backend && uv run python scripts/verify_massive.py
```

Expected: a market status and five priced tickers with timestamps **in the recent past**.
Timestamps far in the future mean the divisor regressed; `AttributeError` means §8.4 regressed.

---

## 9. Selection — `create_market_data_source`

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the market data source indicated by the environment.

    MASSIVE_API_KEY set and non-empty -> MassiveDataSource (real data)
    otherwise                         -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source; the caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)

    logger.info("Market data source: GBM Simulator")
    return SimulatorDataSource(price_cache=price_cache)
```

**`.strip()` before the truth test is deliberate.** `.env` files routinely contain
`MASSIVE_API_KEY=` or a stray space, and a whitespace-only key would otherwise select the Massive
path and then fail every poll with a 401. Empty means empty.

**The choice is made once at startup and never at runtime.** A source that silently switched to
the simulator after a Massive outage would show users invented prices while they believed they
were seeing the market. Rejected keys and failed polls are logged; they do not change the source.
`GET /api/health` reports which one is live:

```json
{"status": "ok", "market_source": "simulator", "llm_mock": false}
```

Returning an **unstarted** source keeps construction synchronous and lets the caller decide the
ticker set from the database — the factory has no business reading tables.

---

## 10. The SSE stream

### 10.1 What ships

`GET /api/stream/prices`, `Content-Type: text/event-stream`. The generator opens with
`retry: 1000`, then pushes the **entire cache as one JSON object** whenever `version` changes,
polled every 500ms:

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.52, "previous_price": 190.48, "timestamp": 1755873791.482, "change": 0.04, "change_percent": 0.021, "direction": "up"}, "GOOGL": {...}}
```

One event carries every ticker. The client replaces its price map wholesale — no merge logic, no
missed-update reconciliation. Because a fresh generator starts at `last_version = -1`, the first
comparison always differs, so **every connecting client immediately receives a full snapshot**,
including after a reconnect. That is why no separate snapshot endpoint exists.

Response headers matter as much as the payload:

```python
return StreamingResponse(
    _generate_events(price_cache, request),
    media_type="text/event-stream",
    headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",   # disable nginx buffering if proxied
    },
)
```

**Why poll-and-push instead of event-driven?** A 500ms integer comparison is cheaper to reason
about than a pub/sub fan-out across an arbitrary number of generators, and it naturally coalesces:
if the cache updated ten tickers since the last check, the client gets one event, not ten.

### 10.2 Keepalive — to implement

When the version has not changed for 15 seconds, emit an SSE comment line. The complete generator:

```python
KEEPALIVE_SECONDS = 15.0


async def _generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = 0.5,
) -> AsyncGenerator[str, None]:
    """Yield SSE events whenever the cache version changes; ping when it does not."""
    yield "retry: 1000\n\n"

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
            elif time.monotonic() - last_sent >= KEEPALIVE_SECONDS:
                yield ": ping\n\n"
                last_sent = time.monotonic()

            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        logger.info("SSE stream cancelled for: %s", client_ip)
```

Without this, a Massive-backed feed sends no bytes between 15-second polls. That idle-times-out
through proxies and leaves the frontend unable to distinguish a quiet market from a dead
connection. The frontend indicator — green on `onopen`, yellow on `onerror`, red after a gap
beyond ~40 seconds — depends on it.

A line beginning with `:` is a comment in the SSE grammar: `EventSource` ignores it entirely, so
it costs the client nothing while keeping the socket warm.

---

## 11. `GET /api/prices/{ticker}/history` — to implement

Backed by `PriceCache.get_history` (§5.2). It belongs in `stream.py` next to the SSE endpoint,
since both are pure cache readers with no database involvement.

```python
history_router = APIRouter(prefix="/api/prices", tags=["prices"])


def create_history_router(price_cache: PriceCache) -> APIRouter:
    @history_router.get("/{ticker}/history")
    async def get_price_history(ticker: str, limit: int = 600) -> dict:
        """Rolling in-memory price history for the main chart.

        Returns an empty `points` list for an untracked ticker — not a 404,
        so the chart draws nothing rather than erroring.
        """
        ticker = ticker.strip().upper()
        limit = max(1, min(limit, HISTORY_MAXLEN))
        points = price_cache.get_history(ticker, limit=limit)
        return {
            "ticker": ticker,
            "points": [{"timestamp": ts, "price": price} for ts, price in points],
        }

    return history_router
```

Response:

```json
{"ticker": "AAPL", "points": [{"timestamp": 1755873791.482, "price": 190.52}]}
```

Oldest-first, matching what Recharts wants for a left-to-right time axis. `limit` is clamped
rather than validated with a 400 — a chart asking for 10,000 points should get 600, not an error.

This endpoint reads only in-memory state, so `async def` is correct here; there is no SQLite call
to keep off the event loop.

---

## 12. Wiring

### 12.1 Lifespan

One `PriceCache` and one source per process, owned by the FastAPI lifespan.

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()                       # lazy schema creation + seed (PLAN.md §7)

    cache = PriceCache()
    source = create_market_data_source(cache)

    # Reconciliation: watchlist ∪ held positions, not just the watchlist
    tickers = sorted(set(get_watchlist_tickers()) | set(get_position_tickers()))
    await source.start(tickers)

    app.state.price_cache = cache
    app.state.market_source = source
    try:
        yield
    finally:
        await source.stop()


app = FastAPI(lifespan=lifespan)

# 1. API routers FIRST
app.include_router(create_stream_router(cache))
app.include_router(create_history_router(cache))
# ... portfolio, watchlist, chat routers ...
# 2. static assets
# 3. catch-all -> index.html
```

Three things this gets right and are easy to get wrong:

**Reading both tables at startup**, not just the watchlist, is what makes a position held across
a restart come back with a live price. Without it, an off-watchlist holding valuates at `avg_cost`
forever and the snapshot task stalls under the "skip if any held ticker has no price" rule.

**The cache and source are passed explicitly** (via router factories or `app.state`) rather than
held in module globals, which is what keeps tests able to construct an isolated cache per test.

**Mount all `/api/*` routers before the static file mount.** A `StaticFiles(html=True)` mount at
`/` registered first shadows every endpoint, including the SSE stream (`PLAN.md` §11).

Route handlers reach the cache through `app.state` or a dependency:

```python
def get_price_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market_source(request: Request) -> MarketDataSource:
    return request.app.state.market_source
```

### 12.2 The tracked ticker set

**The tracked set is `watchlist ∪ {tickers with a non-zero position}`.**

The two sets diverge the moment a user buys TSLA and then removes it from the watchlist. The
position still needs a live price for valuation, P&L, the heatmap, and snapshots.

| Trigger | Action |
|---|---|
| `POST /api/watchlist` | always `await source.add_ticker(t)` |
| `DELETE /api/watchlist/{t}` | `await source.remove_ticker(t)` **only if no position in `t` is held** |
| Buy a ticker not currently tracked | `await source.add_ticker(t)` as part of trade execution |
| Sell a position to zero | if `t` is not on the watchlist, `await source.remove_ticker(t)` |
| `POST /api/reset` | re-sync the tracked set to exactly the ten default tickers |

One helper keeps the rule in one place rather than at four call sites:

```python
async def untrack_if_unused(source: MarketDataSource, ticker: str) -> None:
    """Stop tracking a ticker only if it is neither watched nor held."""
    if is_on_watchlist(ticker) or has_position(ticker):
        return
    await source.remove_ticker(ticker)
```

### 12.3 Ticker validation at the boundary

Applied at `POST /api/watchlist`, `POST /api/portfolio/trade`, and every LLM-proposed action, so
the market layer only ever sees canonical symbols:

```python
TICKER_PATTERN = re.compile(r"^[A-Z][A-Z.]{0,5}$")


def normalize_ticker(raw: str) -> str:
    """Uppercase and validate. Raises ValueError with the user-facing message."""
    ticker = raw.strip().upper()
    if not TICKER_PATTERN.match(ticker):
        raise ValueError("Invalid ticker symbol")
    return ticker
```

No allowlist. Any symbol matching the pattern is accepted; the simulator invents plausible
behavior for it, and under Massive an unknown symbol shows `—`. Rejecting unknown symbols would
make the LLM's `watchlist_changes` feature feel broken.

Uppercasing is not cosmetic: the `UNIQUE(user_id, ticker)` constraints would otherwise happily
hold both `AAPL` and `aapl`.

---

## 13. Failure modes

| Situation | Behavior | Where |
|---|---|---|
| Empty ticker list at startup | `step()` returns `{}`, SSE sends nothing until a ticker is added | §7.4 |
| One bad simulator tick | Logged, loop continues next interval | §7.6 |
| Massive poll fails (429, network) | Logged, cache keeps last prices, retry next interval | §8.6 |
| Massive key rejected | `AuthError` re-raised; **no automatic fallback to the simulator** | §8.6, §9 |
| Ticker has no `last_trade` yet | Skipped; ticker shows `—` | §8.5 |
| Held ticker has no cached price | Portfolio values it at `avg_cost`; snapshot task skips the write entirely | `PLAN.md` §7 |
| Ticker removed while held | Prevented by `untrack_if_unused` | §12.2 |
| Client disconnects mid-stream | `request.is_disconnected()` breaks the generator | §10.2 |
| Quiet feed (Massive, 15s polls) | `: ping` every 15s keeps the connection and the indicator alive | §10.2 |
| History requested for untracked ticker | `{"ticker": "X", "points": []}` | §11 |

---

## 14. Testing

Current state: **73 tests, 91% coverage** on the market module. `stream.py` sits at 33% — the SSE
generator is the least-tested code in the subsystem and the keepalive change is a good moment to
fix that.

```bash
cd backend
uv run --extra dev pytest -v
uv run --extra dev pytest --cov=app --cov-report=term-missing
```

### 14.1 A stub source

The cache and the tracked-set rules can be tested without either real source:

```python
class StubDataSource(MarketDataSource):
    """Records lifecycle calls; writes nothing on its own."""

    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache
        self._tickers: list[str] = []
        self.started = False

    async def start(self, tickers): self._tickers = list(tickers); self.started = True
    async def stop(self): self.started = False
    async def add_ticker(self, t):
        if t not in self._tickers:
            self._tickers.append(t)
    async def remove_ticker(self, t):
        self._tickers = [x for x in self._tickers if x != t]
        self._cache.remove(t)
    def get_tickers(self): return list(self._tickers)
```

### 14.2 Simulator

Seed **both** RNGs — the simulator uses `numpy.random` for the normal draws and stdlib `random`
for events:

```python
def test_step_is_reproducible():
    np.random.seed(42)
    random.seed(42)
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    first = sim.step()

    np.random.seed(42)
    random.seed(42)
    sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
    assert sim.step() == first
```

Statistical properties need wide tolerances and events disabled — a 5% jump is a massive outlier
at this `dt` and would dominate the sample variance:

```python
def test_realised_volatility_is_close_to_sigma():
    sim = GBMSimulator(tickers=["AAPL"], event_probability=0.0)
    prices = [sim.get_price("AAPL")]
    for _ in range(20_000):
        prices.append(sim.step()["AAPL"])

    log_returns = np.diff(np.log(prices))
    realised = log_returns.std() / np.sqrt(GBMSimulator.DEFAULT_DT)
    assert 0.15 < realised < 0.35      # nominal sigma is 0.22


def test_tech_tickers_are_positively_correlated():
    sim = GBMSimulator(tickers=["AAPL", "MSFT"], event_probability=0.0)
    a, m = [], []
    for _ in range(10_000):
        p = sim.step()
        a.append(p["AAPL"])
        m.append(p["MSFT"])

    rho = np.corrcoef(np.diff(np.log(a)), np.diff(np.log(m)))[0, 1]
    assert rho > 0.4        # nominal 0.6


def test_correlation_matrix_stays_positive_definite():
    """Run after ANY change to the correlation constants in seed_prices.py."""
    tickers = list(SEED_PRICES) + [f"UNK{i}" for i in range(40)]
    GBMSimulator(tickers=tickers)      # raises LinAlgError if not PD
```

Also cover: prices stay strictly positive over thousands of steps; `step()` returns exactly the
current ticker set; add/remove keeps `_tickers`/`_prices`/`_params` consistent and the Cholesky
shape matching; unknown tickers seed within $50–$300 with `DEFAULT_PARAMS`; `remove_ticker` on an
untracked symbol is a no-op.

### 14.3 Cache and history

```python
def test_history_is_bounded_and_oldest_first():
    cache = PriceCache(history_maxlen=5)
    for i in range(10):
        cache.update("AAPL", 100.0 + i, timestamp=float(i))

    points = cache.get_history("AAPL")
    assert len(points) == 5
    assert [ts for ts, _ in points] == [5.0, 6.0, 7.0, 8.0, 9.0]


def test_history_is_empty_for_untracked_ticker():
    assert PriceCache().get_history("NOPE") == []


def test_remove_clears_price_and_history():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    cache.remove("AAPL")
    assert cache.get("AAPL") is None
    assert cache.get_history("AAPL") == []
```

Plus: `previous_price` derivation, first-update `flat`, `version` monotonicity, and thread safety
under concurrent writers.

### 14.4 Massive — through the real model, never `MagicMock`

This is the test that would have caught both defects in §8.4, and it needs no network:

```python
from massive.rest.models.snapshot import TickerSnapshot

def test_snapshot_parse_produces_a_present_day_timestamp():
    snap = TickerSnapshot.from_dict({
        "ticker": "AAPL",
        "lastTrade": {"p": 190.52, "s": 100, "t": 1755873791482000000, "x": 4},
    })

    cache = PriceCache()
    source = MassiveDataSource(api_key="x", price_cache=cache)
    source._apply_snapshots([snap])          # extract the parse into a testable method

    update = cache.get("AAPL")
    assert update.price == 190.52
    assert 1_600_000_000 < update.timestamp < 2_000_000_000   # plausible present, in SECONDS


def test_snapshot_without_a_last_trade_is_skipped():
    snap = TickerSnapshot.from_dict({"ticker": "AAPL"})
    cache = PriceCache()
    source = MassiveDataSource(api_key="x", price_cache=cache)
    source._apply_snapshots([snap])
    assert cache.get("AAPL") is None
```

Extracting the parse loop into `_apply_snapshots(snapshots)` is worth the small refactor: it makes
the parse testable without touching HTTP, which is the only part that actually broke.

### 14.5 Factory and SSE

- **Factory** — unset, empty, and whitespace-only `MASSIVE_API_KEY` all select the simulator; a
  real value selects Massive.
- **SSE** — map-shaped payload, float timestamp, percent-unit `change_percent`, full snapshot on
  connect, and a `: ping` after 15 idle seconds. Drive the generator directly with a fake request
  object rather than through a live server; the keepalive test is far easier with an injected
  `interval` and a monkeypatched clock than with 15 seconds of real waiting.

### 14.6 Tracked set

The two regressions that silently produce a frozen position:

- Removing a watchlist ticker with an open position **keeps** it in the feed.
- Selling to zero while off-watchlist **removes** it.

### 14.7 Eyeballing it

```bash
cd backend && uv run market_data_demo.py
```

A Rich terminal dashboard of the live simulator — the fastest way to check whether a parameter
change still looks right. Statistical tests confirm σ; only the eye confirms "looks like a
trading terminal".

---

## 15. Implementation order

Small increments, each independently verifiable. Run `uv run --extra dev pytest` after every step.

1. **Fix the Massive parse** (§8.5). Extract `_apply_snapshots`, correct the attribute and the
   divisor, replace the `MagicMock` tests with `TickerSnapshot.from_dict` tests (§14.4). This is
   first because the current code silently produces nothing, and because the fix is provable
   offline.
2. **Add rolling history to `PriceCache`** (§5.2). Deque, `get_history`, `remove` clearing both.
   Tests in §14.3.
3. **Add `GET /api/prices/{ticker}/history`** (§11). Depends on step 2.
4. **Add the SSE keepalive** (§10.2) and raise `stream.py` coverage off 33% (§14.5).
5. **Wire the lifespan** (§12.1) with startup reconciliation over `watchlist ∪ positions`, and
   add `untrack_if_unused` (§12.2) where the watchlist and trade routes are built.

Steps 1–4 are self-contained in `app/market/`. Step 5 is the seam with the rest of the backend and
should land alongside the portfolio and watchlist routes, not before them.

---

## 16. Configuration reference

| Setting | Default | Where | Effect |
|---|---|---|---|
| `MASSIVE_API_KEY` | unset | env | Non-empty selects Massive; otherwise simulator |
| `update_interval` | `0.5` | `SimulatorDataSource` | Simulator tick rate — **change `dt` with it** |
| `event_probability` | `0.001` | `SimulatorDataSource` | Shock chance per ticker per tick |
| `poll_interval` | `15.0` | `MassiveDataSource` | Seconds between snapshot requests |
| `HISTORY_MAXLEN` | `600` | `cache.py` | Rolling history depth (~5 min at 500ms) |
| `KEEPALIVE_SECONDS` | `15.0` | `stream.py` | Idle gap before a `: ping` |
| SSE poll `interval` | `0.5` | `stream.py` | How often the version is checked |

### Tuning the simulator

| Want | Change | Watch for |
|---|---|---|
| More visible motion | Raise σ in `TICKER_PARAMS` | Above ~0.8 it stops looking like equity |
| Faster updates | `update_interval` | **`DEFAULT_DT` hard-codes the 500ms tick** — see below |
| More drama | Raise `event_probability` | Above ~0.005 the series becomes jumps, not prices |
| Bigger shocks | Widen `random.uniform(0.02, 0.05)` | Beyond ~10% the P&L chart loses all detail |
| Different sectors | Edit `CORRELATION_GROUPS` and coefficients | Re-run the positive-definiteness test (§14.2) |
| A trending market | Raise μ | μ is annualized; even 0.5 is barely visible over a demo |

**The `DEFAULT_DT` coupling is the one that catches people.** `DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR`
hard-codes the 500ms tick. Passing `update_interval=0.1` without also passing a matching `dt` runs
the simulation five times faster in model time, and annualized volatility silently becomes 5× what
`TICKER_PARAMS` claims.

---

## 17. Summary

| Concern | Resolution |
|---|---|
| Two sources, one consumer | `MarketDataSource` ABC + shared `PriceCache` |
| Which source | `create_market_data_source`, decided once at startup from `MASSIVE_API_KEY` |
| Default | Simulator — always alive, no key, no rate limit, any ticker |
| How prices are read | Only from the cache, never from the source |
| Which tickers are live | `watchlist ∪ positions`, reconciled at startup |
| Price model | GBM, per-ticker μ and σ, Cholesky-correlated by sector |
| Timestamp format | Unix epoch seconds (float), converted at each source boundary |
| Update delivery | SSE, full cache per event, on `version` change, `: ping` when idle |
| Chart backfill | 600-point in-memory deque per ticker, never persisted |
| Blocking I/O | `asyncio.to_thread` at the source, always |
| Failure handling | Per-cycle `try` inside the loop; the feed never dies from one bad tick |
| Outstanding work | The five steps in §15 |
