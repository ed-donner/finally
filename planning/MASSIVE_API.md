# Massive API Reference (formerly Polygon.io)

Research notes for the FinAlly market data subsystem. Covers only what this project needs:
fetching current and end-of-day prices for a handful of tickers.

Polygon.io rebranded to Massive on 30 October 2025. Existing API keys, code, and
`api.polygon.io` URLs continue to work; the Python SDK and docs have moved to the new name.

## Package and Client

The official SDK is the `massive` PyPI package. This project pins **massive 2.2.0**.

```bash
uv add massive
```

```python
from massive import RESTClient

client = RESTClient(api_key="your-key")   # explicit
client = RESTClient()                     # reads MASSIVE_API_KEY from the environment
```

Verified against the installed package: `RESTClient()` with no argument picks up
`MASSIVE_API_KEY` automatically. FinAlly passes the key explicitly anyway, because the
factory has already read the variable to decide simulator-vs-real (see `MARKET_INTERFACE.md`).

Default base URL is `https://api.massive.com`. Constructor options worth knowing:

```python
RESTClient(
    api_key=None,
    connect_timeout=10.0,
    read_timeout=10.0,
    retries=3,              # SDK retries failed requests itself
    base="https://api.massive.com",
    pagination=True,
)
```

The client is **synchronous** (urllib3 under the hood). In an async FastAPI app every call
must be wrapped in `asyncio.to_thread(...)` or it blocks the event loop.

## Authentication

Two equivalent forms:

```bash
# Query parameter
curl "https://api.massive.com/v2/aggs/ticker/AAPL/prev?apiKey=YOUR_KEY"

# Authorization header (preferred - keeps the key out of logs and URLs)
curl -H "Authorization: Bearer YOUR_KEY" \
     "https://api.massive.com/v2/aggs/ticker/AAPL/prev"
```

The SDK uses the header form. Keys come from https://massive.com/dashboard/keys.

## Rate Limits and Plan Tiers

| Tier | Requests | Data recency | History |
|---|---|---|---|
| Basic (free) | 5 / minute | End-of-day | 2 years |
| Starter / Developer | Unlimited | 15-minute delayed | 5-10 years |
| Advanced / Business | Unlimited | Real-time | Back to Sept 2003 |

The free tier's 5 requests/minute is the binding constraint on FinAlly's design: it permits
one request every 12 seconds. This is why the poller fetches **all watched tickers in a
single request** and defaults to a 15-second interval, rather than one request per ticker.

## Endpoints

### Full Market Snapshot — the one FinAlly polls

Returns a complete snapshot for many tickers in one request. This is the workhorse.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

| Parameter | Type | Notes |
|---|---|---|
| `tickers` | comma-separated list | Case-sensitive. Omit or pass empty to get all 10,000+ tickers |
| `include_otc` | boolean | Defaults to `false` |

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient(api_key=key)
snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],
)

for snap in snapshots:
    print(snap.ticker, snap.last_trade.price, snap.todays_change_percent)
```

Signature:

```python
get_snapshot_all(
    market_type: str | SnapshotMarketType,
    tickers: str | List[str] | None = None,
    params: Dict[str, Any] | None = None,
    raw: bool = False,
    include_otc: bool | None = False,
) -> List[TickerSnapshot]
```

`TickerSnapshot` fields (from the installed package):

```
ticker                  str
last_trade              LastTradeSnapshot  (.price, .size, .timestamp, .exchange)
last_quote              LastQuoteSnapshot  (.bid_price, .ask_price, .timestamp)
day                     Agg                (.open, .high, .low, .close, .volume, .vwap)
min                     MinuteSnapshot     (.open, .high, .low, .close, .volume, .timestamp)
prev_day                Agg                (previous session OHLCV)
todays_change           float
todays_change_percent   float
updated                 int                (nanoseconds - see below)
fair_market_value       float | None
```

Raw JSON, if you bypass the SDK, uses short keys — `lastTrade.p` is price, `day.c` is close,
`todaysChangePerc`, and so on. The SDK's long names map onto these.

### Single Ticker Snapshot

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}
```

```python
snap = client.get_snapshot_ticker(SnapshotMarketType.STOCKS, "AAPL")
```

Not used by FinAlly — one request per ticker exhausts the free tier's budget almost
immediately. Documented for completeness.

### Unified Snapshot (v3)

A newer, paginated endpoint spanning asset classes.

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=250
```

| Parameter | Notes |
|---|---|
| `ticker.any_of` | Comma-separated, **max 250 tickers** |
| `limit` | Default 10, max 250 |
| `type` | `stocks`, `options`, `fx`, `crypto`, `indices` |
| `sort`, `order`, `ticker.gt/gte/lt/lte` | Filtering and ordering |

```python
for snap in client.list_universal_snapshots(
    type="stocks", ticker_any_of=["AAPL", "GOOGL"], limit=250
):
    print(snap.ticker, snap.session)
```

Note the **default `limit` is 10** — asking for 30 tickers without raising `limit` silently
returns 10. FinAlly stays on the v2 endpoint, which has no such trap and returns exactly the
tickers requested.

### Custom Bars / Aggregates — for sparkline history

```
GET /v2/aggs/ticker/{stocksTicker}/range/{multiplier}/{timespan}/{from}/{to}
```

| Parameter | Notes |
|---|---|
| `multiplier` | Integer scaling the timespan (e.g. `1`) |
| `timespan` | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year` |
| `from` / `to` | `YYYY-MM-DD` or millisecond epoch |
| `adjusted` | Split-adjusted; defaults `true` |
| `sort` | `asc` or `desc` |
| `limit` | Default 5,000, max 50,000 |

```python
bars = client.get_aggs(
    ticker="AAPL", multiplier=1, timespan="minute",
    from_="2026-08-03", to="2026-08-03", limit=60, sort="desc",
)
history = [bar.close for bar in reversed(bars)]   # oldest first
```

`Agg` fields: `open`, `high`, `low`, `close`, `volume`, `vwap`, `timestamp`,
`transactions`, `otc`.

This is how FinAlly backfills the ~60 points of sparkline history when running against real
data. One request per ticker, made once at startup — acceptable because it is not on the
polling loop.

### Previous Day Bar

```
GET /v2/aggs/ticker/{stocksTicker}/prev
```

```python
prev = client.get_previous_close_agg("AAPL")
print(prev.close)
```

Returns the prior session's OHLCV. Available on every tier including free, which makes it the
reliable fallback for a session baseline when the market is closed.

### Daily Ticker Summary — end-of-day open/close

```
GET /v1/open-close/{stocksTicker}/{date}
```

```python
day = client.get_daily_open_close_agg(ticker="AAPL", date="2026-08-03")
print(day.open, day.close, day.pre_market, day.after_hours)
```

`DailyOpenCloseAgg` fields: `open`, `high`, `low`, `close`, `volume`, `pre_market`,
`after_hours`, `from_`, `status`, `symbol`, `otc`.

This is the true end-of-day endpoint, including extended-hours prices. Useful if FinAlly ever
wants a real "official daily open" rather than the session baseline described in `PLAN.md`.

### Market Status

```python
status = client.get_market_status()   # GET /v1/marketstatus/now
```

Tells you whether the market is open, closed, or in extended hours. Worth surfacing in
`/api/health` so "prices are not moving" can be explained rather than debugged.

## Timestamp Units — the trap

Massive uses **different time units in different places**, and mixing them up produces
timestamps tens of thousands of years off without raising an error.

| Field | Unit |
|---|---|
| `last_trade.timestamp`, `last_quote.timestamp`, `snapshot.updated` | **nanoseconds** |
| `Agg.timestamp` (bars from `/v2/aggs`), `min.t` | **milliseconds** |

Proof, using the `lastTrade.t` value from Massive's own sample response
(`1605195918306274000`):

```
/ 1e3  (as ms) -> out of range
/ 1e6  (as us) -> out of range
/ 1e9  (as ns) -> 2020-11-12 15:45:18 UTC   correct
```

So the conversion to the Unix-seconds float that `PriceCache` expects is:

```python
timestamp = snap.last_trade.timestamp / 1_000_000_000.0   # ns -> s
bar_time  = bar.timestamp / 1_000.0                       # ms -> s
```

**This is currently wrong in the codebase.** `backend/app/market/massive_client.py:106` reads
`timestamp = snap.last_trade.timestamp / 1000.0`, treating nanoseconds as milliseconds. Every
real-data price lands in the cache stamped roughly 50,000 years in the future. It has gone
unnoticed because the default path is the simulator and nothing yet reads the timestamp. It
must be fixed before the Massive path is trusted — see `MARKET_INTERFACE.md`.

## Behaviour Outside Market Hours

US equities trade 09:30-16:00 ET, weekdays. Outside that window `last_trade` holds the final
trade of the previous session and does not change. Consequences for FinAlly:

- Every price is flat; no flash animations fire
- `change_from_open_percent` sits at zero
- The SSE cache version never advances, so the stream emits only heartbeats

This is correct behaviour, not a fault. It is the main reason the simulator remains the
recommended default for demos and for the course. A UI running on real data at 21:00 looks
broken to someone who does not know the market is shut, so `/api/health` reporting
`market_source` and price age matters.

Free-tier data is additionally 15 minutes delayed, and the free tier's snapshot access is
limited — the single-ticker and full-market snapshot endpoints require Starter or above for
real-time. On the free tier the aggregate endpoints (`/v2/aggs/...`) are the dependable ones.

## Error Handling

| Status | Cause | Response |
|---|---|---|
| 401 | Missing or invalid key | Log once, keep polling; do not crash the app |
| 403 | Endpoint not in plan | Log the endpoint; consider falling back to aggregates |
| 429 | Rate limit exceeded | Back off; raise the poll interval |
| 5xx / network | Transient | SDK retries 3 times; the poll loop retries on the next tick |

The governing rule for FinAlly: **a market data failure must never take down the app**. The
poller catches broadly, logs, and lives to poll again. Stale prices in the cache are strictly
better than a 500 on every portfolio request.

## Sources

- [Polygon.io is Now Massive](https://massive.com/blog/polygon-is-now-massive)
- [Stocks REST API Overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot)
- [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Custom Bars (OHLC)](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Daily Ticker Summary](https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary)
- [Official Python client](https://github.com/massive-com/client-python)
- [Request limits](https://polygon.io/knowledge-base/article/what-is-the-request-limit-for-polygons-restful-apis)
