# Massive API Reference (formerly Polygon.io)

Reference documentation for the Massive REST API as used in FinAlly's market data layer (`backend/app/market/massive_client.py`). Polygon.io rebranded as Massive on **October 30, 2025**; existing API keys, accounts, and the legacy `api.polygon.io` host continue to work. This document reflects the live docs at `massive.com/docs` and the `massive` PyPI package (currently pinned `>=1.0.0` in `backend/pyproject.toml`, resolving to `2.2.0` in `backend/uv.lock`) as of 2026-08-24.

## Overview

- **Base URL**: `https://api.massive.com` (legacy `https://api.polygon.io` still supported)
- **Python package**: `massive` (`pip install -U massive` / `uv add massive`), min Python 3.9
- **Auth**: API key, either passed to `RESTClient(api_key=...)` or read automatically from the `MASSIVE_API_KEY` environment variable when `RESTClient()` is constructed with no arguments — this is exactly the env var FinAlly already uses to select the data source, so no separate credential wiring is needed
- **Response objects**: the client deserializes JSON into typed model classes (e.g. `TickerSnapshot`, `Agg`, `LastTrade`) rather than returning raw dicts

## Pricing Tiers & Rate Limits

| Tier | Price | Rate limit | Data recency | History |
|------|-------|-----------|---------------|---------|
| Basic (free) | $0/mo | 5 requests/min | End-of-day only | 2 years |
| Starter | $29/mo | Unlimited (soft guidance: stay under 100 req/s) | 15-minute delayed | 5 years |
| Developer | $79/mo | Unlimited | 15-minute delayed, + trades data | 10 years |
| Advanced | $199/mo | Unlimited | Real-time | 20+ years, + quotes/financials |

**⚠️ This matters for FinAlly specifically**: the multi-ticker snapshot endpoint (§1 below), which is the one FinAlly polls, is documented as **not included in the Basic (free) plan** — it requires Starter or above. A free-tier `MASSIVE_API_KEY` will get a `403` from `get_snapshot_all`, not a slow-but-working response. The `previous close` aggregate endpoint (§3) *is* available on Basic. See "Discrepancies & Gotchas" at the end of this document — this affects whether FinAlly's "free tier: poll every 15s" assumption is viable at all, since the endpoint it needs isn't free.

## Client Initialization

```python
from massive import RESTClient

# Reads MASSIVE_API_KEY from the environment automatically
client = RESTClient()

# Or pass explicitly
client = RESTClient(api_key="your_key_here")
```

## Endpoints Used in FinAlly

### 1. Snapshot — All Tickers (Primary Endpoint)

Gets current prices for multiple tickers in a **single API call** — the endpoint FinAlly's poller uses.

**REST**: `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT`

Query params: `tickers` (comma-separated, case-sensitive), `include_otc` (bool, default `false`).

**Python client** — verified against the official `stocks-snapshots_all.py` example in `massive-com/client-python`:

```python
from massive import RESTClient
from massive.rest.models import TickerSnapshot, Agg

client = RESTClient()  # MASSIVE_API_KEY read from environment

tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA"]

# market_type is a plain string, and it — plus tickers — are POSITIONAL args
snapshots = client.get_snapshot_all("stocks", tickers)

for snap in snapshots:
    if not isinstance(snap, TickerSnapshot):
        continue
    print(f"{snap.ticker}: ${snap.last_trade.price}")
    print(f"  Today's change: {snap.todays_change_percent:.2f}%")
    if isinstance(snap.day, Agg):
        print(f"  Day OHLC: O={snap.day.open} H={snap.day.high} L={snap.day.low} C={snap.day.close}")
        print(f"  Volume: {snap.day.volume}")
```

**`TickerSnapshot` fields** (verified from the model source, `massive/rest/models/snapshot.py`):

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | |
| `day` | `Agg` | today's running OHLCV bar |
| `prev_day` | `Agg` | **previous session's** OHLCV bar — there is no `day.previous_close` |
| `last_trade` | `LastTrade` | |
| `last_quote` | `LastQuote` | bid/ask |
| `min` | `MinuteSnapshot` | latest minute bar |
| `todays_change` | `float` | absolute change vs. previous close |
| `todays_change_percent` | `float` | percentage change vs. previous close — use this directly instead of computing it from `day`/`prev_day` |
| `updated` | `int` | Unix nanoseconds |
| `fair_market_value` | `float` | Business plan only |

**`LastTrade` fields** (verified from `massive/rest/models/trades.py`) — **there is no plain `.timestamp` field**:

| Field | Type | Notes |
|---|---|---|
| `price` | `float` | |
| `size` | `float` | |
| `exchange` | `int` | numeric exchange code, not a string like `"XNYS"` |
| `sip_timestamp` | `int` | Unix **nanoseconds**, SIP-received time — the one to use for "when was this priced" |
| `participant_timestamp` | `int` | Unix nanoseconds, exchange-reported time |
| `trf_timestamp` | `int` | Unix nanoseconds, TRF-reported time (off-exchange trades only) |
| `conditions` | `list[int]` | trade condition codes |
| `correction`, `id`, `trf_id`, `tape`, `fractional_size` | — | rarely needed for a snapshot poller |

`Agg` fields (day/prev_day): `open`, `high`, `low`, `close`, `volume`, `vwap`, `timestamp` (Unix **milliseconds** — note this differs from `LastTrade`'s nanosecond timestamps), `transactions`, `otc`.

### 2. Single Ticker Snapshot

For a detail view when the user clicks one ticker.

**REST**: `GET /v3/snapshot?ticker.any_of=AAPL` (the newer "Unified Snapshot" endpoint, also covers options/forex/crypto in one call, `ticker.any_of` accepts up to 250 comma-separated tickers) — or the older per-ticker path `GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}`.

**Python client**:

```python
snapshot = client.get_snapshot_ticker("stocks", ticker="AAPL")

print(f"Price: ${snapshot.last_trade.price}")
print(f"Bid/Ask: ${snapshot.last_quote.bid} / ${snapshot.last_quote.ask}")
print(f"Day range: ${snapshot.day.low} - ${snapshot.day.high}")
```

### 3. Previous Close

Previous day's OHLC for a ticker — available even on the free Basic tier, useful for seed prices or as a Basic-tier fallback when the snapshot endpoint isn't accessible.

**REST**: `GET /v2/aggs/ticker/{ticker}/prev`

**Python client** — verified against the official `stocks-previous_close.py` example:

```python
from massive import RESTClient

client = RESTClient()
aggs = client.get_previous_close_agg("AAPL")

for agg in aggs:
    print(f"Previous close: ${agg.close}")
    print(f"OHLC: O={agg.open} H={agg.high} L={agg.low} C={agg.close}")
    print(f"Volume: {agg.volume}")
```

**Response** (raw REST):
```json
{
  "adjusted": true,
  "status": "OK",
  "ticker": "AAPL",
  "resultsCount": 1,
  "results": [
    { "T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97, "v": 131704427, "vw": 116.3058, "t": 1605042000000 }
  ]
}
```

### 4. Aggregates (Bars)

Historical OHLCV bars over a date range — not needed for live polling, but the natural source if FinAlly ever adds a longer-range historical chart (beyond the in-memory rolling buffer described in `MARKET_INTERFACE.md`).

**REST**: `GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`

**Python client**:
```python
aggs = list(client.list_aggs(
    ticker="AAPL",
    multiplier=1,
    timespan="day",
    from_="2026-07-01",
    to="2026-08-01",
    limit=50000,
))

for a in aggs:
    print(f"t={a.timestamp} O={a.open} H={a.high} L={a.low} C={a.close} V={a.volume}")
```

### 5. Last Trade / Last Quote

Individual endpoints for the most recent trade or NBBO quote on one ticker — rarely needed once the snapshot endpoint is in use, since a snapshot already embeds both.

```python
trade = client.get_last_trade(ticker="AAPL")
print(f"Last trade: ${trade.price} x {trade.size}")

quote = client.get_last_quote(ticker="AAPL")
print(f"Bid: ${quote.bid} x {quote.bid_size}")
print(f"Ask: ${quote.ask} x {quote.ask_size}")
```

## How FinAlly Uses the API

The Massive poller runs as a background asyncio task, one batched call per interval regardless of ticker count:

1. Collects the tracked ticker set (watchlist ∪ positions — see `MARKET_INTERFACE.md`)
2. Calls `get_snapshot_all("stocks", tickers)` in a thread (the client is synchronous) — one API call
3. Extracts `last_trade.price` and `last_trade.sip_timestamp` from each snapshot
4. Writes to the shared in-memory `PriceCache`
5. Sleeps for the poll interval, then repeats

```python
import asyncio
from massive import RESTClient

async def poll_massive(api_key: str, get_tickers, price_cache, interval: float = 15.0) -> None:
    client = RESTClient(api_key=api_key)

    while True:
        tickers = get_tickers()
        if tickers:
            snapshots = await asyncio.to_thread(client.get_snapshot_all, "stocks", tickers)
            for snap in snapshots:
                if snap.last_trade is None:
                    continue
                price_cache.update(
                    ticker=snap.ticker,
                    price=snap.last_trade.price,
                    timestamp=snap.last_trade.sip_timestamp / 1_000_000_000,  # ns -> seconds
                )

        await asyncio.sleep(interval)
```

## Error Handling

The client raises exceptions for HTTP errors:
- **401**: invalid API key
- **403**: plan doesn't include the endpoint (this is the practical failure mode on the Basic tier for the snapshot endpoint — see below)
- **429**: rate limit exceeded (Basic tier: 5 req/min)
- **5xx**: server errors (client has built-in retry, 3 attempts by default)

FinAlly's poller (`massive_client.py::_poll_once`) already wraps each poll cycle in a broad `try/except` and logs rather than crashing, so a `403`/`429`/network blip degrades to "stale prices until the next successful poll" rather than taking down the app.

## Discrepancies & Gotchas Found vs. FinAlly's Current Code and Prior Docs

Verified against the live `massive-com/client-python` GitHub repo (README, official examples, and the `TickerSnapshot`/`LastTrade` model source) as of this research pass:

1. **Free tier likely can't use the snapshot endpoint at all.** Massive's docs state the "Full Market Snapshot" / all-tickers snapshot family is "Included in Stocks Starter, Developer, Advanced, and Business plans — Not included in Stocks Basic." If a user runs FinAlly with a free Massive key, `get_snapshot_all` will plausibly 403 rather than just being slow at 5 req/min. `get_previous_close_agg` *is* available on Basic. Worth deciding: either document that Massive real-data mode requires a paid (Starter+) key, or fall back to polling `get_previous_close_agg` per ticker on Basic keys (loses intraday movement).
2. **`get_snapshot_all` signature differs from what's in `backend/app/market/massive_client.py`.** The current code calls it as `client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=self._tickers)`, importing `SnapshotMarketType` from `massive.rest.models`. The verified official example calls it positionally as `client.get_snapshot_all("stocks", tickers)` — a plain string, not an enum — and no `SnapshotMarketType` symbol was found anywhere in the package's public model exports during this research. This import may simply not exist in `massive==2.2.0` and would raise `ImportError` at startup. **Recommend verifying this against the installed package directly** (`python -c "from massive.rest.models import SnapshotMarketType"`) since it wasn't installable in this research environment (no package-index network access) — flagging as unverified-but-suspicious rather than confirmed-broken.
3. **`LastTrade` has no `.timestamp` attribute.** `massive_client.py` reads `snap.last_trade.timestamp`. The verified model only exposes `sip_timestamp`, `participant_timestamp`, and `trf_timestamp` — all in **nanoseconds**, not milliseconds. The current code's ms→s conversion (`/ 1000.0`) would also be off by 1,000,000x if the field did exist at millisecond resolution; nanosecond timestamps need `/ 1_000_000_000`. This is a likely real bug, not just a docs staleness issue — worth a follow-up ticket against `massive_client.py` and `test_massive.py` (whose mocks currently model `last_trade.timestamp` in milliseconds).
4. **`day.previous_close` doesn't exist.** The prior archived `MASSIVE_API.md` and the interface sketch both reference `snap.day.previous_close` / `day.change_percent`. The real model has a separate `prev_day: Agg` field (previous session's full OHLCV) and top-level `todays_change_percent` — there's no `previous_close` key nested under `day`.
5. **Rate limit table simplification confirmed accurate in spirit**: 5 req/min free, unlimited (with a 100 req/s courtesy guideline) on all paid tiers — this part of the prior docs holds up.
6. **The `/v3/snapshot` "Unified Snapshot" endpoint** (`ticker.any_of=...`, up to 250 tickers, spans stocks/options/forex/crypto in one call) is newer and not what FinAlly currently uses or needs — the stocks-only `/v2/snapshot/locale/us/markets/stocks/tickers` endpoint remains supported and is simpler for this project's stocks-only scope.
