# Massive API Reference (formerly Polygon.io)

Reference for the parts of the Massive REST API that FinAlly uses to get stock prices for multiple tickers, both realtime and end of day. Verified against the Massive docs and the installed `massive` Python package (`backend/.venv`, `RESTClient`) in November 2026.

## Basics

| Item | Value |
|---|---|
| Rebrand | Polygon.io became Massive on 2025-10-30. Existing keys and accounts work unchanged |
| Base URL | `https://api.massive.com` (`api.polygon.io` still works for now) |
| Python package | `massive` (`uv add massive`), import `from massive import RESTClient` |
| Auth | `RESTClient(api_key=...)`. If omitted, the client reads the `MASSIVE_API_KEY` env var itself |
| REST auth on the wire | The client sends `Authorization: Bearer <key>` |
| Client defaults | 10s connect/read timeouts, `retries=3` (urllib3 retry on 413/429/499/5xx, backoff 0.1s) |
| Client style | Synchronous (urllib3). In asyncio code, wrap calls in `asyncio.to_thread` |
| Tickers | Case-sensitive, uppercase |

## Plans and limits

| Plan | Recency | Snapshot endpoints | Rate limit |
|---|---|---|---|
| Stocks Basic (free) | End of day | Not included | 5 calls/min (as stated in PLAN.md; not on the docs pages checked) |
| Starter / Developer | 15-minute delayed | Included | Not verified |
| Advanced ($199/mo) / Business | Real-time | Included | Not verified |

Caveats:
- The snapshot docs list Stocks Basic as not included. PLAN.md assumes free-tier polling of the snapshot endpoint every 15s; a Basic key will probably get a 403 (the docs do not give the error body). Verify with a real free key. Free keys can fall back to the aggregate endpoints (see "Free-tier fallback").
- Rate limits are not published on the pricing page or the endpoint pages; confirm them in the dashboard.
- Data is delayed or stale outside market hours; snapshot data resets daily at 3:30 AM ET and repopulates from about 4:00 AM ET.

## 1. Full market snapshot (the realtime workhorse)

One call returns the latest data for many tickers.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,TSLA
```

Query params: `tickers` (comma-separated, empty = all ~10k tickers), `include_otc` (default false).

Response:

```json
{
  "status": "OK",
  "count": 2,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": 1.52,
      "todaysChangePerc": 0.8,
      "updated": 1605192894630916600,
      "day":     {"o": 190.1, "h": 192.0, "l": 189.5, "c": 191.6, "v": 41000000, "vw": 190.9},
      "min":     {"o": 191.5, "h": 191.7, "l": 191.4, "c": 191.6, "v": 5000, "av": 41000000, "t": 1684428600000},
      "prevDay": {"o": 189.0, "h": 190.5, "l": 188.2, "c": 190.08, "v": 52000000, "vw": 189.4},
      "lastTrade": {"p": 191.6, "s": 100, "t": 1605192894630916600, "x": 4, "c": [14, 41], "i": "71675577320245"},
      "lastQuote": {"P": 191.62, "S": 2, "p": 191.58, "s": 3, "t": 1605192959994246100}
    }
  ]
}
```

Notes:
- `lastTrade` and `lastQuote` only appear if the plan includes trades or quotes. Code must fall back to `min.c` or `day.c`.
- Timestamps: `lastTrade.t`, `lastQuote.t` and `updated` are Unix **nanoseconds** (19 digits); `min.t` is Unix **milliseconds** (13 digits).
- `prevDay.c` is the previous close, which is the baseline for daily change %.
- `todaysChange` / `todaysChangePerc` are relative to the previous close.
- Unknown tickers are silently omitted from `tickers`; no error is raised.

### Python client

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key="...")  # or rely on MASSIVE_API_KEY

snaps = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "TSLA"],
)
for s in snaps:                       # list[TickerSnapshot]
    print(s.ticker, s.last_trade.price, s.prev_day.close, s.todays_change_percent)
```

`TickerSnapshot` attributes (snake_case): `ticker`, `day`, `min`, `prev_day`, `last_trade`, `last_quote`, `todays_change`, `todays_change_percent`, `updated`, `fair_market_value`.

Model field names differ from the JSON keys:

| JSON | Python attribute |
|---|---|
| `lastTrade.p` | `last_trade.price` |
| `lastTrade.s` | `last_trade.size` |
| `lastTrade.t` | `last_trade.sip_timestamp` (nanoseconds) |
| `prevDay.c` | `prev_day.close` |
| `min.c`, `min.t` | `min.close`, `min.timestamp` (milliseconds) |
| `day.c` | `day.close` |
| `todaysChangePerc` | `todays_change_percent` |

`LastTrade` has **no** `.timestamp` attribute. Use `sip_timestamp` (ns). See "Known issue in current code" in MARKET_INTERFACE.md.

## 2. Single ticker snapshot

Used to validate a ticker not yet in the cache.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{ticker}
```

```python
snap = client.get_snapshot_ticker(SnapshotMarketType.STOCKS, "AAPL")
print(snap.ticker, snap.last_trade.price)
```

Response wraps the same object in `{"status": "OK", "ticker": {...}}`. The docs do not describe unknown-ticker behaviour; expect a 404 (`NOT_FOUND`) which the client raises as an exception. Verify against a live key before relying on it.

## 3. End of day prices

### Previous day bar (one ticker)

```
GET /v2/aggs/ticker/{ticker}/prev?adjusted=true
```

```python
prev = client.get_previous_close_agg("AAPL")
# returns PreviousCloseAgg-like model: open, high, low, close, volume, vwap, timestamp
print(prev.close)
```

```json
{"ticker": "AAPL", "adjusted": true, "resultsCount": 1, "status": "OK",
 "results": [{"T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13, "c": 115.97, "v": 131704427, "vw": 116.3058, "t": 1605042000000}]}
```

`t` is Unix milliseconds (window start).

### Grouped daily bars (whole market, one call)

Best way to get end-of-day closes for many tickers: one call returns every US stock for a date.

```
GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=true&include_otc=false
```

```python
bars = client.get_grouped_daily_aggs("2026-11-06")   # list[GroupedDailyAgg]
closes = {b.ticker: b.close for b in bars if b.ticker in {"AAPL", "MSFT"}}
```

Result fields: `T` ticker, `o`, `h`, `l`, `c`, `v`, `vw`, `t` (ms, window end), `n`. Available on all plans, including free (end of day, 2 years of history on Basic). Returns an empty result for weekends and holidays.

### Daily bars for one ticker over a range

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
```

```python
for a in client.list_aggs("AAPL", 1, "day", "2026-10-01", "2026-11-06", limit=50000):
    print(a.timestamp, a.close)       # timestamp in ms
```

Pagination is on by default. For many tickers this costs one call per ticker, so prefer grouped daily bars.

### Daily open/close

```python
d = client.get_daily_open_close_agg("AAPL", "2026-11-06")
print(d.open, d.close, d.after_hours, d.pre_market)
```

## 4. Last trade (single ticker)

```
GET /v2/last/trade/{ticker}
```

```python
t = client.get_last_trade("AAPL")
print(t.price, t.size, t.sip_timestamp)   # nanoseconds
```

Requires a plan with trades access. One call per ticker, so not suited to polling a watchlist.

## Free-tier fallback

A key without snapshot access can still produce end-of-day prices:

1. Call `get_grouped_daily_aggs(<last trading day>)` once, filter to the tracked tickers.
2. Treat each `close` as the current price. Prices will not move until the next day, so the UI's "market closed" label applies.

This costs 1 call per refresh and stays well under 5 calls/min. FinAlly does not implement this fallback by default; it is listed because the plan advertises free-tier support.

## Errors

| HTTP | Meaning | Client behaviour |
|---|---|---|
| 401 | Bad or missing key | Raises; poller logs and retries next interval |
| 403 | Plan does not include the endpoint | Raises `NOT_AUTHORIZED` |
| 404 | Unknown ticker or no data (single-ticker endpoints) | Raises |
| 429 | Rate limit exceeded | Client retries up to 3 times with short backoff, then raises |

The 401/403/404 rows are the expected HTTP semantics; the docs pages checked do not specify error bodies.

## Polling cost

One `get_snapshot_all` call covers the full watchlist plus positions, so the cost is 1 call per poll regardless of ticker count. At the 15s default that is 4 calls/min, leaving 1/min spare for ticker validation on a 5 calls/min plan. Paid plans can poll every 2-5s.

## Sources

- https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot
- https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- https://github.com/massive-com/client-python
