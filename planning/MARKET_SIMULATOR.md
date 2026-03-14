# Market Simulator Design

## Purpose

The simulator generates realistic-looking stock price movements for the FinAlly demo when no Massive API key is available. It runs entirely in-process with zero external dependencies (besides numpy for matrix math).

## Mathematical Model: Geometric Brownian Motion (GBM)

Each tick computes:

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where:
- `S(t)` = current price
- `mu` = annualized drift (expected return, e.g. 0.05 = 5%/year)
- `sigma` = annualized volatility (e.g. 0.25 = 25%/year)
- `dt` = time step as fraction of a trading year
- `Z` = correlated standard normal random variable

### Time Step

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800
dt = 0.5 / TRADING_SECONDS_PER_YEAR          # ~8.48e-8
```

The tiny dt produces sub-cent moves per tick that accumulate naturally over time, creating realistic price charts.

## Correlated Price Movements

Stocks don't move independently -- tech stocks tend to move together, financial stocks correlate with each other, etc. The simulator models this using **Cholesky decomposition** of a correlation matrix.

### Process

1. Build an NxN correlation matrix for all tracked tickers
2. Compute the Cholesky decomposition: `L = cholesky(C)`
3. Each tick: generate N independent standard normals, multiply by L to get correlated draws
4. Apply the correlated draws to the GBM formula

### Correlation Structure

| Pair Type | Correlation |
|-----------|------------|
| Tech + Tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Finance + Finance (JPM, V) | 0.5 |
| TSLA + anything | 0.3 |
| Cross-sector | 0.3 |
| Unknown tickers (dynamically added) | 0.3 |

The matrix is rebuilt whenever tickers are added or removed. This is O(n^2) but n < 50 so it's instant.

## Random Events

To add drama and make the demo visually interesting:

- Each tick, each ticker has a ~0.1% chance of a random event
- Events produce a sudden 2-5% price shock (up or down, 50/50)
- With 10 tickers at 2 ticks/sec, expect an event roughly every 50 seconds

```python
if random.random() < 0.001:
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    price *= (1 + shock)
```

## Seed Data

Realistic starting prices and per-ticker parameters (`backend/app/market/seed_prices.py`):

| Ticker | Seed Price | Volatility (sigma) | Drift (mu) | Notes |
|--------|-----------|-------------------|-----------|-------|
| AAPL | $190 | 0.22 | 0.05 | |
| GOOGL | $175 | 0.25 | 0.05 | |
| MSFT | $420 | 0.20 | 0.05 | |
| AMZN | $185 | 0.28 | 0.05 | |
| TSLA | $250 | 0.50 | 0.03 | High vol, low drift |
| NVDA | $800 | 0.40 | 0.08 | High vol, strong drift |
| META | $500 | 0.30 | 0.05 | |
| JPM | $195 | 0.18 | 0.04 | Low vol (bank) |
| V | $280 | 0.17 | 0.04 | Low vol (payments) |
| NFLX | $600 | 0.35 | 0.05 | |

Dynamically added tickers get default params: sigma=0.25, mu=0.05, and a random starting price between $50-$300.

## Code Structure

### GBMSimulator (`backend/app/market/simulator.py`)

The core math engine. Stateful -- holds current prices and per-ticker parameters.

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float, event_probability: float)
    def step(self) -> dict[str, float]          # Advance all tickers, return new prices
    def add_ticker(self, ticker: str) -> None    # Add ticker, rebuild Cholesky
    def remove_ticker(self, ticker: str) -> None # Remove ticker, rebuild Cholesky
    def get_price(self, ticker: str) -> float | None
    def get_tickers(self) -> list[str]
```

`step()` is the hot path -- called every 500ms. It generates correlated normal draws, applies GBM, checks for random events, and returns rounded prices.

### SimulatorDataSource (`backend/app/market/simulator.py`)

Implements `MarketDataSource` by wrapping `GBMSimulator` in an async background task.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5)
    async def start(self, tickers: list[str]) -> None   # Create GBMSimulator, start loop
    async def stop(self) -> None                          # Cancel background task
    async def add_ticker(self, ticker: str) -> None       # Delegate to GBMSimulator
    async def remove_ticker(self, ticker: str) -> None    # Delegate + remove from cache
    def get_tickers(self) -> list[str]
```

The background loop:
1. Call `GBMSimulator.step()` to get new prices for all tickers
2. Write each price to `PriceCache` via `cache.update(ticker, price)`
3. Sleep for `update_interval` (500ms)
4. Repeat

On `start()`, seeds the cache with initial prices so SSE clients have data immediately.
