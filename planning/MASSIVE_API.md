# Massive API Reference (formerly Polygon.io)

Documentation for the Massive REST API as used by FinAlly for real-time and end-of-day stock prices.

## Overview

Massive (rebranded from Polygon.io on October 30, 2025) provides REST and WebSocket APIs for US stock market data. FinAlly uses the REST API exclusively — simpler, works on all tiers, and sufficient for our polling model.

- **Base URL:** `https://api.massive.com/v2`
- **Python SDK:** `massive` (PyPI), current version 2.4.0, requires Python 3.9+
- **Authentication:** API key passed as Bearer token via the SDK client

## Installation

```bash
pip install -U massive
# or in pyproject.toml:
# dependencies = ["massive>=1.0.0"]
```

## Authentication

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_MASSIVE_API_KEY")
```

API keys are managed at `https://massive.com/dashboard/api-keys`.

---

## Endpoints Used by FinAlly

### 1. All Tickers Snapshot (Primary — Real-Time Prices)

The main endpoint for live price data. Returns the latest trade, quote, day bar, and previous day bar for multiple tickers in a single request.

**REST:**
```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

**Python SDK:**
```python
from massive.rest.models import SnapshotMarketType

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price} at {snap.last_trade.timestamp}")
```

**Response JSON shape:**
```json
{
  "count": 3,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "updated": 1605195918306274000,
      "todaysChange": 0.98,
      "todaysChangePerc": 0.82,
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
        "v": 270796, "vw": 120.4129, "t": 1605195900000, "n": 50
      },
      "lastTrade": {
        "p": 120.47,
        "s": 236,
        "t": 1605195918306274000,
        "x": 10,
        "c": []
      },
      "lastQuote": {
        "p": 120.46,
        "P": 120.47,
        "s": 8,
        "S": 4,
        "t": 1605195918507251700
      }
    }
  ]
}
```

**Key fields we extract:**

| SDK Path | JSON Path | Description |
|----------|-----------|-------------|
| `snap.ticker` | `tickers[].ticker` | Ticker symbol |
| `snap.last_trade.price` | `tickers[].lastTrade.p` | Latest trade price |
| `snap.last_trade.timestamp` | `tickers[].lastTrade.t` | Trade timestamp (milliseconds in SDK) |
| `snap.prev_day.close` | `tickers[].prevDay.c` | Previous day close |
| `snap.todays_change` | `tickers[].todaysChange` | Dollar change from prev close |
| `snap.todays_change_perc` | `tickers[].todaysChangePerc` | Percent change from prev close |

**Notes:**
- Omitting the `tickers` parameter returns all 10,000+ US stocks — always filter
- Snapshot data resets daily at 3:30 AM EST, repopulates around 4:00 AM EST
- The `lastTrade.t` field is nanoseconds in raw JSON, but the SDK normalizes to milliseconds

### 2. Previous Close (End-of-Day)

Returns the previous trading day's OHLCV for a single ticker.

**REST:**
```
GET /v2/aggs/ticker/AAPL/prev?adjusted=true
```

**Python SDK:**
```python
aggs = client.get_previous_close_agg(ticker="AAPL")
for agg in aggs:
    print(f"Close: {agg.close}, Volume: {agg.volume}")
```

**Response:**
```json
{
  "adjusted": true,
  "status": "OK",
  "ticker": "AAPL",
  "resultsCount": 1,
  "results": [{
    "T": "AAPL",
    "o": 115.55,
    "h": 117.59,
    "l": 114.13,
    "c": 115.97,
    "v": 131704427,
    "vw": 116.3058,
    "t": 1605042000000
  }]
}
```

**Note:** Previous close is also available in the snapshot response under `prevDay`, so a separate call is rarely needed.

### 3. Single Ticker Snapshot

Same data as the batch snapshot, for one ticker.

**REST:**
```
GET /v2/snapshot/locale/us/markets/stocks/tickers/AAPL
```

**Python SDK:**
```python
snap = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(f"{snap.ticker}: ${snap.last_trade.price}")
```

---

## Other Useful Endpoints (Not Currently Used)

| Endpoint | Description |
|----------|-------------|
| `GET /v2/aggs/grouped/locale/us/market/stocks/{date}` | All tickers OHLCV for a date (one call) |
| `GET /v2/aggs/ticker/{ticker}/range/{mult}/{span}/{from}/{to}` | Aggregate bars (e.g., 1-minute bars) |
| `GET /v2/last/trade/{ticker}` | Last trade only |
| `GET /v2/last/nbbo/{ticker}` | Last NBBO quote |

---

## Rate Limits

| Tier | Limit | Recommended Poll Interval |
|------|-------|--------------------------|
| Free | 5 requests/min | 15 seconds |
| Starter | 100 requests/min | 2–5 seconds |
| Developer | Unlimited | 1–2 seconds |

A single `get_snapshot_all()` call with a `tickers` filter counts as **one request** regardless of how many tickers are included. This makes batch polling efficient.

---

## Error Handling

| HTTP Status | Meaning | Action |
|-------------|---------|--------|
| 200 | Success | Parse response |
| 401 | Invalid API key | Log error, stop polling |
| 403 | Insufficient permissions | Log error, stop polling |
| 429 | Rate limit exceeded | Back off, retry next interval |
| 5xx | Server error | Log warning, retry next interval |

The SDK raises exceptions for non-200 responses. Our client catches all exceptions in the poll loop and retries on the next interval — the loop never crashes.

---

## Complete Working Example

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def fetch_prices():
    client = RESTClient(api_key="YOUR_KEY")
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"]

    # Fetch is synchronous — run in thread for async context
    snapshots = await asyncio.to_thread(
        client.get_snapshot_all,
        market_type=SnapshotMarketType.STOCKS,
        tickers=tickers,
    )

    for snap in snapshots:
        price = snap.last_trade.price
        ts = snap.last_trade.timestamp / 1000.0  # ms → seconds
        prev_close = snap.prev_day.close
        change_pct = snap.todays_change_perc
        print(f"{snap.ticker}: ${price:.2f} ({change_pct:+.2f}%) prev close: ${prev_close:.2f}")

asyncio.run(fetch_prices())
```
