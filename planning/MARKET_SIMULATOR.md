# Market Simulator Design

Design for `SimulatorDataSource` in `backend/app/market/simulator.py` — the default price feed
used whenever `MASSIVE_API_KEY` is not set (`PLAN.md` §6). It implements the same
`MarketDataSource` interface described in `MARKET_INTERFACE.md`, so nothing downstream needs to
know a simulator is running instead of live Massive data.

## 1. Goals

- Realistic-*looking* price action: continuous small moves, occasional visible jumps, correlated
  sector behavior — not just uncorrelated random noise
- Zero external dependencies — runs entirely in-process, no network calls, works offline
- Deterministic enough to test (seedable RNG), lively enough to demo well
- Updates at ~500ms per `PLAN.md` §6, matching the cadence the SSE stream pushes to the frontend

## 2. Model: Geometric Brownian Motion (GBM)

Each ticker's price follows discrete-time GBM, the standard model for simulating a stock price
path:

```
S(t + dt) = S(t) * exp((μ - σ²/2) * dt + σ * sqrt(dt) * Z)
```

where:
- `S(t)` — current price
- `μ` (mu) — annualized drift (expected return)
- `σ` (sigma) — annualized volatility
- `dt` — time step, expressed in *years* (so a 500ms tick is a very small `dt`)
- `Z` — a standard normal random draw (`N(0, 1)`)

GBM is used because it's the textbook model for equity prices: returns are log-normally
distributed, prices never go negative, and `μ`/`σ` map directly onto real-world "this stock trends
up slowly but is choppy" (high σ) vs. "steady mover" (low σ) intuitions — easy to tune per ticker.

### Converting the update cadence into `dt`

With ticks every 500ms and 252 trading days/year × 6.5 trading hours/day of "market time" as the
reference frame (a simplification — the sim runs continuously, not just market hours, since it's a
demo):

```python
TICKS_PER_YEAR = 252 * 6.5 * 3600 / 0.5   # ≈ 11,793,600 ticks/year
dt = 1 / TICKS_PER_YEAR
```

This keeps per-tick moves small (fractions of a percent) so 500ms updates look like continuous
streaming price action rather than jumpy random walks, while still compounding into plausible
daily/weekly ranges over a longer demo session.

## 3. Per-Ticker Parameters and Seed Prices

Each ticker gets a starting price and its own `(μ, σ)` pair, tuned to feel roughly true to the real
stock's character (not intended to be predictive — just recognizable):

```python
# seed_prices.py
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.0, "GOOGL": 175.0, "MSFT": 420.0, "AMZN": 185.0, "TSLA": 250.0,
    "NVDA": 120.0, "META": 500.0, "JPM": 200.0, "V": 275.0, "NFLX": 650.0,
}

GBM_PARAMS: dict[str, tuple[float, float]] = {
    # ticker: (annual drift, annual volatility)
    "AAPL":  (0.10, 0.25),
    "GOOGL": (0.10, 0.28),
    "MSFT":  (0.12, 0.22),
    "AMZN":  (0.12, 0.30),
    "TSLA":  (0.05, 0.55),   # high volatility, near-zero net drift — famously choppy
    "NVDA":  (0.20, 0.45),   # high growth, high volatility
    "META":  (0.10, 0.32),
    "JPM":   (0.08, 0.20),   # financials: steadier
    "V":     (0.09, 0.18),
    "NFLX":  (0.11, 0.35),
}
```

## 4. Correlated Moves Across Tickers

Real markets don't move ticker-by-ticker independently — sectors move together (tech stocks rally
or sell off as a group; financials react to rate news together). Independent random draws per
ticker look obviously fake once you watch the demo for more than a few seconds.

**Approach: Cholesky decomposition of a sector correlation matrix.**

1. Assign each ticker a sector group:
   ```python
   SECTOR: dict[str, str] = {
       "AAPL": "tech", "GOOGL": "tech", "MSFT": "tech", "NVDA": "tech", "META": "tech",
       "AMZN": "consumer", "TSLA": "consumer", "NFLX": "consumer",
       "JPM": "finance", "V": "finance",
   }
   ```
2. Build a target correlation matrix `Σ` from a few constants:
   ```python
   SAME_SECTOR_CORR = 0.6     # e.g. AAPL vs MSFT
   CROSS_GROUP_CORR = 0.3     # e.g. AAPL vs JPM
   FINANCE_CORR = 0.5         # JPM vs V, slightly tighter than the general cross-sector figure
   ```
   with 1.0 on the diagonal.
3. Compute the Cholesky factor `L` such that `L @ L.T == Σ` once at startup (it's fixed for the
   life of the process, since sector assignments don't change).
4. On every tick, draw one vector of independent standard normals `Z_indep` (length = number of
   tickers), then correlate them: `Z_correlated = L @ Z_indep`. Feed each ticker's entry from
   `Z_correlated` into its own GBM step as `Z` in the formula in §2.

```python
# simulator.py (sketch)
import numpy as np

class GBMSimulator:
    def __init__(self, tickers: list[str], seed: int | None = None) -> None:
        self._tickers = tickers
        self._rng = np.random.default_rng(seed)
        self._prices = {t: SEED_PRICES[t] for t in tickers}
        self._corr = _build_correlation_matrix(tickers)   # Σ, from SECTOR groups
        self._chol = np.linalg.cholesky(self._corr)

    def step(self, dt: float) -> dict[str, float]:
        z_indep = self._rng.standard_normal(len(self._tickers))
        z_corr = self._chol @ z_indep
        new_prices = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = GBM_PARAMS[ticker]
            s = self._prices[ticker]
            z = z_corr[i]
            new_prices[ticker] = s * math.exp((mu - sigma**2 / 2) * dt + sigma * math.sqrt(dt) * z)
        new_prices = self._maybe_apply_shocks(new_prices)
        self._prices = new_prices
        return new_prices

    def get_tickers(self) -> list[str]:
        return list(self._tickers)
```

Adding a ticker at runtime (via watchlist) appends a row/column to the correlation matrix (default
it to `CROSS_GROUP_CORR` against everything unless it matches a known `SECTOR` entry) and
recomputes the Cholesky factor — cheap at this scale (≤ a few dozen tickers).

## 5. Random Shock Events

Continuous GBM alone looks smooth and a little boring for a demo. Per `PLAN.md` §6, the simulator
adds occasional sudden moves:

- Each tick, each ticker independently has a small probability (~0.1%) of a "shock"
- A shock is a one-off 2–5% move (uniformly sampled magnitude, random sign) applied on top of the
  normal GBM step for that tick only — it does not alter `μ`/`σ` going forward
- At ~2 ticks/second this yields roughly one shock every few minutes across a 10-ticker watchlist,
  often enough to be noticeable in a live demo without dominating the price action

```python
SHOCK_PROBABILITY = 0.001
SHOCK_MAGNITUDE_RANGE = (0.02, 0.05)

def _maybe_apply_shocks(self, prices: dict[str, float]) -> dict[str, float]:
    for ticker in prices:
        if self._rng.random() < SHOCK_PROBABILITY:
            magnitude = self._rng.uniform(*SHOCK_MAGNITUDE_RANGE)
            sign = self._rng.choice([-1, 1])
            prices[ticker] *= 1 + sign * magnitude
    return prices
```

## 6. `SimulatorDataSource`: Wiring into the `MarketDataSource` Interface

```python
# simulator.py (sketch, continued)
class SimulatorDataSource(MarketDataSource):
    def __init__(self, cache: PriceCache, tick_interval: float = 0.5,
                 seed: int | None = None) -> None:
        self._cache = cache
        self._interval = tick_interval
        self._seed = seed
        self._sim: GBMSimulator | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers, seed=self._seed)
        self._running = True
        self._task = asyncio.create_task(self._tick_loop())

    async def _tick_loop(self) -> None:
        dt = 1 / TICKS_PER_YEAR
        while self._running:
            new_prices = self._sim.step(dt)
            for ticker, price in new_prices.items():
                prev = self._cache.get_price(ticker)
                baseline = prev if prev is not None else price
                await self._cache.set(PriceUpdate(
                    ticker=ticker, price=price, previous_price=baseline,
                    timestamp=datetime.now(timezone.utc),
                    direction=_direction(price, baseline),
                ))
            await asyncio.sleep(self._interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def add_ticker(self, ticker: str) -> None:
        self._sim.add_ticker(ticker)   # seeds price, extends correlation matrix

    async def remove_ticker(self, ticker: str) -> None:
        self._sim.remove_ticker(ticker)
```

This is a direct structural mirror of `MassiveDataSource` from `MARKET_INTERFACE.md` §6 — same
task-loop shape, same "write into a shared `PriceCache`" contract — which is what makes the two
implementations swappable via the factory without touching any consumer.

## 7. Testing Strategy

- **GBM math**: with a fixed seed, assert prices stay positive over a long run, and that
  aggregate drift/volatility over many ticks roughly matches the configured `μ`/`σ` (statistical
  assertions with generous tolerance, not exact-value checks)
- **Correlation**: with a fixed seed and many ticks, compute the empirical correlation between two
  same-sector tickers' log returns and assert it's closer to `SAME_SECTOR_CORR` than to
  `CROSS_GROUP_CORR`
- **Shocks**: force `SHOCK_PROBABILITY = 1.0` in a test to assert a shock is applied and its
  magnitude falls within `SHOCK_MAGNITUDE_RANGE`
- **Interface conformance**: the same test suite structure used for `MassiveDataSource` (start/
  stop/add_ticker/remove_ticker/get_tickers behave per the ABC contract) runs against
  `SimulatorDataSource` too, since both implement `MarketDataSource`

## 8. Dependencies

Only `numpy` is needed beyond the standard library (for the RNG and Cholesky decomposition) —
no external services, no API keys, works fully offline.
