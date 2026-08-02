# Market Simulator

The default market data source for FinAlly. Generates realistic, correlated,
continuously-moving stock prices with no external dependency, no API key, and
no cost.

Companion documents: `MARKET_INTERFACE.md` (the contract it implements),
`MASSIVE_API.md` (the real-data alternative).

---

## 1. Why Simulate

The simulator is the **default**, not a fallback. Most people running FinAlly
will never set `MASSIVE_API_KEY`, and the simulator is a better experience for
them:

| Property | Benefit |
|---|---|
| Always moving | Real markets are closed nights, weekends, and holidays — a demo showing frozen prices looks broken |
| 500ms updates | Matches the UI's flash-animation design; real free-tier data is 15-minute delayed |
| No API key | Zero-setup `docker run` |
| No rate limits | Add 50 tickers, no throttling |
| Deterministic under seed | Reproducible tests |
| Free | Massive's snapshot endpoints require a paid plan |

The design goal is **plausibility, not prediction**. Prices must look like a
trading terminal: drifting, occasionally jumping, with tech names moving
together. Nobody should mistake this for a forecast.

---

## 2. The Model — Geometric Brownian Motion

GBM is the standard model for equity price paths (the basis of Black-Scholes).
It has the two properties that matter here: prices stay **strictly positive**,
and **returns** rather than absolute prices are normally distributed, so a $800
stock and a $15 stock both move in plausible percentage terms.

The discrete-time update:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt  +  σ·√dt·Z )
          └──────┘   └────────────┘     └───────┘
           current       drift          diffusion
```

| Symbol | Meaning |
|---|---|
| `S(t)` | Current price |
| `μ` | Annualized drift (expected return) |
| `σ` | Annualized volatility |
| `dt` | Time step, as a fraction of a trading year |
| `Z` | Correlated standard normal draw |

The `−σ²/2` term is the Itô correction. Without it the *median* path drifts
below the intended `μ`, because `E[exp(X)] ≠ exp(E[X])` for a normal `X`. Its
presence is what makes `μ` mean "expected annual return" rather than an
arbitrary tuning knob.

### Choosing `dt`

`dt` must be expressed in the same time units as `μ` and `σ`, which are
annualized. A trading year is:

```
252 trading days × 6.5 hours/day × 3600 s/hour = 5,896,800 seconds
```

So a 500ms tick is:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ≈ 8.479e-8
```

Using wall-clock seconds per year (31.5M) instead would understate volatility
by roughly 2.3×, making the terminal look sleepy. Anchoring to *trading* time
means one hour of watching FinAlly produces about as much price action as one
hour of watching a real market.

### Verified magnitudes

Per-tick standard deviation is `σ·√dt`. Measured against the shipped seed
values:

| Ticker | σ | Per-tick move | Per-tick $ | Over 1 min | Over 1 hr |
|---|---|---|---|---|---|
| AAPL | 0.22 | 0.0064% | $0.012 | 0.070% | 0.544% |
| TSLA | 0.50 | 0.0146% | $0.036 | 0.159% | 1.235% |
| NVDA | 0.40 | 0.0116% | $0.093 | 0.128% | 0.988% |
| JPM | 0.18 | 0.0052% | $0.010 | 0.057% | 0.445% |
| V | 0.17 | 0.0050% | $0.014 | 0.054% | 0.420% |

An hour of TSLA moving ~1.2% and JPM ~0.45% is squarely in realistic
territory, and the sub-cent-to-few-cent per-tick moves produce the continuous
flicker the UI wants without prices visibly running away.

---

## 3. Correlation via Cholesky Decomposition

Independent random walks look wrong. In a real market, tech names move
together — when the sector sells off, the whole watchlist reddens at once. That
collective motion is most of what makes a terminal feel alive.

### The technique

To generate correlated normals with a target correlation matrix `C`:

1. Compute the Cholesky decomposition `C = L·Lᵀ`, where `L` is lower-triangular.
2. Draw a vector `Z` of independent standard normals.
3. `Z_correlated = L · Z` now has correlation matrix exactly `C`.

```python
z_independent = np.random.standard_normal(n)
z_correlated = self._cholesky @ z_independent
```

Each ticker then applies its *own* `σ` to its correlated draw, so tickers share
directional tendency while keeping individual volatility.

### The correlation structure

```python
CORRELATION_GROUPS = {
    "tech":    {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

INTRA_TECH_CORR    = 0.6   # tech names move together
INTRA_FINANCE_CORR = 0.5   # banks/payments move together
CROSS_GROUP_CORR   = 0.3   # broad market beta between sectors
TSLA_CORR          = 0.3   # TSLA does its own thing
```

Pairwise resolution order:

```python
if t1 == "TSLA" or t2 == "TSLA":      return TSLA_CORR          # 0.3
if t1 in tech    and t2 in tech:      return INTRA_TECH_CORR    # 0.6
if t1 in finance and t2 in finance:   return INTRA_FINANCE_CORR # 0.5
return CROSS_GROUP_CORR                                          # 0.3
```

TSLA is checked **first**, before the tech-group test. It is a member of the
tech set but is deliberately decorrelated — it is the ticker most likely to be
doing something idiosyncratic, and giving it independence adds visual variety.

Unknown tickers (anything a user adds) fall through to `CROSS_GROUP_CORR`,
which means a newly added symbol still participates in broad market moves.

### Verified behaviour

Building the 10-ticker default matrix and reconstructing `L·Lᵀ`:

```
AAPL/MSFT → 0.600    JPM/V → 0.500    AAPL/JPM → 0.300    TSLA/AAPL → 0.300
minimum eigenvalue → 0.400   (positive definite ✓)
```

Empirical correlation of log returns over 20,000 simulated steps:

| Pair | Target | Measured |
|---|---|---|
| AAPL / MSFT | 0.60 | 0.593 |
| JPM / V | 0.50 | 0.497 |
| AAPL / JPM | 0.30 | 0.298 |
| TSLA / AAPL | 0.30 | 0.295 |

The model reproduces its target structure to within sampling error.

> **Positive-definiteness constraint.** `np.linalg.cholesky` raises
> `LinAlgError` on a non-positive-definite matrix. The current values are safe
> (min eigenvalue 0.4), but arbitrary hand-tuned correlations can easily be
> invalid — e.g. A/B = 0.9, A/C = 0.9, B/C = 0.0 is not a realizable
> correlation matrix. Any change to these constants must be checked with
> `np.linalg.eigvalsh(corr).min() > 0`, and there is a unit test asserting the
> matrix builds for the default watchlist.

---

## 4. Random Shock Events

Pure GBM is smooth. Real markets gap on news. Each tick, each ticker has a
small chance of a discrete jump:

```python
if random.random() < self._event_prob:          # default 0.001
    shock_magnitude = random.uniform(0.02, 0.05)   # 2-5%
    shock_sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock_magnitude * shock_sign
```

Expected frequency with the default watchlist:

```
10 tickers × 2 ticks/sec × 0.001 = 0.02 events/sec ≈ one event every 50 seconds
```

Roughly one dramatic move per minute across the board — frequent enough that a
user watching for a minute sees something happen, rare enough that it reads as
an event rather than noise. The shock is symmetric (equal up/down probability),
so it adds variance without biasing long-run drift.

---

## 5. Code Structure

Two classes with a clean split of concerns:

```
GBMSimulator            ← pure math, fully synchronous, no I/O, no asyncio
    ├── step()          → advance all tickers one tick, return {ticker: price}
    ├── add_ticker()    → seed price/params, rebuild Cholesky
    ├── remove_ticker() → drop state, rebuild Cholesky
    ├── get_price()     → current price for one ticker
    └── get_tickers()   → tracked tickers

SimulatorDataSource     ← implements MarketDataSource, owns the asyncio task
    ├── start()         → build simulator, seed cache, launch _run_loop
    ├── stop()          → cancel task
    ├── add_ticker()    → delegate + seed cache immediately
    ├── remove_ticker() → delegate + evict from cache
    ├── get_tickers()   → delegate
    └── _run_loop()     → step → write cache → sleep, forever
```

**`GBMSimulator` is deliberately free of asyncio and of `PriceCache`.** It is a
deterministic function of its state plus the RNG, which makes the math directly
unit-testable: seed numpy, call `step()` a thousand times, assert on the
distribution of returns. No event loop, no mocking.

`SimulatorDataSource` handles everything stateful and asynchronous. This split
mirrors `MassiveDataSource`, where the REST client is likewise isolated behind
the async lifecycle.

### The core loop

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

The `try`/`except` sits **inside** the loop, so a transient failure (e.g. a
Cholesky error after a bad ticker add) logs and retries on the next tick rather
than silently killing the background task and freezing every price in the app.

### Startup seeding

`start()` writes initial prices to the cache **before** returning:

```python
async def start(self, tickers: list[str]) -> None:
    self._sim = GBMSimulator(tickers=tickers, event_probability=self._event_prob)
    for ticker in tickers:
        price = self._sim.get_price(ticker)
        if price is not None:
            self._cache.update(ticker=ticker, price=price)
    self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
```

Without this, the first SSE frame after startup would be empty and the
watchlist would render blank for up to 500ms. `add_ticker()` seeds the same way,
so a newly added ticker shows a price instantly rather than after a tick.

---

## 6. Seed Data

`seed_prices.py` holds all tunable parameters, separate from the logic:

```python
SEED_PRICES = {
    "AAPL": 190.00,  "GOOGL": 175.00, "MSFT": 420.00, "AMZN": 185.00,
    "TSLA": 250.00,  "NVDA": 800.00,  "META": 500.00, "JPM":  195.00,
    "V":    280.00,  "NFLX": 600.00,
}

TICKER_PARAMS = {
    "AAPL":  {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT":  {"sigma": 0.20, "mu": 0.05},
    "AMZN":  {"sigma": 0.28, "mu": 0.05},
    "TSLA":  {"sigma": 0.50, "mu": 0.03},   # high volatility
    "NVDA":  {"sigma": 0.40, "mu": 0.08},   # high volatility, strong drift
    "META":  {"sigma": 0.30, "mu": 0.05},
    "JPM":   {"sigma": 0.18, "mu": 0.04},   # low volatility (bank)
    "V":     {"sigma": 0.17, "mu": 0.04},   # low volatility (payments)
    "NFLX":  {"sigma": 0.35, "mu": 0.05},
}

DEFAULT_PARAMS = {"sigma": 0.25, "mu": 0.05}
```

Volatilities are chosen to match each name's real-world character: TSLA the
most volatile at 0.50, NVDA next at 0.40 with the strongest drift, the payment
and banking names calmest at 0.17-0.18. The ordering is what a user notices —
TSLA visibly jumping around while V barely moves is the detail that sells the
simulation.

### Unknown tickers

A ticker not in `SEED_PRICES` (anything the user or the LLM adds) gets:

```python
self._prices[ticker] = SEED_PRICES.get(ticker, random.uniform(50.0, 300.0))
self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))
```

A random starting price in $50-300 and mid-range parameters. Note
`dict(DEFAULT_PARAMS)` — a **copy**. Sharing the dict would mean per-ticker
parameter tuning silently mutating the defaults for every other unknown ticker.

---

## 7. Known Limitations

Documented deliberately, since the simulator is the default experience.

### Low-priced tickers barely move visibly

Prices are rounded to 2 decimals at the cache boundary. When `σ·√dt·S` is well
under a cent, most ticks round to the same displayed price. Measured fraction
of ticks with **no visible change**:

| Price level | Flat ticks | Visible move |
|---|---|---|
| $800 (NVDA) | 4.4% | 95.6% |
| $190 (AAPL) | 31.3% | 68.7% |
| $195 (JPM) | 35.3% | 64.7% |
| **$15** | **92.4%** | **7.6%** |

A user adding a sub-$20 ticker sees a nearly frozen price and almost no flash
animation. The default watchlist is unaffected (cheapest is GOOGL at $175), but
this is a real edge for user-added penny-ish names.

Mitigation if it matters: scale volatility upward for low-priced tickers when
assigning `DEFAULT_PARAMS`, or set a floor on per-tick movement. Not currently
implemented — the default watchlist doesn't hit it.

### Other simplifications

- **No mean reversion.** Prices random-walk; over a long session a ticker can
  wander far from its seed. Acceptable for a demo, unrealistic over days.
- **No volume, bid/ask, or order book.** The plan specifies market orders with
  instant fill, so none is needed.
- **No market hours.** Prices move 24/7 by design — a frozen weekend terminal
  would look broken.
- **No day-open reference.** `PriceUpdate.previous_price` is the previous
  *tick*. Day-change percentage (which Massive provides directly as
  `todays_change_percent`) has no simulator equivalent; the portfolio layer
  computes change against the session's first observed price instead.
- **Correlation is static.** Real correlations spike in a crash. Here they are
  fixed constants.

---

## 8. Testing

The math/async split makes both halves straightforwardly testable.

**`GBMSimulator` — pure, synchronous:**

- Prices stay strictly positive across many thousands of steps.
- `step()` returns an entry for every tracked ticker, rounded to 2dp.
- With `np.random.seed(...)` fixed, output is reproducible.
- Empirical volatility of log returns ≈ `σ·√dt` for each ticker.
- Empirical correlation matches the target structure (verified above).
- Cholesky builds and is positive definite for the default watchlist.
- `add_ticker`/`remove_ticker` rebuild the matrix to the right dimension;
  adding a duplicate is a no-op; removing an unknown ticker is a no-op.
- With `event_probability=1.0`, every tick applies a 2-5% shock; with `0.0`,
  none ever does.
- Unknown tickers get a price in $50-300 and a *copy* of `DEFAULT_PARAMS`.
- Single-ticker and empty-ticker cases don't crash (Cholesky is `None` when
  `n <= 1`; `step()` returns `{}` when empty).

**`SimulatorDataSource` — async integration:**

- `start()` populates the cache before returning.
- Prices change after letting several ticks elapse.
- `stop()` halts writes — cache version is stable afterward.
- `stop()` is idempotent, including when never started.
- `add_ticker()` seeds the cache immediately; `remove_ticker()` evicts it.
- An exception raised inside `step()` is logged without killing the loop.

Tests use a short `update_interval` (e.g. 0.01s) so integration cases finish in
milliseconds rather than seconds.

---

## 9. Tuning Guide

| Goal | Change |
|---|---|
| More dramatic price action | Raise `sigma` in `TICKER_PARAMS` |
| Faster/slower updates | `SimulatorDataSource(update_interval=...)` |
| More/fewer shock events | `event_probability` (default `0.001`) |
| Stronger sector coupling | Raise `INTRA_TECH_CORR` — **re-verify positive definiteness** |
| Different starting prices | `SEED_PRICES` |
| Persistent upward market | Raise `mu` across `TICKER_PARAMS` |

`update_interval` and `dt` are independent knobs. Changing the interval without
changing `dt` alters how much simulated time passes per real second — halving
the interval to 250ms while leaving `dt` at the 500ms value makes the market
run at half speed. To keep them consistent, derive `dt` from the interval:

```python
dt = update_interval / TRADING_SECONDS_PER_YEAR
```
