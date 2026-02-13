# Market Data Backend - Current Summary (2026-02-13)

## Status

Market data is running in production mode through `MassiveDataSource` with active SSE streaming to the frontend.

Current behavior is validated end-to-end:

- `/api/health` reports `market_source: MassiveDataSource`
- `/api/stream/prices` is live and continuously publishing
- `/api/watchlist` reflects live cache updates for 50 grouped watchlist symbols

## Active Architecture

```
MassiveDataSource (polling)
  -> PriceCache (thread-safe in-memory latest state)
    -> /api/stream/prices (SSE)
    -> /api/watchlist
    -> portfolio valuation + trade execution
```

Key files:

- `backend/app/market/massive_client.py`
- `backend/app/market/cache.py`
- `backend/app/market/stream.py`
- `backend/app/market/factory.py`

## Price Source Resolution (Implemented)

To reduce static behavior when `last_trade` becomes stale, price selection in `MassiveDataSource` now uses this priority:

1. Fresh `last_trade` price (within `MASSIVE_STALE_TRADE_SECONDS`, default `10s`)
2. `last_quote` midpoint (`(bid + ask) / 2`) with valid quote timestamp
3. `last_minute.close` with valid minute timestamp
4. Stale `last_trade` price as last resort if no better source exists

Additional protections:

- Reject invalid / non-positive numeric values
- Normalize mixed timestamp units (s / ms / us / ns)
- Preserve same-business-day baseline extraction:
  - day open -> previous close -> derived from `todays_change`

## Feed Observations and Interpretation

Live inspection during US market hours showed:

- The stream is functioning and publishing.
- The watchlist now updates more visibly after fallback pricing changes.
- Some payloads from the account/feed still carry delayed trade/quote timelines (`timeframe: DELAYED` observed in v3 snapshot responses).

Implication:

- Apparent "flatness" can be feed-timeline driven even when app plumbing is healthy.
- The fallback pipeline improves visible movement, but cannot exceed source freshness/entitlements.

## Watchlist Universe Validation

All 50 default watchlist symbols were checked against ticker reference metadata and are common stocks (`type=CS`).

Result:

- The default watchlist is not mixing in ETFs/index funds as a cause of missing realtime updates.

## Runtime Configuration

Primary env knobs:

- `MASSIVE_API_KEY` - enables real market source
- `MASSIVE_POLL_INTERVAL_SECONDS` - polling cadence (currently `0.5` in app runtime)
- `MASSIVE_STALE_TRADE_SECONDS` - freshness cutoff for preferring `last_trade` (default `10`)

## Operational Notes

After backend market-data changes, rebuild the container image for local runtime parity:

```bash
./scripts/start_mac.sh --build
```

This has been repeatedly validated in this environment with successful startup + health checks.
