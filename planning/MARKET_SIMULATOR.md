# Market Simulator

The simulator produces realistic, correlated stock prices with no external dependencies. It is the default source when `MASSIVE_API_KEY` is not set. It implements the `MarketDataSource` interface defined in `MARKET_INTERFACE.md`.

## Approach

1. **Model each price as geometric Brownian motion (GBM).** Prices stay positive and move in multiplicative steps, which matches how stock prices behave.
2. **Correlate tickers by sector.** Independent normal draws are mixed through a Cholesky factor of a sector correlation matrix, so tech names move together and banks move together.
3. **Add rare shocks.** About 0.1% of ticks per ticker jump 2-5% in either direction, so the demo shows occasional drama.
4. **Tick every 0.5s** from an in-process asyncio task that writes to the shared `PriceCache`.

Nothing here models order flow, spreads, volume or intraday seasonality. It is a visual and behavioural stand-in, not a pricing model.

## Math

For each tick of length `dt`:

```
S(t+dt) = S(t) * exp( (mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z )
```

| Symbol | Meaning |
|---|---|
| `S` | Current price |
| `mu` | Annualised drift (expected return) |
| `sigma` | Annualised volatility |
| `dt` | Tick length as a fraction of a trading year |
| `Z` | Standard normal draw, correlated across tickers |

Time step:

```
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600      # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR      # ~8.48e-8
```

Each tick a stock moves by a sub-cent amount on average; these accumulate into realistic intraday paths.

### Correlated draws

With `n` tickers, build the correlation matrix `C` (ones on the diagonal) and its Cholesky factor `L` such that `C = L Lᵀ`. Each tick:

```
z_indep = standard_normal(n)
z_corr  = L @ z_indep        # each Z_i has correlation C_ij with Z_j
```

Rebuild `L` whenever a ticker is added or removed. This is O(n²) and fine for the expected n < 50.

### Correlation structure

| Pair | ρ | Constant |
|---|---|---|
| Both in tech group | 0.6 | `INTRA_TECH_CORR` |
| Both in finance group | 0.5 | `INTRA_FINANCE_CORR` |
| TSLA with anything | 0.3 | `TSLA_CORR` |
| Anything else (cross-sector, unknown) | 0.3 | `CROSS_GROUP_CORR` |

Tech group: AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX. Finance group: JPM, V. TSLA is not in the tech set; the TSLA rule is checked first anyway, so it is never grouped.

The matrix is positive definite for these values, so Cholesky succeeds.

### Shock events

Per tick, per ticker:

```
if random() < 0.001:
    price *= 1 ± uniform(0.02, 0.05)
```

With 10 tickers at 2 ticks/s, an event lands about every 50 seconds.

### Parameters (`seed_prices.py`)

| Ticker | Seed price | sigma | mu |
|---|---|---|---|
| AAPL | 190 | 0.22 | 0.05 |
| GOOGL | 175 | 0.25 | 0.05 |
| MSFT | 420 | 0.20 | 0.05 |
| AMZN | 185 | 0.28 | 0.05 |
| TSLA | 250 | 0.50 | 0.03 |
| NVDA | 800 | 0.40 | 0.08 |
| META | 500 | 0.30 | 0.05 |
| JPM | 195 | 0.18 | 0.04 |
| V | 280 | 0.17 | 0.04 |
| NFLX | 600 | 0.35 | 0.05 |

Any other ticker gets a random seed price in `[50, 300]` and `DEFAULT_PARAMS = {sigma: 0.25, mu: 0.05}`.

## Code structure

```
backend/app/market/
├── seed_prices.py     Data only: seed prices, TICKER_PARAMS, DEFAULT_PARAMS,
│                      CORRELATION_GROUPS, correlation constants
├── simulator.py       GBMSimulator (pure math, no asyncio)
│                      SimulatorDataSource (MarketDataSource adapter: asyncio loop + cache)
├── cache.py           PriceCache (shared sink)
└── models.py          PriceUpdate
```

Two classes with separate jobs:

| Class | Responsibility | Depends on |
|---|---|---|
| `GBMSimulator` | Holds prices and params, advances them one step, rebuilds Cholesky. Synchronous and deterministic given the RNG. | numpy, `seed_prices` |
| `SimulatorDataSource` | Owns the background task, seeds the cache, forwards each step into `PriceCache`. | `GBMSimulator`, `PriceCache`, `MarketDataSource` |

Keeping the math free of asyncio makes it testable without an event loop.

### `GBMSimulator`

```python
class GBMSimulator:
    TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR

    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None: ...
    def step(self) -> dict[str, float]:          # hot path, every 0.5s
    def add_ticker(self, ticker: str) -> None:   # rebuilds Cholesky
    def remove_ticker(self, ticker: str) -> None # rebuilds Cholesky
    def get_price(self, ticker: str) -> float | None
    def get_tickers(self) -> list[str]
    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float
```

`step()` in outline:

```python
def step(self) -> dict[str, float]:
    if not self._tickers:
        return {}
    z = self._cholesky @ np.random.standard_normal(len(self._tickers)) if self._cholesky is not None \
        else np.random.standard_normal(1)
    for i, t in enumerate(self._tickers):
        p = self._params[t]
        drift = (p["mu"] - 0.5 * p["sigma"] ** 2) * self._dt
        self._prices[t] *= math.exp(drift + p["sigma"] * math.sqrt(self._dt) * z[i])
        if random.random() < self._event_prob:
            self._prices[t] *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
    return {t: round(self._prices[t], 2) for t in self._tickers}
```

Rules that matter:
- Prices are kept unrounded internally and rounded to cents only on output. Rounding each step would stall slow-moving prices.
- A single-ticker simulation has no Cholesky matrix, so the code uses the raw draw.
- `add_ticker` for an existing ticker and `remove_ticker` for a missing one are no-ops.

### `SimulatorDataSource`

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001) -> None: ...
    async def start(self, tickers: list[str]) -> None:   # build sim, seed cache, start loop
    async def stop(self) -> None                         # cancel loop, idempotent
    async def add_ticker(self, ticker: str) -> None      # sim.add_ticker + seed cache now
    async def remove_ticker(self, ticker: str) -> None   # sim.remove_ticker + cache.remove
    def get_tickers(self) -> list[str]
    async def _run_loop(self) -> None:
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker=ticker, price=price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Behaviour:
- `start()` seeds the cache with the initial prices before returning, so SSE and trades have prices immediately.
- `add_ticker()` seeds the cache at once, so a newly added ticker can be traded before the next tick.
- Stepping runs on the event loop. It is about 10 numpy operations per tick, so there is no need for a thread.

## Usage

```python
cache = PriceCache()
source = SimulatorDataSource(price_cache=cache)      # or create_market_data_source(cache)
await source.start(["AAPL", "GOOGL", "MSFT"])
await source.add_ticker("PYPL")                      # any well-formed ticker is accepted
cache.get_price("AAPL")                              # float
await source.stop()
```

A demo dashboard is in `backend/market_data_demo.py` (`uv run market_data_demo.py`).

## Testing approach

Unit tests (no event loop needed for the math):
- `GBMSimulator` with a fixed seed: prices stay positive; step count matches the expected drift over many steps (law of large numbers, loose tolerance).
- Cholesky: `L @ L.T` reproduces the correlation matrix for 1, 2 and 10 tickers; correlation of sampled draws between two tech tickers is near 0.6.
- Shock events: with `event_probability=1.0` every price moves by 2-5%.
- Add/remove: the Cholesky matrix size tracks the ticker count; duplicates are no-ops.
- Unknown ticker: gets a seed in `[50, 300]` and default params.

Integration tests (async):
- `start()` fills the cache for every ticker before returning.
- `add_ticker()` makes a price available immediately; `remove_ticker()` drops it from the cache.
- `stop()` cancels the loop and is safe to call twice.

## Known limitations

- Prices are not persisted. A restart resets every ticker to its seed price; this matches the portfolio reset semantics.
- Correlation is hand-set by sector, not estimated from data.
- The simulator has no notion of market hours, so it always shows prices. The "market closed" label applies only to Massive.
- Only the `event_probability` and `update_interval` knobs are exposed. Per-ticker params come from `seed_prices.py`.
