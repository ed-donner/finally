# Market Simulator Design

The default market data source for FinAlly: a geometric Brownian motion price simulator that
runs in-process, needs no API key, no network, and no market hours.

Companion documents: `MARKET_INTERFACE.md` (the contract it implements), `MASSIVE_API.md` (the
real-data alternative).

## Why the Simulator Is the Default

It is not a fallback for people without an API key. It is the better demo.

| | Simulator | Massive (free tier) |
|---|---|---|
| Prices move | Always, every 500ms | Only 09:30-16:00 ET weekdays |
| Latency | None | 15 minutes delayed |
| Cost | Free | Free tier, 5 req/min |
| Setup | None | Signup, key, `.env` |
| Reproducible | Yes, with a seed | No |

A student running the app at 21:00 on a Sunday against real data sees ten flat prices, no
flash animations, and zero change percentages, and reasonably concludes it is broken. The
simulator always looks alive. Real data is the interesting option, not the default one.

## The Model: Geometric Brownian Motion

GBM is the standard model for equity prices — the one underneath Black-Scholes. It produces
prices that are always positive, whose returns are normally distributed, and whose volatility
scales with price level. That is enough realism for a trading UI.

```
S(t+dt) = S(t) * exp( (mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z )
```

| Term | Meaning |
|---|---|
| `S(t)` | Current price |
| `mu` | Annualised drift — expected return |
| `sigma` | Annualised volatility |
| `dt` | Time step as a fraction of a trading year |
| `Z` | Standard normal draw, correlated across tickers |

The `- sigma^2/2` correction is not cosmetic. Without it, `mu` is the drift of log-price and
the *expected price* grows faster than `mu` — prices would inflate visibly over a long
session. With it, `mu` means what it claims.

### Choosing `dt`

`mu` and `sigma` are quoted annualised, so `dt` must be a fraction of a trading year measured
the same way. A trading year is 252 days of 6.5 hours:

```python
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600   # 5,896,800
DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR   # ~8.48e-8 for a 500ms tick
```

That tiny `dt` is what makes the output look right. A stock at $190 with `sigma=0.22` moves on
the order of a cent per tick, wanders a few tenths of a percent over a minute, and can drift a
percent or two over a long session. Sub-cent-per-tick is exactly what a real level-1 feed
looks like.

Getting `dt` wrong is the classic failure here. Using `dt = 0.5` (half a *year* per tick)
sends prices to five figures within a minute.

## Correlation Between Tickers

Independent random walks look wrong. Real markets move together — when tech sells off, it
sells off broadly. Ten independently wandering lines read as noise; correlated ones read as a
market.

Correlation is imposed by Cholesky decomposition. Given a correlation matrix `C`, factor it as
`C = L * L^T`; then for a vector of independent standard normals `z`, the product `L @ z` has
exactly the correlation structure of `C`.

```python
corr = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        rho = pairwise_correlation(tickers[i], tickers[j])
        corr[i, j] = corr[j, i] = rho

cholesky = np.linalg.cholesky(corr)      # rebuilt on add/remove, O(n^3) but n < 50
z = cholesky @ np.random.standard_normal(n)
```

The correlation structure is sector-based:

| Pair | rho |
|---|---|
| Tech / tech | 0.60 |
| Finance / finance | 0.50 |
| Anything involving TSLA | 0.30 |
| Cross-sector, or unknown ticker | 0.30 |

Groups live in `seed_prices.py`: tech is AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX; finance is
JPM and V. TSLA is carved out deliberately — it is nominally tech but famously does its own
thing, and giving it independence makes the watchlist more interesting to watch.

One constraint to respect: a correlation matrix must be **positive definite** or
`np.linalg.cholesky` raises. Correlations assigned pairwise by ad-hoc rules are not guaranteed
to be. The values above are mild and consistently assigned, so they factor cleanly; anything
more elaborate (high correlations, more groups, exceptions layered on exceptions) needs either
a check or a nearest-positive-definite repair. If `cholesky` ever raises, that is the cause.

## Random Shock Events

GBM alone is smooth. Real markets jump — earnings, news, a halt. Each ticker gets a ~0.1%
chance per tick of a 2-5% move in a random direction:

```python
if random.random() < self._event_prob:
    magnitude = random.uniform(0.02, 0.05)
    sign = random.choice([-1, 1])
    self._prices[ticker] *= 1 + magnitude * sign
```

With ten tickers at 2 ticks/second, that is an event roughly every 50 seconds — often enough
that a user watching for a minute sees one, rare enough that it stays an event. This is the
single highest-value line of code in the simulator for demo purposes: it is what makes the
flash animations and the P&L chart do something worth looking at.

## Unknown Tickers Are Accepted

`SEED_PRICES` has real parameters for ten symbols. The AI assistant can add any symbol to the
watchlist, and users will type PYPL, AMD, and DIS. Rejecting them would dead-end the headline
demo — "ask the AI to watch a new stock" — on the first plausible thing anyone tries.

So unknown tickers are accepted, with parameters **synthesized deterministically from the
symbol**:

```python
def synthesize_params(ticker: str) -> tuple[float, dict[str, float]]:
    """Derive a stable seed price and GBM params from the ticker symbol.

    Deterministic so PYPL is the same price on every run - a restart should
    not silently reprice the user's position.
    """
    digest = hashlib.sha256(ticker.encode()).digest()
    price = 20.0 + (int.from_bytes(digest[0:4], "big") % 48_000) / 100.0  # $20-$500
    sigma = 0.15 + (digest[4] / 255.0) * 0.35                             # 0.15-0.50
    mu = 0.02 + (digest[5] / 255.0) * 0.06                                # 0.02-0.08
    return round(price, 2), {"sigma": sigma, "mu": mu}
```

Two properties matter:

**Deterministic.** SHA-256 of the symbol, not `random.uniform()`. A user holding 10 shares of
PYPL bought at $73 must not restart the container and find PYPL now trades at $412 — their
position's P&L would be nonsense. The current code uses `random.uniform(50.0, 300.0)`, which
has exactly that bug and needs replacing.

**Plausible ranges.** $20-$500, `sigma` 0.15-0.50, `mu` 0.02-0.08. Every synthesized ticker
looks like an ordinary large-cap and behaves like one.

Synthesized tickers join the cross-sector correlation group at rho 0.30 — no attempt is made
to guess sectors from a symbol.

The shape check `^[A-Z]{1,5}$` still applies (see `MARKET_INTERFACE.md`). "ZZZZZ" is accepted
and gets a price; "hello world" and "12345" are rejected with a 400. The simulator validates
form, not existence — and cannot do otherwise, since it has no universe of real symbols.

## History Backfill

Sparklines need ~60 points. Without backfill, a fresh page shows sixty seconds of empty
charts, which is the first thing anyone sees on launch.

So at startup — and again for each newly added ticker — the simulator runs the GBM recurrence
*backwards* from the seed price to manufacture plausible prior history:

```python
def backfill_history(self, ticker: str, points: int = 60) -> list[float]:
    """Generate synthetic prior history ending at the current price."""
    params = self._params[ticker]
    sigma, mu = params["sigma"], params["mu"]
    dt = self._dt * 120          # one point per minute, not per tick

    price = self._prices[ticker]
    history = [price]
    for _ in range(points - 1):
        drift = (mu - 0.5 * sigma**2) * dt
        diffusion = sigma * math.sqrt(dt) * np.random.standard_normal()
        price /= math.exp(drift + diffusion)     # step backwards
        history.append(round(price, 2))
    return list(reversed(history))               # oldest first
```

Dividing rather than multiplying walks the process backwards, so the series **ends** at the
current price and joins continuously with the live stream. The coarser `dt` gives history a
per-minute cadence, so the sparkline shows an hour of price action rather than thirty seconds
of it — visible shape instead of a flat line.

The result goes into `cache.seed_history()`, and `/api/watchlist` serves it. The frontend
extends it from SSE thereafter.

## Code Structure

```
backend/app/market/
├── models.py          PriceUpdate
├── interface.py       MarketDataSource ABC
├── cache.py           PriceCache
├── seed_prices.py     Seed prices, GBM params, correlation groups, synthesize_params()
├── simulator.py       GBMSimulator + SimulatorDataSource
├── massive_client.py  MassiveDataSource
├── factory.py         create_market_data_source()
└── stream.py          SSE router
```

The split inside `simulator.py` is the important one.

### `GBMSimulator` — the maths, no I/O

Pure, synchronous, testable. Holds prices, parameters, and the Cholesky factor. Knows nothing
about the cache, asyncio, or FastAPI.

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT,
                 event_probability: float = 0.001) -> None: ...

    def step(self) -> dict[str, float]:      # advance all tickers one tick
    def add_ticker(self, ticker: str) -> None
    def remove_ticker(self, ticker: str) -> None
    def get_price(self, ticker: str) -> float | None
    def get_tickers(self) -> list[str]
    def backfill_history(self, ticker: str, points: int = 60) -> list[float]
```

`step()` is the hot path — every 500ms, forever. It draws all `n` normals in one numpy call
and applies one matrix multiply, rather than looping per ticker. At `n < 50` this is
irrelevant to performance, but it is also simply the clearer way to express "correlated draws".

Because it is pure, its tests need no clock and no event loop: construct with a large `dt`,
call `step()`, assert prices moved and stayed positive. Statistical properties (drift and
variance over many steps) are testable by seeding numpy.

### `SimulatorDataSource` — the plumbing, no maths

Implements `MarketDataSource`. Owns the asyncio task, writes into the cache, and does nothing
else.

```python
class SimulatorDataSource(MarketDataSource):
    async def start(self, tickers: list[str]) -> None:
        self._sim = GBMSimulator(tickers, event_probability=self._event_prob)
        for ticker in tickers:
            self._cache.seed_history(ticker, self._sim.backfill_history(ticker))
            self._cache.update(ticker, self._sim.get_price(ticker))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self) -> None:
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker, price)
            except Exception:
                logger.exception("Simulator step failed")
            await asyncio.sleep(self._interval)
```

Two details worth keeping:

**The cache is seeded before the task starts.** `start()` returns with prices already
available, so the first HTTP request never sees an empty cache. Same for `add_ticker()` — a
newly added ticker gets a price immediately, which is what keeps the 2-second trade wait from
ever mattering on the simulator path.

**The loop catches and continues.** A background task that raises dies silently and takes the
entire price feed with it, leaving a UI that looks connected and frozen. Logging and
continuing is the right call for a loop that will get another chance in 500ms.

## Parameters

`seed_prices.py`, chosen to be recognisable rather than current:

| Ticker | Seed | sigma | mu | |
|---|---|---|---|---|
| AAPL | 190.00 | 0.22 | 0.05 | |
| GOOGL | 175.00 | 0.25 | 0.05 | |
| MSFT | 420.00 | 0.20 | 0.05 | |
| AMZN | 185.00 | 0.28 | 0.05 | |
| TSLA | 250.00 | 0.50 | 0.03 | high volatility |
| NVDA | 800.00 | 0.40 | 0.08 | high volatility, strong drift |
| META | 500.00 | 0.30 | 0.05 | |
| JPM | 195.00 | 0.18 | 0.04 | low volatility |
| V | 280.00 | 0.17 | 0.04 | low volatility |
| NFLX | 600.00 | 0.35 | 0.05 | |

The spread in `sigma` is the point. TSLA at 0.50 against V at 0.17 means TSLA visibly jumps
while V barely moves — the watchlist has texture instead of ten lines doing the same thing.
NVDA carries the strongest drift so something in the portfolio tends to trend upward, which
makes the P&L chart more interesting than a random walk around zero.

Seed prices bear no relation to the actual market on any given day, and should not. This is a
simulation with $10,000 of pretend money.

## Testing

| Target | Assertion |
|---|---|
| GBM step | Prices stay positive; moves are sub-percent at the default `dt` |
| Drift | Over many seeded steps, mean log-return approximates `mu * dt` |
| Volatility | Sample std of log-returns approximates `sigma * sqrt(dt)` |
| Correlation | Tech pairs co-move more than tech/finance pairs over many steps |
| Cholesky | Rebuilds on add and remove; matrix stays factorable |
| Unknown tickers | `synthesize_params("PYPL")` returns identical values across calls and processes |
| Backfill | Returns exactly 60 points, oldest first, ending at the current price |
| Events | With `event_probability=1.0`, every tick moves 2-5% |
| Data source | `start()` populates cache and history; `stop()` is idempotent; a raising step does not kill the loop |

Determinism comes from `np.random.seed()` and `random.seed()` in fixtures. Statistical
assertions need generous tolerances — these are random processes, and a test that fails once a
week is worse than no test.

Nothing here requires waiting in real time. Inject a large `dt` to make moves observable, and
drive `step()` directly rather than sleeping through the asyncio loop.
