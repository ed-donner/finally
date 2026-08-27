# Massive API Reference (formerly Polygon.io)

Research notes and code examples for retrieving real-time and end-of-day
prices for multiple tickers. Scoped to what FinAlly needs: batch price
lookups for a watchlist, plus historical bars for charts.

Verified against the live API and `https://massive.com/docs/llms.txt` on
2026-08-27.

---

## 1. The Rebrand

Polygon.io became **Massive** on 2025-10-30.

| Was | Now |
|---|---|
| `polygon.io` | `massive.com` |
| `api.polygon.io` | `api.massive.com` |
| `POLYGON_API_KEY` | `MASSIVE_API_KEY` |
| `pip install polygon-api-client` | `pip install massive` |

Existing keys, accounts and endpoint paths are unchanged. `api.polygon.io`
still resolves "for an extended period" but new code should target
`api.massive.com`. Endpoint paths (`/v2/aggs/...`, `/v2/snapshot/...`) were
not renamed, so most Polygon.io tutorials and StackOverflow answers still
apply verbatim.

The authoritative machine-readable index is
`https://massive.com/docs/llms.txt`. Every documentation page has a `.md`
twin, e.g. `https://massive.com/docs/rest/stocks/aggregates/custom-bars.md`.

---

## 2. Authentication

Base URL: `https://api.massive.com`

Two accepted methods. Both were confirmed working against the live API.

**Bearer header (preferred).** Keeps the key out of URLs, logs and proxy
access records.

```
Authorization: Bearer <MASSIVE_API_KEY>
```

**Query parameter (fallback).** Useful for `curl` spelunking and for the
handful of clients that cannot set headers.

```
GET /v2/aggs/ticker/AAPL/prev?apiKey=<MASSIVE_API_KEY>
```

Get a key at `https://massive.com/dashboard/keys` (signup:
`https://massive.com/dashboard/signup`). Read it from the `MASSIVE_API_KEY`
environment variable; never hardcode it. The official Python client reads
that variable automatically.

---

## 3. Plans, Rate Limits and Recency

This table drives every design decision downstream. Read it before choosing
an endpoint.

| Plan | Price | Rate limit | Recency | History |
|---|---|---|---|---|
| Stocks Basic | Free | **5 calls/min** | **End-of-day only** | 2 years |
| Stocks Starter | $29/mo | Unlimited | 15-minute delayed | 5 years |
| Stocks Developer | $79/mo | Unlimited | 15-minute delayed | 10 years |
| Stocks Advanced | $199/mo | Unlimited | Real-time | 20+ years |

Two consequences that are easy to miss:

1. **The snapshot endpoints are not on the free plan.** Both Full Market
   Snapshot and Unified Snapshot are marked "Not included" for Stocks
   Basic. A free key gets a 403 on them.
2. **A free key returns end-of-day data.** Prices from a Basic key do not
   change during the trading day. Any UI built on a free key will render
   correct but motionless prices.

5 calls/min is one call every 12 seconds, so a 15-second poll interval
(4 calls/min) leaves headroom for the occasional retry.

---

## 4. Endpoint Reference

### 4.1 Full Market Snapshot — batch real-time prices

The natural fit for a watchlist: one call, many tickers, current price plus
the previous close needed for a daily-change figure.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

| Parameter | Type | Required | Notes |
|---|---|---|---|
| `tickers` | string | No | Case-sensitive, comma separated, no spaces. Omit to get all 10,000+ tickers. |
| `include_otc` | boolean | No | Default `false`. |

Plan access: Starter and above. Recency: 15-minute delayed on
Starter/Developer, real-time on Advanced/Business.

Response (trimmed to the fields FinAlly uses):

```json
{
  "count": 1,
  "status": "OK",
  "tickers": [
    {
      "ticker": "BCAT",
      "todaysChange": -0.124,
      "todaysChangePerc": -0.601,
      "updated": 1605192894630916600,
      "day":     { "o": 20.64, "h": 20.64, "l": 20.506, "c": 20.506, "v": 37216, "vw": 20.616 },
      "prevDay": { "o": 20.79, "h": 21.0,  "l": 20.5,   "c": 20.63,  "v": 292738, "vw": 20.6939 },
      "min":     { "o": 20.506, "h": 20.506, "l": 20.506, "c": 20.506, "v": 5000, "n": 1, "t": 1684428600000 },
      "lastTrade": { "p": 20.506, "s": 2416, "t": 1605192894630916600, "x": 4, "i": "71675577320245" },
      "lastQuote": { "p": 20.5, "s": 13, "P": 20.6, "S": 22, "t": 1605192959994246100 }
    }
  ]
}
```

Picking the current price, most robust first:

1. `lastTrade.p` — the actual last print. Absent unless the plan includes
   trades.
2. `min.c` — close of the most recent minute bar. Present on all snapshot
   plans.
3. `day.c` — close of the running daily bar. Always present, but it is
   `0` before the first trade of the session.

Previous close is `prevDay.c`. `todaysChange` and `todaysChangePerc` are
precomputed by the server; prefer them over recomputing.

Snapshot data is **cleared daily at 3:30 AM EST** and repopulates as
exchanges report, starting as early as 4:00 AM EST. Between those times
`day` and `lastTrade` may be empty or zero.

### 4.2 Unified Snapshot — batch across asset classes

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=250
```

| Parameter | Type | Notes |
|---|---|---|
| `ticker.any_of` | string | Comma separated, **max 250 tickers**. |
| `type` | string | `stocks`, `options`, `fx`, `crypto`, `indices`. |
| `limit` | integer | Default 10, **max 250**. Set it explicitly. |
| `order`, `sort` | string | Ordering controls. |

Plan access: Starter and above. Same recency tiers as 4.1.

The response uses readable field names rather than OHLC shorthand, and
reports errors per ticker instead of failing the whole request:

```json
{
  "status": "OK",
  "results": [
    {
      "ticker": "AAPL",
      "type": "stocks",
      "name": "Apple Inc.",
      "market_status": "closed",
      "session": {
        "open": 22.49, "high": 22.49, "low": 21.35, "close": 21.4,
        "previous_close": 22.45, "change": -1.05, "change_percent": -4.67,
        "volume": 37,
        "early_trading_change": -0.39, "late_trading_change": 1.2
      },
      "last_trade":  { "price": 0.05, "size": 2, "last_updated": 1675280958783136800 },
      "last_minute": { "open": 412.1, "close": 412.05, "volume": 610 }
    },
    { "ticker": "TSLAAPL", "error": "NOT_FOUND", "message": "Ticker not found." }
  ]
}
```

Two reasons to prefer this over 4.1 despite the newer path: `market_status`
comes back inline (no second call), and a bad ticker yields an `error` entry
rather than silently vanishing from `tickers[]`. The cost is the 250-ticker
cap and mandatory `limit` handling.

### 4.3 Daily Market Summary (grouped daily bars) — the free-tier workhorse

```
GET /v2/aggs/grouped/locale/us/market/stocks/2026-08-26?adjusted=true
```

| Parameter | Required | Notes |
|---|---|---|
| `date` (path) | Yes | `YYYY-MM-DD`. Must be a trading day. |
| `adjusted` | No | Default `true` (split-adjusted). |
| `include_otc` | No | Default `false`. |

Plan access: **all plans, including Basic.** Recency: end-of-day on Basic.

Every US ticker for that date in a single response. That is the important
property: one API call covers an entire watchlist regardless of its size,
which fits inside the free tier's 5 calls/min with room to spare.

```json
{
  "adjusted": true,
  "queryCount": 3,
  "resultsCount": 3,
  "status": "OK",
  "results": [
    { "T": "VSAT", "o": 34.9, "h": 35.47, "l": 34.21, "c": 34.24,
      "v": 312583, "vw": 34.4736, "n": 4966, "t": 1602705600000 }
  ]
}
```

`T` is the ticker. Filter the array down to the watchlist client-side.

Caveats: a non-trading date returns `resultsCount: 0` with `status: "OK"`,
not an error, so walk backwards through calendar days (or consult
`/v1/marketstatus/now`) to find the last session. Basic plans also see a
delay of roughly one day before a session's grouped bar is published.

### 4.4 Previous Day Bar

```
GET /v2/aggs/ticker/AAPL/prev?adjusted=true
```

Plan access: all plans. One ticker per call, so a 10-ticker watchlist costs
10 calls — twice the free tier's per-minute budget. Use 4.3 for batches;
this endpoint is for one-off lookups such as validating a ticker the user
just added.

```json
{
  "ticker": "AAPL", "adjusted": true, "queryCount": 1, "resultsCount": 1,
  "status": "OK", "request_id": "6a7e466379af0a71039d60cc78e72282",
  "results": [
    { "T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97,
      "v": 131704427, "vw": 116.3058, "t": 1605042000000 }
  ]
}
```

### 4.5 Daily Ticker Summary (open/close)

```
GET /v1/open-close/AAPL/2026-08-26?adjusted=true
```

Plan access: all plans. Returns spelled-out fields plus pre-market and
after-hours prices, which the aggregate endpoints do not expose.

```json
{
  "status": "OK", "symbol": "AAPL", "from": "2023-01-09",
  "open": 324.66, "high": 326.2, "low": 322.3, "close": 325.12,
  "preMarket": 324.5, "afterHours": 322.1, "volume": 26122646
}
```

### 4.6 Custom Bars — history for charts

```
GET /v2/aggs/ticker/AAPL/range/1/day/2026-06-01/2026-08-27?adjusted=true&sort=asc&limit=5000
```

| Parameter | Notes |
|---|---|
| `multiplier` (path) | Size of the timespan multiplier, e.g. `5`. |
| `timespan` (path) | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year`. |
| `from`, `to` (path) | `YYYY-MM-DD` or millisecond epoch. |
| `sort` | `asc` (oldest first) or `desc`. |
| `limit` | Default 5000, **max 50000**. |

Plan access: all plans, subject to per-plan history depth (Basic: 2 years).
Windows with no qualifying trades produce no bar at all rather than a
zero-volume bar, so never assume evenly spaced timestamps. Responses may
paginate via `next_url`.

This is the endpoint that seeds a chart with real history before live ticks
start arriving. Note that FinAlly's spec builds sparklines purely from the
SSE stream since page load, so this is optional for the core build.

### 4.7 Market Status

```
GET /v1/marketstatus/now
```

Plan access: all plans, real-time on every tier.

```json
{
  "market": "extended-hours",
  "earlyHours": false,
  "afterHours": true,
  "exchanges": { "nasdaq": "extended-hours", "nyse": "extended-hours", "otc": "closed" },
  "currencies": { "crypto": "open", "fx": "open" },
  "serverTime": "2020-11-10T17:37:37-05:00"
}
```

`market` is one of `open`, `closed`, `extended-hours`. Cheap enough to poll
once a minute; useful for backing off the price poller when the market is
closed and for driving a "market closed" badge in the header.

---

## 5. Choosing Endpoints for FinAlly

| Key's plan | Batch price endpoint | Calls per poll | Poll interval | What the user sees |
|---|---|---|---|---|
| Basic (free) | `/v2/aggs/grouped/.../{date}` | 1 | 15s | Correct but static EOD prices |
| Starter / Developer | `/v2/snapshot/.../tickers?tickers=` | 1 | 2-5s | Prices moving, 15 min behind |
| Advanced / Business | `/v2/snapshot/.../tickers?tickers=` | 1 | 1-2s | Live prices |

Every row is a single call per poll, so the poller's shape does not change
with watchlist size.

The recommendation for FinAlly is to implement the snapshot path and fall
back to grouped daily bars on a 403 or when the operator declares a free
key. See `MARKET_INTERFACE.md` for how this is wrapped.

Worth stating plainly: a free Massive key produces a trading terminal whose
prices never move. The built-in simulator is the better default for demos
and for the course, which is why `PLAN.md` makes it the default. Real data
becomes worthwhile at the Starter tier and above.

---

## 6. Code Examples

### 6.1 Async batch fetch with httpx (recommended for FinAlly)

FastAPI runs on asyncio and the official client is synchronous urllib3, so a
direct `httpx.AsyncClient` avoids blocking the event loop for the two
endpoints this project actually needs.

```python
"""Batch price fetches against the Massive REST API."""

import os
from datetime import date, timedelta

import httpx

BASE_URL = "https://api.massive.com"


def _client() -> httpx.AsyncClient:
    """Build a client with the API key bound as a Bearer token."""
    api_key = os.environ["MASSIVE_API_KEY"]
    return httpx.AsyncClient(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=httpx.Timeout(10.0, connect=5.0),
    )


async def fetch_snapshots(client: httpx.AsyncClient, tickers: list[str]) -> dict[str, dict]:
    """Return {ticker: snapshot} for the given tickers. Starter plan and above."""
    response = await client.get(
        "/v2/snapshot/locale/us/markets/stocks/tickers",
        params={"tickers": ",".join(tickers)},
    )
    response.raise_for_status()
    return {row["ticker"]: row for row in response.json().get("tickers", [])}


async def fetch_grouped_daily(
    client: httpx.AsyncClient, tickers: list[str], on: date
) -> dict[str, dict]:
    """Return {ticker: daily bar} from the grouped endpoint. Works on the free plan.

    Walks back up to five calendar days to skip weekends and holidays.
    """
    wanted = set(tickers)
    for offset in range(5):
        day = on - timedelta(days=offset)
        response = await client.get(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            params={"adjusted": "true"},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        if results:
            return {row["T"]: row for row in results if row["T"] in wanted}
    return {}
```

Reading a current price out of a snapshot, in preference order:

```python
def snapshot_price(snapshot: dict) -> float | None:
    """Best available current price from a v2 snapshot row."""
    for value in (
        snapshot.get("lastTrade", {}).get("p"),
        snapshot.get("min", {}).get("c"),
        snapshot.get("day", {}).get("c"),
    ):
        if value:
            return float(value)
    return None
```

### 6.2 Official Python client

```
uv add massive
```

`RESTClient` reads `MASSIVE_API_KEY` from the environment when no key is
passed. It is synchronous; in an async service, call it through
`asyncio.to_thread`.

```python
from massive import RESTClient

client = RESTClient()  # reads MASSIVE_API_KEY

# Batch snapshot (Starter and above)
for snapshot in client.get_snapshot_all("stocks", ["AAPL", "GOOGL", "MSFT"]):
    print(snapshot.ticker, snapshot.last_trade.price, snapshot.prev_day.close)

# Grouped daily bars, entire market in one call (all plans)
for bar in client.get_grouped_daily_aggs("2026-08-26", adjusted=True):
    print(bar.ticker, bar.close)

# Previous close for one ticker (all plans)
prev = client.get_previous_close_agg("AAPL")
print(prev)

# Historical bars for a chart
bars = list(
    client.list_aggs("AAPL", 1, "day", "2026-06-01", "2026-08-27", limit=5000)
)
```

Relevant method names, since they still use Polygon-era vocabulary:

| Method | Endpoint |
|---|---|
| `get_snapshot_all(market_type, tickers)` | `/v2/snapshot/.../tickers` |
| `get_snapshot_ticker(market_type, ticker)` | `/v2/snapshot/.../tickers/{ticker}` |
| `list_universal_snapshots(ticker_any_of=[...])` | `/v3/snapshot` |
| `get_grouped_daily_aggs(date)` | `/v2/aggs/grouped/...` |
| `get_previous_close_agg(ticker)` | `/v2/aggs/ticker/{t}/prev` |
| `get_daily_open_close_agg(ticker, date)` | `/v1/open-close/{t}/{date}` |
| `list_aggs(ticker, multiplier, timespan, from_, to)` | `/v2/aggs/ticker/{t}/range/...` |

Pagination is automatic on `list_*` methods; `limit` is the page size, not a
total. Pass `pagination=False` to `RESTClient` to make `limit` a hard cap.

Note the package situation on PyPI: `massive` (v2.8.0) is the current
official client, `polygon-api-client` (v1.16.3) is the frozen predecessor,
and `massive-api-client` is an unrelated third-party async wrapper. Install
`massive`.

### 6.3 curl

```bash
# Batch snapshot
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT"

# Grouped daily bars, free plan
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-08-26?adjusted=true"

# Market status
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v1/marketstatus/now"
```

---

## 7. Errors and Retries

Every response carries `status` and `request_id`. Quote `request_id` when
contacting support.

| Status | Body | Meaning | Response |
|---|---|---|---|
| 401 | `{"status":"ERROR","error":"API Key was not provided"}` | No key sent | Fail fast at startup |
| 401 | `{"status":"ERROR","error":"Unknown API Key"}` | Bad key | Fail fast at startup |
| 403 | `NOT_AUTHORIZED` | Plan lacks this endpoint | Fall back to a free-tier endpoint |
| 429 | Rate limit exceeded | Over the plan's calls/min | Back off, then retry |
| 404 | | Unknown ticker or path | Surface to the user |
| 5xx | | Upstream trouble | Retry with backoff |

The 401 bodies above are verbatim from the live API. A 200 with
`resultsCount: 0` is normal for a non-trading date and is not an error.

The official client retries 413, 429, 499, 500, 502, 503 and 504 with
exponential backoff (factor 0.1). Mirror that policy when using httpx:

```python
import asyncio

import httpx

RETRY_STATUS = {413, 429, 499, 500, 502, 503, 504}


async def get_with_retry(
    client: httpx.AsyncClient, url: str, params: dict, attempts: int = 4
) -> httpx.Response:
    """GET with exponential backoff on transient statuses."""
    for attempt in range(attempts):
        response = await client.get(url, params=params)
        if response.status_code not in RETRY_STATUS:
            response.raise_for_status()
            return response
        await asyncio.sleep(0.2 * 2**attempt)
    response.raise_for_status()
    return response
```

Never retry a 401 or 403; the answer will not change.

---

## 8. Field Shorthand and Gotchas

The aggregate and v2 snapshot endpoints use single-letter keys:

| Key | Meaning |
|---|---|
| `T` | Ticker |
| `o` `h` `l` `c` | Open, high, low, close |
| `v` | Volume |
| `vw` | Volume-weighted average price |
| `n` | Number of transactions |
| `t` | Timestamp |
| `av` | Accumulated volume for the day (minute bars) |
| `p` / `s` | Trade or bid price / size |
| `P` / `S` | Ask price / size (uppercase is the ask side) |
| `x` | Exchange id |

Things that bite:

- **Timestamp units differ.** Aggregate `t` is **milliseconds**; snapshot
  `updated`, `lastTrade.t` and `lastQuote.t` are **nanoseconds**. Divide by
  1e9 before `datetime.fromtimestamp`.
- **Tickers are case-sensitive.** Uppercase everything on input.
- **A missing ticker vanishes silently** from `/v2/snapshot`'s `tickers[]`
  array. Only `/v3/snapshot` reports it as an explicit `error` entry.
- **`day.c` is 0 pre-market** before the first print. Fall back to
  `prevDay.c` so the UI never renders a $0.00 price.
- **`adjusted` defaults to true.** Historical bars are split-adjusted, so a
  cached price from before a split will not match a fresh fetch.
- **Snapshots reset at 3:30 AM EST** and repopulate from about 4:00 AM EST.
- **Non-trading dates return 200 with an empty `results`.** Handle the
  empty case; do not treat it as a failure.

---

## Sources

- [Massive API docs index (llms.txt)](https://massive.com/docs/llms.txt)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Daily Market Summary (grouped daily bars)](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Daily Ticker Summary](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary)
- [Custom Bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Market Status](https://massive.com/docs/rest/stocks/market-operations/market-status)
- [Pricing and rate limits](https://massive.com/pricing)
- [Official Python client](https://github.com/massive-com/client-python)
- [massive on PyPI](https://pypi.org/project/massive/)
