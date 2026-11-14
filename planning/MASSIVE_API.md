# Massive API (formerly Polygon.io) — Stock Price Reference

Reference for retrieving **real-time / delayed** and **end-of-day** US stock prices for multiple tickers from Massive, for use by FinAlly's `MassiveDataSource`.

Researched 2026-11-11 against the official docs at `massive.com/docs` and the `massive` Python SDK **v2.9.2** (source inspected directly). Where the official docs and the SDK disagree, the SDK behaviour is noted because that is what our code runs.

---

## 1. Basics

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` (old `api.polygon.io` host is the legacy name) |
| Auth (header, preferred) | `Authorization: Bearer <MASSIVE_API_KEY>` — this is what the Python SDK sends |
| Auth (query param) | `?apiKey=<MASSIVE_API_KEY>` |
| Key management | https://massive.com/dashboard/keys |
| Python SDK | `pip install massive` / `uv add massive` → `from massive import RESTClient` |
| Ticker format | Uppercase, **case-sensitive** (`AAPL`, not `aapl`). Share classes use a dot: `BRK.B` |
| Timestamps | **Mixed units** — see §6. Trade/quote/`updated` timestamps are **nanoseconds**; aggregate bar `t` is **milliseconds** |

### Plans, data recency and rate limits

Massive sells per-asset-class plans. For stocks:

| Plan | Price (indicative) | Recency | REST rate limit | Snapshot endpoints | Last Trade endpoint |
|---|---|---|---|---|---|
| **Stocks Basic** (free) | $0 | **End-of-day** | **5 requests / minute** | ❌ not included | ❌ |
| Stocks Starter | ~$29/mo | 15-min delayed | Unlimited* | ✅ | ❌ |
| Stocks Developer | ~$79/mo | 15-min delayed | Unlimited* | ✅ | ✅ |
| Stocks Advanced | $199/mo (individual use) | **Real-time** | Unlimited* | ✅ | ✅ |
| Stocks Business | custom | Real-time | Unlimited* | ✅ (+ `fmv`) | Business+Expansion only |

\* Massive's knowledge base: *"free tier subscriptions come with a limit of 5 API requests per minute. Paying customers have access to unlimited API requests."* Stay well under ~100 req/s to be polite.

Prices for Starter/Developer come from third-party comparisons (Massive's pricing page loads them dynamically); confirm at https://massive.com/pricing before quoting to users.

**Consequence for FinAlly:** a student with a free key gets **end-of-day data only, 5 calls/min, and no snapshot endpoint**. Our client must therefore support two modes (see §7 and `MARKET_INTERFACE.md`):

- **Snapshot mode** (paid key): one call to the Full Market Snapshot every 2–15 s.
- **End-of-day mode** (free key): one call to Grouped Daily (whole market) every few minutes.

---

## 2. Endpoints for multi-ticker prices (the ones we use)

### 2.1 Full Market Snapshot — *primary, paid plans*

Latest trade, quote, minute bar, today's bar and previous day's bar for many tickers in **one request**.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,TSLA
```

| Query param | Type | Notes |
|---|---|---|
| `tickers` | comma list | Case-sensitive. **Empty = entire market (10,000+ tickers — never do this)**. No documented max; Massive suggests batching beyond a few hundred. |
| `include_otc` | bool | Default `false` |

Plans: Starter / Developer (15-min delayed), Advanced / Business (real-time). **Not Basic.**

Response (condensed):

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 0.98,
      "todaysChangePerc": 0.82,
      "updated": 1605195918306274000,
      "day":     {"o": 119.62, "h": 120.53, "l": 118.81, "c": 120.42, "v": 28727868, "vw": 119.725},
      "prevDay": {"o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49, "v": 110597265, "vw": 118.4998},
      "min":     {"o": 120.435, "h": 120.468, "l": 120.37, "c": 120.4401, "v": 270796, "vw": 120.4129,
                  "av": 28724441, "n": 1240, "t": 1684428600000},
      "lastTrade": {"p": 120.47, "s": 236, "x": 10, "i": "4046", "c": [14, 41], "t": 1605195918306274000},
      "lastQuote": {"p": 120.46, "s": 8, "P": 120.47, "S": 4, "t": 1605195918507251700}
    }
  ]
}
```

Field meanings: `p` price, `s` size, `x` exchange id, `c` condition codes, `t` timestamp; quote `p/s` = bid price/size, `P/S` = ask price/size; bars `o/h/l/c/v/vw/n` = open/high/low/close/volume/VWAP/transactions.

**Gotchas**

- `lastTrade` / `lastQuote` are present **only if the plan includes trades/quotes**. Always fall back: `lastTrade.p → min.c → day.c → prevDay.c`.
- Snapshot data is **cleared nightly** (~12:00–3:30 AM ET, per different doc pages) and repopulates from ~4:00 AM ET as exchanges report. Overnight, `day` may be all zeros and `lastTrade` may be missing; `prevDay.c` is the safe fallback.
- `updated`, `lastTrade.t`, `lastQuote.t` are **nanoseconds**. `min.t` is **milliseconds**.

### 2.2 Grouped Daily (Daily Market Summary) — *primary, free plan*

Daily OHLCV for **every** US stock for a given date in **one request**. Available on **all plans, including Basic** — the best fit for a 5-req/min free key.

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true
```

| Param | Notes |
|---|---|
| `date` (path) | `YYYY-MM-DD`. For EOD data, use the most recent completed trading day |
| `adjusted` | Split-adjusted, default `true` |
| `include_otc` | Default `false` |

History: Basic 2 years, Starter 5, Developer 10, Advanced/Business all (back to 2003-09-10).

```json
{
  "status": "OK", "adjusted": true, "queryCount": 3, "resultsCount": 3,
  "results": [
    {"T": "AAPL", "o": 324.66, "h": 326.2, "l": 322.3, "c": 325.12, "v": 26122646, "vw": 324.1, "n": 412000, "t": 1673298000000}
  ]
}
```

**Gotchas**

- No `tickers` filter — you get ~10k rows (a few MB). Filter client-side. One call per poll is fine.
- Requesting a weekend/holiday or today-before-close returns `resultsCount: 0` and no `results`. Walk back day-by-day until you get data (max ~4 steps for long weekends).
- `t` is milliseconds (end of window per the docs).

---

## 3. Other useful endpoints

### 3.1 Single Ticker Snapshot (paid)

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```
Same object as one element of §2.1, wrapped in `{"status","request_id","ticker":{...}}`. Use for one-off lookups (e.g., validating a newly added ticker), not for polling.

### 3.2 Previous Day Bar (all plans)

```
GET /v2/aggs/ticker/{ticker}/prev?adjusted=true
```
```json
{"ticker":"AAPL","status":"OK","resultsCount":1,
 "results":[{"T":"AAPL","o":115.55,"h":117.59,"l":114.13,"c":115.97,"v":131704427,"vw":116.3058,"t":1605042000000}]}
```
One ticker per call — on the free plan, 10 tickers = 2 minutes of quota. Prefer §2.2.

### 3.3 Daily Ticker Summary — open/close for a date (all plans)

```
GET /v1/open-close/{ticker}/{date}?adjusted=true
```
```json
{"status":"OK","symbol":"AAPL","from":"2023-01-09","open":324.66,"high":326.2,"low":322.3,
 "close":325.12,"volume":26122646,"preMarket":324.5,"afterHours":322.1}
```

### 3.4 Custom Bars / Aggregates (all plans) — historical charts

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}?adjusted=true&sort=asc&limit=5000
```
`timespan` ∈ `second, minute, hour, day, week, month, quarter, year`. `from`/`to` are `YYYY-MM-DD` or ms timestamps (inclusive). `limit` default 5000, max 50000. Paginate with `next_url`. Bars are ET and include pre/after-hours; no bar is emitted for intervals with no qualifying trades.

Could be used later to backfill the main chart with intraday history; not needed for the core build.

### 3.5 Last Trade (Developer+)

```
GET /v2/last/trade/{ticker}
```
```json
{"status":"OK","results":{"T":"AAPL","p":129.8473,"s":25,"x":4,"c":[37],"t":1617901342969834000,"y":1617901342968000000,"q":3135876,"z":3}}
```
One ticker per call — the snapshot (§2.1) is strictly better for us.

### 3.6 Unified Snapshot (paid)

```
GET /v3/snapshot?ticker.any_of=AAPL,MSFT&limit=250
```
Cross-asset (stocks, options, fx `C:`, crypto `X:`, indices `I:`), up to 250 tickers, `snake_case` fields (`last_trade.price`, `session.change_percent`, `session.previous_close`, `market_status`). Unknown tickers come back as `{"error":"NOT_FOUND","ticker":"..."}` entries rather than failing the call. A reasonable alternative to §2.1 if we ever want crypto/indices.

### 3.7 Market Status (all plans)

```
GET /v1/marketstatus/now
```
```json
{"market":"extended-hours","earlyHours":false,"afterHours":true,"serverTime":"2020-11-10T17:37:37-05:00",
 "exchanges":{"nasdaq":"extended-hours","nyse":"extended-hours","otc":"closed"}}
```
Useful to slow polling when the market is closed (prices won't change). Costs one request — on the free plan, don't call it every poll.

---

## 4. Python SDK (`massive` v2.x)

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType
from massive.exceptions import BadResponse, AuthError

client = RESTClient(
    api_key="...",        # defaults to os.getenv("MASSIVE_API_KEY"); raises AuthError if None
    connect_timeout=10.0,
    read_timeout=10.0,
    retries=3,            # urllib3 Retry on 413/429/499/500/502/503/504, backoff 0.1s
)
```

The client is **synchronous** (urllib3). In FastAPI, call it via `await asyncio.to_thread(...)`.

> ⚠️ With a free key, the built-in retry on **429** will immediately burn the remaining per-minute quota (backoff starts at 0.1 s). Construct the client with `retries=0` (or 1) and let our poll loop handle back-off.

### SDK methods and model attribute names

| Endpoint | SDK call | Returns | Key attributes |
|---|---|---|---|
| Full snapshot | `client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=[...])` | `list[TickerSnapshot]` | `.ticker`, `.last_trade.price`, `.last_trade.sip_timestamp` (ns), `.min.close`, `.day.close`, `.prev_day.close`, `.todays_change`, `.todays_change_percent`, `.updated` (ns) |
| Single snapshot | `client.get_snapshot_ticker(SnapshotMarketType.STOCKS, "AAPL")` | `TickerSnapshot` | as above |
| Grouped daily | `client.get_grouped_daily_aggs("2026-11-10", adjusted=True)` | `list[GroupedDailyAgg]` | `.ticker`, `.open`, `.high`, `.low`, `.close`, `.volume`, `.vwap`, `.timestamp` (ms) |
| Previous close | `client.get_previous_close_agg("AAPL")` | `PreviousCloseAgg` | `.ticker`, `.close`, `.timestamp` (ms) |
| Open/close | `client.get_daily_open_close_agg("AAPL", "2026-11-10")` | `DailyOpenCloseAgg` | `.open`, `.close`, `.pre_market`, `.after_hours` |
| Bars | `client.get_aggs("AAPL", 1, "minute", "2026-11-10", "2026-11-10")` / `list_aggs(...)` (paginating iterator) | `list[Agg]` | `.open..close`, `.timestamp` (ms) |
| Last trade | `client.get_last_trade("AAPL")` | `LastTrade` | `.price`, `.sip_timestamp` (ns) |
| Universal | `client.list_universal_snapshots(ticker_any_of=[...])` | iterator | `.last_trade`, `.session` |
| Market status | `client.get_market_status()` | `MarketStatus` | `.market`, `.exchanges` |

> ⚠️ **`LastTrade` has no `.timestamp` attribute.** The SIP timestamp is `.sip_timestamp`, in **nanoseconds**. Aggregate models (`Agg`, `GroupedDailyAgg`) do use `.timestamp`, in **milliseconds**. Mixing these up is the most common bug with this API.

Errors: any non-200 raises `massive.exceptions.BadResponse(body_text)`. The body is JSON like `{"status":"NOT_AUTHORIZED","message":"You are not entitled to this data. Please upgrade your plan..."}` (403) or `{"status":"ERROR","error":"You've exceeded the maximum requests per minute..."}` (429). There's no status code attribute; inspect the message text.

---

## 5. Code examples

### 5.1 Real-time/delayed prices for many tickers (paid plan)

```python
import time
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=API_KEY, retries=0)

def latest_prices(tickers: list[str]) -> dict[str, tuple[float, float]]:
    """Return {ticker: (price, unix_seconds)} using one snapshot call."""
    out: dict[str, tuple[float, float]] = {}
    for snap in client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=tickers):
        lt = snap.last_trade
        if lt and lt.price:
            price, ts = lt.price, (lt.sip_timestamp or 0) / 1e9
        elif snap.min and snap.min.close:
            price, ts = snap.min.close, (snap.min.timestamp or 0) / 1e3
        elif snap.day and snap.day.close:
            price, ts = snap.day.close, (snap.updated or 0) / 1e9
        elif snap.prev_day and snap.prev_day.close:
            price, ts = snap.prev_day.close, (snap.prev_day.timestamp or 0) / 1e3
        else:
            continue
        out[snap.ticker] = (price, ts or time.time())
    return out

print(latest_prices(["AAPL", "MSFT", "NVDA"]))
```

### 5.2 End-of-day prices for many tickers (free plan, one call)

```python
from datetime import date, timedelta
from massive import RESTClient

client = RESTClient(api_key=API_KEY, retries=0)

def eod_prices(tickers: list[str], max_lookback: int = 5) -> tuple[date, dict[str, float]]:
    """Closing prices from the most recent trading day that has data."""
    wanted = set(tickers)
    d = date.today() - timedelta(days=1)
    for _ in range(max_lookback):
        aggs = client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        if aggs:
            return d, {a.ticker: a.close for a in aggs if a.ticker in wanted and a.close}
        d -= timedelta(days=1)  # weekend / holiday
    return d, {}

day, closes = eod_prices(["AAPL", "MSFT", "NVDA"])
```

Note each loop iteration is one request; on a free key, a long weekend can cost 3–4 of the 5 per-minute calls. Cache the "last good date".

### 5.3 Raw HTTP with `httpx` (no SDK)

```python
import httpx

BASE = "https://api.massive.com"
headers = {"Authorization": f"Bearer {API_KEY}"}

with httpx.Client(base_url=BASE, headers=headers, timeout=10) as http:
    r = http.get("/v2/snapshot/locale/us/markets/stocks/tickers",
                 params={"tickers": "AAPL,MSFT,NVDA"})
    if r.status_code == 403:
        ...  # plan doesn't include snapshots → use grouped daily
    r.raise_for_status()
    for t in r.json().get("tickers", []):
        price = (t.get("lastTrade") or {}).get("p") or t["day"]["c"] or t["prevDay"]["c"]
        print(t["ticker"], price, t.get("todaysChangePerc"))
```

### 5.4 curl

```bash
curl -s -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT" | jq .

curl -s "https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-11-10?apiKey=$MASSIVE_API_KEY" \
  | jq '.results[] | select(.T=="AAPL")'
```

### 5.5 Detecting the plan at startup

There's no "what plan am I on" endpoint. Probe instead: call the snapshot for one ticker. A `BadResponse` containing `NOT_AUTHORIZED` (HTTP 403) means a Basic/free key → switch to EOD mode. Any other success → snapshot mode.

```python
from massive.exceptions import BadResponse

def detect_mode(client) -> str:
    try:
        client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=["AAPL"])
        return "snapshot"
    except BadResponse as e:
        if "NOT_AUTHORIZED" in str(e) or "not entitled" in str(e).lower():
            return "eod"
        raise
```

---

## 6. Timestamp cheat-sheet

| Field | Unit | To Unix seconds |
|---|---|---|
| snapshot `updated` | ns | `/ 1e9` |
| snapshot `lastTrade.t` / SDK `last_trade.sip_timestamp` | ns | `/ 1e9` |
| snapshot `lastQuote.t` | ns | `/ 1e9` |
| snapshot `min.t` / SDK `min.timestamp` | ms | `/ 1e3` |
| aggregates `t` / SDK `Agg.timestamp`, `GroupedDailyAgg.timestamp`, `PreviousCloseAgg.timestamp` | ms | `/ 1e3` |
| market status `serverTime` | RFC 3339 string | `datetime.fromisoformat` |

---

## 7. Recommended usage for FinAlly

| Mode | When | Endpoint | Calls | Interval | Price used |
|---|---|---|---|---|---|
| Snapshot | paid key | §2.1 Full Market Snapshot, `tickers=<watchlist>` | 1 per poll | 15 s default (`MASSIVE_POLL_INTERVAL`, 2 s on paid is fine) | `lastTrade.p` → `min.c` → `day.c` → `prevDay.c` |
| End-of-day | free key (403 on snapshot) | §2.2 Grouped Daily, latest trading day | 1 per poll | 300 s (the data only changes once a day) | `close` |

Rules:

1. **One request per poll** regardless of watchlist size — never loop over tickers on the free plan.
2. Build the client with `retries=0`; on a 429, skip polls and double the interval (cap at 60 s), reset on success.
3. A newly added ticker appears on the next poll. For snapshot mode, a ticker absent from the response is likely invalid; log it once, don't crash.
4. Never call the snapshot endpoint with an empty `tickers` list — that downloads the whole market.
5. In EOD mode, prices don't move, so the UI shows static prices with no flashes. That's expected; the README should tell free-key users the simulator gives a livelier demo.

### Sources

- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot) · [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot) · [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Daily Market Summary](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary) · [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar) · [Daily Ticker Summary](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary) · [Custom Bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Last Trade](https://massive.com/docs/rest/stocks/trades-quotes/last-trade) · [Market Status](https://massive.com/docs/rest/stocks/market-operations/market-status) · [REST Quickstart](https://massive.com/docs/rest/quickstart)
- [Request limit (KB)](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis) · [Max tickers per snapshot (KB)](https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-massives-snapshot) · [Pricing](https://massive.com/pricing) · [Massive vs Alpha Vantage](https://massive.com/compare/massive-vs-alpha-vantage) · [Third-party plan summary](https://optiontoolstack.com/tools/massive/)
- `massive` Python SDK 2.9.2 source (`massive/rest/snapshot.py`, `aggs.py`, `models/snapshot.py`, `models/trades.py`, `base.py`)
