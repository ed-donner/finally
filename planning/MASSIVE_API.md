# Massive API Reference (formerly Polygon.io)

Polygon.io rebranded to **Massive** on October 30, 2025. The API base URL is now `https://api.massive.com`, but all endpoint paths remain identical. Existing API keys and SDKs continue to work unchanged.

## Authentication

Two methods are supported on all endpoints:

1. **Query parameter**: `?apiKey=YOUR_API_KEY`
2. **Authorization header**: `Authorization: Bearer YOUR_API_KEY`

The FinAlly project uses the query parameter approach for simplicity.

**Error responses:**

- No key provided: `{"status": "ERROR", "error": "API Key was not provided"}` (HTTP 200)
- Invalid key: `{"status": "ERROR", "error": "Unknown API Key"}` (HTTP 401)

---

## Rate Limits

| Plan          | Rate Limit         | Data Access                              | Notes                |
| ------------- | ------------------ | ---------------------------------------- | -------------------- |
| **Free**      | **5 calls/minute** | End-of-day aggregates, delayed snapshots | FinAlly default tier |
| **Starter**   | Unlimited          | 15-minute delayed                        | ~$29-100/month       |
| **Developer** | Unlimited          | Real-time NBBO quotes & trades           | ~$200/month          |
| **Advanced**  | Unlimited          | Real-time + full history                 | ~$500/month          |

**Key constraint for FinAlly**: The free tier allows 5 API calls per minute. The snapshot endpoint (below) is the most efficient way to use this budget since it returns data for all tickers in a single call.

---

## Endpoints Used by FinAlly

### 1. Snapshot — Multiple Tickers (Primary Polling Endpoint)

**`GET /v2/snapshot/locale/us/markets/stocks/tickers`**

Returns a comprehensive snapshot for multiple US stock tickers in a **single API call**. This is the batch endpoint — far more efficient than per-ticker quote calls under rate limits.

#### Query Parameters

| Parameter     | Type    | Required | Default | Description                                                             |
| ------------- | ------- | -------- | ------- | ----------------------------------------------------------------------- |
| `tickers`     | string  | No       | all     | Comma-separated, case-sensitive ticker symbols (e.g., `AAPL,TSLA,GOOG`) |
| `include_otc` | boolean | No       | false   | Include OTC securities                                                  |
| `apiKey`      | string  | Yes      | —       | API key                                                                 |

#### Response

```json
{
  "status": "OK",
  "request_id": "abc123",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 0.8,
      "todaysChangePerc": 0.421,
      "updated": 1680000000000000000,
      "day": {
        "o": 189.5,
        "h": 191.25,
        "l": 188.9,
        "c": 190.8,
        "v": 58432100,
        "vw": 189.75
      },
      "prevDay": {
        "o": 188.0,
        "h": 190.5,
        "l": 187.75,
        "c": 190.0,
        "v": 62100000,
        "vw": 189.1
      },
      "min": {
        "o": 190.75,
        "h": 190.82,
        "l": 190.7,
        "c": 190.8,
        "v": 125000,
        "vw": 190.76,
        "av": 45000000,
        "n": 1250
      },
      "lastQuote": {
        "P": 190.82,
        "p": 190.8,
        "S": 3,
        "s": 2,
        "t": 1680000000000000000
      },
      "lastTrade": {
        "p": 190.81,
        "s": 100,
        "t": 1680000000000000000,
        "x": 11,
        "i": "trade123",
        "c": [14, 41]
      }
    }
  ]
}
```

#### Field Reference

**Top-level per ticker:**

| Field              | Type    | Description                           |
| ------------------ | ------- | ------------------------------------- |
| `ticker`           | string  | Ticker symbol                         |
| `todaysChange`     | number  | Dollar change from previous close     |
| `todaysChangePerc` | number  | Percentage change from previous close |
| `updated`          | integer | Last update timestamp (nanoseconds)   |

**`day` / `prevDay` (aggregate bars):**

| Field | Type   | Description                   |
| ----- | ------ | ----------------------------- |
| `o`   | number | Open price                    |
| `h`   | number | High price                    |
| `l`   | number | Low price                     |
| `c`   | number | Close price                   |
| `v`   | number | Volume                        |
| `vw`  | number | Volume-weighted average price |

**`min` (most recent minute bar):**

| Field              | Type   | Description                    |
| ------------------ | ------ | ------------------------------ |
| `o`, `h`, `l`, `c` | number | OHLC for the minute            |
| `v`                | number | Volume                         |
| `vw`               | number | VWAP                           |
| `av`               | number | Accumulated volume for the day |
| `n`                | number | Number of trades               |

**`lastQuote` (most recent NBBO):**

| Field | Type   | Description               |
| ----- | ------ | ------------------------- |
| `p`   | number | **Bid price** (lowercase) |
| `P`   | number | **Ask price** (uppercase) |
| `s`   | number | Bid size in round lots    |
| `S`   | number | Ask size in round lots    |
| `t`   | number | Timestamp (nanoseconds)   |

**`lastTrade`:**

| Field | Type   | Description             |
| ----- | ------ | ----------------------- |
| `p`   | number | Trade price             |
| `s`   | number | Trade size              |
| `t`   | number | Timestamp (nanoseconds) |

**Note:** `lastQuote` and `lastTrade` fields require a Developer plan or higher. On the free tier, these may be absent — use `day.c` as the current price and `prevDay.c` as the previous close instead.

#### Code Example

```python
import httpx

async def fetch_snapshots(tickers: list[str], api_key: str) -> dict:
    """Fetch snapshot data for multiple tickers in one call."""
    ticker_str = ",".join(tickers)
    url = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"tickers": ticker_str, "apiKey": api_key}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "OK":
        raise ValueError(f"API error: {data.get('error', 'Unknown')}")

    result = {}
    for t in data.get("tickers", []):
        ticker = t["ticker"]
        # Prefer lastQuote midpoint if available, fall back to day close
        if "lastQuote" in t and t["lastQuote"].get("p") and t["lastQuote"].get("P"):
            price = (t["lastQuote"]["p"] + t["lastQuote"]["P"]) / 2
        elif "day" in t and t["day"].get("c"):
            price = t["day"]["c"]
        else:
            continue

        prev_close = t.get("prevDay", {}).get("c")
        result[ticker] = {
            "price": price,
            "prev_close": prev_close,
            "day_change": t.get("todaysChange"),
            "day_change_pct": t.get("todaysChangePerc"),
        }

    return result
```

---

### 2. Snapshot — Single Ticker

**`GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`**

Same data as the batch endpoint but for a single ticker. Useful for on-demand price seeding when a trade is requested for a ticker not currently being tracked.

#### Response

```json
{
  "status": "OK",
  "request_id": "abc123",
  "ticker": {
    "ticker": "AAPL",
    "todaysChange": 0.8,
    "todaysChangePerc": 0.421,
    "day": {
      "o": 189.5,
      "h": 191.25,
      "l": 188.9,
      "c": 190.8,
      "v": 58432100,
      "vw": 189.75
    },
    "prevDay": {
      "o": 188.0,
      "h": 190.5,
      "l": 187.75,
      "c": 190.0,
      "v": 62100000,
      "vw": 189.1
    },
    "lastQuote": {
      "P": 190.82,
      "p": 190.8,
      "S": 3,
      "s": 2,
      "t": 1680000000000000000
    },
    "lastTrade": { "p": 190.81, "s": 100, "t": 1680000000000000000 }
  }
}
```

Note: Response uses `"ticker"` (singular object) instead of `"tickers"` (array).

#### Code Example

```python
async def fetch_single_snapshot(ticker: str, api_key: str) -> dict:
    """Fetch snapshot for a single ticker (used for on-demand price seeding)."""
    url = f"https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}"
    params = {"apiKey": api_key}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()

    if data.get("status") != "OK":
        raise ValueError(f"API error: {data.get('error', 'Unknown')}")

    t = data["ticker"]
    if "lastQuote" in t and t["lastQuote"].get("p") and t["lastQuote"].get("P"):
        price = (t["lastQuote"]["p"] + t["lastQuote"]["P"]) / 2
    elif "day" in t and t["day"].get("c"):
        price = t["day"]["c"]
    else:
        raise ValueError(f"No price data available for {ticker}")

    return {
        "price": price,
        "prev_close": t.get("prevDay", {}).get("c"),
        "day_change": t.get("todaysChange"),
        "day_change_pct": t.get("todaysChangePerc"),
    }
```

---

### 3. Quotes — Single Ticker (Alternative)

**`GET /v3/quotes/{stockTicker}`**

Returns NBBO quote history. Use `?limit=1` to get just the latest quote. This endpoint has **no batch capability** — one ticker per request. The snapshot endpoint above is preferred for polling.

#### Query Parameters

| Parameter     | Type          | Required | Default | Description                         |
| ------------- | ------------- | -------- | ------- | ----------------------------------- |
| `stockTicker` | string (path) | Yes      | —       | Case-sensitive ticker symbol        |
| `limit`       | integer       | No       | 1000    | Number of results (max 50,000)      |
| `timestamp`   | string        | No       | —       | Date or nanosecond timestamp filter |
| `order`       | string        | No       | —       | `asc` or `desc`                     |
| `sort`        | string        | No       | —       | Field to sort by                    |
| `apiKey`      | string        | Yes      | —       | API key                             |

#### Response

```json
{
  "status": "OK",
  "request_id": "abc123",
  "results": [
    {
      "bid_price": 190.25,
      "ask_price": 190.27,
      "bid_size": 2,
      "ask_size": 3,
      "participant_timestamp": 1680000000000000000,
      "sip_timestamp": 1680000000001000000,
      "sequence_number": 12345,
      "tape": 3,
      "conditions": [1],
      "indicators": []
    }
  ]
}
```

**Price derivation**: `(bid_price + ask_price) / 2`

**Note**: Field names here use full snake_case (`bid_price`, `ask_price`), unlike the snapshot endpoint which uses abbreviated single-letter keys (`p`, `P`) in `lastQuote`.

---

### 4. Previous Close (Alternative)

**`GET /v2/aggs/ticker/{stocksTicker}/prev`**

Returns the previous trading day's OHLCV bar. The snapshot endpoint includes this data in the `prevDay` field, so this endpoint is only needed if snapshots aren't available.

#### Response

```json
{
  "ticker": "AAPL",
  "adjusted": true,
  "queryCount": 1,
  "resultsCount": 1,
  "status": "OK",
  "results": [
    {
      "T": "AAPL",
      "o": 189.5,
      "h": 191.25,
      "l": 188.9,
      "c": 190.0,
      "v": 62100000,
      "vw": 189.1,
      "t": 1680000000000,
      "n": 524301
    }
  ]
}
```

The `c` (close) field is the previous close price used for daily change calculations.

---

## Recommended Strategy for FinAlly

### Free Tier (5 calls/min)

Use the **batch snapshot endpoint** as the primary polling mechanism:

1. **Every 12 seconds** (= 5 calls/min), call the batch snapshot with all tracked tickers
2. Extract price from `lastQuote` midpoint (if available) or `day.c` as fallback
3. Extract `prevDay.c` for previous close (fetched with every snapshot — always fresh)
4. Use `todaysChange` and `todaysChangePerc` directly for daily change values

This gives all tracked tickers a price update every 12 seconds using 100% of the rate limit budget. Far better than the round-robin per-ticker approach which would update each ticker only every 2 minutes with 10 tickers.

### On-Demand Price Seeding

When a trade is requested for a ticker not in the price cache, use the **single ticker snapshot** endpoint to fetch its current price before executing the trade.

### Paid Tiers

With unlimited calls, poll the batch snapshot endpoint every 2-5 seconds for near-real-time updates across all tickers.
