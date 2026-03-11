# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive stock market data API, covering the endpoints used by FinAlly.

## Overview

Polygon.io rebranded to **Massive** in late 2025. The legacy domain `api.polygon.io` still works, but the canonical base URL is now `https://api.massive.com`.

## Authentication

Two methods to pass the API key:

```
# Header (recommended)
Authorization: Bearer YOUR_API_KEY

# Query parameter
GET /v2/snapshot/...?apiKey=YOUR_API_KEY
```

## Rate Limits

| Tier | Limit | Recommended Poll Interval |
|------|-------|---------------------------|
| Free | 5 requests/minute | Every 15 seconds |
| Starter | Unlimited (soft cap ~100/sec) | Every 5 seconds |
| Developer+ | Unlimited (soft cap ~100/sec) | Every 2 seconds |

For FinAlly: the free tier allows polling every 15 seconds. This is the baseline we design around.

---

## Key Endpoints

### 1. Snapshot — Multiple Tickers (Primary Endpoint)

**This is the main endpoint FinAlly uses.** One API call returns current price data for all watched tickers.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
Authorization: Bearer YOUR_API_KEY
```

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tickers` | string | No | Comma-separated tickers. Omit for ALL tickers. |
| `include_otc` | boolean | No | Include OTC securities. Default: false |

**Response:**

```json
{
  "count": 3,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.25,
      "todaysChangePerc": 0.65,
      "updated": 1617827221349730300,
      "day": {
        "o": 190.00,
        "h": 192.50,
        "l": 189.75,
        "c": 191.25,
        "v": 54321000,
        "vw": 190.85
      },
      "prevDay": {
        "o": 189.00,
        "h": 190.50,
        "l": 188.25,
        "c": 190.00,
        "v": 48000000,
        "vw": 189.50
      },
      "min": {
        "t": 1617827160000000000,
        "o": 191.10,
        "h": 191.30,
        "l": 191.05,
        "c": 191.25,
        "v": 125000,
        "vw": 191.15,
        "n": 342,
        "av": 54321000
      },
      "lastTrade": {
        "t": 1617827221349730300,
        "x": 4,
        "p": 191.25,
        "s": 100,
        "c": [37],
        "i": "118749"
      },
      "lastQuote": {
        "t": 1617827221349730300,
        "p": 191.24,
        "s": 1,
        "P": 191.26,
        "S": 3
      }
    }
  ]
}
```

**Nested field reference:**

| Object | Fields |
|--------|--------|
| `day` / `prevDay` | `o`=open, `h`=high, `l`=low, `c`=close, `v`=volume, `vw`=VWAP |
| `min` | Same as day, plus `n`=trade count, `av`=accumulated volume, `t`=timestamp (ns) |
| `lastTrade` | `p`=price, `s`=size, `t`=SIP timestamp (ns), `x`=exchange ID |
| `lastQuote` | `p`=bid, `s`=bid size, `P`=ask, `S`=ask size, `t`=timestamp (ns) |

**What FinAlly extracts per ticker:**
- **Current price**: `lastTrade.p` (most recent trade price)
- **Previous close**: `prevDay.c`
- **Daily change**: `todaysChange` and `todaysChangePerc`
- **Timestamp**: `updated` (nanosecond epoch)

---

### 2. Previous Day Bar

Returns the previous day's OHLCV for a single ticker. Useful for seed prices on startup.

```
GET /v2/aggs/ticker/AAPL/prev
Authorization: Bearer YOUR_API_KEY
```

**Response:**

```json
{
  "adjusted": true,
  "results": [
    {
      "T": "AAPL",
      "o": 115.55,
      "h": 117.59,
      "l": 114.13,
      "c": 115.97,
      "v": 131704427,
      "vw": 116.3058,
      "t": 1605042000000
    }
  ],
  "status": "OK"
}
```

---

### 3. Grouped Daily Bars

Returns OHLCV for **every stock** on a given date. One call, entire market.

```
GET /v2/aggs/grouped/locale/us/market/stocks/2025-01-15
Authorization: Bearer YOUR_API_KEY
```

**Response:** Same shape as Previous Day Bar but with results for all tickers.

---

### 4. Last Trade

```
GET /v2/last/trade/AAPL
Authorization: Bearer YOUR_API_KEY
```

**Response:**

```json
{
  "results": {
    "T": "AAPL",
    "p": 129.8473,
    "s": 25,
    "t": 1617901342969834000,
    "x": 4
  },
  "status": "OK"
}
```

---

## Python Client Library

**Package:** `polygon-api-client` (works with the Massive API)

```bash
uv add polygon-api-client
```

### Code Examples

#### Initialize the client

```python
from polygon import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")
```

#### Get snapshots for multiple tickers (primary use case)

```python
from polygon import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")

snapshots = client.get_snapshot_all("stocks")
# Returns list of TickerSnapshot objects for ALL tickers

# Filter to specific tickers
tickers_wanted = {"AAPL", "GOOGL", "MSFT", "TSLA"}
for snap in snapshots:
    if snap.ticker in tickers_wanted:
        price = snap.last_trade.price if snap.last_trade else snap.day.close
        prev_close = snap.prev_day.close if snap.prev_day else None
        print(f"{snap.ticker}: ${price:.2f} (prev close: ${prev_close:.2f})")
```

**Note:** The Python client's `get_snapshot_all` fetches all tickers. For filtering to specific tickers, use the REST API directly with `?tickers=` param (more efficient).

#### Direct HTTP call for filtered snapshots (recommended for FinAlly)

```python
import httpx

API_KEY = "YOUR_API_KEY"
BASE_URL = "https://api.polygon.io"

async def fetch_snapshots(tickers: list[str]) -> dict[str, dict]:
    """Fetch price snapshots for specific tickers in one API call."""
    ticker_str = ",".join(tickers)
    url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            url,
            params={"tickers": ticker_str},
            headers={"Authorization": f"Bearer {API_KEY}"},
        )
        resp.raise_for_status()
        data = resp.json()

    results = {}
    for t in data.get("tickers", []):
        ticker = t["ticker"]
        last_trade = t.get("lastTrade", {})
        prev_day = t.get("prevDay", {})
        results[ticker] = {
            "price": last_trade.get("p"),
            "prev_close": prev_day.get("c"),
            "change": t.get("todaysChange"),
            "change_pct": t.get("todaysChangePerc"),
            "updated_ns": t.get("updated"),
        }
    return results
```

#### Get previous close for a single ticker

```python
prev = client.get_previous_close_agg("AAPL")
for bar in prev:
    print(f"AAPL prev close: ${bar.close:.2f}")
```

#### Get grouped daily bars (all tickers for a date)

```python
bars = client.get_grouped_daily_aggs("2025-01-15")
for bar in bars:
    print(f"{bar.ticker}: close=${bar.close:.2f}, volume={bar.volume}")
```

---

## Endpoint Selection for FinAlly

| Use Case | Endpoint | Calls |
|----------|----------|-------|
| Poll prices for watchlist (10 tickers) | `GET /v2/snapshot/...?tickers=AAPL,GOOGL,...` | 1 call per poll |
| Seed price for newly added ticker | Same snapshot endpoint (add to comma list) | 0 extra calls |
| Historical reference (stretch goal) | `GET /v2/aggs/ticker/.../range/...` | 1 call per ticker |

The snapshot endpoint is the only one needed for the core polling loop. One API call returns everything for all watched tickers.

## Error Handling

| Status | Meaning |
|--------|---------|
| 200 | Success |
| 401 | Invalid or missing API key |
| 403 | Endpoint not available on your plan |
| 429 | Rate limit exceeded (free tier: 5/min) |

On 429, the backend should back off and retry after the rate limit window resets (60 seconds for free tier). Log the event but do not crash — the SSE stream simply has a gap until the next successful poll.

## Market Hours

- Regular: 9:30 AM – 4:00 PM ET, Mon–Fri
- Extended hours data available on paid plans
- Outside market hours, snapshot data returns the most recent available prices (last close)
- The simulator runs 24/7, making it the better default for demos and development
