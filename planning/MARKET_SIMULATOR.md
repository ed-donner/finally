# Market Simulator Design

Specification for the built-in stock price simulator that serves as the default market data source when no `MASSIVE_API_KEY` is configured.

## Goals

- Generate realistic, continuously updating stock prices with no external dependencies
- Correlated sector moves (tech stocks move together, etc.)
- Occasional dramatic events for visual impact
- Support dynamic addition/removal of tickers at runtime
- Sub-second update cadence (~500ms) for responsive streaming

---

## Mathematical Model: Geometric Brownian Motion (GBM)

### Core Formula

Each price step follows the standard GBM discrete update:

```
S(t+dt) = S(t) × exp((μ - σ²/2) × dt + σ × √dt × Z)
```

| Symbol | Meaning |
|--------|---------|
| `S(t)` | Current price |
| `μ` | Annualized drift (expected return) |
| `σ` | Annualized volatility |
| `dt` | Time step as fraction of a trading year |
| `Z` | Correlated standard normal random variable |

### Why GBM?

- Industry-standard model for stock price simulation (Black-Scholes foundation)
- Prices are always positive (exponential ensures no negatives)
- Log-normal distribution matches empirical stock returns
- Simple to implement, easy to parameterize per ticker
- Drift and volatility map directly to real-world financial intuition

### Time Scaling

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # = 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.48e-8
```

- 252 trading days/year × 6.5 hours/day × 3600 seconds/hour
- With 500ms updates, `dt` is tiny — each tick produces sub-cent moves
- Over a session, moves accumulate naturally to realistic magnitudes
- A ticker with 25% annualized volatility will show ~0.01–0.05% moves per tick

---

## Per-Ticker Parameters

### Seed Prices

Realistic starting prices for the default watchlist:

| Ticker | Seed Price | Sector |
|--------|-----------|--------|
| AAPL | $190.00 | Tech |
| GOOGL | $175.00 | Tech |
| MSFT | $420.00 | Tech |
| AMZN | $185.00 | Tech |
| TSLA | $250.00 | Tech* |
| NVDA | $800.00 | Tech |
| META | $500.00 | Tech |
| JPM | $195.00 | Finance |
| V | $280.00 | Finance |
| NFLX | $600.00 | Tech |

*TSLA is in the tech group but uses reduced correlation (0.3) to reflect its idiosyncratic behavior.

Dynamically added tickers with no seed price get a random value between $50–$300.

### Volatility and Drift

Each ticker has annualized parameters:

| Ticker | σ (volatility) | μ (drift) | Character |
|--------|---------------|-----------|-----------|
| AAPL | 0.22 | 0.05 | Moderate |
| GOOGL | 0.25 | 0.05 | Moderate |
| MSFT | 0.20 | 0.05 | Steady |
| AMZN | 0.28 | 0.05 | Slightly volatile |
| TSLA | 0.50 | 0.03 | Very volatile, low drift |
| NVDA | 0.40 | 0.08 | Volatile, strong drift |
| META | 0.30 | 0.05 | Moderate-high |
| JPM | 0.18 | 0.04 | Stable (bank) |
| V | 0.17 | 0.04 | Stable (payments) |
| NFLX | 0.35 | 0.05 | Volatile |

**Default parameters** for unknown tickers: `σ = 0.25, μ = 0.05`.

---

## Correlated Moves

### Why Correlations?

Without correlation, all tickers move independently — unrealistic. In reality, tech stocks tend to move together, and market-wide sentiment affects everything. Correlated random draws produce this effect.

### Sector Groups and Correlation Coefficients

```
tech:    {AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX}
finance: {JPM, V}
```

| Pair Type | Correlation |
|-----------|------------|
| Both in tech group | 0.6 |
| Both in finance group | 0.5 |
| Cross-sector or unknown | 0.3 |
| Either ticker is TSLA | 0.3 (override) |

### Cholesky Decomposition

To generate correlated random draws from independent ones:

1. Build an `n × n` correlation matrix `C` where `C[i][j]` is the pairwise correlation
2. Compute the Cholesky decomposition: `L = cholesky(C)` (lower triangular matrix where `L × Lᵀ = C`)
3. Generate `n` independent standard normal draws: `z_independent`
4. Transform: `z_correlated = L @ z_independent`

The resulting `z_correlated` vector has the desired correlation structure. Each `z_correlated[i]` is used as the `Z` in the GBM formula for ticker `i`.

```python
import numpy as np

# Build correlation matrix
n = len(tickers)
corr_matrix = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        rho = _pairwise_correlation(tickers[i], tickers[j])
        corr_matrix[i, j] = rho
        corr_matrix[j, i] = rho

# Decompose once (reuse until tickers change)
cholesky = np.linalg.cholesky(corr_matrix)

# Each tick:
z_independent = np.random.standard_normal(n)
z_correlated = cholesky @ z_independent
```

The Cholesky matrix is **rebuilt** whenever tickers are added or removed (infrequent operation).

---

## Random Shock Events

To add visual drama, the simulator occasionally injects sudden price moves:

- **Probability:** 0.1% per tick per ticker (`event_probability = 0.001`)
- **Magnitude:** 2–5% of current price (uniformly random)
- **Direction:** 50/50 up or down
- **Expected frequency:** With 10 tickers at 2 ticks/second, expect ~1 event every 50 seconds

```python
if random.random() < event_probability:
    shock = random.uniform(0.02, 0.05)
    direction = random.choice([-1, 1])
    price *= (1 + direction * shock)
```

Events are applied **after** the GBM step, so they compound on top of normal movement.

---

## Code Structure

### GBMSimulator (Synchronous Math Engine)

Pure computation — no async, no I/O, no cache dependency.

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001)

    def step(self) -> dict[str, float]:
        """Advance one time step. Returns {ticker: new_price} for all tickers."""

    def add_ticker(self, ticker: str) -> None:
        """Add ticker with seed price; rebuilds Cholesky matrix."""

    def remove_ticker(self, ticker: str) -> None:
        """Remove ticker; rebuilds Cholesky matrix."""

    def get_price(self, ticker: str) -> float | None:
        """Current simulated price."""

    def get_tickers(self) -> list[str]:
        """Currently tracked tickers."""
```

**`step()` algorithm:**
1. Generate `n` independent standard normals
2. Apply Cholesky to get correlated normals
3. For each ticker `i`:
   - Compute drift: `(μ - 0.5σ²) × dt`
   - Compute diffusion: `σ × √dt × z_correlated[i]`
   - Update: `price *= exp(drift + diffusion)`
   - Roll for shock event
4. Return all prices rounded to 2 decimals

### SimulatorDataSource (Async Wrapper)

Adapts `GBMSimulator` to the `MarketDataSource` interface with an async run loop.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5,
                 event_probability: float = 0.001)

    async def start(self, tickers: list[str]) -> None:
        """Create GBMSimulator, seed cache, start background loop."""

    async def stop(self) -> None:
        """Cancel background task."""

    async def add_ticker(self, ticker: str) -> None:
        """Add to simulator and seed cache immediately."""

    async def remove_ticker(self, ticker: str) -> None:
        """Remove from simulator and cache."""

    def get_tickers(self) -> list[str]:
        """Delegate to simulator."""
```

**Run loop:**
```python
async def _run_loop(self):
    while True:
        try:
            prices = self._sim.step()
            for ticker, price in prices.items():
                self._cache.update(ticker, price)
            await asyncio.sleep(self._interval)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Simulator error")
            # Log but don't crash — continue on next tick
```

---

## Realistic Behavior Examples

Over a typical 5-minute demo session (~600 ticks at 500ms):

| Ticker | Volatility | Expected Range | Character |
|--------|-----------|---------------|-----------|
| JPM (σ=0.18) | Low | ±$0.20–0.50 | Gentle drift |
| AAPL (σ=0.22) | Moderate | ±$0.30–0.80 | Steady movement |
| TSLA (σ=0.50) | High | ±$1.00–3.00 | Wild swings |
| NVDA (σ=0.40) | High | ±$2.00–5.00 | Large moves (high price × high vol) |

Plus 6–12 shock events adding 2–5% sudden jumps for visual excitement.

---

## Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| GBM over random walk | Log-normal prices (always positive), industry-standard, parameterizable |
| Cholesky for correlations | Exact correlation structure, efficient (O(n³) but n ≤ 20 tickers) |
| Separate GBMSimulator from DataSource | Pure math is testable without async; async wrapper handles lifecycle |
| Shock events | Without them, 500ms ticks produce imperceptible moves — shocks add drama |
| Tiny dt (8.48e-8) | Correctly scales annualized params to sub-second ticks without manual tuning |
| Rebuild Cholesky on ticker change | Simple and correct; ticker changes are infrequent (user action only) |

---

## Dependencies

- `numpy` — random number generation, Cholesky decomposition, matrix operations
- `asyncio` — background task scheduling (standard library)
- No external market data services required
