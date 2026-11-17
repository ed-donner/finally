# Market Simulator: Approach and Code Structure

The default market data source. It needs no API key and makes no network calls, and it produces prices that *look* like a live market: continuous small moves, correlated sectors, occasional dramatic jumps. It implements the `MarketDataSource` contract from `MARKET_INTERFACE.md`.

Code: `backend/app/market/simulator.py` (engine plus source) and `backend/app/market/seed_prices.py` (data).

---

## 1. Goals

| Goal | How |
|---|---|
| Realistic-looking ticks every ~500 ms | Geometric Brownian Motion (GBM) with per-ticker volatility |
| Prices never go negative | GBM is multiplicative (`exp(...)`) |
| "Tech moves together" | Correlated normal draws via a Cholesky factor of a sector correlation matrix |
| Drama for the demo | Random 2–5% shock events |
| Sensible starting points | Seed prices near real levels; random $50–$300 for unknown tickers |
| Dynamic watchlist | Add or remove tickers at runtime; the correlation matrix is rebuilt |
| Cheap | One numpy draw and one matrix-vector product per tick; n ≤ ~50 |
| Testable | Pure, synchronous engine (`GBMSimulator`), separate from the async wrapper |

The goal is **not** a faithful market model: no mean reversion, no volatility clustering, no order book, no market hours.

---

## 2. The maths

### 2.1 GBM step

For each ticker with price `S`, drift `μ` and volatility `σ` (both annualised):

```
S(t+Δt) = S(t) · exp( (μ − σ²/2)·Δt + σ·√Δt·Z )
```

`Z ~ N(0,1)`, correlated across tickers (§2.2). The `−σ²/2` term (Itô correction) makes the *expected* growth rate equal `μ`.

### 2.2 Time step

Ticks represent real elapsed time over a trading year:

```
TRADING_SECONDS_PER_YEAR = 252 days × 6.5 h × 3600 s = 5,896,800
Δt = 0.5 / 5,896,800 ≈ 8.48e-8
```

So the simulator runs at **real-time speed**. The size of moves this produces:

| Horizon | σ = 0.22 (AAPL) | σ = 0.50 (TSLA) |
|---|---|---|
| per tick (0.5 s) | ~0.0064% ≈ $0.012 on $190 | ~0.015% ≈ $0.036 on $250 |
| 1 minute | ~0.07% | ~0.16% |
| 1 hour | ~0.54% | ~1.2% |

Most ticks move the price by about a cent, so some 2-dp prices come out "flat". The flash animation still fires often because of correlation and the higher-σ names. Over a few minutes, sparklines show clear trends. Drift (`μ` ≈ 5%/yr) has no visible effect at demo time scales; it is kept for correctness.

> **Tuning knob:** to make the demo livelier, scale Δt (for example `dt = DEFAULT_DT * 10`). That speeds up the market's clock without changing the tick rate. Leave the default at real time.

### 2.3 Correlated draws (Cholesky)

Build an `n×n` correlation matrix `C` from sector membership, factor it once as `C = L·Lᵀ` (`np.linalg.cholesky`), and on each tick:

```python
z = np.random.standard_normal(n)   # independent
z_corr = L @ z                     # Cov(z_corr) = L·Lᵀ = C
```

Pairwise correlation rules (`_pairwise_correlation`), applied in order:

| Pair | ρ |
|---|---|
| either is `TSLA` | 0.3 (TSLA "does its own thing" even though it is a tech name) |
| both in tech {AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX} | 0.6 |
| both in finance {JPM, V} | 0.5 |
| anything else (cross-sector, unknown tickers) | 0.3 |

A block structure with these values is positive definite, so Cholesky always succeeds. If more groups or custom values are added later, wrap the factorisation in `try/except np.linalg.LinAlgError` and fall back to independent draws (`L = None`).

The matrix is rebuilt (O(n²) to fill, O(n³) to factor) **only when the ticker set changes**, never per tick. With n ≤ 30 (the watchlist cap) plus a few position-only tickers, this takes microseconds.

### 2.4 Shock events

After the GBM step, each ticker independently has a chance `event_probability` (default `0.001`) of a jump:

```python
if random.random() < event_prob:
    S *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
```

10 tickers × 2 ticks/s × 0.001 ≈ **one event every ~50 s** across the watchlist. That is often enough to keep the demo interesting without becoming noise. The jump is permanent (it changes the level), like a news event.

---

## 3. Data: `seed_prices.py`

Plain module-level constants so they are easy to tweak and to import in tests:

```python
SEED_PRICES: dict[str, float]               # AAPL 190, GOOGL 175, MSFT 420, AMZN 185, TSLA 250,
                                            # NVDA 800, META 500, JPM 195, V 280, NFLX 600
TICKER_PARAMS: dict[str, dict[str, float]]  # {"sigma": ..., "mu": ...} per default ticker
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # for tickers added at runtime
CORRELATION_GROUPS = {"tech": {...}, "finance": {"JPM", "V"}}
INTRA_TECH_CORR = 0.6; INTRA_FINANCE_CORR = 0.5; CROSS_GROUP_CORR = 0.3; TSLA_CORR = 0.3
```

The volatilities are chosen to *feel* right rather than to match real values: banks and payments 0.17–0.18, mega-cap tech 0.20–0.30, NFLX 0.35, NVDA 0.40, TSLA 0.50.

Unknown tickers (for example `PYPL` added by the user) start at `random.uniform(50, 300)` with `DEFAULT_PARAMS` and ρ = 0.3 against everything. To make a ticker look realistic, add it to `SEED_PRICES`, `TICKER_PARAMS` and a correlation group. No code changes are needed.

---

## 4. Code structure

The engine (pure, sync) is separate from the source (async, writes to the cache):

```
simulator.py
├── class GBMSimulator              # pure math + state, no asyncio, no cache
│   ├── __init__(tickers, dt=DEFAULT_DT, event_probability=0.001)
│   ├── step() -> dict[str, float]  # hot path: advance all tickers one Δt
│   ├── add_ticker(t) / remove_ticker(t)   # idempotent; rebuild Cholesky
│   ├── get_price(t) -> float | None
│   ├── get_tickers() -> list[str]
│   ├── _add_ticker_internal(t)     # seed price + params, no rebuild (batch init)
│   ├── _rebuild_cholesky()         # None when n <= 1
│   └── _pairwise_correlation(a, b) # staticmethod, sector rules
│
└── class SimulatorDataSource(MarketDataSource)
    ├── __init__(price_cache, update_interval=0.5, event_probability=0.001)
    ├── start(tickers)   # build GBMSimulator, write seed prices to cache, spawn task
    ├── stop()           # cancel + await task; idempotent
    ├── add_ticker(t)    # sim.add_ticker + write its price to cache immediately
    ├── remove_ticker(t) # sim.remove_ticker + cache.remove
    ├── get_tickers()
    └── _run_loop()      # while True: step → cache.update each → sleep(interval)
```

### 4.1 Engine state

```python
self._tickers: list[str]                    # order == row/col order of the Cholesky factor
self._prices:  dict[str, float]             # full precision (never rounded internally)
self._params:  dict[str, dict[str, float]]  # {"mu", "sigma"}
self._cholesky: np.ndarray | None
```

Prices are stored at **full precision** and only rounded to 2 dp in `step()`'s return value (the cache rounds again). Rounding the internal state would add a bias that builds up over thousands of ticks.

### 4.2 Hot path

```python
def step(self) -> dict[str, float]:
    n = len(self._tickers)
    if n == 0:
        return {}
    z = np.random.standard_normal(n)
    if self._cholesky is not None:
        z = self._cholesky @ z
    out = {}
    for i, t in enumerate(self._tickers):
        mu, sigma = self._params[t]["mu"], self._params[t]["sigma"]
        self._prices[t] *= math.exp((mu - 0.5 * sigma**2) * self._dt
                                    + sigma * math.sqrt(self._dt) * z[i])
        if random.random() < self._event_prob:
            self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
        out[t] = round(self._prices[t], 2)
    return out
```

### 4.3 Async loop

```python
async def _run_loop(self) -> None:
    while True:
        try:
            for ticker, price in self._sim.step().items():
                self._cache.update(ticker=ticker, price=price)
        except Exception:
            logger.exception("Simulator step failed")   # never let the loop die
        await asyncio.sleep(self._interval)
```

- It runs on the event loop. `step()` takes microseconds, so there is no need for a thread.
- `add_ticker` and `remove_ticker` are called from request handlers on the **same** event loop, so they never run concurrently with `step()`. No lock is needed inside the engine.
- The sleep is a fixed 0.5 s after each step, so there is small drift (0.5 s + step time). That is fine for a simulation.
- The cache timestamp is `time.time()` at write time.

### 4.4 Lifecycle and the cache

| Call | Effect on cache |
|---|---|
| `start(["AAPL", ...])` | writes every seed price → first `PriceUpdate` has `direction="flat"`, `session_open = seed price` |
| each tick | `update()` for every tracked ticker → `version` bumps → SSE emits one batch |
| `add_ticker("PYPL")` | writes PYPL's start price **immediately**, so it can be traded straight away |
| `remove_ticker("PYPL")` | `cache.remove("PYPL")` |

Because the seed price is the first value written, **session open = seed price** in simulator mode (PLAN §6).

---

## 5. Testing

The engine is pure, so most tests need no asyncio:

- **Valid prices:** after 10,000 steps every price is `> 0` and finite.
- **GBM statistics:** with events turned off (`event_probability=0`), a large `dt` (for example `1/252`) and many steps, the sample mean and std of `log(S_{t+1}/S_t)` should match `(μ−σ²/2)Δt` and `σ√Δt` within tolerance.
- **Correlation:** with events off, the empirical correlation of log-returns for AAPL/MSFT is ≈ 0.6, for AAPL/TSLA ≈ 0.3, and for JPM/V ≈ 0.5 (±0.05 over ~20k steps).
- **Events:** `event_probability=1.0` gives a jump of 2–5% every step (compare against the step with `event_probability=0` and the same seed).
- **Dynamic tickers:** add, remove, then add again works; `add_ticker` is idempotent; Cholesky shape is `(n, n)` and `None` for n ≤ 1; an unknown ticker's start price is in [50, 300].
- **Source:** `start()` populates the cache before returning; `add_ticker` makes `cache.get_price` non-None immediately; `stop()` twice is safe; no writes after `stop()`.

**Determinism:** the engine currently uses the global `np.random` and `random` modules, so tests call `np.random.seed(…)` and `random.seed(…)`. A cleaner later refactor is to accept an `rng: np.random.Generator | None = None` and draw everything (normals, event coin flips, magnitudes, unknown-ticker start prices) from it. That makes simulations reproducible without global state.

Existing coverage: `tests/market/test_simulator.py` (17 tests) and `test_simulator_source.py` (10 tests).

---

## 6. Possible extensions (not required)

| Idea | Sketch |
|---|---|
| Faster "demo clock" | `SIM_SPEED` multiplier on Δt |
| Mean reversion | Ornstein–Uhlenbeck pull toward the seed on log-price |
| Volatility clustering | GARCH(1,1)-style σ update after big moves |
| Market-wide events | One shock applied to every ticker (scaled by beta) |
| Sector config | Move groups/ρ to a dict-of-dicts so new sectors need no code change |
