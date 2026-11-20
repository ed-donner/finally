# Market Simulator Research

**Status:** Research note, 2026-11-19. Companion to [PLAN.md](PLAN.md) §6 (Simulator) and §12 (statistical tests).
**Question answered:** How should FinAlly's default price simulator be built so that it looks realistic on screen, is statistically checkable, is reproducible, and keeps the portfolio math correct?

---

## 1. Summary

- Use **exact-discretization geometric Brownian motion (GBM)** for each ticker's log-price. The per-tick update is `S' = S · exp((μ − σ²/2)Δt + σ√Δt · z)`. This is exact for the GBM model at any step size, so it never produces negative prices and the Itô term is not an approximation.
- Correlation comes from a **single market factor**: `z_i = β_i·z_m + √(1 − β_i²)·ε_i`. The sample correlation between tickers *i* and *j* is `β_i·β_j`. This is cheaper and easier to explain than a full covariance matrix with a Cholesky factor, and it is exactly what PLAN.md specifies.
- Random jumps are a **multiplicative Bernoulli event per tick**, applied after the diffusion step.
- **Two numbers in the spec need correcting or confirming**, both found while checking the arithmetic here:
  1. The jump-variance multiplier is about **3.4×** the diffusion variance, not "2–3×" (PLAN.md §6). Realized annualized volatility on screen is about **52%**, not "about 50%" by a wide margin. It is still close enough to the spec's wording. The number should be written as 3.4× so the tests and docs agree.
  2. The time scale means the simulator runs about **60× faster than the market**. One real minute covers one market hour. This is intended ("flickers without drifting"), but the spec should say so in those terms, because it changes how a user perceives "realistic" motion.
- Seed parameters (starting prices, μ, σ, β) are **product inputs**. This research does not invent current market prices. Section 7 gives the proposed structure and placeholder values.

---

## 2. The model

### 2.1 Price process

For each ticker *i*, with market time step Δt (in years):

```
ln S_i(t+Δt) − ln S_i(t) = (μ_i − σ_i²/2)·Δt + σ_i·√Δt·z_i
```

Exact for GBM. Because the update multiplies `S` by `exp(·)`, the price is always positive and finite. Stepping the SDE directly with Euler (`S' = S(1 + μΔt + σ√Δt z)`) would also work at small Δt but can go negative for large σ, so exact discretization is the better choice at no extra cost. Source: the exact GBM solution is standard; the arXiv paper on portfolio optimization ([arXiv 1810.08584](https://arxiv.org/pdf/1810.08584)) uses the same exponential form for stepping.

### 2.2 Time scale

PLAN.md fixes one tick as 500 ms of wall-clock time and **Δt = 30 trading seconds**:

```
trading seconds per year   = 252 × 6.5 × 3600 = 5,896,800
Δt (years) per tick        = 30 / 5,896,800    ≈ 5.088 × 10⁻⁶
```

For σ = 25%, one tick's diffusion sd is `σ√Δt = 0.25 × 0.002256 ≈ 0.056%`, which matches PLAN.md's "about 0.06%".

What that means on screen:

| Wall-clock | Ticks | Market time | Diffusion sd (σ = 25%, no jumps) |
|---|---|---|---|
| 1 s | 2 | 1 min | ≈ 0.08% |
| 1 min | 120 | 1 market hour | ≈ 0.61% |
| 10 min | 1,200 | 10 market hours (≈ 1.5 trading days) | ≈ 1.9% |
| 1 hour | 7,200 | 60 market hours (≈ 9 trading days) | ≈ 4.8% |

So one real hour is about nine trading days of price movement. That is the intended "flicker without drifting far in a session" behaviour (PLAN.md §6), but it means a user who leaves the app open will see multi-day moves. This is a product decision. The spec should state it. Changing `SIM_TICK_MARKET_SECONDS` is the one knob.

### 2.3 Correlation via one market factor

Per tick:

```
z_m ~ N(0,1)                       # one shared market shock
ε_i ~ N(0,1), independent per ticker
z_i = β_i · z_m + √(1 − β_i²) · ε_i
```

Then `Var(z_i) = β_i² + (1 − β_i²) = 1` and `Cov(z_i, z_j) = β_i β_j`. So the correlation is exactly `β_i·β_j`, provided `|β_i| ≤ 1`. This is why PLAN.md's test (within 0.02 of `β_i·β_j`) is the right oracle.

Why not a Cholesky factor of a full matrix: with 10–30 tickers and a single factor, a full matrix is more code and more configuration for no visible benefit. The factor model also gives a sensible story: "tech moves together, banks move together, everything moves with the market." Cholesky is the upgrade path if sector structure is ever needed. The general construction is `z = L·ε` for a correlation matrix `R = L·Lᵀ` (see [the Julia correlated-GBM example](https://cdn.jsdelivr.net/npm/code-tokenizer@1.0.6/samples/Julia/stockcorr.jl) for the pattern).

### 2.4 Jumps

Per ticker, per tick:

```
with probability p = 1/1200:
    J ~ Uniform[0.02, 0.05]
    s = random sign in {+1, −1}
    S ← S · (1 + s·J)     # applied after the diffusion step
```

Properties worth knowing:

- **Frequency:** `p = 1/1200` per tick → 120 ticks per real minute → 0.1 jumps per ticker per minute, about one every 10 minutes (PLAN.md §6).
- **Size:** a jump is 2–5% against a typical tick of 0.06%, so one jump equals roughly 40–90 normal ticks.
- **Variance contribution (corrected):** per tick, the jump variance is `p · E[J²]` with `E[J²] = (0.02² + 0.02·0.05 + 0.05²)/3 ≈ 1.3 × 10⁻³`. So jump variance per tick is `≈ 1.08 × 10⁻⁶`. The diffusion variance per tick is `σ²Δt = 0.0625 × 5.088 × 10⁻⁶ ≈ 3.18 × 10⁻⁷`. **Ratio ≈ 3.4×**, not 2–3×. Annualized: diffusion gives 25%, total gives `√(0.275) ≈ 52%`.
- Jumps are symmetric in *price* sign, but not in *log* space. The mean log-jump is `½·ln(1 − J²) ≈ −J²/2`, so jumps add a small negative log-drift of about `p·E[J²]/2 ≈ 5 × 10⁻⁷` per tick. That is the same order as the intended `μΔt ≈ 4 × 10⁻⁷` for μ = 8%. So with jumps on, the realized drift is not exactly μ. This is why all drift and Itô tests (PLAN.md §12) run with `SIM_EVENTS_ENABLED=false`. Jumps add variance and a small bias; the spec should say so.

**Product decision (from PLAN.md §6, unchanged):** if the owner wants the observed volatility to match σ, lower the jump size to 1–3% or lower `p`. Writing "3.4×" into the spec, not "2–3×", lets the owner make that call with the right number.

### 2.5 Reproducibility

- A single seeded RNG (`SIM_SEED`) is used for all draws, in a fixed order per tick: first `z_m`, then `ε_i` for each ticker in a fixed sorted order, then jump draws per ticker. Changing the order changes every price, so the order is part of the contract.
- Use one RNG object owned by the simulator, not the module-level generator. Two simulators in one test process then do not interfere.
- Python: `random.Random(seed)` with `gauss` and `random` is sufficient and avoids a numpy dependency. If numpy is already in the project for other reasons, `numpy.random.Generator(PCG64(seed))` is equally good. Pick one and do not mix.

---

## 3. Statistical checks (for PLAN.md §12)

These are the tests the spec already lists. Here is what each one needs and the reasoning behind the tolerances.

| Check | Setup | Oracle | Notes |
|---|---|---|---|
| Drift, exact | σ = 0, jumps off | one-step log-return = `μΔt` to 1e-12 | Exact arithmetic, no sampling error. |
| Diffusion variance | μ = 0, σ = 0.25, jumps off, 100,000 steps, fixed seed | sample var of log-returns within 2% of `σ²Δt` | Standard error of a variance estimate with N = 100k is about `√(2/N) ≈ 0.45%`, so 2% is about 4.5 SE. Safe. |
| Itô term (power) | σ = 0.8, Δt = 1/252, jumps off | mean log-return ≈ `(μ − σ²/2)Δt` | Itô term per step = `0.32/252 ≈ 1.27 × 10⁻³`. Step sd = `0.8/√252 ≈ 0.050`; standard error over 100k steps ≈ `1.6 × 10⁻⁴`. The term is about 8 SE. The test fails if the term is dropped. |
| Drift (power) | μ = 1.0, long run | sample mean within 4 SE of `(μ − σ²/2)Δt`; must reject μ = 0 and μ = −1 | Confirms the test has teeth, as H2 requires. |
| Correlation | two tickers, β_i and β_j, jumps off | sample corr within 0.02 of `β_i β_j` | With N = 100k, sampling sd of a correlation ≈ `1/√N ≈ 0.003`, so 0.02 is about 6 SE. |
| Jump count | σ = 0, N ticks | count ~ Binomial(N, 1/1200) | Check within a 4-SE binomial band. |
| Jump size and sign | σ = 0 | `|J| ∈ [0.02, 0.05]`; signs roughly 50/50 | Sign balance tested with a binomial band. |
| Unknown symbol | any well-formed symbol not in the seed table | μ = 5%, σ = 25%, β = 0.5, start $100.00 | Table-driven. |
| Reproducibility | same seed, same number of ticks | identical price sequences | Guards the draw-order contract in §2.5. |

All statistical tests run with `SIM_EVENTS_ENABLED=false` so jumps do not contaminate the oracle (PLAN.md §12).

---

## 4. Restart and continuity

- On startup the simulator reads `price_state` (PLAN.md §7). Tickers with a stored price start from that price; others start from the seed.
- `prev_close` for the simulator is the **starting price** of the process, not the stored value, so the day change starts at 0% after a restart. **Check this against PLAN.md §6:** it says the simulator baseline is "the starting price, so the day change starts at 0%". If a restart resumes from the stored price, the starting price is that resumed price, so day change resets to 0% on restart. That is a visible discontinuity in the change column. Recommend storing `prev_close` in `price_state` (it already has the column) and reading it back, so the day change survives a restart. This is a small spec clarification.

---

## 5. Portfolio coupling

The simulator's only job for portfolio math is to supply a price. The invariants in PLAN.md §7 are independent of the price path. Two points matter:

- **Fills use the cache price at request time** (PLAN.md §8). The simulator's price is therefore the fill price. Because the simulator moves about 0.06% per tick, a trade and the next second's price can differ slightly, which is expected.
- **Snapshot values** are cash + Σ(qty × price) at snapshot time (PLAN.md §8). A jump between snapshots shows up as a step in the P&L chart. That is correct behaviour.

No changes are needed in the portfolio code for the simulator.

---

## 6. Event loop placement

- The simulator runs inside the cache's background task, not in its own thread. It is CPU-cheap: a few dozen normal draws per tick.
- At 2 ticks per second, a single `asyncio` task with `await asyncio.sleep(0.5)` is accurate enough. Drift from `sleep` is irrelevant because time is simulated, not read from the wall clock. **The simulator must never use wall-clock time to decide Δt.** Each tick advances market time by exactly 30 s, regardless of how late the tick ran.
- Timestamps in updates use wall-clock `now` for display ordering, but price evolution does not depend on them.

---

## 7. Seed table: structure and placeholder values

The spec does not pin values (PLAN.md §6). Structure per ticker:

```python
@dataclass(frozen=True)
class Seed:
    start: float   # starting price, dollars
    mu: float      # annual drift, e.g. 0.08
    sigma: float   # annual volatility, e.g. 0.25
    beta: float    # market beta, 0 < beta < 1
    sector: str    # for documentation; beta chosen by sector
```

Beta guidance from PLAN.md §6: about 0.7 for tech, 0.6 for financials, 0.5 default. Proposed placeholders below. **Starting prices are placeholders and must be set by the product owner.** They are not current market quotes, and this research did not verify any.

| Ticker | Sector | β (proposed) | σ (proposed) | μ (proposed) | Start (placeholder) |
|---|---|---|---|---|---|
| AAPL | tech | 0.7 | 0.25 | 0.08 | TBD |
| GOOGL | tech | 0.7 | 0.28 | 0.08 | TBD |
| MSFT | tech | 0.7 | 0.25 | 0.08 | TBD |
| AMZN | tech | 0.7 | 0.30 | 0.08 | TBD |
| TSLA | auto/tech | 0.7 | 0.55 | 0.05 | TBD |
| NVDA | tech | 0.7 | 0.45 | 0.10 | TBD |
| META | tech | 0.7 | 0.35 | 0.08 | TBD |
| JPM | financials | 0.6 | 0.22 | 0.07 | TBD |
| V | financials | 0.6 | 0.22 | 0.07 | TBD |
| NFLX | tech | 0.7 | 0.38 | 0.08 | TBD |
| (unknown) | — | 0.5 | 0.25 | 0.05 | 100.00 |

Notes:

- Higher σ for TSLA and NVDA gives more visible motion, which suits the demo, but it is still a choice, not a calibration.
- σ values here are illustrative and are not calibrated to historical data. A calibration exercise would need historical data and is out of scope.
- Keep the table in `backend/app/market/seed_prices.py` (PLAN.md §6), not in the spec.

---

## 8. Implementation sketch

```python
import math
import random
from dataclasses import dataclass

TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600
SIM_TICK_MARKET_SECONDS = 30.0
DT_YEARS = SIM_TICK_MARKET_SECONDS / TRADING_SECONDS_PER_YEAR

class SimulatorSource(PriceSource):
    def __init__(self, seeds, seed=42, jump_prob=1/1200, events_enabled=True):
        self._rng = random.Random(seed)            # one RNG, owned here
        self._seeds = dict(seeds)                  # ticker -> Seed
        self._state = {}                           # ticker -> current price
        self._jump_prob = jump_prob
        self._events = events_enabled
        self._tickers = []                         # sorted, for stable draw order

    async def add_ticker(self, symbol):
        if symbol not in self._seeds:
            self._seeds[symbol] = UNKNOWN_SEED     # default params (PLAN §6)
        self._state.setdefault(symbol, self._seeds[symbol].start)
        self._tickers = sorted(self._state)

    def tick(self):
        z_m = self._rng.gauss(0, 1)                # draw order: market first
        updates = []
        for t in self._tickers:                    # sorted: fixed order
            s = self._seeds[t]
            eps = self._rng.gauss(0, 1)
            z = s.beta * z_m + math.sqrt(1 - s.beta ** 2) * eps
            drift = (s.mu - 0.5 * s.sigma ** 2) * DT_YEARS
            diff = s.sigma * math.sqrt(DT_YEARS) * z
            price = self._state[t] * math.exp(drift + diff)
            if self._events and self._rng.random() < self._jump_prob:
                j = self._rng.uniform(0.02, 0.05)
                sign = 1 if self._rng.random() < 0.5 else -1
                price *= (1 + sign * j)
            self._state[t] = price
            updates.append(PriceUpdate(t, round(price, 2), now_ms(), prev_close=s.start))
        return updates
```

Points to keep in the real implementation:

- **Rounding:** prices are rounded to cents for display only. The internal state keeps full precision so that the random walk is not distorted by rounding. The price written to the cache is the rounded value. This matters for the zero-change check: a tick that moves less than half a cent produces no visible change, and `direction` will be `flat`.
- **Draw order is fixed** (market shock, then each ticker in sorted order, then jump draws). Any change here changes every price for a given seed, so the reproducibility test must be updated in the same commit.
- **Unknown symbol** is added with the default seed and the default start price, so `add_ticker` never fails in simulator mode (PLAN.md §6, §8).
- **`prev_close`** is the seed start, per PLAN.md §6, unless §4 above is adopted.

---

## 9. Open questions for the product owner

1. **Time scale.** Is one real hour ≈ nine trading days of motion acceptable? If not, reduce `SIM_TICK_MARKET_SECONDS` (for example, to 5 s, which is about 10× slower). Every statistical test oracle is written in terms of Δt, so they update automatically.
2. **Volatility on screen.** About 52% annualized with jumps on, 25% with them off. Keep, or lower jump size?
3. **Restart baseline.** Adopt the `prev_close` persistence in §4 so the day change survives a restart?
4. **Seed table.** Who sets the starting prices? They are not verified here.

---

## 10. Sources

- [arXiv 1810.08584, portfolio optimization with exact GBM stepping and Cholesky correlation](https://arxiv.org/pdf/1810.08584): source for the exponential update form and the Cholesky generalization.
- [Julia correlated GBM sample](https://cdn.jsdelivr.net/npm/code-tokenizer@1.0.6/samples/Julia/stockcorr.jl): pattern for correlated draws via a Cholesky factor.
- Standard GBM and Itô references, used for the exact solution in §2.1. No external source was needed for the arithmetic in §2.2–§2.4, which is shown inline so it can be rechecked.

**Related:** [PLAN.md](PLAN.md) §6, §7, §12, §13.6 H1, H2 · [DATA_INTERFACE_DESIGN.md](DATA_INTERFACE_DESIGN.md) · [MASSIVE_API_RESEARCH.md](MASSIVE_API_RESEARCH.md)

