# Market Simulator — Design and Code Structure

The simulator generates realistic-looking stock price movements without any external API. It is the default when `MASSIVE_API_KEY` is not set.

---

## Design Goals

- Visually convincing price movements for a demo/course environment
- Correlated moves across related stocks (tech sector, finance sector)
- Occasional dramatic events to keep the UI interesting
- Runs as an in-process asyncio background task — no external dependencies

---

## Mathematical Model — Geometric Brownian Motion (GBM)

Each ticker's price evolves as:

```
S(t+dt) = S(t) * exp( (mu - sigma²/2) * dt  +  sigma * sqrt(dt) * Z )
```

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `mu` | Annualised drift (expected return, e.g. 0.05 = 5%/year) |
| `sigma` | Annualised volatility (e.g. 0.25 = 25%/year) |
| `dt` | Time step as a fraction of a trading year |
| `Z` | Standard normal random variable (correlated across tickers) |

**Time step**: ticks occur every 500ms.

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800
dt = 0.5 / TRADING_SECONDS_PER_YEAR           # ≈ 8.48e-8
```

This tiny `dt` means each tick produces sub-cent moves that accumulate naturally over time, matching how real intraday charts look.

---

## Correlated Moves (Cholesky Decomposition)

Real stocks in the same sector move together. The simulator replicates this using a Cholesky decomposition of a correlation matrix.

**Correlation structure:**

| Pair | Correlation |
|------|-------------|
| Two tech stocks (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Two finance stocks (JPM, V) | 0.5 |
| TSLA with anything | 0.3 (behaves independently) |
| Cross-sector | 0.3 |
| Unknown tickers | 0.3 |

**Algorithm per tick:**
1. Generate `n` independent standard normals: `Z_independent ~ N(0, I)`
2. Multiply by the Cholesky factor `L` of the correlation matrix: `Z_correlated = L @ Z_independent`
3. Apply each `Z_correlated[i]` to its ticker's GBM formula

When tickers are added or removed, `_rebuild_cholesky()` recomputes `L` from scratch — O(n²) but n is always small (<50).

---

## Random Shock Events

Every tick, each ticker has a 0.1% chance of a sudden 2–5% shock (up or down). With 10 tickers at 2 ticks/sec, a shock occurs roughly every 50 seconds. This produces the kind of dramatic single-bar moves that make charts interesting.

```python
if random.random() < self._event_prob:  # default: 0.001
    magnitude = random.uniform(0.02, 0.05)
    sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + magnitude * sign
```

---

## Per-Ticker Parameters

Defined in `seed_prices.py`:

| Ticker | Seed Price | Sigma (vol) | Mu (drift) | Notes |
|--------|-----------|-------------|------------|-------|
| AAPL   | $190      | 0.22        | 0.05       | |
| GOOGL  | $175      | 0.25        | 0.05       | |
| MSFT   | $420      | 0.20        | 0.05       | Lowest vol in tech |
| AMZN   | $185      | 0.28        | 0.05       | |
| TSLA   | $250      | 0.50        | 0.03       | High vol, lower drift |
| NVDA   | $800      | 0.40        | 0.08       | High vol, strong drift |
| META   | $500      | 0.30        | 0.05       | |
| JPM    | $195      | 0.18        | 0.04       | Low vol (bank) |
| V      | $280      | 0.17        | 0.04       | Lowest vol overall |
| NFLX   | $600      | 0.35        | 0.05       | |
| Unknown| $50–$300  | 0.25        | 0.05       | Random seed, default params |

---

## Code Structure

### `GBMSimulator` — Pure Math

Lives in `simulator.py`. Stateful — holds current prices, params, Cholesky matrix.

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None: ...

    def step(self) -> dict[str, float]:
        """Advance all prices by one dt. Returns {ticker: new_price}.
        This is the hot path — called every 500ms."""

    def add_ticker(self, ticker: str) -> None:
        """Add ticker and rebuild Cholesky."""

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker and rebuild Cholesky."""

    def get_price(self, ticker: str) -> float | None: ...
    def get_tickers(self) -> list[str]: ...
```

`GBMSimulator` has no I/O and no asyncio — it's a pure math engine that can be tested synchronously.

### `SimulatorDataSource` — Async Wrapper

Also in `simulator.py`. Implements `MarketDataSource`. Owns the asyncio background task.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None: ...

    async def start(self, tickers: list[str]) -> None:
        # Creates GBMSimulator, seeds PriceCache with initial prices,
        # starts _run_loop() as an asyncio Task

    async def stop(self) -> None:
        # Cancels the background task

    async def add_ticker(self, ticker: str) -> None:
        # Delegates to GBMSimulator, seeds PriceCache immediately

    async def remove_ticker(self, ticker: str) -> None:
        # Delegates to GBMSimulator, removes from PriceCache

    async def _run_loop(self) -> None:
        # Core loop: call sim.step(), write to cache, sleep(interval)
```

### `seed_prices.py` — Configuration

Separate module to keep parameters easy to find and adjust:

```python
SEED_PRICES: dict[str, float]            # Starting price per ticker
TICKER_PARAMS: dict[str, dict[str, float]]  # sigma and mu per ticker
DEFAULT_PARAMS: dict[str, float]         # Fallback for unknown tickers
CORRELATION_GROUPS: dict[str, set[str]]  # "tech" and "finance" groups
INTRA_TECH_CORR = 0.6
INTRA_FINANCE_CORR = 0.5
CROSS_GROUP_CORR = 0.3
TSLA_CORR = 0.3
```

---

## Data Flow

```
asyncio Task: _run_loop()
    │
    ├── every 500ms: GBMSimulator.step()
    │       ├── generate correlated Z via Cholesky
    │       ├── apply GBM formula to each price
    │       └── apply random shock (0.1% chance)
    │
    └── for each new price: PriceCache.update(ticker, price)
            └── creates PriceUpdate(ticker, price, previous_price, timestamp)
                    └── SSE stream reads from cache
```

---

## Initialisation Sequence

```
SimulatorDataSource.start(tickers):
    1. Create GBMSimulator(tickers) → initialises prices from SEED_PRICES
    2. For each ticker: PriceCache.update(ticker, price)  ← so SSE has data immediately
    3. asyncio.create_task(_run_loop())
```

This ensures the cache is populated before any SSE client connects.

---

## Testing

The `GBMSimulator` and `SimulatorDataSource` are tested in `backend/tests/market/`:

- `test_simulator.py` — 17 tests covering GBM math, correlation, shock events, add/remove ticker
- `test_simulator_source.py` — 10 integration tests covering lifecycle, cache writes, add/remove

Key test patterns:
- Call `step()` many times and assert price stays positive (GBM guarantee)
- Assert correlated tickers have higher return correlation than cross-sector pairs
- Assert `add_ticker` and `remove_ticker` correctly update `get_tickers()`
- Assert cache is seeded immediately on `start()` (not after first tick)
