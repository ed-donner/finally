---
name: massive-api
description: Use this to write code to retrieve realtime stock market equity prices using the MASSIVE API
---

# Rertieving Stock Market equity prices with the MASSIVE API

These instructions cover using [Massive](https://pypi.org/project/massive/) Python SDK (formerly known as Polygon.io) to fetch live US stock prices. This document covers the working approach, including critical gotchas discovered during integration.

## Setup

Install the SDK. The MASSIVE_API_KEY is already set in the .env file.

```bash
uv add massive
```

## Fetching Stock Snapshots

The core API call uses `get_snapshot_all` to retrieve price snapshots for a list of tickers in a single request.

```python
from massive import RESTClient

client = RESTClient(api_key="your_key_here")

snapshots = client.get_snapshot_all(
    market_type="stocks",       # MUST be the literal string, not an enum
    tickers=["AAPL", "GOOGL", "MSFT"],
)
```

**Critical: use `market_type="stocks"`, not `SnapshotMarketType.STOCKS`.**
The SDK enum object produces a malformed URL that returns a `404 page not found`. Always pass the literal string `"stocks"`.

## Snapshot Response Structure

Each snapshot object contains nested data. The key attributes:

| Attribute | Description |
|---|---|
| `snap.ticker` | Symbol string (e.g. `"AAPL"`) |
| `snap.last_trade.price` | Most recent trade price |
| `snap.last_quote.bid_price` | Current bid |
| `snap.last_quote.ask_price` | Current ask |
| `snap.min.close` | Most recent minute-bar close |
| `snap.day.open` | Today's opening price |
| `snap.prev_day.close` | Previous day's closing price |
| `snap.todays_change` | Price change since open |
| `snap.updated` | Snapshot-level timestamp |

## Extracting the Price

Use a fallback chain, because not every field is always populated:

```python
import time

def resolve_price(snap, stale_seconds=10.0):
    """Pick the best available price from a snapshot."""
    now = time.time()

    # 1. Fresh last trade (if recent enough)
    trade = getattr(snap, "last_trade", None)
    trade_price = getattr(trade, "price", None)
    trade_ts = extract_timestamp(trade, snap)
    if trade_price and trade_price > 0 and trade_ts:
        if now - trade_ts <= stale_seconds:
            return trade_price, trade_ts

    # 2. Quote midpoint
    quote = getattr(snap, "last_quote", None)
    bid = getattr(quote, "bid_price", None)
    ask = getattr(quote, "ask_price", None)
    if bid and ask and bid > 0 and ask > 0:
        quote_ts = extract_timestamp(quote, snap)
        return (bid + ask) / 2.0, quote_ts or now

    # 3. Minute-bar close
    minute = getattr(snap, "min", None)
    minute_close = getattr(minute, "close", None)
    if minute_close and minute_close > 0:
        minute_ts = extract_timestamp(minute, snap)
        return minute_close, minute_ts or now

    # 4. Stale trade (better than nothing)
    if trade_price and trade_price > 0:
        return trade_price, trade_ts or now

    return None, now
```

## Extracting Timestamps (the hard part)

**Do not assume `last_trade.timestamp` exists.** The SDK response model exposes different timestamp fields depending on the data source. You must check multiple fields and normalize the units.

```python
def extract_timestamp(obj, snap):
    """Try multiple timestamp fields, return Unix seconds or None."""
    raw = first_non_none(
        getattr(obj, "timestamp", None),
        getattr(obj, "sip_timestamp", None),
        getattr(obj, "participant_timestamp", None),
        getattr(obj, "trf_timestamp", None),
        getattr(snap, "updated", None),       # snapshot-level fallback
    )
    return normalize_timestamp(raw)


def first_non_none(*values):
    for v in values:
        if v is not None:
            return v
    return None


def normalize_timestamp(raw_ts):
    """Convert from nanoseconds/microseconds/milliseconds to Unix seconds."""
    if raw_ts is None or not isinstance(raw_ts, (int, float)):
        return None
    ts = float(raw_ts)
    if ts > 1e17:       # nanoseconds
        return ts / 1e9
    if ts > 1e14:       # microseconds
        return ts / 1e6
    if ts > 1e11:       # milliseconds
        return ts / 1e3
    return ts            # already seconds
```

The timestamp fallback order is:

1. `obj.timestamp` (canonical, but often missing)
2. `obj.sip_timestamp` (SIP feed timestamp)
3. `obj.participant_timestamp` (exchange timestamp)
4. `obj.trf_timestamp` (TRF timestamp)
5. `snap.updated` (snapshot-level, always present)

## Day Change Calculation

To compute the day's price change, extract a baseline price:

```python
def extract_day_baseline(snap, current_price):
    """Get the reference price for daily change: open > prev close > derived."""
    day = getattr(snap, "day", None)
    day_open = getattr(day, "open", None)
    if day_open and float(day_open) > 0:
        return float(day_open)

    prev_day = getattr(snap, "prev_day", None)
    prev_close = getattr(prev_day, "close", None)
    if prev_close and float(prev_close) > 0:
        return float(prev_close)

    todays_change = getattr(snap, "todays_change", None)
    if todays_change is not None:
        baseline = float(current_price) - float(todays_change)
        if baseline > 0:
            return baseline

    return None
```

## Async Polling Pattern

The Massive `RESTClient` is synchronous. In an async application, run it in a thread:

```python
import asyncio

async def poll_loop(client, tickers, cache, interval=0.5):
    while True:
        snapshots = await asyncio.to_thread(
            client.get_snapshot_all,
            market_type="stocks",
            tickers=tickers,
        )
        for snap in snapshots:
            price, ts = resolve_price(snap)
            if price is not None:
                cache.update(snap.ticker, price, ts)
        await asyncio.sleep(interval)
```