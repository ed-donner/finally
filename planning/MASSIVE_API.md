# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive REST API and its official Python client, as used by `backend/app/market/massive_client.py`.

## Overview

- **Rebrand**: Polygon.io rebranded as **Massive** on 2025-10-30. Existing API keys and integrations continue to work unchanged.
- **Base URL**: the SDK now defaults to `https://api.massive.com`; the legacy `https://api.polygon.io` host remains supported for an extended transition period.
- **Python package**: `massive` on PyPI (source: [`massive-com/client-python`](https://github.com/massive-com/client-python)). Install via `pip install -U massive` / `uv add massive`.
- **Min Python version**: 3.9+ (this project targets 3.12).
- **Pinned version in this project**: `massive>=1.0.0` (see `backend/pyproject.toml`).
- **Auth**: API key via the `MASSIVE_API_KEY` environment variable (read automatically by `RESTClient()`), or passed explicitly to `RESTClient(api_key=...)`.

## Rate Limits

| Tier | Limit |
|------|-------|
| Free | 5 requests/minute |
| Paid (all tiers) | No hard cap, but Massive asks clients to stay under ~100 req/s to avoid throttling |

FinAlly polls on a timer rather than using WebSockets. Free tier: poll every 15s. Paid: poll every 2–5s. This is exactly what `MassiveDataSource` in this codebase does — see `poll_interval` in `massive_client.py`.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from the environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

## Endpoints Used in FinAlly

### 1. Stocks Snapshot — Multiple Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call**. This is the endpoint `MassiveDataSource._fetch_snapshots()` polls.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

Query parameters:
- `tickers` — case-sensitive comma-separated list. Omit to snapshot the entire US stock market (not used here — we always pass our watchlist).
- `include_otc` — include OTC securities; defaults to `false`.

**Python client** (verified against the official `stocks-snapshots_all.py` example in `client-python`):
```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()

# Market type can be passed as the enum (used in this codebase) or the literal
# string "stocks" (used in the client's own examples) — both are accepted.
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"],
)

for snap in snapshots:
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Prev close: {snap.prev_day.close}")
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Volume: {snap.day.volume}")
```

**Response structure** (per ticker, raw JSON — the client maps these into a `TickerSnapshot` object with snake_case attributes):
```json
{
  "ticker": "AAPL",
  "day": {
    "o": 129.61, "h": 130.15, "l": 125.07, "c": 125.07,
    "v": 111237700, "vw": 127.35
  },
  "prevDay": {
    "o": 128.0, "h": 129.9, "l": 127.5, "c": 129.61, "v": 98000000
  },
  "lastTrade": { "p": 125.07, "s": 100, "x": 11, "t": 1675190399000 },
  "lastQuote": { "p": 125.08, "P": 125.06, "s": 500, "S": 1000, "t": 1675190399500 },
  "todaysChange": -4.54,
  "todaysChangePerc": -3.50,
  "updated": 1675190399000
}
```

**Key fields we extract** (see `massive_client.py:_poll_once`):
- `snap.last_trade.price` — current price for trading and display
- `snap.last_trade.timestamp` — Unix **milliseconds**; the code divides by 1000 before writing to `PriceCache`
- `snap.ticker` — used as the cache key

`day.previous_close` / `todaysChangePerc` are available if the app ever needs a proper "day change %" independent of the in-session `PriceCache` baseline, but are not currently consumed — `PriceCache` computes its own `change`/`change_percent` relative to the previously cached price (see `MARKET_INTERFACE.md`).

### 2. Single Ticker Snapshot

Not currently used in this codebase (the app always polls the whole watchlist via endpoint #1), but useful for a future per-ticker detail view.

```python
snapshot = client.get_snapshot_ticker(
    market_type=SnapshotMarketType.STOCKS,
    ticker="AAPL",
)
print(f"Price: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid_price} / ${snapshot.last_quote.ask_price}")
```

### 3. Previous Close

Gets the previous trading day's OHLC for a ticker. Not currently called at runtime, but this is the endpoint to use if seed prices are ever refreshed from real data instead of the hardcoded table in `seed_prices.py`.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

```python
aggs = client.get_previous_close_agg("AAPL")
print(aggs)  # Agg object: open, high, low, close, volume, timestamp
```

### 4. Aggregates (Bars)

Historical OHLCV bars over a date range. Not needed for live polling; would back a historical chart feature if added later.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

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

### 5. Last Trade / Last Quote

Individual endpoints for the most recent trade or NBBO quote on one ticker. Not used — superseded by the batched snapshot call — but available if a single-ticker fast path is ever needed.

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price} x {trade.size}")

quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid_price} x {quote.bid_size}")
print(f"Ask: ${quote.ask_price} x {quote.ask_size}")
```

## A Newer Alternative: Unified/Universal Snapshot (Not Used, Noted for Future Reference)

As part of the Massive rebrand, a newer cross-asset-class endpoint was introduced:

**REST**: `GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT`

- Supports up to 250 comma-separated tickers via `ticker.any_of`, and can mix asset classes in one call (stocks, options `O:...`, forex `C:...`, crypto `X:...`) via the `type` filter.
- Response fields use a flatter shape: `last_trade`, `last_quote`, `session.close`, `session.previous_close`, `session.change_percent`, `market_status`.
- Real-time data on this endpoint requires a Starter-tier-or-above plan; the free tier's usual 5 req/min cap still applies.

We deliberately kept using the older stocks-only snapshot endpoint (#1 above) because it's what the current `massive_client.py` implements, it's simpler for a single-asset-class app, and Massive has committed to keeping it supported. If FinAlly ever adds options/crypto/forex tickers, revisit this endpoint — it would let one API call cover every asset class in the watchlist instead of one call per asset type.

## Error Handling

The client raises exceptions for HTTP errors:
- **401**: Invalid API key
- **403**: Insufficient permissions (plan doesn't include the endpoint)
- **429**: Rate limit exceeded (free tier: 5 req/min)
- **5xx**: Server errors (client has built-in retry)

`MassiveDataSource._poll_once()` catches all exceptions from a poll cycle, logs them, and retries on the next interval rather than crashing the background task — see `MARKET_INTERFACE.md` for how this fits into the broader retry/error strategy.

## Notes

- The snapshot endpoint returns data for **all requested tickers in one call** — critical for staying within the free tier's rate limit regardless of watchlist size.
- Timestamps from the API are Unix **milliseconds**; `massive_client.py` converts to seconds before writing to the shared `PriceCache`.
- During closed market hours, `last_trade.price` reflects the last traded price (may be from after-hours or the prior session).
- `day` resets at market open; during pre-market it may still reflect the previous session.

## Sources

- [massive-com/client-python](https://github.com/massive-com/client-python) — official Python client, README and `examples/rest/`
- [Full Market Snapshot — Stocks REST API](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Unified Snapshot — Stocks REST API](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Massive + Python blog post](https://massive.com/blog/polygon-io-with-python-for-stock-market-data)
- [What is the request limit for Massive's RESTful APIs?](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
