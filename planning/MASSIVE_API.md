# Massive API (formerly Polygon.io) — Reference for FinAlly

Research notes and code examples for the endpoints FinAlly's market-data layer needs: real-time/latest quotes for multiple tickers and end-of-day (previous close) prices. This document is the input to `MARKET_INTERFACE.md`, which designs the unified Python interface built on top of it.

Sources: [API Docs](https://massive.com/docs), [Stocks REST Overview](https://massive.com/docs/rest/stocks/overview), [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot.md), [Unified Snapshot](https://massive.com/docs/rest/stocks/snapshots/unified-snapshot.md), [Single Ticker Snapshot](https://massive.com/docs/rest/stocks/snapshots/single-ticker-snapshot.md), [Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar.md), [Rate Limits](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis), [Python client](https://github.com/massive-com/client-python)

> Polygon.io rebranded to **Massive** in early 2026. `MASSIVE_API_KEY` in `.env` refers to this service.

## 1. Base URL & Authentication

- Base URL: `https://api.massive.com`
- Auth: `Authorization: Bearer <MASSIVE_API_KEY>` HTTP header (the official Python client sends this; the legacy Polygon.io `?apiKey=` query param may still work but the header form is documented as current).

```bash
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT"
```

## 2. Rate Limits

- **Free tier**: 5 requests/minute.
- **Paid tiers**: effectively unlimited, but Massive still asks clients to stay under ~100 req/s to avoid throttling.
- This is the reason PLAN.md §6 specifies polling (not per-request-per-tick) on a 15s interval for the free tier.

## 3. Endpoints Relevant to FinAlly

### 3.1 Full Market Snapshot (multi-ticker, one call) — **primary endpoint for polling the watchlist**

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,TSLA,GOOG
```

Params:
- `tickers` (optional, comma-separated, case-sensitive): restricts the snapshot to specific symbols — this is how FinAlly fetches all watchlist tickers in a single call.
- `include_otc` (optional, bool, default `false`)

Response:

```json
{
  "count": 1,
  "status": "OK",
  "tickers": [
    {
      "ticker": "AAPL",
      "todaysChange": -0.124,
      "todaysChangePerc": -0.601,
      "day": { "c": 20.506, "h": 20.64, "l": 20.506, "o": 20.64, "v": 37216, "vw": 20.616 },
      "prevDay": { "c": 20.63, "h": 21, "l": 20.5, "o": 20.79, "v": 292738 },
      "min": { "c": 20.506, "h": 20.506, "l": 20.506, "o": 20.506, "v": 5000 },
      "lastQuote": { "P": 20.6, "p": 20.5 },
      "lastTrade": { "p": 20.506, "s": 2416 },
      "updated": 1605192894630916600
    }
  ]
}
```

Fields FinAlly needs: `ticker`, `lastTrade.p` (latest price), `prevDay.c` (previous close, for daily % change fallback), `day.c` (today's close-so-far), `updated` (nanosecond epoch timestamp).

### 3.2 Single Ticker Snapshot

```
GET /v2/snapshot/locale/us/markets/stocks/tickers/{stocksTicker}
```

Same shape as one entry of the full snapshot's `tickers[]` array, nested under `ticker` instead. Useful for on-demand lookups (e.g., when the AI or user adds a new ticker to the watchlist and a price is needed immediately, without waiting for the next poll cycle).

### 3.3 Unified Snapshot (multi-asset-class, newer endpoint)

```
GET /v3/snapshot?ticker.any_of=AAPL,GOOGL,MSFT&limit=10
```

- `ticker.any_of`: comma-separated list, up to 250 tickers.
- Returns `last_quote.bid/ask`, `last_trade.price/size`, `session.open/high/low/close/volume`.
- Broader (covers options/crypto/forex too) but the field names differ from the v2 snapshot. **Recommendation: use 3.1 (`/v2/snapshot/.../tickers`) for FinAlly** since it's stocks-only, matches the schema FinAlly's `MarketDataSource` interface needs (see `MARKET_INTERFACE.md`), and keeps parsing simpler.

### 3.4 Previous Day Bar (end-of-day OHLC)

```
GET /v2/aggs/ticker/{stocksTicker}/prev?adjusted=true
```

```json
{
  "results": [
    { "T": "AAPL", "c": 115.97, "h": 117.59, "l": 114.13, "o": 115.55, "t": 1605042000000, "v": 131704427, "vw": 116.3058 }
  ],
  "status": "OK",
  "ticker": "AAPL"
}
```

Single-ticker only — for the watchlist's set of tickers this requires one call per ticker, so it's mainly useful for seeding a starting price for a newly-added ticker outside market hours, not for the live polling loop (use 3.1 for that, whose `prevDay` block already carries this data per ticker in one call).

## 4. Recommended Polling Strategy for FinAlly

1. On each poll tick, call **3.1 Full Market Snapshot** once with `tickers=` set to the comma-joined current watchlist (+ any tickers with open positions, per PLAN.md REVIEW.md note on delisted-from-watchlist positions).
2. Parse each entry into `{ticker, price: lastTrade.p, previous_close: prevDay.c, timestamp: updated}`.
3. Poll interval: 15s on free tier (5 req/min ceiling — one snapshot call per tick, well under the limit regardless of watchlist size since it's a single request). Configurable via env for paid tiers (down to 2s).
4. On request failure or rate-limit (HTTP 429): back off and retry next tick; do not fall back to the simulator mid-session (would produce a discontinuous price jump) — just skip the tick and keep the last known price in the cache.

## 5. Ticker Validation

Massive returns `status: "OK"` with an empty or partial `tickers[]` array for unknown symbols rather than erroring — so ticker existence should be validated via the snapshot response itself (empty result for that symbol = invalid/unrecognized ticker), not via a separate call to `/v3/reference/tickers`.
