# Massive API (formerly Polygon.io) — Reference

Massive.com rebranded from Polygon.io on October 30, 2025. The Python client package is `massive` (legacy: `polygon-api-client`, still supported).

---

## Installation

```bash
uv add massive
```

---

## Authentication

```python
from massive import RESTClient

# Explicit key
client = RESTClient(api_key="YOUR_API_KEY")

# Or reads MASSIVE_API_KEY env var automatically
client = RESTClient()
```

---

## Batch Snapshot — Multiple Tickers

The primary method for fetching prices for a set of tickers in one call.

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.Stocks,
    tickers=["AAPL", "GOOGL", "MSFT", "TSLA"],
)
```

**Underlying endpoint:** `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,...`

The method is **synchronous** (blocking). In an asyncio context, run it in a thread:

```python
import asyncio

snapshots = await asyncio.to_thread(
    client.get_snapshot_all,
    SnapshotMarketType.Stocks,
    tickers,
)
```

---

## Response Model — `TickerSnapshot`

Each element of the returned list is a `TickerSnapshot`:

```python
for snap in snapshots:
    snap.ticker                  # str   — "AAPL"
    snap.last_trade.price        # float — latest trade price (aliased from .p)
    snap.last_trade.size         # float — shares in last trade
    snap.last_trade.timestamp    # int   — nanosecond Unix timestamp
    snap.last_quote.bid_price    # float — best bid
    snap.last_quote.ask_price    # float — best ask
    snap.day.open                # float — today's open
    snap.day.high                # float — today's high
    snap.day.low                 # float — today's low
    snap.day.close               # float — today's close / last price during trading
    snap.day.volume              # float — today's volume
    snap.day.vwap                # float — today's VWAP
    snap.prev_day.close          # float — yesterday's close
    snap.todays_change           # float — dollar change from prev_day.close
    snap.todays_change_percent   # float — % change from prev_day.close
    snap.updated                 # int   — nanosecond timestamp of last update
```

### Price Field Selection

During market hours, `snap.day.close` holds the latest traded price.
Outside hours, fall back to `snap.last_trade.price`.

```python
def best_price(snap) -> float | None:
    try:
        return snap.day.close or snap.last_trade.price
    except AttributeError:
        return None
```

---

## Previous Close / End-of-Day

`snap.prev_day.close` is always present in the snapshot response — no separate call needed.

For a dedicated end-of-day call:

```python
prev = client.get_previous_close_agg("AAPL")
# Returns a list of OHLCV bars (usually one bar for yesterday)
```

---

## `SnapshotMarketType` Enum

```python
from massive.rest.models import SnapshotMarketType

SnapshotMarketType.Stocks   # equities
SnapshotMarketType.Options
SnapshotMarketType.Forex
SnapshotMarketType.Crypto
SnapshotMarketType.Indices
```

String values (`"stocks"`) may be passed directly.

---

## Rate Limits

| Tier | Limit | Recommended poll interval |
|------|-------|--------------------------|
| Free | 5 requests/minute | 15 seconds |
| All paid tiers | Unlimited (soft cap ~100 req/s) | 2–5 seconds |

Free tier data is **end-of-day only** — `last_trade` prices will be stale outside market hours.

---

## Complete Working Example

```python
import asyncio
import os
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])

async def fetch_prices(tickers: list[str]) -> dict[str, float]:
    """Return {ticker: price} for all tickers in one API call."""
    snapshots = await asyncio.to_thread(
        client.get_snapshot_all,
        SnapshotMarketType.Stocks,
        tickers,
    )
    result: dict[str, float] = {}
    for snap in snapshots:
        try:
            price = snap.day.close or snap.last_trade.price
            if price:
                result[snap.ticker] = price
        except AttributeError:
            pass  # Ticker had no data
    return result

# Run
prices = asyncio.run(fetch_prices(["AAPL", "GOOGL", "MSFT"]))
# {"AAPL": 190.50, "GOOGL": 175.20, "MSFT": 421.00}
```

---

## WebSocket Alternative (Real-Time Streaming)

For continuous real-time data, Massive provides a `WebSocketClient` — but this requires a paid tier and adds complexity. The FinAlly project uses REST polling to keep the integration simple and free-tier compatible.

```python
from massive import WebSocketClient

def handle(msgs):
    for msg in msgs:
        print(f"{msg.symbol} @ ${msg.price}")

ws = WebSocketClient(subscriptions=["T.AAPL", "T.MSFT"])
ws.run(handle_msg=handle)
```

---

## Notes

- The `RESTClient` has no async methods — always wrap in `asyncio.to_thread` for FastAPI/asyncio use
- Max ~250 tickers per `get_snapshot_all` call
- Free tier only returns end-of-day data; `last_trade.price` will be yesterday's close after hours
- Legacy import `from polygon import RESTClient` still works but `from massive import RESTClient` is preferred
