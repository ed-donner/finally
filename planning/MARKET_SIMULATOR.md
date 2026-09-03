# Market Simulator — Approach and Code Structure

**Status:** Design + measured review of the shipped implementation in
`backend/app/market/simulator.py`.
Companion documents: [`MASSIVE_API.md`](MASSIVE_API.md) (why the simulator carries far more
weight than PLAN.md assumed) and [`MARKET_INTERFACE.md`](MARKET_INTERFACE.md) (how it is
selected).

Every number below marked **[measured]** came from running the shipped simulator on
2026-09-03, not from theory.

---

## 1. Why the Simulator Is the Primary Path

PLAN.md frames the simulator as the fallback for users without an API key. The entitlement
research changes its status: a **free Massive key cannot return a live price at all**, so
the simulator is what drives the terminal for users with no key *and* for the much larger
group with a free key. Real live data begins at $199/month.

The simulator is therefore not a stand-in for the real product. For nearly every student
running this project, it **is** the product. It deserves to be correct.

Its job, in priority order:

1. **Look alive.** Prices must change visibly every tick or the flash animation, the
   sparklines, and the whole terminal aesthetic fall flat.
2. **Be statistically defensible.** A finance-adjacent teaching project should not move
   prices by `random.uniform(-1, 1)`. Volatility should mean something.
3. **Be correlated.** Watching ten tickers move independently looks synthetic instantly.
   Real markets have a factor structure.
4. **Provide drama.** Occasional sharp moves so the P&L chart and heatmap have something to
   show inside a five-minute demo.
5. **Never dead-end.** Any ticker a user or the LLM invents must get a price.

Requirements 2 and 4 are in tension, and §6 shows the shipped code currently resolves that
tension badly.

---

## 2. The Model: Geometric Brownian Motion

GBM is the standard model for equity prices — the basis of Black-Scholes — and it has the
two properties that matter here: prices stay strictly positive, and returns rather than
absolute levels are what scale with volatility, so a $500 stock and an $80 stock look
equally plausible under the same σ.

The discrete update:

```
S(t+Δt) = S(t) · exp[ (μ − σ²/2)·Δt  +  σ·√Δt·Z ]
```

| Term | Meaning |
|---|---|
| `S(t)` | current price |
| `μ` | annualised drift (expected return) |
| `σ` | annualised volatility |
| `Δt` | time step as a fraction of a trading year |
| `Z` | standard normal draw, correlated across tickers (§4) |
| `−σ²/2` | Itô correction — without it the *median* path drifts up spuriously |

### Calibrating Δt

The tick interval is 500 ms of wall-clock time, and σ is quoted per trading year, so:

```
TRADING_SECONDS_PER_YEAR = 252 days × 6.5 hours × 3600 = 5,896,800
Δt = 0.5 / 5,896,800 ≈ 8.48 × 10⁻⁸
```

This is right, and it is worth stating why: one trading day is 46,800 ticks, and
46,800 × Δt = 1/252 exactly. So a simulated trading day reproduces the target daily
volatility by construction.

**Verified empirically [measured].** 200 independent one-trading-day runs of AAPL
(σ = 0.22, shocks disabled):

```
realized daily log-return sd = 1.3425%
target  σ/√252               = 1.3859%
standard error               = 0.0695%     z = −0.62
```

Well within sampling noise. The GBM core is correctly calibrated.

### Visible movement

A model can be correct and still look dead if every tick rounds to the same cent. Per-tick
standard deviation is `S · σ · √Δt` — for AAPL at 324.96 that is **2.1 cents**, comfortably
above the 1-cent display quantum.

Fraction of ticks where the 2-decimal displayed price actually changes, over 3,000 ticks at
the real 2026-09-02 closes **[measured]**:

| Ticker | Price | σ | Per-tick sd | Visible ticks |
|---|---|---|---|---|
| AAPL | 324.96 | 0.22 | $0.0208 | 81.1% |
| GOOGL | 337.12 | 0.25 | $0.0245 | 84.6% |
| MSFT | 496.82 | 0.20 | $0.0289 | 87.0% |
| AMZN | 254.98 | 0.28 | $0.0208 | 81.5% |
| TSLA | 357.01 | 0.50 | $0.0520 | 92.5% |
| NVDA | 224.41 | 0.40 | $0.0261 | 84.8% |
| META | 592.85 | 0.30 | $0.0518 | 91.2% |
| JPM | 356.22 | 0.18 | $0.0187 | 79.9% |
| V | 378.40 | 0.17 | $0.0187 | 80.0% |
| NFLX | 82.73 | 0.35 | $0.0084 | 58.2% |

Every ticker flashes on the majority of ticks. NFLX is the floor case at 58% because it is
the only low-priced name — its per-tick sd is under a cent, so rounding eats roughly two
ticks in five. Still perfectly lively at two visible changes per second, and no
intervention is needed; the number is recorded here so nobody "fixes" a non-problem.

**Design rule:** if a future ticker prices below roughly $20, per-tick sd falls under a
tenth of a cent and it will look frozen. Either widen display precision for sub-$20 names
or floor their σ.

---

## 3. Seeding

### Anchored seeds are strongly preferred

`backend/app/market/seed_prices.py` hard-codes ten starting prices. They have drifted
badly — as measured against real 2026-09-02 closes:

| Ticker | Seed in repo | Real close | Error |
|---|---|---|---|
| NVDA | 800.00 | 224.41 | 3.6× too high (splits) |
| NFLX | 600.00 | 82.73 | 7.3× too high (splits) |
| META | 500.00 | 592.85 | 16% low |
| MSFT | 420.00 | 496.82 | 15% low |
| GOOGL | 175.00 | 337.12 | 48% low |

A demo showing NVDA at $800 next to a real quote is embarrassing, and the table will keep
rotting. `AnchoredSimulatorDataSource` (see `MARKET_INTERFACE.md` §6) replaces it with one
free-tier API call that prices the whole market. The static table stays only as the no-key
fallback, and should be refreshed to the values in `MASSIVE_API.md` §5.1.

```python
class SimulatorDataSource:
    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 1e-4,          # see §6
        seed_overrides: dict[str, float] | None = None,   # real anchors when available
    ) -> None: ...
```

Seed resolution order: `seed_overrides` → `SEED_PRICES` → synthesised.

### Unknown tickers must not dead-end

PLAN.md §13.2 item 8 asks what happens when a user — or the LLM's `watchlist_changes` —
requests a symbol the simulator has never heard of. Recommendation there was: synthesise,
never reject. The shipped code does synthesise, via `random.uniform(50.0, 300.0)`, but with
a flaw **[measured]**:

```
unknown-ticker seeds across 3 fresh sims:  [141.23, 124.29, 268.96]
```

`ZZZZ` gets a different price on every restart, so a held position's cost basis jumps
between container restarts and the P&L chart lies. Make the synthetic seed **deterministic
per symbol** by hashing it:

```python
def _synthesize_seed(ticker: str) -> float:
    """Stable pseudo-price for an unknown symbol. Same ticker, same price, always."""
    h = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    return round(20.0 + (h % 48_000) / 100.0, 2)      # $20.00 – $500.00
```

Unknown tickers also take `DEFAULT_PARAMS` (σ = 0.25, μ = 0.05) and cross-group correlation.
In Massive-backed modes, validate the symbol with `get_ticker_details()` first — it is
free-tier accessible and 404s on nonsense — then synthesise only if validation is
unavailable.

---

## 4. Correlation

Independent tickers read as fake within seconds: real markets move together. The simulator
imposes a factor structure through a correlation matrix and its Cholesky decomposition.

Generate `n` independent standard normals `z`, then `L @ z` where `L Lᵀ = C` has exactly
covariance `C`:

```python
z_independent = np.random.standard_normal(n)
z_correlated  = self._cholesky @ z_independent      # L is lower-triangular from np.linalg.cholesky
```

The shipped correlation structure:

| Pair | ρ |
|---|---|
| Tech ∩ Tech (AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX) | 0.6 |
| Finance ∩ Finance (JPM, V) | 0.5 |
| TSLA with anything | 0.3 |
| Cross-sector, or any unknown ticker | 0.3 |

Sensible, and cheap to extend with more sectors. Two properties worth recording.

**The matrix stays positive-definite as the watchlist grows [measured].** `np.linalg.cholesky`
raises `LinAlgError` on a non-PD matrix, and `_rebuild_cholesky` has no error handling — so
if it could fail, `add_ticker` would raise and a watchlist addition would 500. Tested with
the default ten plus 5, 20, 50 and 100 synthetic tickers:

```
n=110   cholesky OK   min_eigenvalue = 0.400000
```

The minimum eigenvalue is pinned at `1 − ρ_max = 0.4` regardless of size, because the
structure is a block matrix of equicorrelated groups. It is safe. Still, wrap the call and
fall back to the identity matrix on `LinAlgError` — the cost is three lines, and the failure
mode otherwise is a user-facing 500 from a watchlist add.

**Rebuild cost is O(n²) per add/remove**, negligible at n < 100 and only on watchlist edits,
never in the tick loop.

---

## 5. The Tick Loop

```python
async def _run_loop(self) -> None:
    while True:
        try:
            prices = self._sim.step()                 # one vectorised normal draw for all
            for ticker, price in prices.items():
                self._cache.update(ticker, price, open_price=self._anchors.get(ticker))
        except Exception:
            logger.exception("Simulator step failed")  # never let one bad tick kill the loop
        await asyncio.sleep(self._interval)
```

Three properties to preserve:

- **Full precision internally, rounded at the boundary.** `self._prices[ticker]` keeps the
  unrounded float; only the value handed to the cache is `round(price, 2)`. Rounding in
  place would accumulate a drift bias over tens of thousands of ticks. The shipped code gets
  this right.
- **The loop never dies.** A raise inside `step()` would silently end the background task
  and freeze every price with no error path. The blanket `except` is correct here.
- **One `np.random.standard_normal(n)` per tick**, not per ticker. At 10–50 tickers and
  2 Hz this is free.

`asyncio.sleep(interval)` after the work means the true period is `interval + work`, so
ticks drift slightly slower than 500 ms. Irrelevant for display; do not add a compensating
scheduler.

---

## 6. Random Events — the one thing that needs fixing

The shipped implementation applies, per ticker per tick:

```python
if random.random() < self._event_prob:          # default 0.001
    shock = random.uniform(0.02, 0.05) * random.choice([-1, 1])
    self._prices[ticker] *= 1 + shock
```

PLAN.md §6 asks for "occasional random events — sudden 2-5% moves on a ticker for drama."
The intent is right. The calibration is not.

At `p = 0.001` with 7,200 ticks per wall-clock hour, each ticker takes **7.2 shocks per
hour**, each a permanent 2–5% level shift with random sign. That is a random walk of shocks
layered on top of the GBM, and it does not merely add drama — it obliterates the σ
calibration that §2 verified.

60 one-hour simulations of AAPL at each setting **[measured]**:

| `event_probability` | Shocks/hour/ticker | 1-hour return sd | Worst move seen |
|---|---|---|---|
| **0.001 (current)** | 7.20 | **10.02%** | 28.77% |
| 0.0001 | 0.72 | 3.10% | 8.51% |
| 0.0 (pure GBM) | 0.00 | 0.505% | 1.05% |
| *theory, σ = 0.22* | — | *0.544%* | — |

**The shock process inflates realised volatility roughly 20×.** AAPL, parameterised at a
22% annual vol, actually delivers a 10% standard deviation *per hour* — an annualised
volatility somewhere north of 400%. Every per-ticker σ in `seed_prices.py` is decorative:
TSLA's 0.50 and JPM's 0.18 produce nearly identical behaviour, because both are swamped by
the same shock process. A user who opens the app, buys $2,000 of AAPL, and comes back from
lunch may find the position down 25% — from a model calibrated to move 1.4% in a day.

### Recommended fix: transient shocks, not permanent ones

The underlying error is that a shock is a **permanent level shift**. Real intraday spikes
substantially mean-revert. Model the event as a decaying additive component so the drama is
visible on the chart but the long-run distribution stays governed by σ:

```python
@dataclass
class Shock:
    magnitude: float      # signed, e.g. -0.03
    decay: float          # per-tick multiplier, e.g. 0.985 → ~half-life 46 ticks (23s)

def step(self) -> dict[str, float]:
    ...
    for i, ticker in enumerate(self._tickers):
        # 1. GBM evolves the underlying price — untouched, still calibrated
        self._prices[ticker] *= math.exp(drift + diffusion)

        # 2. Shocks are a separate, decaying overlay
        if random.random() < self._event_prob:
            self._shocks[ticker] = Shock(
                magnitude=random.uniform(0.015, 0.04) * random.choice([-1, 1]),
                decay=0.985,
            )
        shock = self._shocks.get(ticker)
        if shock:
            displayed = self._prices[ticker] * (1 + shock.magnitude)
            shock.magnitude *= shock.decay
            if abs(shock.magnitude) < 1e-4:
                del self._shocks[ticker]
        else:
            displayed = self._prices[ticker]

        result[ticker] = round(displayed, 2)
```

This gives a sharp visible spike that bleeds off over roughly a minute — which is what an
intraday spike actually looks like on a chart — while `self._prices` continues to follow
calibrated GBM.

Pair it with `event_probability = 1e-4`: across a 10-ticker watchlist that is about
**1.2 events per 10-minute demo**, enough for something to happen while the user is
watching, without a shock every 50 seconds.

If a decaying overlay is judged too much machinery for the teaching value, the minimum
acceptable change is `event_probability = 1e-4` and a 1–3% magnitude range. That still
leaves shocks dominating σ by ~6×, but takes the worst case from 29% to 8.5%.

---

## 7. Price History for Charts

PLAN.md §13.2 item 15 notes that charts and sparklines accumulate from SSE "since page
load", so the main chart is empty on first paint and a refresh discards everything. It
recommends a bounded ring buffer, and that is right — it is the difference between a chart
that looks alive on load and one that looks broken.

`MARKET_INTERFACE.md` §3 puts this on the interface as `get_history()`. The simulator's
implementation is a `collections.deque`:

```python
class SimulatorDataSource:
    HISTORY_POINTS = 600            # 600 ticks × 500ms = 5 minutes per ticker

    def __init__(self, ...):
        self._history: dict[str, deque[tuple[float, float]]] = defaultdict(
            lambda: deque(maxlen=self.HISTORY_POINTS)
        )

    async def get_history(self, ticker: str, points: int = 120) -> list[PricePoint]:
        series = self._history.get(ticker, ())
        return [PricePoint(epoch_to_iso(t), p) for t, p in list(series)[-points:]]
```

Memory: 600 points × 2 floats × 50 tickers ≈ 480 KB. Trivial.

**Prefill on start** so the very first paint is not a single dot: run the simulator forward
`HISTORY_POINTS` steps from the seed price with no sleeping, record the path, then reset the
price to the seed. Half a second of CPU buys a chart with five minutes of plausible history
at t=0.

Massive-backed sources override `get_history()` with real minute bars — available on the
free tier, and a genuine improvement over synthetic backfill (`MASSIVE_API.md` §5.4).

---

## 8. Code Structure

```
backend/app/market/
├── simulator.py
│   ├── GBMSimulator                 # pure, synchronous, no I/O — the model
│   │     .step() -> dict[str, float]
│   │     .add_ticker() / .remove_ticker() / .get_price() / .get_tickers()
│   │     ._rebuild_cholesky()       # + LinAlgError fallback (§4)
│   │     ._synthesize_seed()        # deterministic hash (§3)
│   └── SimulatorDataSource          # async MarketDataSource adapter
│         .start() / .stop() / .add_ticker() / .remove_ticker()
│         .describe() / .get_history()
│         ._run_loop()               # 500ms tick → PriceCache
├── seed_prices.py                   # SEED_PRICES, TICKER_PARAMS, correlation config
└── anchored.py                      # AnchoredSimulatorDataSource (see MARKET_INTERFACE.md)
```

The split between `GBMSimulator` and `SimulatorDataSource` is the best structural decision
in the shipped code and should be defended in review. `GBMSimulator` has no async, no I/O
and no cache reference — it is a deterministic function of its own state, so its statistical
properties can be tested at tens of thousands of steps per second with no event loop. Every
measurement in this document was produced by driving `GBMSimulator` directly. Keep the model
free of the transport.

---

## 9. Testing

Beyond the existing unit tests, the properties that actually matter are statistical.

**Distributional (the tests that would have caught §6):**

- Realised daily log-return sd over ≥200 one-day runs is within 3 standard errors of
  `σ/√252`, **with shocks enabled at the production default**. This is the regression test
  for the event calibration; run it against every σ in `TICKER_PARAMS`, not just AAPL.
- Realised correlation between two tech tickers over a long run is 0.6 ± 0.05, and
  tech-vs-finance is 0.3 ± 0.05. Confirms the Cholesky is applied and not silently bypassed.
- Mean log return over many runs is consistent with `(μ − σ²/2)·t` — catches a dropped Itô
  correction.

Seed `np.random` and `random` in these tests; both are used and both need fixing for
reproducibility.

**Invariants:**

- Price is strictly positive after 100,000 steps. GBM guarantees it analytically; a shock
  multiplier below −1 would not.
- No NaN or infinity for any σ in the table.
- `step()` returns exactly the current ticker set, always.
- Cholesky survives 100 unknown tickers added one at a time (the incremental path, which
  rebuilds on every call, not the batch constructor).

**Behavioural:**

- Visible-tick rate exceeds 50% for every default ticker at anchored prices — the guard on
  the §2 rounding cliff.
- Deterministic seeding: the same unknown ticker yields the same synthetic price across two
  fresh instances.
- `remove_ticker` then `add_ticker` restores the price it had, not a fresh seed.

---

## 10. Summary of Changes to the Shipped Simulator

| # | Change | Severity |
|---|---|---|
| 1 | `event_probability` 0.001 → 1e-4, and make shocks **decay** rather than permanently shift the level | **High** — currently inflates realised volatility ~20× and voids every σ parameter |
| 2 | Deterministic hash-based seeds for unknown tickers | **Medium** — cost basis and P&L change across restarts today |
| 3 | Accept `seed_overrides` so real Massive closes can anchor the simulation | **Medium** — the static table is 3–7× wrong on two tickers |
| 4 | Record `open_price` so the daily-change column has a baseline | **Medium** — PLAN.md §13.1 item 1 |
| 5 | Ring-buffer history + prefill, exposed via `get_history()` | **Medium** — charts are empty on first paint |
| 6 | `LinAlgError` fallback in `_rebuild_cholesky` | **Low** — verified safe to n=110, but the failure is a user-facing 500 |
| 7 | Refresh `SEED_PRICES` to real 2026-09-02 closes | **Low** — only the no-key path |
| 8 | Statistical tests, including one that fails at the current `event_probability` | **High** — nothing today would catch #1 |
