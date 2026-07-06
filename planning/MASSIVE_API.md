# Massive API Reference

Reference documentation for the Massive (formerly Polygon.io) REST API as used in FinAlly. Verified against `massive.com/docs` and the `massive-com/client-python` source in July 2026 — this supersedes `planning/archive/MASSIVE_API.md`, which predates verification and contains two inaccuracies called out below.

## Overview

- **Base URL**: `https://api.massive.com` (legacy `https://api.polygon.io` still works — full API compatibility, identical JSON schemas)
- **Python package**: `massive` on PyPI (`pip install massive` / `uv add massive`), formerly `polygon-api-client`
- **Min Python version**: 3.9 (tested through 3.14)
- **Auth**: API key via `MASSIVE_API_KEY` env var (read automatically by `RESTClient()`) or passed explicitly as `RESTClient(api_key=...)`. The legacy `POLYGON_API_KEY` still works during the transition period.
- **License**: MIT

Polygon.io rebranded to Massive on 2025-10-30. Existing API keys and accounts continued to work unchanged through the rename; only the package name, import path, and base URL changed.

## Rate Limits

| Tier | Limit |
|------|-------|
| Free (Stocks Starter) | 5 requests/minute |
| Paid tiers | Unlimited (recommend staying under 100 req/s) |

FinAlly polls on a timer. Free tier: poll every 15s. Paid: poll every 2-5s. This matches `MassiveDataSource`'s `poll_interval` default of `15.0` in `backend/app/market/massive_client.py`.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY (or legacy POLYGON_API_KEY) from environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

## Endpoints Used in FinAlly

### 1. Snapshot — All Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call**. This is the endpoint FinAlly's poller uses.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

**Python client method**:
```python
def get_snapshot_all(
    self,
    market_type: Union[str, SnapshotMarketType],
    tickers: Optional[Union[str, List[str]]] = None,
    include_otc: Optional[bool] = False,
    ...
) -> Union[List[TickerSnapshot], HTTPResponse]
```

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
    print(f"  Day change: {snap.todays_change_percent}%")
    print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
    print(f"  Prev close: {snap.prev_day.close}")
```

**`TickerSnapshot` fields** (Python model, `massive/rest/models/snapshot.py`):

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | Symbol |
| `day` | `Agg` | Today's OHLCV bar — `open`, `high`, `low`, `close`, `volume`, `vwap` |
| `prev_day` | `Agg` | **Previous day's** OHLCV bar, same shape as `day` |
| `last_trade` | `LastTrade` | Most recent trade (see below) |
| `last_quote` | `LastQuote` | Most recent NBBO quote |
| `min` | `MinuteSnapshot` | Most recent minute bar |
| `todays_change` | `float` | Absolute change vs. previous close |
| `todays_change_percent` | `float` | Percent change vs. previous close |
| `updated` | `int` | Last-updated timestamp (ns) |
| `fair_market_value` | `float` \| `None` | Business plan tiers only |

**Correction vs. the archived doc**: there is no `day.previous_close` field. Previous close lives on the sibling `prev_day.close` (raw JSON: `prevDay.c`). Any code (or doc) that reads `snap.day.previous_close` will raise `AttributeError`.

**`LastTrade` fields** (`massive/rest/models/trades.py`, reused inside snapshots):

| Field | Type | Notes |
|---|---|---|
| `price` | `float` | Trade price |
| `size` | `float` | Trade size |
| `sip_timestamp` | `int` | **Nanosecond**-accuracy Unix timestamp — when the SIP received the trade |
| `participant_timestamp` | `int` | Nanosecond-accuracy timestamp — when the exchange generated the trade |
| `trf_timestamp` | `int` \| `None` | Trade Reporting Facility timestamp, nanoseconds |
| `exchange`, `conditions`, `id`, `tape`, `correction` | — | Trade metadata |

**Correction vs. the archived doc, and a real bug in the current codebase**: there is **no `.timestamp` attribute** on `LastTrade` — only `sip_timestamp` / `participant_timestamp` / `trf_timestamp`, all in **nanoseconds**, not milliseconds. See "Known Issue" below.

### 2. Single Ticker Snapshot

For detailed data on one ticker (e.g. the detail view when a user clicks a ticker).

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}`

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

Previous trading day's OHLC for a ticker. Useful for seed prices.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

```python
def get_previous_close_agg(
    self,
    ticker: str,
    adjusted: Optional[bool] = None,
    ...
) -> Union[PreviousCloseAgg, HTTPResponse]
```

```python
prev = client.get_previous_close_agg(ticker="AAPL")

for agg in prev:
    print(f"Previous close: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume}")
```

### 4. Aggregates (Bars)

Historical OHLCV bars over a date range. Not needed for live polling but useful if historical charts are added later.

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

```python
def list_aggs(
    self,
    ticker: str,
    multiplier: int,
    timespan: str,
    from_: Union[str, int, datetime, date],
    to: Union[str, int, datetime, date],
    adjusted: Optional[bool] = None,
    sort: Optional[Union[str, Sort]] = None,
    limit: Optional[int] = None,
    ...
) -> Union[Iterator[Agg], HTTPResponse]
```

```python
aggs = []
for a in client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2026-01-01",
    to="2026-01-31",
    limit=50000,
):
    aggs.append(a)

for a in aggs:
    print(f"O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

Pagination is enabled by default (`pagination=True`) — `list_aggs` automatically follows `next_url` across pages.

### 5. Last Trade / Last Quote

Individual endpoints for the most recent trade or NBBO quote on one ticker.

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price} x {trade.size}")

quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid_price} x {quote.bid_size}")
print(f"Ask: ${quote.ask_price} x {quote.ask_size}")
```

## How FinAlly Uses the API

The Massive poller runs as a background asyncio task (`MassiveDataSource` in `backend/app/market/massive_client.py`):

1. Collects all tickers from the watchlist
2. Calls `get_snapshot_all()` with those tickers (one API call, run via `asyncio.to_thread` since the client is synchronous)
3. Extracts `last_trade.price` from each snapshot and writes it to the shared `PriceCache`
4. Sleeps for the poll interval, then repeats

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
            snapshots = await asyncio.to_thread(
                client.get_snapshot_all,
                market_type=SnapshotMarketType.STOCKS,
                tickers=tickers,
            )
            for snap in snapshots:
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.sip_timestamp / 1_000_000_000,  # ns -> seconds
                )

        await asyncio.sleep(interval)
```

## Fixed Issue — timestamp handling (resolved 2026-07-02)

`backend/app/market/massive_client.py::_poll_once` previously did:

```python
price = snap.last_trade.price
timestamp = snap.last_trade.timestamp / 1000.0  # ms -> seconds  [WRONG]
```

`LastTrade` has no `.timestamp` field — real attribute names are `sip_timestamp` / `participant_timestamp`, and they're **nanoseconds**, not milliseconds. Against the real API this raised `AttributeError` inside the per-snapshot `try/except (AttributeError, TypeError)` block, which logged a warning and **skipped the cache update for every ticker, every poll**. With a real `MASSIVE_API_KEY` set, the price cache would never have been populated from Massive data and the SSE stream would sit frozen.

This wasn't caught by `test_massive.py` because the test's mock manually stamped a `.timestamp` attribute onto a plain `MagicMock()` — an attribute the real `LastTrade` class doesn't have — so the test validated behavior against a shape the live API never returns.

**Fix applied**: `massive_client.py` now reads `snap.last_trade.sip_timestamp / 1_000_000_000`. `test_massive.py`'s `_make_snapshot` helper now builds `last_trade` as `MagicMock(spec_set=LastTrade)` (the real model class from `massive.rest.models.trades`) rather than a bare `MagicMock()` — `spec_set` rejects both reading *and* setting any attribute not on the real class, so a future regression back to `.timestamp` would fail the test suite immediately instead of silently mocking a field that doesn't exist. All 73 tests in `backend/tests/market/` pass with the fix.

## Error Handling

The client raises exceptions for HTTP errors:
- **401**: Invalid API key
- **403**: Insufficient permissions (plan doesn't include the endpoint)
- **429**: Rate limit exceeded (free tier: 5 req/min)
- **5xx**: Server errors (client has built-in retry, 3 retries by default)

`MassiveDataSource._poll_once` wraps the whole poll in a broad `except Exception` and logs+continues rather than crashing the background task — appropriate for 401/429/network errors, and also what had masked the timestamp bug above (see "Fixed Issue") until the test mock was tightened to `spec_set`.

## Notes

- The snapshot endpoint returns data for **all requested tickers in one call** — critical for staying within the free tier's rate limit
- All Massive timestamp fields are Unix **nanoseconds**, not milliseconds — this differs from many other market data APIs and is easy to get wrong (see above)
- During market-closed hours, `last_trade.price` reflects the last traded price (may include after-hours)
- The `day` object resets at market open; during pre-market, its values may still reflect the previous session
- A newer, multi-asset `GET /v3/snapshot` "Unified Snapshot" endpoint exists (stocks/options/forex/crypto in one call, up to 250 tickers via `ticker.any_of`). FinAlly doesn't need it — it's stocks-only and the per-market-type `get_snapshot_all` is simpler and already covers the watchlist use case — but it's worth knowing about if a future feature needs cross-asset data.

## Sources

- [API Docs | Massive](https://massive.com/docs)
- [Full Market Snapshot | Stocks REST API - Massive](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Single Ticker Snapshot | Stocks REST API - Massive](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot)
- [Unified Snapshot | Stocks REST API - Massive](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Daily Ticker Summary (OHLC) | Stocks REST API - Massive](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary)
- [What is the request limit for Massive's RESTful APIs? | Massive](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
- [Polygon.io is Now Massive | Massive](https://massive.com/blog/polygon-is-now-massive)
- [massive-com/client-python (GitHub)](https://github.com/massive-com/client-python)
- [massive (PyPI)](https://pypi.org/project/massive/)
- [Migration Guide | massive-com/client-python | DeepWiki](https://deepwiki.com/massive-com/client-python/8-migration-guide)
