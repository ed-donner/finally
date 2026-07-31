# Massive API Research (formerly Polygon.io)

Research notes on the [Massive](https://massive.com) market data API — the optional real-data
source for FinAlly, used when `MASSIVE_API_KEY` is set (see `PLAN.md` §5, §6).

## 1. Background

Polygon.io rebranded to **Massive** in late 2025. Existing Polygon.io API keys, endpoints, and
the `polygon-api-client` Python package continue to work unchanged — Massive is the same company,
same data, same infrastructure, new name. Key facts:

- Base URL: `https://api.massive.com` (the old `https://api.polygon.io` still works — same backend)
- Official Python client package is now **`massive`** (was `polygon-api-client`)
- Coverage: US equities, options, indices, forex, crypto, and futures; equities data goes back to 2003
- Products relevant to FinAlly: **Stocks REST API** (snapshots, aggregates) — we do not need
  options, forex, or the WebSocket product for this project (see §6 for why)

Docs root: `https://massive.com/docs` — the stocks section has a machine-readable dump at
`https://massive.com/docs/rest/stocks/llms-full.txt` which is the fastest way to get exact
endpoint/field names.

## 2. Authentication

Every REST request needs an API key, supplied one of two ways:

**Query parameter** (raw HTTP):
```
GET https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/AAPL?apiKey=YOUR_API_KEY
```

**Authorization header** (what the official client sends under the hood):
```
Authorization: Bearer YOUR_API_KEY
```

The Python client takes the key as a constructor argument — it does **not** read an environment
variable itself, so FinAlly's backend must read `MASSIVE_API_KEY` from `.env` and pass it in
explicitly:

```python
import os
from massive import RESTClient

client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])
```

## 3. Rate Limits

This is the single most important constraint for the project's design:

| Tier | Limit |
|---|---|
| Free | **5 requests/minute** |
| Paid (Stocks Starter and up) | Much higher / effectively unlimited, but Massive asks clients to stay under ~100 req/sec |

FinAlly targets students running with the **free tier**, so the Massive-backed data source must
poll infrequently and batch every ticker into a single request rather than one request per ticker.
This directly shapes the interface in `MARKET_INTERFACE.md` — see §6 below for the polling cadence
this implies.

## 4. Installing the Python Client

```bash
pip install -U massive
# or, in this project: uv add massive
```

```python
from massive import RESTClient          # REST polling client
from massive import WebSocketClient      # real-time streaming client (not used by FinAlly, see §6)
```

## 5. Endpoints FinAlly Needs

### 5.1 Real-time / latest price — multi-ticker snapshot (batch)

The **Full Market Snapshot** endpoint takes a comma-separated ticker list and returns the latest
trade, quote, and day/prev-day bar for each in a single call. This is the endpoint FinAlly's
poller uses — one call covers the whole watchlist regardless of size (up to 250 tickers).

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT&apiKey=YOUR_API_KEY
```

Raw HTTP example:

```python
import requests

resp = requests.get(
    "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
    params={"tickers": "AAPL,GOOGL,MSFT", "apiKey": API_KEY},
    timeout=10,
)
resp.raise_for_status()
data = resp.json()
```

Example response shape (one entry per ticker, plus per-ticker errors for bad symbols):

```json
{
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 0.98,
      "todaysChangePerc": 0.82,
      "updated": 1605195918306274000,
      "day":     { "o": 119.62, "h": 120.53, "l": 118.81, "c": 120.4229, "v": 28727868, "vw": 119.725 },
      "prevDay": { "o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49,  "v": 110597265, "vw": 118.4998 },
      "lastTrade": { "p": 120.47, "s": 236, "t": 1605195918306274000 },
      "lastQuote": { "p": 120.46, "P": 120.47, "s": 8, "S": 4, "t": 1605195918507251700 },
      "min": { "o": 120.435, "h": 120.468, "l": 120.37, "c": 120.4201, "v": 270796, "t": 1684428720000 }
    },
    { "ticker": "BADSYM", "error": "NOT_FOUND", "message": "Ticker not found." }
  ]
}
```

Fields FinAlly's `MassiveDataSource` cares about per ticker:

| Field | Meaning | Maps to |
|---|---|---|
| `ticker` | Symbol | `PriceUpdate.ticker` |
| `day.c` (fallback: `lastTrade.p`) | Latest/current price | `PriceUpdate.price` |
| `prevDay.c` | Previous close | Baseline for computing `previous_price` on the first poll |
| `updated` | Nanosecond timestamp of last update | `PriceUpdate.timestamp` |
| `todaysChangePerc` | % change since prev close | Available for display, though FinAlly computes its own tick-over-tick change |

Using the official client instead of raw `requests` (client method for this endpoint is the
v2 "snapshot all" call — verify the exact method name against the `massive` version pinned in
`pyproject.toml`, since the client has been migrating callers toward `list_universal_snapshots()`,
the v3 cross-asset equivalent described next):

```python
from massive import RESTClient

client = RESTClient(api_key=API_KEY)
snapshot = client.get_snapshot_all("stocks", tickers=["AAPL", "GOOGL", "MSFT"])
for t in snapshot:
    print(t.ticker, t.day.close, t.prev_day.close)
```

### 5.2 Alternative: unified/universal snapshot (v3)

A newer, cross-asset-class endpoint that also accepts a batched ticker list
(`ticker.any_of=AAPL,MSFT`, up to 250) and is what Massive now recommends for new integrations.
Response shape differs slightly (nested `session` instead of `day`/`prevDay`). Either endpoint
works for FinAlly; the v2 multi-ticker snapshot above is simpler and its field names map more
directly onto our `PriceUpdate` model, so that's the one documented in `MARKET_INTERFACE.md`.

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&apiKey=YOUR_API_KEY
```

### 5.3 End-of-day (EOD) — all tickers in one call

The **Grouped Daily** endpoint returns OHLCV for *every* US stock ticker for one trading date in
a single response — useful for EOD backfill/seeding without per-ticker requests:

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true&apiKey=YOUR_API_KEY
```

```python
resp = requests.get(
    f"https://api.massive.com/v2/aggs/grouped/locale/us/market/stocks/2026-07-30",
    params={"adjusted": "true", "apiKey": API_KEY},
    timeout=10,
)
```

### 5.4 End-of-day (EOD) — single ticker, previous close

For a quick "yesterday's close" for one symbol:

```
GET /v2/aggs/ticker/{ticker}/prev?adjusted=true&apiKey=YOUR_API_KEY
```

Response fields: `c` (close), `h` (high), `l` (low), `o` (open), `v` (volume), `vw` (VWAP),
`t` (timestamp).

### 5.5 Historical bars (for future charting needs beyond SSE-accumulated sparklines)

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}?adjusted=true&sort=asc&limit=50000
```

```python
aggs = []
for a in client.list_aggs(ticker="AAPL", multiplier=1, timespan="day",
                           from_="2026-01-01", to="2026-07-30", limit=50000):
    aggs.append(a)
```

FinAlly doesn't need this initially — the frontend builds sparklines from the SSE stream it has
already seen since page load (per `PLAN.md` §10) — but it's here in case a "load more history"
feature is added later.

## 6. Why FinAlly Polls REST Instead of Using the WebSocket

Massive offers a WebSocket product (`wss://socket.massive.com/stocks`, channels like `T.AAPL` for
trades, `Q.AAPL` for quotes, `AM.AAPL` for minute aggregates) for true tick-by-tick streaming.
FinAlly does **not** use it, per `PLAN.md` §6:

- The free tier's WebSocket access is far more restricted than even the 5 req/min REST limit
- A persistent outbound WebSocket connection from the backend adds reconnection/backoff
  complexity that a simple polling loop avoids
- FinAlly's own client-facing stream is already SSE (`/api/stream/prices`), which is one-way and
  polling-friendly — the backend's *internal* refresh cadence (2–15s against Massive) is decoupled
  from the *external* cadence it pushes to the browser (~500ms, reusing the last known cache value
  between polls), so students don't see "choppy" updates even though the upstream data itself only
  changes every several seconds on the free tier

## 7. Error Handling Notes

- Per-ticker errors come back *inside* a 200 OK batch response (`"error": "NOT_FOUND"` in the
  ticker's own object) rather than failing the whole request — the client must check each entry.
- A `429 Too Many Requests` means the poll interval is too aggressive for the current plan tier;
  back off and keep serving the last cached prices rather than raising to the frontend.
- Network/timeout errors should also fall back to last-known-cache values so a transient Massive
  outage doesn't blank out the watchlist — the SSE stream should never emit "no data."

## 8. Sources

- [Polygon.io is Now Massive](https://massive.com/blog/polygon-is-now-massive)
- [Stocks REST API Overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot)
- [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Previous Day Bar (OHLC)](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Daily Market Summary / Grouped Daily](https://massive.com/docs/rest/stocks/aggregates/daily-market-summary)
- [Custom Bars (OHLC)](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Massive + Python blog post](https://massive.com/blog/polygon-io-with-python-for-stock-market-data)
- [massive-com/client-python (GitHub)](https://github.com/massive-com/client-python)
- [What is the request limit for Massive's RESTful APIs?](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
