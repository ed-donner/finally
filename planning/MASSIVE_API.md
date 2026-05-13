# Massive API (formerly Polygon.io) — Reference Documentation

## Overview

Massive (rebranded from Polygon.io on October 30, 2025) provides REST and WebSocket APIs for real-time and historical US stock market data. Existing Polygon.io API keys and integrations continue to work without changes.

- **Docs:** https://massive.com/docs
- **Base URL:** `https://api.massive.com` (legacy `https://api.polygon.io` still supported)
- **Python client:** `pip install -U massive`

---

## Authentication

All requests require an API key. Pass it as a Bearer token in the `Authorization` header (preferred) or as a query parameter:

```python
# Header (preferred)
headers = {"Authorization": "Bearer YOUR_API_KEY"}

# Query parameter (simpler for manual testing)
params = {"apiKey": "YOUR_API_KEY"}
```

---

## Rate Limits

| Plan | Rate Limit | Data Freshness |
|---|---|---|
| Free / Developer | 5 requests/minute | 15-minute delayed |
| Starter | Unlimited | 15-minute delayed |
| Advanced | Unlimited | Real-time |
| Business | Unlimited | Real-time + Fair Market Value |

**Implication for FinAlly:** On the free tier, poll no faster than every 15 seconds (4 tickers per call, 3 calls needed for 10 tickers = 3 req/15s is well within 5/min). On a paid plan, poll every 2–5 seconds.

---

## Key Endpoints for FinAlly

### 1. Snapshot — Multiple Tickers (primary real-time endpoint)

Retrieves the latest price data for a comma-separated list of tickers in a single request. This is the main endpoint used by the FinAlly polling loop.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,GOOGL
```

**Query parameters:**

| Parameter | Type | Required | Description |
|---|---|---|---|
| `tickers` | string | No | Comma-separated ticker list. Omit to return all ~10,000 tickers. |
| `include_otc` | boolean | No | Include OTC securities; defaults to false. |

**Response:**

```json
{
  "status": "OK",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.23,
      "todaysChangePerc": 0.65,
      "updated": 1605195918306274000,
      "day": {
        "o": 189.30,
        "h": 191.05,
        "l": 188.82,
        "c": 190.54,
        "v": 52341000,
        "vw": 190.12
      },
      "prevDay": {
        "o": 188.10,
        "h": 189.90,
        "l": 187.35,
        "c": 189.31,
        "v": 48200000,
        "vw": 188.75
      },
      "min": {
        "o": 190.48,
        "h": 190.60,
        "l": 190.40,
        "c": 190.54,
        "v": 145200,
        "vw": 190.51
      },
      "lastTrade": { "p": 190.54, "s": 100, "t": 1605195918306274000 },
      "lastQuote": { "P": 190.55, "p": 190.54, "t": 1605195918507251700 }
    }
  ]
}
```

**Field reference:**

| Field | Description |
|---|---|
| `todaysChange` | Dollar change vs. previous close |
| `todaysChangePerc` | Percentage change vs. previous close |
| `updated` | Nanosecond Unix timestamp of last update |
| `day.c` | Current day's latest price (closing or most recent) |
| `day.o` / `h` / `l` | Day's open / high / low |
| `day.v` | Volume today |
| `prevDay.c` | Previous close — use as daily open reference |
| `min.c` | Most recent minute's closing price |
| `lastTrade.p` | Most recent trade price |

**Best field to use as "current price":** `lastTrade.p` (most recent trade, plan-dependent) or `day.c` (current day bar close, always available).

---

### 2. Snapshot — Single Ticker

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

Same response schema as above but for one ticker. Use when you need a single ticker's data outside the main polling loop.

**Example:**
```python
import requests

response = requests.get(
    "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/AAPL",
    headers={"Authorization": "Bearer YOUR_API_KEY"}
)
data = response.json()
price = data["ticker"]["day"]["c"]
change_pct = data["ticker"]["todaysChangePerc"]
```

---

### 3. Custom Bars (OHLC) — Historical Data

Retrieves OHLCV bars over a custom date/time range. Use this to seed chart history or back-test.

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
```

**Path parameters:**

| Parameter | Type | Description |
|---|---|---|
| `ticker` | string | Ticker symbol (e.g., `AAPL`) |
| `multiplier` | integer | Time unit multiplier (e.g., `1` for 1-day bars) |
| `timespan` | string | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` |
| `from` | string | Start date `YYYY-MM-DD` or Unix millisecond timestamp |
| `to` | string | End date `YYYY-MM-DD` or Unix millisecond timestamp |

**Query parameters:**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `adjusted` | boolean | true | Adjust for stock splits |
| `sort` | string | `asc` | `asc` or `desc` by timestamp |
| `limit` | integer | 5000 | Max results per page (max 50000) |

**Response:**

```json
{
  "ticker": "AAPL",
  "status": "OK",
  "adjusted": true,
  "queryCount": 30,
  "resultsCount": 30,
  "results": [
    {
      "o": 189.30,
      "h": 191.05,
      "l": 188.82,
      "c": 190.54,
      "v": 52341000,
      "vw": 190.12,
      "n": 412305,
      "t": 1700006400000
    }
  ],
  "next_url": "https://api.massive.com/v2/aggs/..."
}
```

| Field | Description |
|---|---|
| `o` / `h` / `l` / `c` | Open / High / Low / Close |
| `v` | Volume |
| `vw` | Volume-weighted average price |
| `n` | Number of transactions in the bar |
| `t` | Bar start time (Unix milliseconds) |

---

### 4. Previous Day Bar

Retrieves the prior trading day's OHLCV for a single ticker. Useful as a fallback when the snapshot hasn't populated `prevDay` yet (early pre-market).

```
GET /v2/aggs/ticker/{ticker}/prev
```

**Response:**
```json
{
  "status": "OK",
  "ticker": "AAPL",
  "results": [{
    "T": "AAPL",
    "o": 188.10,
    "h": 189.90,
    "l": 187.35,
    "c": 189.31,
    "v": 48200000,
    "vw": 188.75,
    "t": 1700006400000
  }]
}
```

---

### 5. Daily Ticker Summary (Open/Close)

Returns OHLCV for a specific historical date, including pre-market and after-hours prices.

```
GET /v1/open-close/{ticker}/{date}
```

**Response:**
```json
{
  "status": "OK",
  "symbol": "AAPL",
  "from": "2023-01-09",
  "open": 130.47,
  "high": 133.41,
  "low": 129.89,
  "close": 130.15,
  "volume": 70790813,
  "preMarket": 130.16,
  "afterHours": 130.40
}
```

---

## Python Client — Official Library

The official `massive` Python package wraps all REST endpoints and handles pagination automatically.

### Installation

```bash
pip install -U massive
```

### Client Setup

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")
```

### Get Snapshot for Multiple Tickers

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_API_KEY")

# Fetch snapshot for specific tickers
snapshot = client.get_snapshot_all(
    market_type="stocks",
    tickers=["AAPL", "MSFT", "GOOGL", "NVDA"]
)

for ticker_data in snapshot:
    symbol = ticker_data.ticker
    price = ticker_data.day.c           # current day close
    prev_close = ticker_data.prev_day.c # previous day close
    change_pct = ticker_data.todays_change_perc
    print(f"{symbol}: ${price:.2f}  ({change_pct:+.2f}%)")
```

### Get Historical Daily Bars

```python
from datetime import date, timedelta

today = date.today()
one_year_ago = today - timedelta(days=365)

aggs = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_=one_year_ago.isoformat(),
    to=today.isoformat(),
    limit=50000
):
    aggs.append(bar)

# Each bar: bar.open, bar.high, bar.low, bar.close, bar.volume, bar.timestamp
```

### Get Previous Day Close

```python
result = client.get_previous_close_agg("AAPL")
prev_close = result[0].close
```

---

## Raw HTTP Example (no client library)

For use with `httpx` or `aiohttp` in async code:

```python
import httpx

BASE_URL = "https://api.massive.com"

async def fetch_snapshots(api_key: str, tickers: list[str]) -> dict:
    url = f"{BASE_URL}/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"tickers": ",".join(tickers)}
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()

async def fetch_daily_bars(api_key: str, ticker: str, from_date: str, to_date: str) -> list[dict]:
    url = f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/day/{from_date}/{to_date}"
    params = {"adjusted": "true", "sort": "asc", "limit": 5000}
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient() as client:
        response = await client.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])
```

---

## Polling Strategy for FinAlly

The SSE stream pushes price updates every ~500ms. The Massive poller must feed the same price cache at a lower cadence:

| Plan | Poll Interval | Behavior |
|---|---|---|
| Free (5 req/min) | 15 seconds | One request per poll covers all 10 default tickers |
| Starter+ (unlimited) | 2–5 seconds | Closer to real-time; still REST not WebSocket |

Recommended implementation: a single background `asyncio` task that polls the All Tickers Snapshot endpoint with the current watchlist as the `tickers` query parameter. Because the watchlist may change at runtime, the task should re-read the tickers list on each poll iteration from the shared price cache registry.

---

## Error Handling

| HTTP Status | Meaning | Action |
|---|---|---|
| 200 | OK | Parse and cache |
| 403 | Bad/missing API key | Log error, do not retry |
| 429 | Rate limit exceeded | Exponential back-off; double poll interval |
| 500 / 503 | Server error | Retry after 5 seconds |

```python
import asyncio

async def safe_poll(api_key: str, tickers: list[str]) -> dict | None:
    try:
        data = await fetch_snapshots(api_key, tickers)
        if data.get("status") == "OK":
            return data
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 429:
            await asyncio.sleep(60)  # back off on rate limit
        else:
            await asyncio.sleep(5)
    except httpx.RequestError:
        await asyncio.sleep(5)
    return None
```
