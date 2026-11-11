# Market Simulator — Approach and Code Structure

The default market data source when `MASSIVE_API_KEY` is not set. It must look convincingly like a live market on a demo screen: prices that tick every 500 ms, move in sensible amounts, move *together* by sector, and occasionally jump. It needs no network, no API key, and no state on disk.

Implemented in `backend/app/market/simulator.py` and `seed_prices.py`. It plugs into the unified interface described in `MARKET_INTERFACE.md`.

---

## 1. Model: Geometric Brownian Motion

Each ticker follows GBM, the standard model behind Black-Scholes:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )
```

| Symbol | Meaning | Source |
|---|---|---|
| `S(t)` | current price | simulator state |
| `μ` | annualised drift (expected return) | `TICKER_PARAMS[t]["mu"]` |
| `σ` | annualised volatility | `TICKER_PARAMS[t]["sigma"]` |
| `dt` | tick length as a fraction of a **trading** year | `0.5 / (252 × 6.5 × 3600) ≈ 8.48e-8` |
| `Z` | standard normal draw, **correlated** across tickers | Cholesky, §3 |

Why GBM:

- Prices stay **positive** (it's multiplicative: `exp(...) > 0`).
- Moves scale with price: a $800 stock moves more dollars than a $50 one, the same in percent.
- The `−σ²/2` term makes the *median* path drift at `μ` instead of drifting upward from volatility.
- Exact discretisation: no accumulated numerical error however many ticks run.

### What the numbers look like

`dt` is measured in trading time, so a demo running for an hour "moves" like an hour of a real session:

| Horizon | Scale factor `√(horizon/year)` | AAPL (σ 0.22) | TSLA (σ 0.50) | V (σ 0.17) |
|---|---|---|---|---|
| 1 tick (0.5 s) | 2.91e-4 | ±0.0064% (~1.2¢ at $190) | ±0.015% | ±0.005% |
| 1 minute | 3.19e-3 | ±0.07% | ±0.16% | ±0.05% |
| 1 hour | 2.47e-2 | ±0.54% | ±1.24% | ±0.42% |
| 6.5 h (1 trading day) | 6.30e-2 | ±1.4% | ±3.2% | ±1.1% |

(±1 standard deviation.) Per-tick moves are around a cent, so most ticks flash a direction, but the chart doesn't look like noise. Drift is negligible on demo time scales (`μ·dt ≈ 4e-9` per tick); it's there for realism, not effect.

---

## 2. Per-ticker parameters (`seed_prices.py`)

```python
SEED_PRICES = {"AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00, "TSLA": 250.00,
               "NVDA": 800.00, "META": 500.00, "JPM": 195.00, "V": 280.00, "NFLX": 600.00}

TICKER_PARAMS = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},  "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},  "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  "NVDA":  {"sigma": 0.40, "mu": 0.08},
    "META": {"sigma": 0.30, "mu": 0.05},  "JPM":   {"sigma": 0.18, "mu": 0.04},
    "V":    {"sigma": 0.17, "mu": 0.04},  "NFLX":  {"sigma": 0.35, "mu": 0.05},
}
DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}   # tickers added at runtime
```

Volatilities are roughly in line with each stock's long-run implied volatility: banks/payments low, mega-cap tech moderate, TSLA/NVDA high. Seed prices only need to be plausible — PLAN §6 says "realistic seed prices (e.g., AAPL ~$190)". Refresh them occasionally if desired; nothing depends on their exact values.

**Tickers not in the table** (added via the watchlist or AI chat) get `DEFAULT_PARAMS` and a seed price.

**[CHANGE]** Today that seed is `random.uniform(50, 300)`, so `PYPL` gets a different price on every restart, and a position bought at $210 can reopen at $80 after a container restart. Make it deterministic per ticker:

```python
def _seed_price_for(ticker: str) -> float:
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker]
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    return round(50 + (h % 25_000) / 100, 2)      # stable $50.00–$299.99
```

Better still, if the user holds a position in the ticker, seed from its `avg_cost` (the backend can pass a `seed_prices` override into `start()`), so restarting doesn't create a fake P&L jump. Prices restarting at seed on every boot is otherwise acceptable for a simulator.

---

## 3. Correlated moves (Cholesky)

Real stocks don't move independently: on a tech sell-off, AAPL, MSFT and NVDA all go red together. That's what makes the watchlist and heatmap look alive.

Correlation by sector:

| Pair | ρ |
|---|---|
| tech ↔ tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| finance ↔ finance (JPM, V) | 0.5 |
| TSLA ↔ anything | 0.3 ("does its own thing") |
| cross-sector, or any unknown ticker | 0.3 |

Every tick:

```python
z = np.random.standard_normal(n)     # n independent N(0,1)
z_corr = L @ z                       # L = cholesky(C), C = correlation matrix
```

If `C = L·Lᵀ`, then `Cov(L·z) = L·I·Lᵀ = C`, so the draws have exactly the target correlations and each still has unit variance.

- `C` is built in `_rebuild_cholesky()` from `_pairwise_correlation(t1, t2)` and re-factored only when tickers are added or removed (O(n³), trivial for n < 50). The per-tick cost is one matrix-vector multiply.
- This block structure (all off-diagonals 0.3–0.6, higher within groups) is positive-definite for any number of tickers, so `np.linalg.cholesky` won't fail. If someone adds new groups with arbitrary values, guard it: on `LinAlgError`, fall back to independent draws and log a warning.
- n = 1 → no matrix, plain draw.

To add a sector, add a set to `CORRELATION_GROUPS` and a branch in `_pairwise_correlation`.

---

## 4. Random events (drama)

After the GBM step, each ticker has a small chance of a sudden jump:

```python
if random.random() < event_probability:          # default 0.001 per ticker per tick
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= 1 + shock
```

With 10 tickers at 2 ticks/s that's `10 × 2 × 0.001 = 0.02` events/s → **about one every 50 s** somewhere on the board. A 2–5% jump is roughly 4–9 hours of normal AAPL movement in one tick, so it's clearly visible in sparklines, the heatmap and the P&L chart. Jumps are idiosyncratic (one ticker, not correlated) and symmetric up/down, so they don't bias the drift.

`event_probability` is a constructor argument: tests pass `0.0` for determinism and `1.0` to force events.

---

## 5. Code structure

```
backend/app/market/
├── seed_prices.py     # data only: SEED_PRICES, TICKER_PARAMS, DEFAULT_PARAMS,
│                      #            CORRELATION_GROUPS, correlation constants
└── simulator.py
    ├── GBMSimulator          # pure, synchronous math engine — no asyncio, no cache
    │   ├── __init__(tickers, dt=DEFAULT_DT, event_probability=0.001)
    │   ├── step() -> dict[str, float]        # advance one tick, return rounded prices
    │   ├── add_ticker(t) / remove_ticker(t)  # rebuild Cholesky
    │   ├── get_price(t) -> float | None
    │   ├── get_tickers() -> list[str]
    │   ├── _add_ticker_internal(t)           # batch add without refactoring
    │   ├── _rebuild_cholesky()
    │   └── _pairwise_correlation(t1, t2)     # staticmethod
    └── SimulatorDataSource(MarketDataSource) # async adapter: GBMSimulator → PriceCache
        ├── __init__(price_cache, update_interval=0.5, event_probability=0.001)
        ├── start(tickers)    # build simulator, seed cache, launch task "simulator-loop"
        ├── stop()            # cancel task; idempotent
        ├── add_ticker(t)     # add to sim + write price to cache immediately
        ├── remove_ticker(t)  # remove from sim + evict from cache
        ├── get_tickers()
        └── _run_loop()       # while True: step → cache.update(...) → sleep(interval)
```

Why two classes: `GBMSimulator` is pure math with no I/O, so it's unit-testable without an event loop (seed numpy, call `step()`, assert). `SimulatorDataSource` is only lifecycle and plumbing, and is what satisfies the `MarketDataSource` contract.

Implementation notes:

- **Internal prices are unrounded floats.** Only the values returned from `step()` (and written to the cache) are rounded to 2 dp. Rounding the internal state would add a bias on low-priced stocks.
- **Hot path.** `step()` runs every 500 ms. It does one numpy draw, one matvec and a Python loop over n tickers — microseconds for n ≈ 10–50. No per-tick allocation of the correlation matrix.
- **Loop resilience.** `_run_loop` wraps each step in `try/except Exception: logger.exception(...)`, then always sleeps, so one bad tick can't kill the stream. `CancelledError` propagates for a clean `stop()`.
- **Concurrency.** Everything runs on the event loop thread; `add_ticker`/`remove_ticker` are `async` but don't `await` between mutating the simulator and the cache, so they can't interleave with `step()`.
- **Timing.** `asyncio.sleep(interval)` after work means the period is slightly over 500 ms. That's fine; `dt` is fixed per tick, so simulated volatility doesn't depend on wall-clock jitter.

**[CHANGE]** Small items to fold in when the backend is built:
1. Normalise tickers with `.strip().upper()` in `SimulatorDataSource.add_ticker/remove_ticker` (the Massive source does).
2. Deterministic seed prices for unknown tickers (§2).
3. Pass `reference_price` (the starting price) to `cache.update` so the watchlist can show change since the session started (`MARKET_INTERFACE.md` §2.1).
4. Optional `seed: int | None` constructor argument that seeds a private `np.random.Generator` and `random.Random`, for reproducible demos and tests without touching global RNG state.

---

## 6. Usage

```python
from app.market import PriceCache
from app.market.simulator import SimulatorDataSource, GBMSimulator

# As a data source (what the app uses, via the factory)
cache = PriceCache()
source = SimulatorDataSource(cache)
await source.start(["AAPL", "MSFT", "TSLA"])
cache.get_price("AAPL")        # 190.0 immediately, then moves every 0.5 s
await source.add_ticker("PYPL")
await source.stop()

# The math engine on its own (tests, notebooks, the Rich demo)
sim = GBMSimulator(["AAPL", "MSFT"], event_probability=0.0)
for _ in range(7200):          # one simulated hour
    prices = sim.step()
```

Live terminal demo: `cd backend && uv run market_data_demo.py`.

---

## 7. Testing

Existing tests: `tests/market/test_simulator.py` (engine) and `test_simulator_source.py` (async adapter). What they should cover:

| Area | Test |
|---|---|
| Validity | Prices stay positive after 10k steps with high σ; `step()` returns every ticker, rounded to 2 dp |
| GBM statistics | With `np.random.seed`, events off and many steps, the std of log-returns ≈ `σ·√dt` (±10%) |
| Correlation | Over ~5k steps, sample correlation of log-returns: AAPL–MSFT ≈ 0.6, JPM–V ≈ 0.5, AAPL–JPM ≈ 0.3 (±0.1) |
| Events | `event_probability=1.0` → every tick moves ≥2% before diffusion; `0.0` → no tick moves >0.5% |
| Dynamic tickers | add/remove rebuild the matrix with the right shape; add is idempotent; unknown ticker gets default params |
| Edge cases | Empty simulator `step() == {}`; single ticker (no Cholesky) |
| Source | `start` seeds cache; loop updates `cache.version`; `add_ticker` writes immediately; `remove_ticker` evicts; `stop` twice is safe |

---

## 8. Tuning knobs

| Want | Change |
|---|---|
| Livelier board | Raise `sigma` values, or `dt` (e.g. ×10 makes each tick feel like 5 s of trading) |
| More/fewer jumps | `event_probability` (0.001 ≈ one per 50 s across 10 tickers) |
| Smoother/faster ticks | `update_interval` (keep `dt` consistent: `dt = interval / TRADING_SECONDS_PER_YEAR`) |
| Stronger sector moves | `INTRA_TECH_CORR` up to ~0.8 |
| Mean-reversion (prices don't wander far over long sessions) | Not implemented; would require an Ornstein-Uhlenbeck term on log-price. Not needed for demo durations. |
