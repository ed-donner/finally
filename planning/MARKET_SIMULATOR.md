# Market Simulator

The simulator generates realistic-looking stock prices without any external API. It is the default data source when `MASSIVE_API_KEY` is not set. It runs entirely in-process as an asyncio background task.

## Implementation Files

```
backend/app/market/
├── seed_prices.py   — Seed prices, per-ticker GBM params, correlation groups
├── simulator.py     — GBMSimulator class + SimulatorDataSource
```

---

## Geometric Brownian Motion

Each tick advances every price using the GBM formula:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` — current price
- `mu` — annualized drift (expected return, e.g. 0.05 = 5%/year)
- `sigma` — annualized volatility (e.g. 0.25 = 25%/year)
- `dt` — time step as a fraction of a trading year
- `Z` — standard normal random variable (correlated across tickers)

### Time Step

Ticks run every 500ms. `dt` is computed as:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8
```

This produces sub-cent moves per tick that accumulate naturally over time — prices drift over minutes and hours the way real markets do, rather than jumping randomly.

---

## Per-Ticker Parameters

Defined in `seed_prices.py`:

```python
SEED_PRICES = {
    "AAPL": 190.00,  "GOOGL": 175.00, "MSFT": 420.00,
    "AMZN": 185.00,  "TSLA": 250.00,  "NVDA": 800.00,
    "META": 500.00,  "JPM":  195.00,  "V":    280.00,
    "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},  # Stable large-cap
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},  # Lowest volatility tech
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},  # High vol, does its own thing
    "NVDA":  {"sigma": 0.40, "mu": 0.08},  # High vol, strong upward drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},  # Low vol (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},  # Lowest vol (payments network)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}  # Fallback for unknown tickers
```

Unknown tickers (dynamically added) receive a random seed price in `[50, 300]` and `DEFAULT_PARAMS`.

---

## Correlated Moves

Real stocks in the same sector tend to move together. The simulator models this using a Cholesky decomposition of a sector-based correlation matrix.

### Correlation Structure

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # Tech stocks move together
INTRA_FINANCE_CORR = 0.5   # Finance stocks move together
CROSS_GROUP_CORR   = 0.3   # Between sectors or unknown tickers
TSLA_CORR          = 0.3   # TSLA is in tech but does its own thing
```

### How It Works

Each tick, `n` independent standard normal draws are generated, then multiplied by the Cholesky factor `L` (where `C = L @ L.T`):

```python
z_independent = np.random.standard_normal(n)   # shape (n,)
z_correlated  = self._cholesky @ z_independent  # shape (n,) — now correlated
```

Each ticker then uses its own `z_correlated[i]` in the GBM formula. This produces coordinated sector moves: when tech sells off, AAPL, GOOGL, MSFT, and META all dip together, while JPM and V are less affected.

The Cholesky matrix is rebuilt whenever tickers are added or removed. With `n < 50`, this is negligible overhead.

---

## Random Shock Events

Every tick, each ticker has a 0.1% (`event_probability=0.001`) chance of a sudden 2–5% move:

```python
if random.random() < self._event_prob:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

With 10 tickers at 2 ticks/second: expect roughly one shock event every 50 seconds. Events create visible drama in the UI — price flashes bright green or red, sparklines spike. The event is logged at DEBUG level.

---

## Code Structure

### `GBMSimulator`

Pure price-generation logic. No asyncio, no cache coupling. Straightforward to unit test.

```python
sim = GBMSimulator(tickers=["AAPL", "TSLA"], event_probability=0.001)
prices: dict[str, float] = sim.step()  # advance one tick, return new prices
sim.add_ticker("NVDA")      # rebuilds Cholesky
sim.remove_ticker("TSLA")   # rebuilds Cholesky
sim.get_price("AAPL")       # current internal price (float | None)
sim.get_tickers()           # list[str]
```

### `SimulatorDataSource`

Wraps `GBMSimulator` in a `MarketDataSource`. Runs the tick loop as an asyncio background task.

```python
source = SimulatorDataSource(price_cache=cache, update_interval=0.5)
await source.start(["AAPL", "GOOGL", "MSFT"])
# Background task calls sim.step() every 500ms and writes to cache
await source.add_ticker("AMD")
await source.remove_ticker("GOOGL")
await source.stop()
```

On `start()`, the cache is seeded immediately with initial prices so SSE clients receive data on first `snapshot` event rather than waiting for the first tick.

### Tick Loop

```python
async def _run_loop(self) -> None:
    while True:
        try:
            if self._sim:
                prices = self._sim.step()
                for ticker, price in prices.items():
                    self._cache.update(ticker=ticker, price=price)
        except Exception:
            logger.exception("Simulator step failed")
        await asyncio.sleep(self._interval)
```

The try/except around `step()` ensures one bad tick never kills the loop.

---

## Tuning

| Parameter | Default | Effect |
|-----------|---------|--------|
| `update_interval` | 0.5s | Tick rate. Lower = more frequent UI updates. |
| `event_probability` | 0.001 | 0.1% per tick per ticker. Increase for more drama. |
| `sigma` (per ticker) | 0.17–0.50 | Intra-day price variability. |
| `mu` (per ticker) | 0.03–0.08 | Long-run drift direction. Minimal effect over demo timeframes. |

For a demo that looks lively but not chaotic, keep `sigma` in the 0.20–0.35 range and `event_probability` at 0.001.

---

## Previous Close

The PLAN specifies that each ticker needs a `previous_close` for computing daily change %. In the simulator, the seed price at startup serves as the synthetic previous close. The SSE stream derives `change_pct` from `(current_price - previous_close) / previous_close * 100`.

The `PriceCache.update()` method tracks `previous_price` per tick (for flash direction), not previous close. The SSE stream endpoint needs to separately maintain `previous_close` per ticker — set once at startup from the seed price, never updated during the session. This is handled in `stream.py`.
