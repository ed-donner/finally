# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

---

## Overview

| | |
|---|---|
| **Base URL** | `https://api.massive.com` (legacy `https://api.polygon.io` still supported) |
| **Python package** | `massive` — install via `uv add massive` or `pip install -U massive` |
| **Min Python** | 3.9+ |
| **Auth** | API key passed to `RESTClient(api_key=...)` or read from `MASSIVE_API_KEY` env var |
| **Auth header** | `Authorization: Bearer <API_KEY>` (client handles this automatically) |

---

## Rate Limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 requests/minute | 15 seconds |
| Paid (all tiers) | Unlimited (stay under 100 req/s) | 2–5 seconds |

FinAlly defaults to a 15-second poll interval, safe for free-tier users. Paid-tier users can reduce this via `poll_interval` on `MassiveDataSource`.

---

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass the key explicitly
client = RESTClient(api_key="your_key_here")
```

The `RESTClient` is **synchronous**. In an async FastAPI context, always wrap calls in `asyncio.to_thread()` to avoid blocking the event loop:

```python
import asyncio

snapshots = await asyncio.to_thread(
    client.get_snapshot_all,
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL"],
)
```

---

## Endpoints Used in FinAlly

### 1. Snapshot — All Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call**. This is the only endpoint FinAlly calls during normal operation.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

**Python client**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Timestamp: {snap.last_trade.timestamp}")   # Unix milliseconds
    print(f"  Day change %: {snap.day.change_percent}")
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low}")
    print(f"  Previous close: {snap.day.previous_close}")
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

**Fields extracted by FinAlly**:
- `last_trade.price` — current price written to `PriceCache`
- `last_trade.timestamp` — Unix **milliseconds**, divided by 1000 to get seconds for `PriceCache`
- `day.previous_close` — available but not currently used (FinAlly tracks change from previous cache entry)
- `day.change_percent` — available for future use

---

### 2. Single Ticker Snapshot

For detailed data on one ticker (e.g., on demand when user clicks a ticker).

```python
from massive.rest.models import SnapshotMarketType

snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"Price: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"Day range: ${snapshot.day.low} – ${snapshot.day.high}")
```

---

### 3. Previous Close

Gets the previous day's OHLCV for a ticker. Useful for seeding prices on startup.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

```python
for agg in client.get_previous_close_agg(ticker="AAPL"):
    print(f"Previous close: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume}")
```

---

### 4. Aggregates (Historical Bars)

Historical OHLCV bars for a date range. Not used for live polling; useful for charting historical price data.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

```python
aggs = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
    limit=50000,
))

for a in aggs:
    print(f"Date: {a.timestamp}, O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

---

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

---

## How FinAlly Polls

The `MassiveDataSource` runs as a background asyncio task:

1. On `start()`, immediately calls `_poll_once()` to seed the cache before the first SSE push
2. Launches `_poll_loop()` as an asyncio background task
3. Each poll cycle: calls `get_snapshot_all()` in a thread pool (non-blocking), iterates snapshots, writes `price` and `timestamp` to `PriceCache`
4. Sleeps for `poll_interval` seconds, then repeats
5. On `stop()`, cancels the background task

```python
import asyncio
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

async def poll_massive(api_key: str, tickers: list[str], price_cache, interval: float = 15.0):
    client = RESTClient(api_key=api_key)

    async def poll_once():
        snapshots = await asyncio.to_thread(
            client.get_snapshot_all,
            market_type=SnapshotMarketType.STOCKS,
            tickers=tickers,
        )
        for snap in snapshots:
            try:
                price = snap.last_trade.price
                timestamp = snap.last_trade.timestamp / 1000.0  # ms → seconds
                price_cache.update(ticker=snap.ticker, price=price, timestamp=timestamp)
            except (AttributeError, TypeError):
                pass  # Skip malformed snapshots

    await poll_once()  # Immediate seed
    while True:
        await asyncio.sleep(interval)
        await poll_once()
```

---

## Error Handling

The client raises exceptions for HTTP errors. `MassiveDataSource` catches all exceptions in `_poll_once()` and logs them without crashing — the loop retries on the next interval.

| Status | Cause | FinAlly behavior |
|--------|-------|-----------------|
| 401 | Invalid API key | Logged as error; cache stops updating |
| 403 | Plan doesn't include endpoint | Logged as error |
| 429 | Rate limit exceeded | Logged as error; retries after interval |
| 5xx | Server error | Client has built-in retry (3 attempts) |
| Network error | Connectivity issue | Logged as error; retries after interval |

---

## Important Notes

- The snapshot endpoint fetches **all requested tickers in one API call** — critical for staying within rate limits on the free tier
- Timestamps from the API are **Unix milliseconds** — divide by 1000 before passing to `PriceCache`
- During market-closed hours, `last_trade.price` reflects the last traded price (may include after-hours trades)
- The `day` object resets at market open; during pre-market, values may be from the previous session
- `get_snapshot_all()` returns only tickers that have data — if a ticker is unknown or has no trades, it may be absent from the response (handle with care)
