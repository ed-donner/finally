# Massive (formerly Polygon.io) REST API Reference

## Overview

Massive provides real-time and historical stock market data via a REST API. The API was formerly at `api.polygon.io` and is now at `api.massive.com` (both work during the transition).

## Authentication

Two methods, both using the API key:

```bash
# Header (preferred)
curl -H "Authorization: Bearer YOUR_API_KEY" "https://api.massive.com/v2/..."

# Query parameter
curl "https://api.massive.com/v2/...?apiKey=YOUR_API_KEY"
```

## Rate Limits

| Tier | Rate Limit | Data Latency |
|------|-----------|--------------|
| Free | 5 req/min | 15-min delayed |
| Starter ($29/mo) | Unlimited (soft cap ~100 req/s) | Real-time |

For FinAlly: free tier polls every 15 seconds (4 req/min), safely within limits.

## Key Endpoints

### Multi-Ticker Snapshot (v2) -- Primary Endpoint for FinAlly

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

One call returns the latest price data for all requested tickers. This is the most efficient endpoint for our use case.

**Response:**

```json
{
  "count": 2,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "day": {
        "o": 190.50,
        "c": 191.20,
        "h": 192.00,
        "l": 189.80,
        "v": 45000000,
        "vw": 190.95
      },
      "min": {
        "o": 191.10,
        "c": 191.15,
        "h": 191.20,
        "l": 191.05,
        "v": 125000,
        "vw": 191.12,
        "n": 45,
        "t": 1636573458000
      },
      "prevDay": {
        "o": 189.00,
        "c": 190.30,
        "h": 190.80,
        "l": 188.50,
        "v": 52000000,
        "vw": 189.90
      },
      "lastTrade": {
        "p": 191.15,
        "s": 100,
        "x": 4,
        "c": [14, 41],
        "t": 1636573458000
      },
      "lastQuote": {
        "P": 191.16,
        "S": 200,
        "p": 191.14,
        "s": 300,
        "t": 1636573458000
      },
      "todaysChange": 0.90,
      "todaysChangePerc": 0.47,
      "updated": 1636573459000000000
    }
  ]
}
```

**Key fields we use:**
- `ticker` -- symbol
- `lastTrade.p` -- latest trade price
- `lastTrade.t` -- timestamp (Unix milliseconds)
- `prevDay.c` -- previous day close (for daily change calculation)
- `todaysChange` / `todaysChangePerc` -- daily change (pre-computed)

### Unified Snapshot (v3) -- Richer Alternative

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&type=stocks&limit=250
```

Newer endpoint with richer metadata (market status, session data, previous close in one object). Up to 250 tickers per call.

**Response (abbreviated):**

```json
{
  "status": "OK",
  "results": [
    {
      "ticker": "AAPL",
      "name": "Apple Inc.",
      "market_status": "open",
      "session": {
        "open": 190.50,
        "close": 191.20,
        "high": 192.00,
        "low": 189.80,
        "volume": 45000000,
        "previous_close": 190.30,
        "change": 0.90,
        "change_percent": 0.47
      },
      "last_trade": {
        "price": 191.15,
        "size": 100,
        "last_updated": 1675280958783136800
      },
      "last_quote": {
        "bid": 191.14,
        "ask": 191.16,
        "midpoint": 191.15
      }
    }
  ]
}
```

### Previous Day OHLC (per ticker)

```
GET /v2/aggs/ticker/AAPL/prev?adjusted=true
```

Returns previous trading day's OHLC for a single ticker. Not efficient for multiple tickers -- use the snapshot endpoint instead.

## Python Client Libraries

### `massive` package (recommended)

```bash
uv add massive
```

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")

# Multi-ticker snapshot (the call our MassiveDataSource uses)
snapshots = client.get_snapshot_all(
    market_type="stocks",
    tickers=["AAPL", "GOOGL", "MSFT"],
)
for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    # snap.last_trade.timestamp is Unix milliseconds
```

### Direct HTTP (no client library)

```python
import httpx

API_KEY = "your_key"
BASE = "https://api.massive.com"
tickers = "AAPL,GOOGL,MSFT,AMZN,TSLA,NVDA,META,JPM,V,NFLX"

resp = httpx.get(
    f"{BASE}/v2/snapshot/locale/us/markets/stocks/tickers",
    params={"tickers": tickers},
    headers={"Authorization": f"Bearer {API_KEY}"},
)
data = resp.json()

for t in data["tickers"]:
    print(f"{t['ticker']}: ${t['lastTrade']['p']:.2f}")
```

## FinAlly Integration

Our `MassiveDataSource` uses the `massive` Python client to call the v2 multi-ticker snapshot endpoint on a 15-second interval (free tier). See `backend/app/market/massive_client.py` for the implementation.
