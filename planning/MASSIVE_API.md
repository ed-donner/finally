# Massive API Reference (formerly Polygon.io)

Reference for the parts of the Massive REST API that FinAlly uses to get **real-time** and **end-of-day** prices for **multiple tickers**. It covers the raw HTTP API and the official Python SDK (`massive`, pinned at **2.2.0** in `backend/uv.lock`).

> Polygon.io renamed itself **Massive** on 2025-10-30. Accounts, API keys and endpoint paths did not change. The new base URL is `https://api.massive.com`. The SDK package is `massive` (previously `polygon-api-client`), and the client class is still `RESTClient`.

Researched 2026-11-17 from the official docs at `massive.com/docs/rest/...` and from reading the installed SDK source. Where the docs and the SDK differ, the SDK source wins, because that is the code we run.

---

## 1. Basics

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` |
| Auth (header, preferred) | `Authorization: Bearer <MASSIVE_API_KEY>` (this is what the SDK sends) |
| Auth (query string) | `?apiKey=<MASSIVE_API_KEY>` |
| Format | JSON over HTTPS. Tickers are **case-sensitive** (send `AAPL`, not `aapl`) |
| Env var | The SDK reads `MASSIVE_API_KEY` itself if you don't pass `api_key=` |
| Errors | Non-200 → SDK raises `massive.exceptions.BadResponse(<body text>)`. A missing key raises `AuthError` |
| Built-in retries | The SDK retries 413/429/499/500/502/503/504 up to 3 times with backoff (0.1s factor) |

### Plans and data recency (stocks)

From the per-endpoint plan tables in the docs:

| Plan | Price | Snapshots | Aggregates / prev close / grouped daily | Recency | History |
|---|---|---|---|---|---|
| **Basic** (free) | $0 | ❌ **not included** | ✅ | End-of-day | 2 years |
| Starter | paid | ✅ | ✅ | 15-min delayed | 5 years |
| Developer | paid | ✅ | ✅ | 15-min delayed | 10 years |
| Advanced | $199/mo | ✅ | ✅ | **Real-time** | All (back to 2003) |
| Business | quote | ✅ | ✅ | Real-time | All |

**Rate limits:** the free Basic plan allows **5 requests/minute**. Paid plans have no practical REST limit (the docs suggest staying under about 100 req/s). Starter and Developer prices are not published on the static pricing page, so check `massive.com/pricing`.

**Consequences for FinAlly:**
1. **The free tier cannot call the snapshot endpoint.** A free key gets a `403`/`NOT_AUTHORIZED` from `/v2/snapshot/...`. Free-tier users only get end-of-day data (previous close or grouped daily). The interface design in `MARKET_INTERFACE.md` handles this with a fallback.
2. Starter/Developer snapshots are 15 minutes delayed. That is fine for a demo, but the "live" prices will barely move between polls.
3. Snapshot data is **cleared daily at 3:30 AM ET** and fills in again from about 4:00 AM ET. Early in the morning, `day` fields may be zero or empty. `lastTrade` and `prevDay` are the dependable fields.

### Timestamp units (important)

The units are not the same across endpoints:

| Field | Unit |
|---|---|
| Snapshot `updated`, `lastTrade.t`, `lastQuote.t` | **nanoseconds** (19 digits) |
| Snapshot `min.t`, all aggregate bar `t` | **milliseconds** (13 digits) |
| Unified v3 snapshot `last_trade.sip_timestamp`, `fmv_last_updated` | nanoseconds |

FinAlly uses **Unix seconds (float)** everywhere, so convert with `ns / 1e9` or `ms / 1e3`.

---

## 2. Endpoints for multi-ticker prices

| Need | Endpoint | Tickers per call | Free tier |
|---|---|---|---|
| **Latest price, many tickers** ⭐ | `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=A,B,C` | many (comma list; whole market if omitted) | ❌ |
| Latest price, one ticker | `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}` | 1 | ❌ |
| Latest price, mixed asset types | `GET /v3/snapshot?ticker.any_of=A,B,C` | ≤ 250 per page | ❌ |
| **Previous day close, one ticker** | `GET /v2/aggs/ticker/{ticker}/prev` | 1 | ✅ |
| **EOD OHLC for the whole market** ⭐ | `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | all (~10k) | ✅ |
| Open/close for one ticker on one date | `GET /v1/open-close/{ticker}/{date}` | 1 | ✅ |
| Historical bars | `GET /v2/aggs/ticker/{ticker}/range/{mult}/{timespan}/{from}/{to}` | 1 | ✅ |
| Is the market open? | `GET /v1/marketstatus/now` | — | ✅ |

⭐ = the two calls FinAlly relies on. Each one returns **all tracked tickers in a single request**, which is what keeps us inside 5 req/min.

---

### 2.1 Full Market Snapshot (real-time / delayed, multi-ticker) ⭐

`GET /v2/snapshot/locale/us/markets/stocks/tickers`

| Query param | Type | Default | Notes |
|---|---|---|---|
| `tickers` | comma-separated string | *(all)* | Case-sensitive, e.g. `AAPL,TSLA,GOOG`. Leaving it out returns 10,000+ tickers (a large payload) |
| `include_otc` | bool | `false` | Include OTC securities |

Tickers that are unknown or have no data are **left out of the response** silently. They do not cause an error.

**Response** (trimmed to one ticker):
```json
{
  "status": "OK",
  "count": 1,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.52,
      "todaysChangePerc": 0.80,
      "updated": 1731753600123456789,
      "day":     {"o": 189.1, "h": 191.0, "l": 188.7, "c": 190.52, "v": 41234567, "vw": 190.02},
      "prevDay": {"o": 187.9, "h": 189.6, "l": 187.2, "c": 189.00, "v": 52345678, "vw": 188.71},
      "min":     {"av": 41234567, "t": 1731753540000, "n": 312, "o": 190.4, "h": 190.6,
                  "l": 190.3, "c": 190.52, "v": 15234, "vw": 190.47},
      "lastTrade": {"p": 190.52, "s": 100, "t": 1731753600123456789, "x": 4, "c": [14, 41], "i": "71675577320245"},
      "lastQuote": {"p": 190.51, "s": 2, "P": 190.53, "S": 3, "t": 1731753600120000000}
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `lastTrade.p` | **Last trade price**. This is FinAlly's "current price" |
| `lastTrade.t` | Trade SIP timestamp (**ns**) |
| `day.c` | Today's running close (it can be `0` right after the 3:30 AM reset) |
| `prevDay.c` | Previous session's close |
| `todaysChange` / `todaysChangePerc` | Change compared with `prevDay.c` |
| `lastQuote.p` / `.P` | Bid / ask (only on plans that include quotes) |
| `updated` | Last time the snapshot changed (**ns**) |

`lastTrade` appears only if your plan includes trades, and `lastQuote` only if it includes quotes. Code should fall back `lastTrade.p → min.c → day.c → prevDay.c`.

**curl**
```bash
curl -s -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,NVDA" | jq '.tickers[] | {ticker, price: .lastTrade.p, prev: .prevDay.c}'
```

**Python SDK**
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()  # reads MASSIVE_API_KEY from the environment

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,   # → locale "us"
    tickers=["AAPL", "MSFT", "NVDA"],        # a list is joined with commas by the SDK
)
for s in snapshots:                          # list[TickerSnapshot]
    trade = s.last_trade                     # LastTrade | None
    price = trade.price if trade and trade.price else (s.day.close or s.prev_day.close)
    ts = (trade.sip_timestamp / 1e9) if trade and trade.sip_timestamp else (s.updated or 0) / 1e9
    print(s.ticker, price, s.prev_day.close, s.todays_change_percent, ts)
```

#### ⚠️ SDK attribute names (read from source)

`TickerSnapshot` fields are `ticker, day, prev_day, min, last_trade, last_quote, todays_change, todays_change_percent, updated, fair_market_value`.

`LastTrade` fields are `ticker, price, size, exchange, conditions, id, sip_timestamp, participant_timestamp, trf_timestamp, sequence_number, correction, trf_id, tape`.

**`LastTrade` has no `.timestamp` attribute.** The raw JSON key `t` maps to **`sip_timestamp`** (nanoseconds). Reading `snap.last_trade.timestamp` raises `AttributeError`. `day` and `prev_day` are `Agg` objects (`open, high, low, close, volume, vwap, timestamp, transactions, otc`).

---

### 2.2 Single Ticker Snapshot

`GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}` returns `{"status", "request_id", "ticker": {...same object as above...}}`.

```python
snap = client.get_snapshot_ticker(SnapshotMarketType.STOCKS, "AAPL")
print(snap.last_trade.price)
```
Each call counts against the rate limit, so for N tickers use 2.1 instead.

### 2.3 Unified Snapshot (v3)

`GET /v3/snapshot?ticker.any_of=AAPL,MSFT&limit=250` uses a different, snake_case schema: `results[].last_trade.price`, `results[].session.{open,close,previous_close,change_percent}`, `results[].market_status`. Errors are reported per ticker in `results[].error` / `.message`. The response is paginated with `next_url`, and `limit` can be at most 250. It is useful when a request mixes stocks, options (`O:`), FX (`C:`), crypto (`X:`) and indices (`I:`). FinAlly only needs stocks, so we use 2.1.

```python
for snap in client.list_universal_snapshots(ticker_any_of=["AAPL", "MSFT"], limit=250):
    print(snap.ticker, snap.last_trade.price if snap.last_trade else None, snap.session.previous_close)
```

---

### 2.4 Previous Day Bar (end-of-day, free tier) ✅

`GET /v2/aggs/ticker/{stocksTicker}/prev?adjusted=true`

```json
{"ticker": "AAPL", "status": "OK", "adjusted": true, "queryCount": 1, "resultsCount": 1,
 "results": [{"T": "AAPL", "o": 187.9, "h": 189.6, "l": 187.2, "c": 189.0,
              "v": 52345678, "vw": 188.71, "t": 1731704400000, "n": 612345}]}
```
`t` = start of the bar, in **ms**. Each request covers one ticker, so 10 tickers cost 10 calls (two minutes of free-tier budget).

```python
prev = client.get_previous_close_agg("AAPL")   # list[PreviousCloseAgg] (result_key="results")
bar = prev[0]
print(bar.ticker, bar.close, bar.timestamp / 1e3)
```

### 2.5 Daily Market Summary / Grouped Daily (end-of-day, all tickers, free tier) ⭐ ✅

`GET /v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}?adjusted=true&include_otc=false`

This returns the OHLC bar for **every** US stock on that date in **one call**. It is the most efficient way to get EOD prices for a watchlist on the free tier: fetch once, then filter locally.

```json
{"status": "OK", "adjusted": true, "queryCount": 10950, "resultsCount": 10950,
 "results": [{"T": "AAPL", "o": 187.9, "h": 189.6, "l": 187.2, "c": 189.0,
              "v": 52345678, "vw": 188.71, "t": 1731704400000, "n": 612345}, ...]}
```
- `t` is **ms**. The docs describe it as the *end* of the window, while custom bars use the *start*. Use the date you requested, not `t`, as the bar date.
- A weekend, a holiday, or today before the close returns `resultsCount: 0` and an empty `results`. Walk back day by day until you get results (at most about 4 tries over a long weekend).

```python
from datetime import date, timedelta

def latest_eod_closes(client: RESTClient, tickers: set[str], max_back: int = 5) -> dict[str, float]:
    d = date.today() - timedelta(days=1)
    for _ in range(max_back):
        bars = client.get_grouped_daily_aggs(d.isoformat(), adjusted=True)
        if bars:
            return {b.ticker: b.close for b in bars if b.ticker in tickers}
        d -= timedelta(days=1)
    return {}
```

### 2.6 Daily Ticker Summary (open/close for one date)

`GET /v1/open-close/{ticker}/{YYYY-MM-DD}` returns `{"status", "symbol", "from", "open", "high", "low", "close", "volume", "preMarket", "afterHours"}`.
```python
oc = client.get_daily_open_close_agg("AAPL", "2026-11-16")
print(oc.open, oc.close, oc.pre_market, oc.after_hours)
```

### 2.7 Custom Bars (historical series)

`GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}` takes `timespan` ∈ `second|minute|hour|day|week|month|quarter|year`. `from`/`to` are `YYYY-MM-DD` or ms, inclusive. Other params: `adjusted`, `sort=asc|desc`, `limit` ≤ 50,000 (default 5,000). Results are paginated via `next_url`, and `list_aggs` follows it automatically.
```python
bars = client.get_aggs("AAPL", 1, "day", "2026-10-01", "2026-11-14")   # one page
for b in client.list_aggs("AAPL", 5, "minute", "2026-11-14", "2026-11-14", limit=50000):  # auto-paginates
    print(b.timestamp / 1e3, b.close)
```
FinAlly doesn't need this today, because charts are built from SSE since page load. It is the hook to use if we ever want to pre-fill charts with history.

### 2.8 Market Status

`GET /v1/marketstatus/now` returns `{"market": "open"|"closed"|"extended-hours", "earlyHours": bool, "afterHours": bool, "serverTime": "...", "exchanges": {...}}`.
```python
status = client.get_market_status()
print(status.market, status.server_time)
```
Use it to slow down polling while the market is closed.

---

## 3. Calling the sync SDK from asyncio

`RESTClient` is synchronous (urllib3). In FastAPI, always run it off the event loop:

```python
import asyncio
snapshots = await asyncio.to_thread(
    client.get_snapshot_all, SnapshotMarketType.STOCKS, tickers
)
```

## 4. Error handling cheat sheet

| Situation | What you see | What to do |
|---|---|---|
| Missing key | `AuthError` at `RESTClient()` construction | The factory never builds the Massive source without a key |
| Bad key | `BadResponse` with a 401 body (`"status":"ERROR"`) | Log once at ERROR and keep serving the last cached prices |
| Plan lacks the endpoint (free tier → snapshot) | `BadResponse` with a 403 body (`"status":"NOT_AUTHORIZED"`) | Switch to EOD mode (grouped daily) |
| Rate limited | 429. The SDK retries 3× internally, then `BadResponse` | Skip this cycle and keep the poll interval at or above 12 s on the free tier |
| Unknown ticker in `tickers=` | Missing from `tickers[]` | No price yet. Trade returns `400 "No price available"` |
| Pre-market after reset | `day.c == 0`, `lastTrade` may be stale | Use the `lastTrade.p` → `prevDay.c` fallback chain |

## 5. Minimal standalone check script

```python
# uv run python check_massive.py
import os
from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])
tickers = ["AAPL", "MSFT", "NVDA"]
try:
    for s in client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers):
        lt = s.last_trade
        print(f"{s.ticker:6} last={lt.price if lt else None} prev_close={s.prev_day.close if s.prev_day else None}")
except BadResponse as e:
    print("Snapshot not available on this plan, falling back to EOD:", e)
    for t in tickers:
        bar = client.get_previous_close_agg(t)[0]
        print(f"{t:6} close={bar.close}")
```

## Sources

- Full Market Snapshot: https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- Single Ticker Snapshot: https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot
- Unified Snapshot: https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- Previous Day Bar: https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- Daily Market Summary: https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- Daily Ticker Summary: https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary
- Custom Bars: https://massive.com/docs/rest/stocks/aggregates/custom-bars
- Quickstart / auth: https://massive.com/docs/rest/quickstart
- Pricing: https://massive.com/pricing
- SDK source: `backend/.venv/lib/python3.13/site-packages/massive/rest/{snapshot,aggs,base}.py`, `rest/models/{snapshot,trades,aggs}.py`
