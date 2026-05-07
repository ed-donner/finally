# Massive (formerly Polygon.io) API Reference

Polygon.io rebranded as Massive on October 30, 2025. Existing API keys and integrations continue to work without change. Base URL remains `https://api.polygon.io` (redirects to `https://api.massive.com`).

## Authentication

All endpoints require an API key. Two methods:

```
# Query parameter (simple, works for all clients)
GET https://api.polygon.io/v2/snapshot/.../tickers?apiKey=YOUR_KEY

# Authorization header (preferred for production)
Authorization: Bearer YOUR_KEY
```

## Rate Limits

| Plan | Requests / Minute | Recommended Poll Interval |
|------|-------------------|--------------------------|
| Free | 5 | 15 seconds |
| Paid (any tier) | Unlimited | 2–5 seconds |

On free tier, a single request per poll cycle for all watched tickers uses 1 of 5 allowed calls per minute, leaving headroom for other API calls.

## Python Client

```bash
pip install -U massive
```

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="YOUR_KEY")
```

---

## Key Endpoints

### 1. Snapshot — Multiple Tickers (primary endpoint for this project)

Fetches current market data for a comma-separated list of tickers in a single request. This is the most efficient way to poll prices for a watchlist.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tickers` | string | No | Comma-separated list of ticker symbols (e.g. `AAPL,GOOGL,MSFT`). Omit to get all tickers. |
| `include_otc` | boolean | No | Include OTC securities. Default: `false`. |
| `apiKey` | string | Yes* | API key (or use Authorization header). |

**Example request:**

```python
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "TSLA"],
)
for snap in snapshots:
    price = snap.last_trade.price
    timestamp_ms = snap.last_trade.timestamp  # Unix milliseconds
    print(f"{snap.ticker}: ${price:.2f}")
```

**Response shape (JSON):**

```json
{
  "status": "OK",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 0.98,
      "todaysChangePerc": 0.82,
      "updated": 1605195918306274000,
      "day": {
        "o": 119.62, "h": 120.53, "l": 118.81, "c": 120.42,
        "v": 28727868, "vw": 119.725
      },
      "prevDay": {
        "o": 117.19, "h": 119.63, "l": 116.44, "c": 119.49,
        "v": 110597265, "vw": 118.4998
      },
      "min": {
        "o": 120.435, "h": 120.468, "l": 120.37, "c": 120.42,
        "v": 270796, "vw": 120.4129, "t": 1684428720000, "n": 762
      },
      "lastTrade": {
        "p": 120.47,
        "s": 236,
        "t": 1605195918306274000,
        "x": 10
      },
      "lastQuote": {
        "p": 120.46, "s": 8,
        "P": 120.47, "S": 4,
        "t": 1605195918507251700
      }
    }
  ]
}
```

**Response fields:**

| Field | Description |
|-------|-------------|
| `ticker` | Ticker symbol |
| `todaysChange` | Absolute price change from previous close |
| `todaysChangePerc` | Percentage change from previous close |
| `updated` | Last update timestamp (Unix nanoseconds) |
| `day.c` | Current day's latest close/last price |
| `prevDay.c` | Previous trading day's closing price |
| `min.c` | Most recent one-minute bar close |
| `lastTrade.p` | Price of the most recent trade |
| `lastTrade.t` | Trade timestamp (Unix milliseconds) |
| `lastQuote.p` | Best bid price |
| `lastQuote.P` | Best ask price |

**Extracting price and previous close:**

```python
for snap in snapshots:
    current_price = snap.last_trade.price           # last trade price
    previous_close = snap.prev_day.close            # for daily change %
    timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
    change_pct = snap.todays_change_perc            # daily % change
```

---

### 2. Snapshot — Single Ticker

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

Same response shape as above, but for one ticker. Counts as 1 API call.

**Example:**

```python
snap = client.get_snapshot_ticker(market_type=SnapshotMarketType.STOCKS, ticker="AAPL")
print(snap.last_trade.price)
```

---

### 3. Unified Snapshot (multi-asset, up to 250 tickers)

For watchlists that may include stocks, ETFs, indices, or crypto in one call.

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&type=stocks
```

**Query parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `ticker.any_of` | string | Comma-separated list; max 250 tickers |
| `type` | string | Asset class filter: `stocks`, `options`, `fx`, `crypto`, `indices` |
| `limit` | integer | Max results per page (default 10, max 250) |
| `order` | string | Sort order |
| `sort` | string | Sort field |

**Response shape:**

```json
{
  "status": "OK",
  "request_id": "abc123",
  "results": [
    {
      "ticker": "AAPL",
      "type": "stocks",
      "name": "Apple Inc.",
      "market_status": "open",
      "session": {
        "open": 119.62,
        "high": 120.53,
        "low": 118.81,
        "close": 120.42,
        "change": 0.93,
        "change_percent": 0.78,
        "previous_close": 119.49
      },
      "last_trade": { "price": 120.47, "size": 236, "timestamp": 1605195918306 },
      "last_quote": { "bid": 120.46, "ask": 120.47, "bid_size": 800, "ask_size": 400 }
    }
  ]
}
```

---

### 4. Previous Day Bar

Fetches OHLCV for the prior trading session. Useful for seeding `previous_close` on startup.

```
GET /v2/aggs/ticker/{ticker}/prev
```

**Example:**

```python
prev = client.get_previous_close_agg(ticker="AAPL")
previous_close = prev[0].close if prev else None
```

**Response fields within `results[]`:**

| Field | Description |
|-------|-------------|
| `o` | Opening price |
| `h` | High price |
| `l` | Low price |
| `c` | Closing price |
| `v` | Volume |
| `vw` | Volume-weighted average price |
| `t` | Unix millisecond timestamp |

---

### 5. Market Status

Check whether the market is currently open (useful for handling off-hours polling).

```
GET /v1/marketstatus/now
```

```python
status = client.get_market_status()
is_open = status.market == "open"
```

---

## Error Handling

Common HTTP status codes:

| Code | Meaning | Action |
|------|---------|--------|
| 200 | OK | Normal |
| 401 | Bad API key | Log and fail fast at startup |
| 403 | Plan doesn't include this endpoint | Check plan tier |
| 429 | Rate limit exceeded | Back off and retry after 60s |
| 5xx | Server error | Log and retry on next poll interval |

The Massive client raises exceptions for non-2xx responses. Wrap poll calls in try/except and log errors without crashing — the poller will retry on the next interval.

```python
try:
    snapshots = client.get_snapshot_all(...)
except Exception as e:
    logger.error("Massive poll failed: %s", e)
    # continue — next poll will retry
```

---

## Plan Considerations for This Project

The project uses the **free tier** by default (`MASSIVE_API_KEY` optional). With 5 requests/minute:

- One call per poll cycle fetches all watched tickers (up to ~50) in a single request
- Poll every 15 seconds → 4 calls/minute, within the limit with 1 call headroom
- If `MASSIVE_API_KEY` is not set, the built-in GBM simulator runs instead

On paid plans, reduce `poll_interval` to 2–5 seconds for near-real-time prices.
