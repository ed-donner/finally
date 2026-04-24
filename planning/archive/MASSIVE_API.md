# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

## Overview

- **Base URL**: `https://api.massive.com` (legacy `https://api.polygon.io` still supported)
- **Python package**: `massive` (install via `pip install -U massive` / `uv add massive`)
- **Min Python version**: 3.9+
- **Auth**: API key via `MASSIVE_API_KEY` env var or passed to `RESTClient(api_key=...)`
- **Auth header**: `Authorization: Bearer <API_KEY>` (the client handles this automatically)

## Rate Limits

| Tier | Limit |
|------|-------|
| Free | 5 requests/minute |
| Paid (all tiers) | Unlimited (recommended: stay under 100 req/s) |

For FinAlly, we poll on a timer. Free tier: poll every 15s. Paid: poll every 2-5s.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

## Endpoints Used in FinAlly

### 1. Snapshot — All Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call**. This is the main endpoint we use for polling.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

**Python client**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

# Get snapshots for specific tickers (one API call)
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Day change: {snap.day.change_percent}%")
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Volume: {snap.day.volume}")
```

**Response structure** (per ticker):
```json
{
  "ticker": "AAPL",
  "day": {
    "open": 129.61,
    "high": 130.15,
    "low": 125.07,
    "close": 125.07,
    "volume": 111237700,
    "volume_weighted_average_price": 127.35,
    "previous_close": 129.61,
    "change": -4.54,
    "change_percent": -3.50
  },
  "last_trade": {
    "price": 125.07,
    "size": 100,
    "exchange": "XNYS",
    "timestamp": 1675190399000
  },
  "last_quote": {
    "bid_price": 125.06,
    "ask_price": 125.08,
    "bid_size": 500,
    "ask_size": 1000,
    "spread": 0.02,
    "timestamp": 1675190399500
  },
  "prev_daily_bar": { "...": "previous day OHLCV" },
  "minute_volume": { "...": "volume per minute" }
}
```

**Key fields we extract**:
- `last_trade.price` — current price for trading and display
- `day.previous_close` — for calculating day change
- `day.change_percent` — day change percentage
- `last_trade.timestamp` — when the price was recorded

### 2. Single Ticker Snapshot

For getting detailed data on one ticker (e.g., when user clicks a ticker for the detail view).

**Python client**:
```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"Price: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"Day range: ${snapshot.day.low} - ${snapshot.day.high}")
```

### 3. Previous Close

Gets the previous day's OHLC for a ticker. Useful for seed prices.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

**Python client**:
```python
prev = client.get_previous_close_agg(ticker="AAPL")

for agg in prev:
    print(f"Previous close: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume}")
```

**Response**:
```json
{
  "ticker": "AAPL",
  "results": [
    {
      "o": 150.0,
      "h": 155.0,
      "l": 149.0,
      "c": 154.5,
      "v": 1000000,
      "t": 1672531200000
    }
  ]
}
```

### 4. Aggregates (Bars)

Historical OHLCV bars over a date range. Not needed for live polling but useful if we add historical charts.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

**Python client**:
```python
aggs = []
for a in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
    limit=50000,
):
    aggs.append(a)

for a in aggs:
    print(f"Date: {a.timestamp}, O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

**Response** (each bar):
```json
{
  "o": 130.0,
  "h": 132.5,
  "l": 129.8,
  "c": 131.2,
  "v": 50000000,
  "t": 1672531200000
}
```

### 5. Last Trade / Last Quote

Individual endpoints for the most recent trade or NBBO quote.

```python
# Last trade
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price} x {trade.size}")

# Last NBBO quote
quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid} x {quote.bid_size}")
print(f"Ask: ${quote.ask} x {quote.ask_size}")
```

## How FinAlly Uses the API

The Massive poller runs as a background task:

1. Collects all tickers from the watchlist
2. Calls `get_snapshot_all()` with those tickers (one API call)
3. Extracts `last_trade.price` and `day.previous_close` from each snapshot
4. Writes to the shared in-memory price cache
5. Sleeps for the poll interval, then repeats

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    """Poll Massive API and update the price cache."""
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            snapshots = client.get_snapshot_all(
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snapshots:
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    previous_close=snap.day.previous_close,
                    timestamp=snap.last_trade.timestamp,
                )

        await asyncio.sleep(interval)
```

## Error Handling

The client raises exceptions for HTTP errors:
- **401**: Invalid API key
- **403**: Insufficient permissions (plan doesn't include the endpoint)
- **429**: Rate limit exceeded (free tier: 5 req/min)
- **5xx**: Server errors (client has built-in retry with 3 retries by default)

## Notes

- The snapshot endpoint returns data for **all requested tickers in one call** — this is critical for staying within rate limits on the free tier
- Timestamps from the API are Unix milliseconds
- During market closed hours, `last_trade.price` reflects the last traded price (may include after-hours)
- The `day` object resets at market open; during pre-market, values may be from the previous session
