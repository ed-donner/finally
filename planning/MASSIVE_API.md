# Massive API Reference (formerly Polygon.io)

Reference for the Massive REST API as used by FinAlly's market data layer.

> **Verification note.** Every field name, method signature, and default in this
> document was verified against the installed `massive` Python SDK by
> introspecting the dataclasses and `RESTClient.__init__`, and cross-checked
> against the published REST schemas at <https://massive.com/docs>. Where the
> raw JSON field names differ from the SDK attribute names, both are given —
> this is the single most common source of bugs when working with this API.

---

## 1. Background

Polygon.io rebranded to **Massive** on 30 October 2025.

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` |
| Legacy base URL | `https://api.polygon.io` (still supported for an extended period) |
| Python package | `massive` (renamed from `polygon-api-client`) |
| Install | `uv add massive` / `pip install -U massive` |
| Minimum Python | 3.9+ |
| Auth | `Authorization: Bearer <API_KEY>` — handled by the SDK |
| API key env var | `MASSIVE_API_KEY` |

Existing Polygon.io API keys continue to work unchanged.

---

## 2. Rate Limits & Plan Tiers

| Tier | Price | Rate limit | Data recency |
|---|---|---|---|
| Basic (free) | $0 | **5 requests/minute** | End-of-day / delayed |
| Starter | ~$29/mo | Unlimited | 15-minute delayed |
| Developer | ~$79/mo | Unlimited | Real-time |
| Advanced | ~$199/mo | Unlimited + WebSocket | Real-time |

"Unlimited" is not literally unbounded — Massive monitors usage and recommends
staying **under 100 requests/second**. Exceeding the free tier returns
**HTTP 429**.

### What this means for FinAlly

The free tier's 5 req/min is the binding constraint on our polling design. This
is why we use the **full market snapshot** endpoint: it returns *every requested
ticker in a single HTTP call*, so watchlist size does not affect our request
count.

| Tier | Recommended poll interval | Requests/min |
|---|---|---|
| Free | 15s | 4 |
| Starter / Developer | 5s | 12 |
| Advanced | 2s | 30 |

A 15-second poll on the free tier leaves one spare request per minute of
headroom for retries.

> **Important caveat:** the single-ticker and full-market snapshot endpoints are
> **not included in the free Basic plan** (they require Starter or above). On a
> free key, `get_snapshot_all` will return HTTP 403. FinAlly treats any Massive
> failure as non-fatal and keeps serving the last cached prices, but a free-tier
> key will effectively yield no data. Users without a paid plan should leave
> `MASSIVE_API_KEY` unset and use the simulator.

---

## 3. Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from the environment
client = RESTClient()

# Or pass the key explicitly (what FinAlly does)
client = RESTClient(api_key="your_key_here")
```

Verified constructor defaults:

```python
RESTClient(
    api_key: str | None = None,        # falls back to MASSIVE_API_KEY env var
    connect_timeout: float = 10.0,
    read_timeout: float = 10.0,
    num_pools: int = 10,
    retries: int = 3,                  # automatic retry on 5xx
    base: str = "https://api.massive.com",
    pagination: bool = True,
    verbose: bool = False,
    trace: bool = False,               # trace=True prints request/response
)
```

**The client is synchronous.** It uses `urllib3` under the hood and will block
the event loop if called directly from async code. Always wrap calls in
`asyncio.to_thread(...)` — see §7.

---

## 4. Primary Endpoint — Full Market Snapshot

This is the workhorse endpoint for FinAlly: current prices for many tickers in
one call.

**REST:** `GET /v2/snapshot/locale/us/markets/stocks/tickers`

| Query param | Type | Notes |
|---|---|---|
| `tickers` | comma-separated list | Optional. Omit to get the entire market. |
| `include_otc` | boolean | Optional, defaults to `false`. |

**Python:**

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=api_key)

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change:  {snap.todays_change_percent:.2f}%")
    print(f"  Prev close:  ${snap.prev_day.close}")
    print(f"  Day OHLC:    O={snap.day.open} H={snap.day.high} "
          f"L={snap.day.low} C={snap.day.close}")
```

Verified signature:

```python
get_snapshot_all(
    market_type: str | SnapshotMarketType,
    tickers: str | list[str] | None = None,
    params: dict | None = None,
    raw: bool = False,
    include_otc: bool | None = False,
    options: RequestOptionBuilder | None = None,
) -> list[TickerSnapshot] | HTTPResponse
```

`SnapshotMarketType` members: `STOCKS`, `FOREX`, `CRYPTO`, `INDICES`.

### 4.1 Raw JSON vs. SDK attribute names

The API returns terse single-letter JSON keys; the SDK maps them to readable
attribute names. **Use the right-hand column in Python code.**

Top level (`TickerSnapshot`):

| Raw JSON | SDK attribute | Meaning |
|---|---|---|
| `ticker` | `.ticker` | Symbol |
| `todaysChange` | `.todays_change` | Absolute change vs. previous close |
| `todaysChangePerc` | `.todays_change_percent` | Percent change vs. previous close |
| `updated` | `.updated` | Last update, Unix **nanoseconds** |
| `day` | `.day` | Today's aggregate bar (`Agg`) |
| `prevDay` | `.prev_day` | Previous day's bar (`Agg`) |
| `min` | `.min` | Most recent minute bar (`MinuteSnapshot`) |
| `lastTrade` | `.last_trade` | Most recent trade (`LastTrade`) |
| `lastQuote` | `.last_quote` | Most recent NBBO quote (`LastQuote`) |
| `fmv` | `.fair_market_value` | Fair market value (Business plans) |

`Agg` (used by both `.day` and `.prev_day`):

| Raw | SDK attribute |
|---|---|
| `o` | `.open` |
| `h` | `.high` |
| `l` | `.low` |
| `c` | `.close` |
| `v` | `.volume` |
| `vw` | `.vwap` |
| `t` | `.timestamp` |
| `n` | `.transactions` |

`LastTrade` (snapshot variant):

| Raw | SDK attribute |
|---|---|
| `p` | `.price` |
| `s` | `.size` |
| `x` | `.exchange` |
| `t` | **`.sip_timestamp`** |
| `i` | `.id` |
| `c` | `.conditions` |

`LastQuote`:

| Raw | SDK attribute |
|---|---|
| `p` | `.bid_price` |
| `s` | `.bid_size` |
| `P` | `.ask_price` |
| `S` | `.ask_size` |
| `t` | `.sip_timestamp` |

> ### ⚠️ Two field-name traps
>
> **1. `last_trade` has no `.timestamp` attribute.** The raw key `t` maps to
> **`.sip_timestamp`**. Writing `snap.last_trade.timestamp` raises
> `AttributeError`. Full verified field list for the snapshot `LastTrade`:
> `ticker`, `trf_timestamp`, `sequence_number`, `sip_timestamp`,
> `participant_timestamp`, `conditions`, `correction`, `id`, `price`, `trf_id`,
> `size`, `exchange`, `tape`.
>
> **2. There is no `day.previous_close` or `day.change_percent`.** Previous
> close lives at **`snap.prev_day.close`**; percent change at
> **`snap.todays_change_percent`**.

### 4.2 Timestamp units

Massive mixes units across fields — this is a frequent source of "prices from
1970" bugs.

| Field | Unit | Convert to Unix seconds |
|---|---|---|
| `last_trade.sip_timestamp` | **nanoseconds** | `/ 1_000_000_000` |
| `last_quote.sip_timestamp` | **nanoseconds** | `/ 1_000_000_000` |
| `updated` | **nanoseconds** | `/ 1_000_000_000` |
| `day.timestamp`, `prev_day.timestamp` | **milliseconds** | `/ 1_000` |
| Aggregate bar `t` (`list_aggs`, `prev`) | **milliseconds** | `/ 1_000` |

Rather than hardcode a divisor, prefer a defensive normalizer (see §7).

---

## 5. Other Relevant Endpoints

### 5.1 Single Ticker Snapshot

`GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`

```python
snap = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(snap.last_trade.price, snap.todays_change_percent)
```

Returns the same `TickerSnapshot` shape as §4. FinAlly does not use this in the
poll loop — batching via `get_snapshot_all` is strictly better for rate limits.

### 5.2 Previous Day Bar

`GET /v2/aggs/ticker/{stocksTicker}/prev`

Useful for establishing seed/reference prices without a snapshot subscription.

```python
for agg in client.get_previous_close_agg(ticker="AAPL"):
    print(f"Prev close: ${agg.close}  O={agg.open} H={agg.high} L={agg.low}")
```

Query param: `adjusted` (boolean, default `true` — split-adjusted).

### 5.3 Custom Bars (Aggregates)

`GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

Historical OHLCV. Not used for live polling, but the natural source if FinAlly
later adds real historical charts (currently sparklines accumulate client-side
from the SSE stream).

```python
bars = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",       # minute | hour | day | week | month | quarter | year
    from_="2026-01-01",
    to="2026-01-31",
    limit=50000,
))
for b in bars:
    print(b.timestamp, b.open, b.high, b.low, b.close, b.volume)
```

Pagination is on by default; `limit` controls **page size**, not total results.
Pass `pagination=False` to `RESTClient(...)` for a fixed result count.

### 5.4 Unified Snapshot (v3)

`GET /v3/snapshot` — cross-asset-class snapshot with a cleaner schema
(`last_trade.price`, `last_quote.bid`/`.ask`, `session`, `market_status`).

```python
snaps = list(client.list_universal_snapshots(
    market_type="stocks",
    ticker_any_of=["AAPL", "GOOGL", "MSFT"],
))
```

Constraint: `ticker.any_of` accepts **at most 250 tickers**; `limit` defaults to
10 and maxes at 250 — remember to raise it or you will silently get 10 results.

FinAlly uses v2 `get_snapshot_all` because it is the better-documented, more
widely available endpoint for a stocks-only use case, and it has no per-request
ticker cap for our scale.

### 5.5 Last Trade / Last Quote

```python
trade = client.get_last_trade(ticker="AAPL")
print(trade.price, trade.size)

quote = client.get_last_quote(ticker="AAPL")
print(quote.bid_price, quote.ask_price)
```

One HTTP call per ticker — avoid in the poll loop.

---

## 6. Error Handling

| Status | Meaning | FinAlly response |
|---|---|---|
| 401 | Invalid / missing API key | Log error, keep serving cached prices |
| 403 | Plan does not include endpoint | Log error, keep serving cached prices |
| 429 | Rate limit exceeded (free tier) | Back off, retry next interval |
| 5xx | Server error | SDK auto-retries 3× before raising |

The SDK raises `urllib3`/`requests`-style exceptions and a `BadResponse` on
non-2xx. Because a market data outage must never take down the app, the poller
catches **all** exceptions, logs, and continues — the cache simply holds stale
prices until the next successful poll.

Individual snapshots may also be partially populated (e.g. `last_trade` is
`None` outside market hours on some plans), so per-ticker parsing is wrapped in
its own try/except and skips rather than aborting the batch.

---

## 7. Reference Implementation — Poll Loop

This is the pattern FinAlly's `MassiveDataSource` follows. Note the two details
that matter: threading the blocking client off the event loop, and defensive
timestamp normalization.

```python
import asyncio
import logging
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

logger = logging.getLogger(__name__)

# Anything larger than this is not plausibly Unix seconds, so scale it down.
_YEAR_2100_SECONDS = 4_102_444_800


def normalize_timestamp(raw: float | None) -> float | None:
    """Coerce a Massive timestamp (s / ms / us / ns) to Unix seconds.

    Massive returns nanoseconds for trade/quote SIP timestamps but
    milliseconds for aggregate bars. Rather than depend on the field, scale
    by orders of magnitude until the value is a plausible epoch-seconds value.
    """
    if raw is None:
        return None
    ts = float(raw)
    while ts > _YEAR_2100_SECONDS:
        ts /= 1000.0
    return ts


def extract_price(snap) -> float | None:
    """Best available current price, in priority order."""
    if snap.last_trade and snap.last_trade.price:
        return snap.last_trade.price
    if snap.min and snap.min.close:          # most recent minute bar
        return snap.min.close
    if snap.day and snap.day.close:          # today's close so far
        return snap.day.close
    if snap.prev_day and snap.prev_day.close:  # market closed / pre-open
        return snap.prev_day.close
    return None


async def poll_massive(api_key, get_tickers, price_cache, interval=15.0):
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            try:
                # RESTClient is synchronous — never call it on the event loop.
                snapshots = await asyncio.to_thread(
                    client.get_snapshot_all,
                    market_type=SnapshotMarketType.STOCKS,
                    tickers=tickers,
                )
                for snap in snapshots:
                    try:
                        price = extract_price(snap)
                        if price is None:
                            continue
                        ts = normalize_timestamp(
                            snap.last_trade.sip_timestamp if snap.last_trade
                            else snap.updated
                        )
                        price_cache.update(
                            ticker=snap.ticker, price=price, timestamp=ts
                        )
                    except (AttributeError, TypeError) as e:
                        logger.warning("Skipping %s: %s",
                                       getattr(snap, "ticker", "???"), e)
            except Exception as e:
                # Never let a data outage kill the loop.
                logger.error("Massive poll failed: %s", e)

        await asyncio.sleep(interval)
```

### Why the price fallback chain matters

`last_trade` is only populated when the plan includes trade data and there has
been a trade. Outside US market hours, or on a delayed plan, relying solely on
`last_trade.price` yields no data at all. The chain
`last_trade → min → day → prev_day` guarantees a usable price whenever the API
returns anything at all — important because FinAlly's portfolio valuation and
trade execution both read from the cache.

---

## 8. Market Hours Behaviour

- The `day` aggregate resets at market open; during pre-market its values may
  still reflect the previous session.
- `last_trade.price` includes extended-hours trades on plans that carry them.
- On weekends and holidays, snapshots return the last session's values and
  `todays_change` is typically `0`.
- FinAlly's UI makes no distinction — the price cache is the only contract, and
  a stale-but-valid price renders identically to a live one.

---

## 9. Applicability to FinAlly

| Requirement | Endpoint | Notes |
|---|---|---|
| Live prices for watchlist | `get_snapshot_all` | One call for all tickers |
| Day change % for watchlist | `.todays_change_percent` | Already computed by API |
| Reference / previous close | `.prev_day.close` | No extra call needed |
| Seed prices (simulator parity) | `get_previous_close_agg` | Optional bootstrap |
| Historical charts (future) | `list_aggs` | Not in current scope |

Everything FinAlly needs in the live path comes from **one endpoint, one call
per poll cycle**. See `MARKET_INTERFACE.md` for how this is wrapped behind the
provider-agnostic interface.

---

## Sources

- [Massive API docs](https://massive.com/docs)
- [Stocks REST API overview](https://massive.com/docs/rest/stocks/overview)
- [Full market snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot.md)
- [Single ticker snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot.md)
- [Unified snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot.md)
- [Previous day bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar.md)
- [massive-com/client-python](https://github.com/massive-com/client-python)
- [Polygon.io is now Massive](https://massive.com/blog/polygon-is-now-massive)
- [REST request limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
- [Pricing](https://massive.com/pricing)
