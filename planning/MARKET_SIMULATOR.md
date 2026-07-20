# Market Simulator — Design

Design for `SimulatorMarketDataSource` (PLAN.md §6, `MARKET_INTERFACE.md` §5), the default no-API-key market data source. Produces plausible, continuously-updating stock prices without any external dependency.

## 1. Goals

- Look and feel like a real, moving market: correlated sector moves, occasional volatility spikes, no external calls.
- Deterministic enough to unit test (seedable RNG), realistic enough for a convincing demo.
- Conform exactly to the `MarketDataSource` interface (`MARKET_INTERFACE.md` §3) so the rest of the system can't tell it apart from the Massive-backed source.

## 2. Price Model — Geometric Brownian Motion (GBM)

Each ticker's price evolves per discrete-time GBM:

```
S(t + dt) = S(t) * exp((mu - 0.5 * sigma^2) * dt + sigma * sqrt(dt) * Z)
```

- `S(t)`: current price
- `mu`: drift (annualized), per-ticker, small (e.g. `0.05` = 5%/yr) so prices don't trend away over a demo session
- `sigma`: annualized volatility, per-ticker (e.g. `0.20`–`0.45`, higher for TSLA/NVDA-style names)
- `dt`: elapsed time as a fraction of a trading year for one tick (`tick_interval_seconds / (252 * 6.5 * 3600)`, i.e. treating each tick as a fraction of a 6.5-hour trading day)
- `Z`: standard normal random draw, independent per tick but **correlated across tickers** (§3)

## 3. Correlated Moves

To make sector co-movement visible (PLAN.md: "tech stocks move together"), tickers are grouped, and each tick draws:

1. One shared market-wide factor `Z_market ~ N(0,1)`
2. One sector factor `Z_sector ~ N(0,1)` per group (e.g. `tech`, `finance`)
3. One idiosyncratic factor `Z_idio ~ N(0,1)` per ticker

Each ticker's `Z` is a weighted blend:

```
Z_ticker = beta_market * Z_market + beta_sector * Z_sector + beta_idio * Z_idio
```

with `beta_market + beta_sector + beta_idio` chosen so `Z_ticker` stays unit-variance (e.g. weights `0.4, 0.3, sqrt(1 - 0.4^2 - 0.3^2)`). Default sector groups for the seed watchlist:

| Sector | Tickers |
|---|---|
| tech | AAPL, GOOGL, MSFT, AMZN, NVDA, META, NFLX |
| finance | JPM, V |
| auto/other | TSLA |

## 4. Random Events

Each tick, per ticker, with small probability (e.g. `p_event = 0.001` at a 500ms tick → roughly one event per ~8 minutes per ticker), apply an independent shock:

```
S(t) *= (1 + sign * random.uniform(0.02, 0.05))  # sign = +1 or -1, 50/50
```

This produces the "sudden 2–5% moves... for drama" called for in PLAN.md §6, layered on top of (not replacing) the GBM tick for that step.

## 5. Seed Prices

A single `SEED_PRICES: dict[str, float]` module-level map is the one place starting prices live (addresses the simplification flagged in `planning/REVIEW.md`):

```python
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00, "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 245.00, "NVDA": 135.00, "META": 590.00, "JPM": 210.00,
    "V": 275.00, "NFLX": 680.00,
}
DEFAULT_SEED_PRICE = 100.00  # used for any ticker not in the map (e.g. user/AI adds an unrecognized symbol)
```

When `watch(ticker)` is called for a ticker not already tracked, its starting price is `SEED_PRICES.get(ticker, DEFAULT_SEED_PRICE)`, and it's assigned to the `misc` sector group (`beta_sector = 0`, i.e. only market + idiosyncratic factors) unless explicitly added to a sector table.

## 6. Tick Loop

```python
class SimulatorMarketDataSource(MarketDataSource):
    def __init__(self, tick_interval: float = 0.5, seed: int | None = None):
        self._tick_interval = tick_interval
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = {}
        self._sectors: dict[str, str] = {}
        self._cache: dict[str, PriceTick] = {}
        self._session_open: dict[str, float] = {}  # previous_close per ticker
        self._subscribers: list[asyncio.Queue[PriceTick]] = []
        self._task: asyncio.Task | None = None

    async def start(self, tickers):
        for t in tickers:
            self.watch(t)
        self._task = asyncio.create_task(self._tick_loop())

    def watch(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        price = SEED_PRICES.get(ticker, DEFAULT_SEED_PRICE)
        self._prices[ticker] = price
        self._session_open[ticker] = price
        self._sectors[ticker] = _sector_for(ticker)

    async def _tick_loop(self):
        while True:
            self._advance_all()
            await asyncio.sleep(self._tick_interval)

    def _advance_all(self):
        z_market = self._rng.gauss(0, 1)
        z_sector = {s: self._rng.gauss(0, 1) for s in set(self._sectors.values())}
        for ticker, price in self._prices.items():
            z = _blend(z_market, z_sector[self._sectors[ticker]], self._rng.gauss(0, 1))
            new_price = _gbm_step(price, mu=0.05, sigma=_sigma_for(ticker), dt=_DT, z=z)
            if self._rng.random() < 0.001:
                sign = self._rng.choice([-1, 1])
                new_price *= 1 + sign * self._rng.uniform(0.02, 0.05)
            self._prices[ticker] = new_price
            tick = PriceTick(
                ticker=ticker,
                price=round(new_price, 4),
                previous_close=self._session_open[ticker],
                timestamp=time.time(),
            )
            self._cache[ticker] = tick
            self._publish(tick)
```

`_gbm_step` and `_blend` implement §2/§3 directly; `_DT` is the per-tick fraction of a trading year derived from `tick_interval`.

## 7. Testability

- `seed` param makes the RNG deterministic for unit tests (`GBM math is correct`, per PLAN.md §12).
- Because `_advance_all` is a pure step function over `self._prices`, tests can call it directly N times and assert statistical properties (mean drift ≈ `mu`, no negative prices, event shocks bounded to 2–5%) without needing to run the async tick loop.
- Conformance test: a shared test suite (parametrized over both `SimulatorMarketDataSource` and a mocked `MassiveMarketDataSource`) asserts both satisfy the `MarketDataSource` ABC contract from `MARKET_INTERFACE.md` §3.
