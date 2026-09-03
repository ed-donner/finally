# Massive API — Reference for FinAlly

**Status:** Research complete. Verified against the live API on 2026-09-03 using the
`MASSIVE_API_KEY` in the project root `.env`, and against `massive` Python SDK **2.2.0**
(the version pinned in `backend/uv.lock`).

Everything marked **[verified]** below was executed against the real API. Everything marked
**[docs]** comes from <https://massive.com/docs> and has not been executable with our key.

---

## 1. What Massive Is

Massive is Polygon.io, rebranded in early 2026. Same infrastructure, same endpoint paths,
new domain. Practical consequences:

- Base URL is `https://api.massive.com`, but **legacy `api.polygon.io` paths still work** and
  the two are interchangeable.
- Error messages still leak the old brand — a 403 on our key returns
  `"Please upgrade your plan at https://polygon.io/pricing"` **[verified]**.
- Response headers still carry Polygon infrastructure names (`x-polygon-cluster-name:
  polygon-ny5`) **[verified]**.
- Any Polygon.io tutorial, StackOverflow answer, or LLM training data from before 2026 is
  still accurate for endpoint shapes. Only the hostname and the SDK package name changed.

Coverage is all 19 US stock exchanges plus dark pools, FINRA facilities and OTC, delivered
over REST, WebSocket and S3-style flat files. FinAlly uses **REST only** — see §9.

---

## 2. Authentication

Two equivalent forms. Both **[verified]** returning HTTP 200:

```bash
# Preferred — bearer header (what the Python SDK sends)
curl "https://api.massive.com/v2/aggs/ticker/AAPL/prev?adjusted=true" \
     -H "Authorization: Bearer $MASSIVE_API_KEY"

# Alternative — query parameter (convenient for browser/debug, but leaks the key into logs)
curl "https://api.massive.com/v2/aggs/ticker/MSFT/prev?apiKey=$MASSIVE_API_KEY"
```

Omitting auth entirely returns **HTTP 401** **[verified]**. An API key is 32 characters.

**Use the header form in FinAlly.** Query-param keys end up in access logs, browser history
and error reports.

The SDK reads the environment variable `MASSIVE_API_KEY` automatically if you pass no
`api_key` argument — which is exactly the variable name PLAN.md already specifies, so no
mapping is needed:

```python
from massive import RESTClient

client = RESTClient()                         # reads MASSIVE_API_KEY from the environment
client = RESTClient(api_key="...")            # or pass it explicitly
```

If neither is present the constructor raises `massive.AuthError` immediately — it does not
wait for the first request.

---

## 3. The Python SDK

```toml
# backend/pyproject.toml — already present
dependencies = ["massive>=1.0.0"]   # resolves to 2.2.0 in uv.lock
```

`RESTClient` constructor signature, read from the installed package **[verified]**:

```python
RESTClient(
    api_key: str | None = None,
    connect_timeout: float = 10.0,
    read_timeout: float = 10.0,
    num_pools: int = 10,
    retries: int = 3,                          # urllib3 Retry, see §7
    base: str = "https://api.massive.com",
    pagination: bool = True,
    verbose: bool = False,                     # sets SDK logger to DEBUG
    trace: bool = False,                       # prints full request/response
    custom_json: Any | None = None,            # e.g. orjson
)
```

Three properties matter for FinAlly's design:

1. **It is fully synchronous.** It uses `urllib3.PoolManager`, not `httpx`/`aiohttp`. Every
   call blocks the thread. In an async FastAPI app every call **must** be wrapped in
   `asyncio.to_thread(...)` or it will stall the event loop and freeze the SSE stream for
   every connected client.
2. **It retries automatically.** `retries=3` with `backoff_factor=0.1` over status codes
   `413, 429, 499, 500, 502, 503, 504`. This silently absorbs brief rate limiting but see
   the failure-mode gotcha in §7.
3. **It returns typed dataclasses, not dicts.** Field names are snake_cased and expanded
   from the wire format's single letters (`c` → `close`, `vw` → `vwap`, `T` → `ticker`).

---

## 4. Plans and Entitlements — read this before designing anything

This is the single most consequential finding of the research, and it contradicts an
assumption in PLAN.md §6.

| Plan | Price | Rate limit | Data timeliness | History |
|---|---|---|---|---|
| Stocks Basic | **Free** | **5 calls/min** | **End of day only** | 2 years |
| Stocks Starter | $29/mo | Unlimited | 15-minute delayed | 5 years |
| Stocks Developer | $79/mo | Unlimited | 15-minute delayed | 10 years |
| Stocks Advanced | $199/mo | Unlimited | Real-time | 20+ years |

**[docs]** for the table; **[verified]** for every Basic-tier consequence below.

### What a free key can and cannot do

Probed live on 2026-09-03 with the project's key:

| SDK call | Endpoint | Result |
|---|---|---|
| `get_previous_close_agg("AAPL")` | `/v2/aggs/ticker/{t}/prev` | ✅ **200** |
| `get_daily_open_close_agg("AAPL", "2026-08-28")` | `/v1/open-close/{t}/{date}` | ✅ **200** |
| `get_aggs("AAPL", 1, "day", ...)` | `/v2/aggs/ticker/{t}/range/...` | ✅ **200** |
| `get_aggs("AAPL", 1, "minute", ...)` *(past days)* | `/v2/aggs/ticker/{t}/range/...` | ✅ **200** |
| `get_grouped_daily_aggs("2026-09-02")` | `/v2/aggs/grouped/locale/us/market/stocks/{date}` | ✅ **200** |
| `get_market_status()` | `/v1/marketstatus/now` | ✅ **200** |
| `get_ticker_details("AAPL")` | `/v3/reference/tickers/{t}` | ✅ **200** |
| `get_snapshot_all("stocks", [...])` | `/v2/snapshot/.../tickers` | ❌ **NOT_AUTHORIZED** |
| `get_snapshot_ticker("stocks", "AAPL")` | `/v2/snapshot/.../tickers/{t}` | ❌ **NOT_AUTHORIZED** |
| `list_universal_snapshots(...)` | `/v3/snapshot` | ❌ **NOT_AUTHORIZED** |
| `get_last_trade("AAPL")` | `/v2/last/trade/{t}` | ❌ **NOT_AUTHORIZED** |
| `get_aggs("AAPL", 1, "minute", "2026-09-03", ...)` *(today)* | — | ❌ **NOT_AUTHORIZED** |

Two distinct rejection messages, and the difference matters for diagnostics:

```json
{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data.
 Please upgrade your plan at https://massive.com/pricing"}
```
→ the **endpoint** is out of plan (all snapshot and last-trade endpoints).

```json
{"status":"NOT_AUTHORIZED","message":"Your plan doesn't include this data timeframe.
 Please upgrade your plan at https://polygon.io/pricing"}
```
→ the endpoint is allowed but the **date** is too recent. Requesting today's minute bars on
a free key fails; yesterday's succeed.

### Why this breaks the plan as written

PLAN.md §6 says *"Free tier (5 calls/min): poll every 15 seconds"* and the shipped
`backend/app/market/massive_client.py` polls `get_snapshot_all()`. **On a free key that
client returns zero prices, forever.** Every poll raises `BadResponse`, the exception is
swallowed by the `except Exception` in `_poll_once`, and the cache stays empty — the app
boots to a watchlist of blanks with no error surfaced to the user.

Free-tier keys are what students will have. The interface design in
`planning/MARKET_INTERFACE.md` addresses this directly; §8 below gives the probe that
detects the tier.

---

## 5. Endpoints FinAlly Actually Needs

### 5.1 Daily Market Summary — the free tier's best endpoint

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}
```

One call returns the previous OHLC bar for **every** US ticker. Measured live: **12,541
tickers in 1.04 s** from a single request **[verified]**. On a 5-calls-per-minute budget
this is transformative — it prices an entire watchlist of any size for one call, and adding
a ticker costs nothing.

```python
from massive import RESTClient

client = RESTClient()
bars = client.get_grouped_daily_aggs("2026-09-02", adjusted=True)
by_ticker = {b.ticker: b for b in bars}

aapl = by_ticker["AAPL"]
print(aapl.open, aapl.high, aapl.low, aapl.close, aapl.vwap, aapl.volume)
# 326.865 328.4 323.53 324.96 325.2771 33776370.0
```

Real values returned for the default watchlist on 2026-09-02 **[verified]** — note how far
these have drifted from the seed table in `backend/app/market/seed_prices.py`:

| Ticker | Open | High | Low | **Close** | VWAP | Seed in repo |
|---|---|---|---|---|---|---|
| AAPL | 326.87 | 328.40 | 323.53 | **324.96** | 325.28 | 190.00 |
| GOOGL | 334.06 | 340.00 | 332.82 | **337.12** | 337.16 | 175.00 |
| MSFT | 499.85 | 500.27 | 493.81 | **496.82** | 496.91 | 420.00 |
| AMZN | 254.25 | 256.24 | 253.40 | **254.98** | 255.03 | 185.00 |
| TSLA | 360.41 | 360.62 | 349.92 | **357.01** | 354.07 | 250.00 |
| NVDA | 218.78 | 227.95 | 218.48 | **224.41** | 224.38 | 800.00 |
| META | 578.78 | 600.38 | 577.00 | **592.85** | 593.32 | 500.00 |
| JPM | 357.55 | 361.47 | 353.79 | **356.22** | 356.71 | 195.00 |
| V | 374.01 | 380.17 | 374.01 | **378.40** | 378.40 | 280.00 |
| NFLX | 80.55 | 83.12 | 80.30 | **82.73** | 82.39 | 600.00 |

The bar timestamp is `2026-09-02T20:00:00Z` — the 16:00 ET close.

**Caveat:** the date must be a trading day. Passing a weekend or holiday returns
`"resultsCount": 0` rather than an error, so callers must walk backwards. `get_market_holidays()`
is available on the free tier and returns e.g. `MarketHoliday(date='2026-09-07',
exchange='NYSE', name='Labor Day', status='closed')` **[verified]**.

### 5.2 Previous Day Bar — one ticker, one call

```
GET /v2/aggs/ticker/{stocksTicker}/prev?adjusted=true
```

```python
result = client.get_previous_close_agg("AAPL", adjusted=True)
# returns a LIST of one PreviousCloseAgg, not a bare object — easy mistake
bar = result[0]
print(bar.ticker, bar.close, bar.open, bar.timestamp)
# AAPL 324.96 326.865 1788379200000
```

Automatically resolves "previous trading day", so no weekend arithmetic — that is its one
advantage over §5.1. But it costs **one call per ticker**, which on a 5/min budget prices
only five tickers a minute. Use §5.1 for anything more than a couple of symbols.

### 5.3 Daily Ticker Summary — open/close for a specific date

```
GET /v1/open-close/{stocksTicker}/{date}?adjusted=true
```

```python
day = client.get_daily_open_close_agg("AAPL", "2026-08-28", adjusted=True)
print(day.open, day.close, day.pre_market, day.after_hours, day.volume)
# 316.845 319.7 315.06 320.126 38649398.679189
```

The only endpoint exposing pre-market and after-hours prints on the free tier. Note the
model field is `from_` (trailing underscore) because `from` is a Python keyword.

### 5.4 Custom Bars — the history behind the charts

```
GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}
```

`timespan` ∈ `second, minute, hour, day, week, month, quarter, year`. `limit` defaults to
5000, max 50000. `sort` ∈ `asc, desc`.

```python
# 845 one-minute bars for a full session, 08:00–23:59 UTC  [verified]
bars = client.get_aggs("AAPL", 1, "minute", "2026-09-02", "2026-09-02", limit=50000)

# or stream with automatic pagination
for bar in client.list_aggs("AAPL", 1, "day", "2026-01-01", "2026-09-02"):
    ...
```

This directly answers PLAN.md §13.2 item 15 ("charts are empty on first paint"): a single
call backfills a real intraday series before the first SSE tick arrives.

**Free-tier limit:** `from`/`to` may not include the current day. `("2026-09-03", "2026-09-03")`
on 2026-09-03 returns the *timeframe* NOT_AUTHORIZED error **[verified]**.

### 5.5 Full Market Snapshot — real-time, **paid tiers only**

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT&include_otc=false
```

This is the endpoint PLAN.md §6 assumes. It is the right one *if the key is entitled*: one
call, every watched ticker, last trade plus today's OHLC plus previous close plus a
precomputed daily change percentage.

```python
from massive.rest.models import SnapshotMarketType

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,       # or just the string "stocks"
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for s in snapshots:
    print(s.ticker,
          s.last_trade.price,          # latest print
          s.last_trade.sip_timestamp,  # Unix NANOSECONDS
          s.day.open, s.day.close,     # today's session so far
          s.prev_day.close,            # yesterday's close
          s.todays_change_percent)     # server-computed daily %
```

`TickerSnapshot` fields, from the installed SDK **[verified]**:

```
ticker: str | None
day: Agg | None                 # today's session aggregate
prev_day: Agg | None            # previous session aggregate
min: MinuteSnapshot | None      # the current minute bar
last_trade: LastTrade | None
last_quote: LastQuote | None
todays_change: float | None
todays_change_percent: float | None
updated: int | None             # Unix NANOSECONDS
fair_market_value: float | None
```

`Agg` carries `open, high, low, close, volume, vwap, timestamp, transactions, otc`.
`LastTrade` carries `price, size, exchange, conditions, sip_timestamp,
participant_timestamp, trf_timestamp, id, tape`.

**`prev_day.close` and `day.open` are the missing baseline for PLAN.md §13.1 item 1** — the
"daily change %" that has nowhere to come from today. In snapshot mode they arrive free with
every poll.

### 5.6 Unified Snapshot — friendlier shape, same entitlement

```
GET /v3/snapshot?type=stocks&ticker.any_of=AAPL,MSFT&limit=250
```

```python
for snap in client.list_universal_snapshots(type="stocks", ticker_any_of=["AAPL", "NVDA"]):
    print(snap.ticker, snap.session.close, snap.session.change_percent, snap.market_status)
```

Two genuine advantages over §5.5 if you are already paying: `session.change_percent` and
`session.previous_close` are named rather than abbreviated, and **unknown tickers come back
as inline error rows instead of silently vanishing** —

```json
{"ticker": "TSLAAPL", "error": "NOT_FOUND", "message": "Ticker not found."}
```

That is the clean answer to PLAN.md §13.2 item 8 (what happens when a user adds a garbage
symbol) — but only on a paid key. Max 250 tickers per call.

### 5.7 Reference and status

```python
client.get_market_status()      # {'nasdaq': 'open', 'nyse': 'open', 'after_hours': False, ...}
client.get_market_holidays()    # upcoming closures, per exchange
client.get_ticker_details("AAPL")   # company name, address, branding icons, market cap
```

All three work on the free tier **[verified]**. `get_market_status()` is the correct way to
answer "is the market open?" for PLAN.md §13.2 item 14, rather than hard-coding 09:30–16:00 ET
and a holiday table.

`get_ticker_details()` is also a **symbol validator** — it 404s on nonsense, giving a
free-tier answer to item 8.

---

## 6. Wire Format Gotchas

**Single-letter JSON keys.** Aggregate endpoints return `{"T","o","h","l","c","v","vw","n","t"}`
= ticker, open, high, low, close, volume, VWAP, transaction count, timestamp. The SDK expands
these; raw `httpx` callers must map them by hand. This alone is a good reason to keep the SDK.

**Timestamps are inconsistent across endpoints.**

| Source | Unit |
|---|---|
| Aggregate `t` / `Agg.timestamp` | Unix **milliseconds** |
| `snapshot.updated` | Unix **nanoseconds** |
| `last_trade.sip_timestamp` | Unix **nanoseconds** |
| `DailyOpenCloseAgg.from_` | `YYYY-MM-DD` string |

The shipped `massive_client.py` divides `snap.last_trade.timestamp / 1000.0` to reach
seconds. That is **wrong by a factor of a million** for a nanosecond field — and the
attribute is `sip_timestamp`, not `timestamp`, so it would raise `AttributeError` first and
be swallowed by the existing `except (AttributeError, TypeError)`. Convert explicitly:

```python
def to_epoch_seconds(value: int, unit: str) -> float:
    return value / {"ms": 1e3, "us": 1e6, "ns": 1e9}[unit]
```

Per PLAN.md §13.1 item 6, convert once at the boundary and keep ISO 8601 UTC on the wire.

**`adjusted=true` is the default and should stay that way.** Unadjusted series show a
false −90% cliff on a 10:1 split. Compare NVDA at 224.41 today against the repo's 800.00
seed — that gap is mostly splits, not a crash.

**Tickers are case-sensitive.** Always `.upper().strip()` before sending.

---

## 7. Rate Limiting

Free tier is **5 requests/minute**, enforced server-side as HTTP 429.

Measured live with `retries=0`, calling `/v2/aggs/ticker/AAPL/prev` in a tight loop
**[verified]**:

```
#1 t= 0.32s OK
#2 t= 0.46s OK
#3 t= 0.58s OK
#4 t= 0.68s 429
#5 t= 0.79s 429   ... and every call after, until the window rolls
```

Three succeeded because earlier probes in the same minute had already consumed budget — the
window is a rolling 60 s across the whole key, not per endpoint.

### The gotcha that will cost someone an afternoon

With the SDK's default `retries=3`, an exhausted rate limit does **not** surface as
`massive.BadResponse`. urllib3's retry layer intercepts the 429s and raises:

```
urllib3.exceptions.MaxRetryError: ... (Caused by ResponseError('too many 429 error responses'))
```

`MaxRetryError` is not a subclass of anything in `massive.exceptions`. Any handler written
as `except BadResponse` will miss it entirely. Catch broadly and inspect:

```python
import urllib3.exceptions
from massive.exceptions import AuthError, BadResponse

try:
    bars = client.get_grouped_daily_aggs(date)
except AuthError:
    ...                                   # missing/empty key — fatal, fall back to simulator
except BadResponse as e:
    if "NOT_AUTHORIZED" in str(e):
        ...                               # entitlement — permanent, do not retry
    else:
        ...                               # transient
except urllib3.exceptions.MaxRetryError:
    ...                                   # rate limited or network — back off and retry
```

Also note the default `backoff_factor=0.1` gives retries at 0.0/0.2/0.4 s — all inside the
same 60 s window, so all three are guaranteed to fail. For a 5/min budget the retry is
useless; set `retries=0` and manage backoff yourself.

**No rate-limit headers are returned** — no `X-RateLimit-Remaining`, no `Retry-After`
**[verified]** on 200 responses. The client must track its own budget.

### Budget arithmetic for FinAlly

| Strategy | Calls/poll | Max poll rate on free tier |
|---|---|---|
| `get_previous_close_agg` per ticker, 10 tickers | 10 | impossible (2× over budget for one poll) |
| `get_grouped_daily_aggs`, any number of tickers | **1** | every 12 s, with headroom |
| `get_snapshot_all`, any number of tickers | 1 | n/a — not entitled |

The conclusion drives the whole design: **one call per cycle, never one call per ticker.**

---

## 8. Capability Probe

Because behaviour differs so sharply by plan, FinAlly should establish entitlement once at
startup rather than discovering it through a silent stream of swallowed exceptions.

```python
from dataclasses import dataclass

import urllib3.exceptions
from massive import RESTClient
from massive.exceptions import AuthError, BadResponse


@dataclass(frozen=True, slots=True)
class MassiveCapabilities:
    """What a given API key is actually allowed to do."""

    valid: bool           # key authenticates at all
    realtime: bool        # snapshot / last-trade endpoints entitled
    end_of_day: bool      # aggregate endpoints entitled
    detail: str


def probe_capabilities(api_key: str) -> MassiveCapabilities:
    """Two cheap calls, run once at startup. Costs 2 of the 5/min budget."""
    try:
        client = RESTClient(api_key=api_key, retries=0, read_timeout=5.0)
    except AuthError:
        return MassiveCapabilities(False, False, False, "no API key configured")

    # 1. Cheapest possible entitlement test for real-time.
    try:
        client.get_snapshot_all(market_type="stocks", tickers=["AAPL"])
        return MassiveCapabilities(True, True, True, "real-time snapshots entitled")
    except BadResponse as e:
        if "NOT_AUTHORIZED" not in str(e):
            return MassiveCapabilities(False, False, False, f"unexpected: {e}")
    except urllib3.exceptions.MaxRetryError as e:
        return MassiveCapabilities(False, False, False, f"unreachable/rate-limited: {e}")

    # 2. Snapshots refused — is this a valid key on a lower plan, or a bad key?
    try:
        client.get_previous_close_agg("AAPL")
        return MassiveCapabilities(True, False, True, "end-of-day only (Basic tier)")
    except Exception as e:
        return MassiveCapabilities(False, False, False, f"key rejected: {e}")
```

Feed `detail` into `GET /api/health` — PLAN.md §13.5 item 38 asks health to report the
active source, and "end-of-day only (Basic tier)" answers "why is nothing moving?" in one
glance.

---

## 9. What FinAlly Deliberately Does Not Use

**WebSockets.** `massive.WebSocketClient` exists and would be the natural fit for a
streaming terminal, but it is entitled on the same paid plans as the snapshot endpoints, so
it is unavailable to the students this project is built for. REST polling into the shared
`PriceCache` keeps one code path for both sources. PLAN.md §6 already made this call; the
entitlement data confirms it.

**Flat files (S3).** Bulk historical download. Irrelevant to a live terminal.

**Options, forex, crypto, futures, indices, financials, Benzinga news.** The SDK exposes all
of them (`massive/rest/{futures,economy,financials,benzinga,indicators,...}.py`). Out of scope.

---

## 10. Summary for the Implementer

1. Base URL `https://api.massive.com`; auth `Authorization: Bearer <key>`; SDK package `massive`.
2. The SDK is **synchronous** — always `asyncio.to_thread` it inside FastAPI.
3. **A free key cannot fetch a live price.** No snapshots, no last trade, nothing dated today.
   The currently shipped `MassiveDataSource` silently produces nothing on such a key.
4. `get_grouped_daily_aggs()` is the free tier's workhorse: 12,541 tickers, one call, ~1 s.
5. Rate limiting arrives as `urllib3.MaxRetryError`, not `BadResponse`. Set `retries=0`.
6. Timestamps are milliseconds on aggregates and **nanoseconds** on snapshots.
7. Probe entitlement once at startup and report it from `/api/health`.

How these facts shape the source-selection design is in
[`MARKET_INTERFACE.md`](MARKET_INTERFACE.md); the simulator that covers the free-tier and
no-key cases is in [`MARKET_SIMULATOR.md`](MARKET_SIMULATOR.md).

## Sources

- [Massive API docs](https://massive.com/docs) · [Stocks REST overview](https://massive.com/docs/rest/stocks/overview) · [Pricing](https://massive.com/pricing)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot.md) · [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot.md) · [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot.md)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar.md) · [Daily Ticker Summary](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary.md) · [Custom Bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars.md) · [Daily Market Summary](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary.md)
- `massive` Python SDK 2.2.0, read from `backend/.venv/lib/python3.12/site-packages/massive/`
