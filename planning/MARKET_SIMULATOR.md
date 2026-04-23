# Market Data Simulator

Design and research for the simulator that generates synthetic stock prices when no Massive API key is configured. The simulator is the default path, not a fallback — most users running FinAlly locally will never touch a real API.

**Status:** 2026-04-22. Implemented in `backend/app/market/simulator.py` and `seed_prices.py`. The math and parameters below match what's in the code; this doc is the reference you'd want to read before changing either.

---

## Why a Simulator (and Why the Default)

A simulated market matters for four distinct audiences, and the design has to serve all of them:

1. **First-run users** — the Docker-run-and-go experience. No signup, no API key, no external dependencies. Prices start moving the moment the container boots.
2. **Course students** — this is the capstone project for an agentic AI coding course. API rate limits and billing concerns would distract from the lesson.
3. **E2E tests** — Playwright assertions need reproducible price paths. `SIMULATOR_SEED` makes the GBM deterministic.
4. **Demos** — realistic-looking but controllable. The simulator occasionally injects 2-5% "events" so the UI has something dramatic to render during a demo.

The bar is therefore: produce price paths that look plausible to a trained eye, correlate tickers in sector-obvious ways, support deterministic replay, and run without blocking the event loop.

---

## The Math: Geometric Brownian Motion

Stock prices in the simulator evolve under **Geometric Brownian Motion (GBM)**, the same model underlying Black-Scholes. For each ticker:

```
S(t + dt) = S(t) · exp( (μ - σ²/2) · dt  +  σ · √dt · Z )
```

where:

- `S(t)` — current price
- `μ` (mu) — annualized drift (expected log-return per year)
- `σ` (sigma) — annualized volatility
- `dt` — time step as a fraction of one trading year
- `Z` — standard normal random variable (with correlation structure applied — see below)

### Why GBM

- Prices stay positive (GBM is strictly positive; arithmetic random walks can go negative).
- Returns are approximately lognormal over any finite interval — matches empirical equity data reasonably well.
- The parameters `μ` and `σ` map directly onto things a finance person can reason about (expected return, annualized volatility) rather than arbitrary knobs.
- Cholesky decomposition of a correlation matrix plugs in cleanly to produce correlated moves across tickers.

### Time step `dt`

Simulator ticks every 500ms. With a trading year of 252 days × 6.5 hours × 3600 seconds = ~5.9M trading seconds/year, one 500ms tick is:

```
dt = 0.5 / (252 · 6.5 · 3600) ≈ 8.48e-8
```

This is the value in `simulator.py` as `DEFAULT_DT`. It's deliberately tiny so the annualized `μ` and `σ` parameters are themselves annualized (not per-tick) and therefore interpretable.

### Random event shocks

Independently of the GBM step, every ticker has ~0.1% probability per tick of receiving a "shock": a uniform random ±2-5% instantaneous move. At 500ms ticks this is roughly one event per ticker every 500 ticks ≈ every 4 minutes — rare enough not to dominate the chart, common enough to make demos lively.

The shock is applied multiplicatively on top of the GBM step, so it doesn't break the lognormal structure (just bumps the distribution).

---

## Correlation Structure

Real equities do not move independently. Tech stocks move together; financials move together; tech and financials move somewhat but not strongly. The simulator encodes this with sector groups and a **correlation matrix**, then uses **Cholesky decomposition** to transform independent standard normals into correlated ones.

### Sector groups (in `seed_prices.py`)

- **Tech** (ρ = 0.6): AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX
- **Finance** (ρ = 0.5): JPM, V
- **Independent** (ρ = 0.3 with anything): TSLA
- **Cross-sector** (ρ = 0.3): any ticker in one group vs. any in another

These numbers aren't calibrated to empirical correlations — they're chosen to produce visible co-movement without making the chart look like every ticker is the same line. A student reading the code should be able to see AAPL and MSFT drift together while TSLA does its own thing.

### Cholesky transform

Given the correlation matrix `C` and its Cholesky factor `L` (so `L · L^T = C`), a vector of independent standard normals `Z_ind` becomes correlated normals via `Z_corr = L · Z_ind`. The simulator rebuilds `L` whenever the ticker set changes — O(n²) on add/remove, which is fine up to hundreds of tickers.

### What this does not model

- Regime shifts (bull vs. bear markets). `μ` is static per ticker.
- Volatility clustering (GARCH-style). `σ` is static.
- Intraday seasonality (open/close volume patterns). Prices tick uniformly.
- Earnings events, news shocks, circuit breakers. The random event shock is a weak caricature.

If the course ever wants to teach about these, each one is a discrete additive module on top of the current GBM — not a rewrite.

---

## Seed Prices and Parameters

In `backend/app/market/seed_prices.py` — realistic opening prices and per-ticker `(μ, σ)`:

| Ticker | Seed price | μ (drift) | σ (vol) |
|---|---|---|---|
| AAPL  | $190 | 0.08 | 0.22 |
| GOOGL | $175 | 0.07 | 0.24 |
| MSFT  | $420 | 0.08 | 0.20 |
| AMZN  | $180 | 0.06 | 0.28 |
| TSLA  | $250 | 0.03 | 0.50 |
| NVDA  | $800 | 0.08 | 0.42 |
| META  | $500 | 0.07 | 0.30 |
| JPM   | $195 | 0.05 | 0.18 |
| V     | $275 | 0.06 | 0.17 |
| NFLX  | $620 | 0.05 | 0.32 |

Defaults for unknown tickers: `μ = 0.05`, `σ = 0.25`.

### How these were chosen

- **Seed prices** are approximately what these stocks traded at in early 2024, chosen to look familiar.
- **σ** reflects each stock's real realized volatility roughly — TSLA and NVDA are high, JPM and V are low, the rest cluster around 0.20-0.30.
- **μ** is positive for everything so the portfolio tends to drift up in a long session, making the app feel rewarding to watch. This is an explicit design cheat: a real long-term trading simulator would have a distribution of μ around zero. For a capstone project's aesthetic we err on the side of happy paths.

---

## Deterministic Mode for Tests

Setting `SIMULATOR_SEED` to an integer makes the RNG deterministic. This is load-bearing for two things:

1. **E2E Playwright tests.** Tests set `SIMULATOR_SEED=42` (or similar) and then can assert exact numeric values — "after N seconds, AAPL's price is between $189.90 and $190.10" or even exact equality if they fix the wall-clock via mocking.
2. **Debugging.** If a UI glitch appears only under specific price paths, re-run with the same seed to reproduce.

### What the seed controls

- The GBM innovations (`Z_ind`).
- Whether an event shock fires on this tick (Bernoulli draw).
- The magnitude of the event shock.

### What it does not control

- Wall-clock timing. If tests depend on *when* a price change happens (not just *what* it is), they still need to mock or freeze time.
- The order in which async tasks interleave. `asyncio.sleep(0.5)` is not deterministic; under load you might see 502ms, 510ms, etc.

Tests that assert exact values must therefore either (a) assert structural properties (price > 0, price different from last tick, direction in {up, down, flat}) or (b) mock wall-clock and drive the simulator manually.

---

## Async Lifecycle

`SimulatorDataSource` wraps `GBMSimulator` in an `asyncio.Task`:

```
start(tickers)
  └─ create GBMSimulator(tickers)
  └─ seed cache with initial prices
  └─ spawn _run_loop() task

_run_loop()
  └─ while running:
      └─ sim.step() → dict[ticker, price]
      └─ for ticker, price in result.items():
            cache.update(ticker, price)
      └─ await asyncio.sleep(update_interval)  # 0.5s default

stop()
  └─ task.cancel()
  └─ await task (catches CancelledError)
```

### Why asyncio (not a thread)

The simulator's per-tick work is pure CPU — generate normals, do a matmul, round, write to cache. At 10 tickers every 500ms, this is microseconds of work. Running it in the event loop is simpler than managing a thread, and the `cache.update` is lock-protected anyway.

The Massive source needs threading (`asyncio.to_thread`) because the Polygon SDK is synchronous and makes blocking HTTP calls. The simulator has no such constraint.

### Error handling

Any exception inside `_run_loop()` is caught, logged, and the loop continues. A NumPy NaN from a bad parameter would not crash the app — it would just stop prices from updating on that ticker until the next successful step. In practice we've never seen this fire.

---

## What Could Go Wrong

### Price drift runaway

With positive `μ` on every ticker, a long-running session will see portfolio value drift upward. This is desired for the course project but would break if the session ran for hours/days. Mitigation options:

- Periodically re-seed the simulator with fresh prices (daily reset).
- Cap price paths at a sanity multiplier of the seed price (e.g., clip above 3× and below 0.3×).
- Drive `μ` slightly negative so the distribution is centered.

Not a priority for v1.

### Event shocks too frequent

If we tune `event_probability` up to make demos more exciting, we break the GBM's statistical properties. At 0.001 per tick (current) the shocks are rare enough that they're noise. At 0.01+ they dominate and the "GBM" is really a jump-diffusion process.

### Cholesky on correlation matrices that aren't positive semi-definite

If someone sets correlations by hand to something unphysical (e.g., tech-vs-finance = 0.95 but tech-vs-cross = 0.1), Cholesky may fail or produce complex results. The current static sector matrix is safe; dynamic configuration would need validation.

---

## Reference Implementations

Similar simulators in the wild, for comparison:

- [QuantConnect LEAN](https://github.com/QuantConnect/Lean) — production-quality backtesting engine, overkill but shows what "mature" looks like.
- [zipline](https://github.com/stefan-jansen/zipline-reloaded) — Python backtesting with simulated market data.
- [yfinance simulated mode](https://github.com/ranaroussi/yfinance) — not really a simulator but people use it for offline development.

None of these are drop-in replacements. Our simulator is deliberately small (~200 lines) because its job is "look plausible for 10 tickers in a demo," not "simulate a real market faithfully."

---

## Extensions Worth Considering

Not urgent, but easy wins if the project grows:

1. **Trading halt simulation.** Randomly freeze a ticker for 30-60s to let the UI show "halted" state.
2. **Volume simulation.** Add a synthetic `volume` field to `PriceUpdate`. GBM doesn't speak to volume, but a simple Poisson process would give the UI a volume bar to render.
3. **Bid/ask spread.** Currently prices are single-point. A 0.01-0.05% spread around the GBM price would let the UI show bid/ask and eventually slippage.
4. **Warm starts.** Load the last cached prices from the DB on container restart so the chart doesn't reset.
5. **Configurable sector structure.** Move the correlation matrix into a config file so students can tinker.

---

## Testing

Existing coverage (`backend/tests/market/test_simulator*.py`, 33 tests, ~98% line coverage):

- Step produces positive, finite prices.
- GBM formula matches the reference implementation to within float tolerance.
- Cholesky produces correlated moves (statistical test over many steps).
- add/remove ticker updates internal state and Cholesky factor.
- Seeded runs are reproducible.
- Async lifecycle: start → stop → start again works.

### Gaps

- No test with all 10 default tickers simultaneously — tests use 1-2 to keep assertions tractable. Worth adding at least one smoke test.
- No test that asserts event-shock frequency matches the configured probability (statistical test over 10K+ steps).
- No test that uppercase-normalizes an unusual ticker input (e.g., "aapl " with trailing whitespace).

---

## Summary

The simulator is a small, focused module that earns its keep by making FinAlly feel alive on the first container boot. GBM gives plausible price paths; Cholesky-driven correlation makes co-movement look right; random event shocks give demos drama; deterministic seeding enables E2E tests. The code matches this design in full. The next steps, if we ever revisit, are expanding the per-ticker parameter table and optionally layering on volume and bid/ask. None of that is required for v1.
