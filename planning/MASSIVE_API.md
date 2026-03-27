# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

## Overview

Polygon.io rebranded to **Massive** on October 30, 2025. The API base URL changed to `https://api.massive.com`. The old `https://api.polygon.io` redirects to the same infrastructure and remains functional during the transition period. All existing API keys and integrations continue to work unchanged.

- **Base URL**: `https://api.massive.com`
- **Legacy URL**: `https://api.polygon.io` (redirects, still works)
- **Python package**: `massive` — install with `pip install -U massive` or `uv add massive`
- **Min Python**: 3.9+
- **Auth**: API key passed to `RESTClient(api_key=...)` or set as `MASSIVE_API_KEY` env var

## Authentication

Two methods are supported:

**Query parameter** (simple, for raw HTTP):
```
GET https://api.massive.com/v2/snapshot/.../tickers?apiKey=YOUR_KEY
```

**Bearer header** (recommended):
```
Authorization: Bearer YOUR_KEY
```

The official Python SDK uses the Bearer header automatically.

## Rate Limits

| Tier | Limit |
|------|-------|
| Free | 5 requests / minute |
| Any paid plan | Unlimited (stay under ~100 req/s) |

**FinAlly strategy:**
- Free tier: call snapshot endpoint every **15 seconds** (4 calls/min, leaving headroom)
- Paid tier: poll every **2–5 seconds**

The key insight: the snapshot endpoint returns all tickers in **one API call**, so even with 10+ tickers you consume only 1 request per poll cycle.

## Python Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

---

## Endpoints Used in FinAlly

### 1. Full Market Snapshot — Multiple Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call**. This is the main endpoint for FinAlly's polling loop.

**REST:**
```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

**Python SDK:**
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price:.2f}")
    print(f"  Day change: {snap.day.change_percent:.2f}%")
    print(f"  OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Volume: {snap.day.volume:,}")
    print(f"  Updated: {snap.last_trade.timestamp}")
```

**Raw HTTP example:**
```python
import requests

def get_snapshots_raw(tickers: list[str], api_key: str) -> dict[str, float]:
    """Returns {ticker: current_price} for all requested tickers."""
    url = "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers"
    resp = requests.get(url, params={
        "tickers": ",".join(tickers),
        "apiKey": api_key,
    })
    resp.raise_for_status()
    data = resp.json()

    prices = {}
    for t in data.get("tickers", []):
        ticker = t["ticker"]
        # last trade price is most current; fall back to day close
        price = (t.get("lastTrade") or {}).get("p") or (t.get("day") or {}).get("c")
        if price:
            prices[ticker] = price
    return prices
```

**Response structure** (per ticker, from raw API):
```json
{
  "ticker": "AAPL",
  "day": {
    "o": 129.61,
    "h": 130.15,
    "l": 125.07,
    "c": 125.07,
    "v": 111237700,
    "vw": 127.35
  },
  "lastTrade": {
    "p": 125.07,
    "s": 100,
    "t": 1675190399000000000
  },
  "lastQuote": {
    "P": 125.08,
    "p": 125.06,
    "S": 10,
    "s": 8,
    "t": 1675190399500000000
  },
  "prevDay": {
    "o": 129.61,
    "c": 129.61,
    "h": 130.00,
    "l": 128.50
  },
  "todaysChange": -4.54,
  "todaysChangePerc": -3.50,
  "updated": 1675190399000000000
}
```

**Key fields:**
| Raw field | SDK attribute | Description |
|---|---|---|
| `lastTrade.p` | `last_trade.price` | Current/most recent traded price |
| `day.c` | `day.close` | Running day close price |
| `prevDay.c` | `prev_daily_bar.close` | Previous day's close (for computing day change %) |
| `todaysChangePerc` | `today_change_percent` | % change from previous close |
| `lastTrade.t` | `last_trade.timestamp` | Timestamp in nanoseconds (raw) / milliseconds (SDK) |

---

### 2. Single Ticker Snapshot

For fetching detailed data on one specific ticker (e.g., when user clicks for detail view).

**REST:**
```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

**Python SDK:**
```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"Price: ${snapshot.last_trade.price:.2f}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price:.2f} / ${snapshot.last_quote.ask_price:.2f}")
print(f"Day range: ${snapshot.day.low:.2f} – ${snapshot.day.high:.2f}")
print(f"Prev close: ${snapshot.prev_daily_bar.close:.2f}")
```

---

### 3. Previous Day Bar (End-of-Day Close)

Gets the previous trading day's OHLCV for a ticker. Useful for seeding the simulator or establishing baseline prices at startup.

**REST:**
```
GET /v2/aggs/ticker/{ticker}/prev?adjusted=true
```

**Python SDK:**
```python
prev = client.get_previous_close_agg(ticker="AAPL")

for agg in prev:
    print(f"Prev close: ${agg.close:.2f}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume:,}")
    print(f"Date: {agg.timestamp}")  # milliseconds UTC
```

**Raw response:**
```json
{
  "ticker": "AAPL",
  "resultsCount": 1,
  "results": [
    {
      "T": "AAPL",
      "o": 150.0,
      "h": 155.0,
      "l": 149.0,
      "c": 154.5,
      "v": 80000000,
      "vw": 152.3,
      "t": 1672531200000
    }
  ]
}
```

---

### 4. Aggregate Bars (OHLCV History)

Historical OHLCV bars over a date range. Needed if we add a historical price chart to the detail view.

**REST:**
```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
```

Parameters:
- `multiplier`: integer (e.g., `1`, `5`)
- `timespan`: `minute`, `hour`, `day`, `week`, `month`
- `from` / `to`: `YYYY-MM-DD` or millisecond timestamp
- `adjusted`: `true` (default) — adjusts for splits/dividends
- `sort`: `asc` or `desc`
- `limit`: max 50,000

**Python SDK (daily bars):**
```python
aggs = []
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2025-01-01",
    to="2026-01-01",
    adjusted=True,
    sort="asc",
    limit=50000,
):
    aggs.append(bar)

for bar in aggs:
    print(f"Date: {bar.timestamp}  O={bar.open} H={bar.high} L={bar.low} C={bar.close}  V={bar.volume:,}")
```

**Python SDK (minute bars — intraday):**
```python
for bar in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="minute",
    from_="2026-03-26",
    to="2026-03-27",
    adjusted=True,
    sort="asc",
):
    ...  # bar.timestamp is milliseconds UTC
```

**Raw bar structure:**
```json
{
  "o": 130.0,
  "h": 132.5,
  "l": 129.8,
  "c": 131.2,
  "v": 50000000,
  "vw": 130.9,
  "n": 42831,
  "t": 1672531200000
}
```

Fields: `o`=open, `h`=high, `l`=low, `c`=close, `v`=volume, `vw`=VWAP, `n`=number of trades, `t`=timestamp (ms UTC).

---

### 5. Last Trade / Last Quote

Individual endpoints for most-recent trade or NBBO quote. Generally not needed for FinAlly since the snapshot endpoint includes this data, but useful for spot-checks.

```python
# Last trade
trade = client.get_last_trade(ticker="AAPL")
print(f"Last: ${trade.price:.2f} × {trade.size} shares")

# Last NBBO quote
quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid:.2f} × {quote.bid_size}")
print(f"Ask: ${quote.ask:.2f} × {quote.ask_size}")
```

---

## How FinAlly Uses the API

The Massive poller runs as an async background task:

1. Read the current watchlist tickers from the database
2. Call `get_snapshot_all(tickers=watchlist)` — **one API call for all tickers**
3. Extract `last_trade.price` and `last_trade.timestamp` per ticker
4. Write into the shared `PriceCache`
5. Sleep for the poll interval, then repeat

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    """Poll Massive API and update the shared price cache."""
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            try:
                snapshots = await asyncio.to_thread(
                    client.get_snapshot_all,
                    market_type=SnapshotMarketType.STOCKS,
                    tickers=tickers,
                )
                for snap in snapshots:
                    if snap.last_trade and snap.last_trade.price:
                        price_cache.update(
                            ticker=snap.ticker,
                            price=snap.last_trade.price,
                            timestamp=snap.last_trade.timestamp / 1000,  # ms → seconds
                        )
            except Exception as e:
                # Log and continue — polling loop must not crash
                print(f"Massive poll error: {e}")

        await asyncio.sleep(interval)
```

---

## Error Handling

The SDK raises exceptions for HTTP errors:

| Status | Meaning | Action |
|--------|---------|--------|
| 401 | Invalid API key | Log and abort polling |
| 403 | Insufficient plan permissions | Log and fall back to simulator |
| 429 | Rate limit exceeded | Back off exponentially; log warning |
| 5xx | Server error | SDK retries 3x automatically; then log and continue |

Handle 403 specifically: if the user's plan doesn't support the snapshot endpoint, fall back to the simulator rather than crashing.

---

## Notes on Timestamps

- `lastTrade.t` in raw JSON is **nanoseconds** UTC
- SDK attribute `last_trade.timestamp` is **milliseconds** UTC
- Convert to Unix seconds for the `PriceCache`: `timestamp_ms / 1000`

## Market Hours Notes

- Snapshot data resets daily at ~3:30 AM EST
- Pre-market data starts populating from ~4:00 AM EST
- Outside trading hours, `last_trade.price` reflects the last traded price (may include after-hours trades)
- `day` object values reflect the current session; during pre-market they may be from the previous session

## WebSocket Streaming (Paid Tiers Only)

Not used in FinAlly (REST polling is sufficient and simpler), but available for future enhancement:

```python
from massive import WebSocketClient

ws = WebSocketClient(
    api_key="YOUR_KEY",
    subscriptions=["T.AAPL", "T.MSFT", "AM.*"],  # trades or minute aggregates
)

def handle_msg(msgs):
    for m in msgs:
        print(m)  # Trade or aggregate update

ws.run(handle_msg=handle_msg)
```

Subscription prefixes: `T.` = trades, `Q.` = quotes, `AM.` = per-minute aggregates, `A.` = per-second aggregates. Use `T.*` or `AM.*` for all tickers.
