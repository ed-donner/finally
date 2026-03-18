# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly.

> **Note:** Polygon.io rebranded as Massive.com in October 2025. All existing API keys, accounts, and integrations continue to work without modification. The legacy `api.polygon.io` base URL remains supported.

---

## Overview

| Property | Value |
|----------|-------|
| Base URL | `https://api.massive.com` (legacy: `https://api.polygon.io`) |
| Python package | `massive` (`uv add massive`) |
| Min Python | 3.9+ |
| Auth | API key via `MASSIVE_API_KEY` env var or `RESTClient(api_key=...)` |
| Auth header | `Authorization: Bearer <API_KEY>` (handled by the client) |

---

## Installation

```bash
uv add massive
```

---

## Rate Limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 requests/minute | 15 seconds |
| Paid (all tiers) | Unlimited (stay under 100 req/s) | 2–5 seconds |

FinAlly polls on a timer. On the free tier, one `list_universal_snapshots` call covering all watchlist tickers stays well within limits.

---

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

The `RESTClient` is **synchronous**. In an async context (FastAPI), wrap calls with `asyncio.to_thread()` to avoid blocking the event loop.

---

## Endpoints Used in FinAlly

### 1. Universal Snapshot — Multiple Tickers (Primary Endpoint)

Gets current price snapshots for multiple tickers in a **single API call**. This is the main endpoint for live polling.

**REST**: `GET /snapshot?type=stocks&ticker_any_of=AAPL,GOOGL,MSFT`

**Python client**:
```python
from massive import RESTClient

client = RESTClient()

# Get snapshots for specific tickers (one API call, up to 250 tickers)
snapshots = list(client.list_universal_snapshots(
    type="stocks",
    ticker_any_of=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
))

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
```

**Parameters**:
- `type` — asset type: `"stocks"`, `"crypto"`, `"fx"`, `"options"`, `"indices"`
- `ticker_any_of` — list of tickers, up to 250
- `limit` — results per page, default 10, max 250
- `order` — `"asc"` or `"desc"` (default `"asc"`)
- `sort` — field to sort by, default `"ticker"`

**Response structure** (per ticker — `UniversalSnapshot`):
```json
{
  "ticker": "AAPL",
  "type": "stocks",
  "last_trade": {
    "price": 190.50,
    "size": 100,
    "exchange": "XNYS",
    "timestamp": 1675190399000
  },
  "last_quote": {
    "bid_price": 190.49,
    "ask_price": 190.51,
    "bid_size": 500,
    "ask_size": 1000,
    "spread": 0.02,
    "timestamp": 1675190399500
  },
  "session": {
    "open": 189.00,
    "high": 192.30,
    "low": 188.50,
    "close": 190.50,
    "volume": 45231700,
    "change": 1.50,
    "change_percent": 0.79,
    "previous_close": 189.00
  }
}
```

**Key fields we extract**:
- `last_trade.price` — current price for display and trade execution
- `last_trade.timestamp` — when the price was recorded (Unix **milliseconds** → divide by 1000 for seconds)

> **Deprecation note:** The older `get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=[...])` method still works but is deprecated. Prefer `list_universal_snapshots(type="stocks", ticker_any_of=[...])`.

---

### 2. Single Ticker Snapshot

Detailed snapshot for one ticker. Useful for the detail view when a user clicks a ticker.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`

**Python client**:
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)

print(f"Price:     ${snapshot.last_trade.price}")
print(f"Bid/Ask:   ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
print(f"Day range: ${snapshot.day.low} – ${snapshot.day.high}")
print(f"Change:    {snapshot.day.change_percent:.2f}%")
print(f"Prev close: ${snapshot.day.previous_close}")
```

**Response fields** (same structure as universal snapshot, with `day` object):
- `day.open`, `day.high`, `day.low`, `day.close` — intraday OHLC
- `day.volume` — intraday volume
- `day.previous_close` — prior session close (for daily P&L calculation)
- `day.change`, `day.change_percent` — change from previous close
- `day.extended_hours` — pre/post market data (if available on plan)
- `last_trade.price` / `last_trade.timestamp` — most recent trade
- `last_quote.bid_price` / `last_quote.ask_price` — current NBBO

---

### 3. Previous Close

Previous trading day's OHLCV for a ticker. Useful for seeding prices on startup.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

**Python client**:
```python
from massive import RESTClient

client = RESTClient()

prev = client.get_previous_close_agg(ticker="AAPL", adjusted=True)

for agg in prev:
    print(f"Date:   {agg.timestamp}")          # Unix milliseconds
    print(f"Close:  ${agg.close}")
    print(f"OHLC:   O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume:,}")
```

**Response fields** (each `Agg`):
- `open`, `high`, `low`, `close` — OHLC prices
- `volume` — total volume for the day
- `timestamp` — Unix milliseconds for the start of the session
- `transactions` — number of trades
- `otc` — whether the ticker is OTC

---

### 4. Aggregate Bars (Historical OHLCV)

Historical bars at any timespan. Not needed for live polling, but useful for populating historical charts.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

**Python client**:
```python
from massive import RESTClient
from massive.rest.models import Sort

client = RESTClient()

# Daily bars for the last month
aggs = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2024-01-01",
    to="2024-01-31",
    adjusted=True,
    sort=Sort.ASC,
    limit=50000,
))

for a in aggs:
    print(f"ts={a.timestamp}  O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")

# Intraday: 5-minute bars
intraday = list(client.list_aggs(
    ticker="AAPL",
    multiplier=5,
    timespan="minute",
    from_="2024-01-15",
    to="2024-01-15",
    adjusted=True,
))
```

**Valid `timespan` values**: `"second"`, `"minute"`, `"hour"`, `"day"`, `"week"`, `"month"`, `"quarter"`, `"year"`

**Response fields** (each `Agg`):
- `timestamp` — Unix milliseconds (bar open time)
- `open`, `high`, `low`, `close` — OHLC
- `volume` — total volume in the bar
- `transactions` — number of trades in the bar
- `vwap` — volume-weighted average price (if available)

---

## How FinAlly Uses the API

The `MassiveDataSource` runs as a background asyncio task:

1. Collects all tickers from the active watchlist
2. Calls `list_universal_snapshots(type="stocks", ticker_any_of=tickers)` — **one API call**
3. Extracts `last_trade.price` and `last_trade.timestamp` from each snapshot
4. Writes to the shared `PriceCache`
5. Sleeps for `poll_interval` seconds, then repeats

On the **free tier** (5 req/min), one call every 15 seconds uses 4 req/min — safely under the limit.

```python
import asyncio
from massive import RESTClient

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0):
    """Poll Massive API and update the price cache."""
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            # RESTClient is synchronous — run in thread to avoid blocking event loop
            snapshots = await asyncio.to_thread(
                lambda: list(client.list_universal_snapshots(
                    type="stocks",
                    ticker_any_of=tickers,
                ))
            )
            for snap in snapshots:
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.timestamp / 1000.0,  # ms → seconds
                )

        await asyncio.sleep(interval)
```

---

## Error Handling

The client raises exceptions for HTTP errors:

| Status | Cause | Action |
|--------|-------|--------|
| 401 | Invalid or missing API key | Log and stop — no retry |
| 403 | Plan doesn't include endpoint | Log and stop — no retry |
| 429 | Rate limit exceeded | Log and back off — retry next interval |
| 5xx | Server error | Log — retry next interval (client retries 3x by default) |

In `MassiveDataSource._poll_once()`, all exceptions are caught and logged. The polling loop continues regardless — a failed poll just means the cache retains its last known prices until the next successful poll.

---

## Notes

- **All timestamps** from the API are Unix **milliseconds**. Divide by 1000 to get Unix seconds.
- The `list_universal_snapshots` call returns data for all requested tickers in **one request** — critical for staying within free-tier rate limits.
- During **market closed hours**, `last_trade.price` reflects the last traded price (may include after-hours).
- The `day` object **resets at 3:30 AM EST**; during pre-market, values may be from the previous session.
- `list_universal_snapshots` paginates — use `limit=250` and iterate if you have more than 250 tickers (unlikely for FinAlly).
- The `RESTClient` is **not thread-safe** for concurrent calls. FinAlly uses one client instance per poller task, which makes a single sequential call per interval — no concurrency issue.
