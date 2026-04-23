# Massive Market Data API

Research notes for the real-data implementation of FinAlly's market data layer. Covers the post-rebrand state of the API formerly known as Polygon.io.

**Status:** 2026-04-22. Verify pricing and headers in a browser before quoting to users — the pricing page resists automated fetches.

---

## Rebrand Context

- Polygon.io was renamed to **Massive** effective **2026-10-30** (note: dates in this codebase run April 2026; the rebrand announcement itself is dated Oct 30 2025 in Massive's own blog, written for a general audience — our calendar is slightly ahead).
- APIs, keys, endpoints, tiers, and data quality are unchanged. Existing Polygon API keys continue to work.
- Documentation moved: `polygon.io/docs` now 301-redirects to `massive.com/docs`. Client library repos moved from the `polygon-io` GitHub org to `massive-com`.
- **Dual-host window:** `https://api.massive.com` is the new base URL. `https://api.polygon.io` still works and will for "an extended period" — no hard cutoff announced. We should target `api.massive.com` in new code and fall back only if needed.

Sources: [Massive rebrand blog](https://massive.com/blog/polygon-is-now-massive), [FISD notice](https://fisd.net/polygon-io-is-now-massive/), [client-python README](https://github.com/massive-com/client-python/blob/master/README.md).

---

## Authentication

Two equivalent methods, unchanged from the Polygon era:

- **Authorization header (preferred):** `Authorization: Bearer <API_KEY>`
- **Query parameter:** `?apiKey=<API_KEY>`

The official Python client accepts the key via constructor:

```python
from polygon import RESTClient  # package still named `polygon` for the existing client
client = RESTClient(api_key=os.environ["MASSIVE_API_KEY"])
```

A newer `massive-com/client-python` repo exists and defaults to `api.massive.com` but is API-compatible. Either works for our use case.

---

## Endpoint Choice

For a watchlist of ~10 US equities we need **the latest price per ticker in as few HTTP calls as possible**. Three viable endpoints:

### Recommended: multi-ticker snapshot (one call for the whole watchlist)

```
GET /v2/snapshot/locale/us/markets/stocks/tickers
    ?tickers=AAPL,GOOGL,MSFT,AMZN,TSLA,NVDA,META,JPM,V,NFLX
```

- **One HTTP call for all 10 tickers.** Case-sensitive, comma-separated.
- Optional `include_otc` (bool, default `false`).
- Fits the free tier's 5 calls/min budget comfortably: polling every 15s = 4 calls/min.
- Returns rich per-ticker data (day bar, prev close, last trade, last quote, change %).

Docs: [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot).

### Alternatives (not recommended for our use case)

- `/v2/last/trade/{ticker}` — last trade only, one call per ticker (10 calls for our watchlist). Blows the free-tier budget.
- `/v2/snapshot/locale/us/markets/stocks/tickers/{ticker}` — single-ticker snapshot. Same problem.
- `/v3/snapshot` (Unified Snapshot) — cross-asset multi-ticker, up to 250 tickers/request with `next_url` pagination. Overkill for pure stocks; the v2 snapshot is simpler and returns the fields we care about by default.

---

## Response Shape (multi-ticker snapshot)

```json
{
  "status": "OK",
  "count": 10,
  "tickers": [
    {
      "ticker": "AAPL",
      "day":     { "o": 189.3, "h": 192.1, "l": 188.9, "c": 191.4, "v": 45231200, "vw": 190.7 },
      "prevDay": { "o": 188.0, "h": 190.5, "l": 187.5, "c": 190.0, "v": 50120000, "vw": 189.2 },
      "min":     { "o": 191.2, "h": 191.5, "l": 191.0, "c": 191.4, "v": 12000, "vw": 191.3 },
      "lastTrade": { "p": 191.42, "s": 100, "t": 1776321000120000000, "x": 11 },
      "lastQuote": { "bp": 191.40, "ap": 191.43, "bs": 2, "as": 3, "t": 1776321000119000000 },
      "todaysChange": 1.42,
      "todaysChangePerc": 0.747,
      "updated": 1776321000120000000
    }
  ]
}
```

### Fields to extract into the price cache

| Cache field | JSON path | Notes |
|---|---|---|
| current price | `tickers[i].lastTrade.p` | Preferred — matches the user's "latest quote" expectation |
| day-over-day prev close | `tickers[i].prevDay.c` | For later day-change display (not required by v1) |
| session change / % | `tickers[i].todaysChange`, `.todaysChangePerc` | Ready-computed; avoid re-deriving |
| timestamp | `tickers[i].updated` | **Nanoseconds** — divide by 1e9 for UNIX seconds |
| bid / ask | `tickers[i].lastQuote.bp`, `.ap` | Optional; we don't need for v1 |

Timestamp gotcha: the current `MassiveDataSource` (`backend/app/market/massive_client.py:103`) divides `last_trade.timestamp` by `1000`, assuming milliseconds. **Snapshot responses use nanoseconds** (`.t` and `.updated`). Verify at runtime and fix the divisor if necessary — this is the kind of subtle off-by-1e6 that produces timestamps from the year 57000.

---

## Tier / Rate Limits

| Tier | Price | Rate limit | Data | WebSocket |
|---|---|---|---|---|
| Basic (free) | $0 | **5 req/min** | 15-min delayed, EOD | No |
| Starter | $29/mo | Unlimited REST | 15-min delayed | No |
| Developer | $79/mo | Unlimited | **Real-time** | Not included |
| Advanced | $199/mo | Unlimited | Real-time | **Included** |

Sources: [Massive KB — request limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis), [DataGlobeHub aggregator (Feb 2026 pricing)](https://dataglobehub.com/api-finder/massive-api/). **`massive.com/pricing` blocked automated fetch** (JS-rendered or bot-protected); verify in a browser before quoting publicly.

Key takeaways:

- The PLAN.md figure of **"5 calls/min on free tier"** is still accurate.
- **15-min delayed data on free and Starter** — a meaningful caveat for a UI that advertises "live" prices. Real-time requires **Developer ($79/mo)** or higher.
- The project is designed to fall back to the simulator when no key is configured, so most users will never hit the API. This is deliberate.

---

## Rate-Limit / Error Behavior

- **429 Too Many Requests** is the over-limit status code.
- Response headers on 429 are not documented explicitly by Massive. Standard practice is `Retry-After` plus possibly `X-RateLimit-*`, but the actual header set should be **logged empirically on the first 429** in development rather than assumed.
- Recommended client strategy: pace at a known cadence (one call every 15s for free tier) so 429s never fire under normal operation. Treat any 429 as a 60-second cooldown; exponential backoff otherwise.
- Network / 5xx errors: the existing `MassiveDataSource` catches all exceptions in `_poll_once()` and logs without re-raising, so the poll loop survives transient failures. Keep that behavior.

---

## WebSocket (out of scope)

- Available at `wss://socket.polygon.io` (also `wss://socket.massive.com`).
- Bundled with Advanced ($199/mo) and above only.
- Would eliminate polling entirely and give push-based real-time. Worth revisiting if the project ever targets paid tiers, but irrelevant for v1.

Docs: [WebSocket quickstart](https://massive.com/docs/websocket/quickstart).

---

## Integration Checklist

When wiring the client into FinAlly:

1. Read `MASSIVE_API_KEY` from env; empty/unset → use simulator (already implemented in `factory.py`).
2. Poll `GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=<csv>` every 15s by default; make the interval configurable for paid-tier users.
3. Parse `tickers[].lastTrade.p` for price, `tickers[].updated` for timestamp (nanoseconds → seconds).
4. Write to the shared `PriceCache` exactly as the simulator does — downstream (SSE, portfolio) is source-agnostic.
5. On 429, sleep 60s then retry at normal cadence. On any other exception, log and continue.
6. Normalize tickers to uppercase before adding to the polling set.
7. Include `include_otc=false` explicitly (future-proof against default changes).

---

## Known Issues in the Current Implementation

From the code inventory (`backend/app/market/massive_client.py`):

- **Timestamp divisor:** line 103 divides by 1000 (ms assumption). Snapshot responses are in nanoseconds. Needs verification + fix.
- **No explicit 429 handling:** the generic `except Exception` catches everything equally. A targeted `429 → sleep 60s` branch would be more honest about what's happening.
- **Single-endpoint coupling:** client is hardwired to `get_snapshot_all()`. If we later want individual last-trade calls or unified snapshot, the client needs a small refactor.

---

## Citations

- [Polygon.io is Now Massive — rebrand blog](https://massive.com/blog/polygon-is-now-massive)
- [FISD: Polygon.io is now Massive](https://fisd.net/polygon-io-is-now-massive/)
- [Full Market Snapshot — Stocks REST](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot)
- [Single Ticker Snapshot — Stocks REST](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot)
- [Unified Snapshot v3](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot)
- [Last Trade endpoint](https://massive.com/docs/rest/stocks/trades-quotes/last-trade)
- [Massive KB — RESTful request limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis)
- [WebSocket quickstart](https://massive.com/docs/websocket/quickstart)
- [massive-com/client-python README](https://github.com/massive-com/client-python/blob/master/README.md)
- [DataGlobeHub Massive API summary (Feb 2026 pricing)](https://dataglobehub.com/api-finder/massive-api/)
