# Massive API (formerly Polygon.io)

Reference for retrieving realtime and end-of-day US stock prices for multiple
tickers. Massive is the rebrand of Polygon.io (renamed October 2025); the API
surface, endpoints, and existing API keys are unchanged. Documentation lives at
<https://massive.com/docs>.

This document covers only the endpoints FinAlly needs: pulling the latest price
for the union of watched tickers on a polling interval, plus historical bars for
the detailed chart. See `MARKET_INTERFACE.md` for how we wrap this behind a
unified interface, and `MARKET_SIMULATOR.md` for the fallback used when no key is
set.

---

## 1. Basics

- **Base URL:** `https://api.massive.com`
- **Auth:** API key. Either as a query parameter `?apiKey=YOUR_KEY` or as a
  header `Authorization: Bearer YOUR_KEY`. FinAlly reads it from the
  `MASSIVE_API_KEY` environment variable.
- **Format:** JSON over HTTPS. All timestamps are Unix **nanoseconds** for
  trades/quotes and Unix **milliseconds** for aggregate bars.
- **Tickers are case-sensitive** (`AAPL`, not `aapl`).

### Rate limits

| Plan | Requests | Data freshness |
|------|----------|----------------|
| Free (Basic) | 5 requests / minute | End-of-day + 15-minute-delayed |
| Paid (Starter and up) | Effectively unlimited (stay < ~100 req/s) | Realtime |

The free tier is delayed and heavily rate-limited. This is why FinAlly polls the
**snapshot** endpoint (one request returns every watched ticker) rather than
making one request per ticker, and why the default poll interval is 15 seconds
on the free tier. Realtime, tick-level data requires a paid plan.

---

## 2. Price polling — Full Market Snapshot (primary endpoint)

This is the workhorse for FinAlly. A single request returns the current day
aggregate, last trade, and previous-day aggregate (with computed change) for a
comma-separated list of tickers.

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers={CSV}
```

**Query parameters**

| Parameter | Type | Notes |
|-----------|------|-------|
| `tickers` | string | Case-sensitive comma-separated list, e.g. `AAPL,TSLA,GOOGL`. Omit to get the entire market. |
| `include_otc` | bool | Default `false`. |

**Response**

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": -0.124,
      "todaysChangePerc": -0.601,
      "day":  { "o": 20.64, "h": 20.64, "l": 20.50, "c": 20.506, "v": 37216 },
      "prevDay": { "o": 20.79, "h": 21.0, "l": 20.5, "c": 20.63, "v": 292738 },
      "lastTrade": { "p": 20.506, "s": 2416, "t": 1605192894630916600, "x": 4 }
    }
  ]
}
```

**Field meanings**

- `lastTrade.p` — last trade price. **This is the "current price" FinAlly uses.**
- `lastTrade.t` — trade timestamp (Unix nanoseconds).
- `day.c` / `day.o` / `day.h` / `day.l` / `day.v` — today's OHLCV aggregate.
- `prevDay.c` — previous session close (baseline for daily change %).
- `todaysChange` / `todaysChangePerc` — pre-computed change vs previous close.

**Choosing the current price:** prefer `lastTrade.p`. When the market is closed
or `lastTrade` is missing, fall back to `day.c`, then `prevDay.c`.

### Example (httpx, async — recommended for FinAlly)

```python
import httpx

BASE = "https://api.massive.com"

async def fetch_snapshot(api_key: str, tickers: list[str]) -> dict[str, float]:
    """Return {ticker: current_price} for the requested tickers in one call."""
    url = f"{BASE}/v2/snapshot/locale/us/markets/stocks/tickers"
    params = {"tickers": ",".join(tickers)}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        body = resp.json()

    prices: dict[str, float] = {}
    for item in body.get("tickers", []):
        last = item.get("lastTrade", {}).get("p")
        day_close = item.get("day", {}).get("c")
        prev_close = item.get("prevDay", {}).get("c")
        price = last or day_close or prev_close
        if price:
            prices[item["ticker"]] = float(price)
    return prices
```

One request, every watched ticker, fully async — the right shape for a FastAPI
background poller.

---

## 3. Alternative snapshot — Unified Snapshot (v3)

A newer endpoint that returns a normalized `session` block. Also supports
multiple tickers (up to 250 per call via `ticker.any_of`).

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=250
```

```json
{
  "status": "OK",
  "results": [
    {
      "ticker": "AAPL",
      "type": "stocks",
      "market_status": "closed",
      "last_trade": { "price": 21.25, "size": 2, "exchange": 316 },
      "last_quote": { "bid": 20.9, "ask": 21.25, "last_updated": 1636573458756383500 },
      "session": {
        "open": 22.49, "high": 22.49, "low": 21.35, "close": 21.4,
        "change": -1.05, "change_percent": -4.67, "volume": 37
      }
    }
  ]
}
```

Either endpoint works. FinAlly standardizes on the **v2 full-market snapshot**
(section 2) because its `tickers` list maps cleanly onto our watchlist and it is
the most widely documented. The v3 endpoint is a drop-in alternative if a
normalized schema is preferred; the mapping is `results[].last_trade.price` for
current price and `results[].session.close`/`change_percent` for daily change.

---

## 4. Previous-day close (single ticker)

Useful for seeding a baseline or a single-ticker refresh. Returns one prior-day
OHLC bar.

```
GET /v2/aggs/ticker/{ticker}/prev?adjusted=true
```

```json
{
  "status": "OK",
  "ticker": "AAPL",
  "resultsCount": 1,
  "results": [
    { "T": "AAPL", "o": 115.55, "h": 117.59, "l": 114.13,
      "c": 115.97, "v": 131704427, "vw": 116.3058, "t": 1605042000000 }
  ]
}
```

`c` is the close. `t` is Unix milliseconds. For the multi-ticker case, the
snapshot endpoint already carries `prevDay.c`, so this endpoint is only needed
for one-off lookups.

---

## 5. Historical bars — Custom Bars / Aggregates (for the detail chart)

Used to backfill the main chart area with historical price action for the
selected ticker (the SSE stream only accumulates data since page load).

```
GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}
```

**Path parameters**

| Param | Meaning |
|-------|---------|
| `ticker` | Case-sensitive symbol, e.g. `AAPL`. |
| `multiplier` | Size of the timespan window, e.g. `5`. |
| `timespan` | `minute`, `hour`, `day`, `week`, `month`, `quarter`, `year`. |
| `from` / `to` | `YYYY-MM-DD` or Unix millisecond timestamp. |

**Query parameters:** `adjusted` (default `true`), `sort` (`asc`/`desc`),
`limit` (default 5000, max 50000).

```json
{
  "status": "OK",
  "ticker": "AAPL",
  "adjusted": true,
  "resultsCount": 2,
  "results": [
    { "o": 74.06, "h": 75.15, "l": 73.79, "c": 75.08,
      "v": 135647456, "vw": 74.61, "n": 1, "t": 1577941200000 }
  ]
}
```

Field meanings: `o/h/l/c` OHLC, `v` volume, `vw` volume-weighted average price,
`n` number of transactions, `t` Unix millisecond timestamp (bar start).

### Example

```python
async def fetch_daily_bars(api_key: str, ticker: str, start: str, end: str):
    url = (f"{BASE}/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}")
    params = {"adjusted": "true", "sort": "asc", "limit": 5000}
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(url, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json().get("results", [])
```

---

## 6. Official Python client (reference)

Massive publishes an official synchronous client. FinAlly does **not** use it
(we prefer async `httpx` with the snapshot endpoint, one call for all tickers),
but it is documented here for completeness.

```bash
pip install -U massive          # uv add massive
```

```python
from massive import RESTClient

client = RESTClient(api_key="YOUR_KEY")   # or RESTClient() reads MASSIVE_API_KEY

trade = client.get_last_trade(ticker="AAPL")     # latest trade
quote = client.get_last_quote(ticker="AAPL")     # latest NBBO quote

for bar in client.list_aggs(ticker="AAPL", multiplier=1, timespan="day",
                            from_="2024-01-01", to="2024-06-13", limit=5000):
    print(bar.close)
```

The client is synchronous. If used inside FastAPI, wrap calls in
`asyncio.to_thread(...)` to avoid blocking the event loop. A WebSocket client
also exists (`from massive import WebSocketClient`) but FinAlly deliberately uses
REST polling instead — see the rationale in `PLAN.md`.

---

## 7. What FinAlly actually uses

| Need | Endpoint | Interval |
|------|----------|----------|
| Live price for all watched tickers | `/v2/snapshot/locale/us/markets/stocks/tickers?tickers=...` | 15s (free) / 2-15s (paid) |
| Historical bars for detail chart | `/v2/aggs/ticker/{t}/range/1/day/{from}/{to}` | On demand (ticker selected) |

One snapshot request covers the entire watchlist, staying within the free-tier
5-req/min budget. The poller writes results into the shared in-memory price
cache; everything downstream (SSE, portfolio math) reads from the cache and is
agnostic to whether the data came from Massive or the simulator.

---

## 8. Error handling notes

- **429 Too Many Requests** — free tier exceeded 5/min. Back off; keep the poll
  interval at 15s or higher.
- **403 / 401** — missing or invalid `MASSIVE_API_KEY`.
- **Empty `tickers` array** — an unknown or delisted symbol was requested;
  the response simply omits it. Missing tickers should retain their last cached
  price rather than error.
- **Delayed data on free tier** — prices are 15 minutes behind and the market may
  be closed; treat `lastTrade` as possibly stale and fall back to `day.c` /
  `prevDay.c`.

---

## Sources

- [Polygon.io is Now Massive](https://massive.com/blog/polygon-is-now-massive)
- [Stocks REST API Overview](https://massive.com/docs/rest/stocks/overview)
- [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar)
- [Custom Bars (Aggregates)](https://massive.com/docs/rest/stocks/aggregates/custom-bars)
- [Massive + Python guide](https://massive.com/blog/polygon-io-with-python-for-stock-market-data)
- [Official Python client](https://github.com/massive-com/client-python)
- [REST request limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
