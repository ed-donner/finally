# Massive API Fix - FinAlly

## Summary

FinAlly switched from simulator data to live Massive API data after `MASSIVE_API_KEY` was set, but market prices appeared frozen or incorrect. Root cause was a Massive SDK compatibility mismatch in snapshot request parameters and snapshot timestamp field handling.

This document records the issue and exact fix so it can be applied in other projects.

## Symptoms Observed

- Backend health endpoint showed `MassiveDataSource` was active.
- `/api/watchlist` returned seed-like fallback prices or `null` for non-seeded tickers.
- SSE stream often emitted only the retry preamble and no meaningful price updates.
- Container logs repeatedly showed:
  - `Massive poll failed: 404 page not found`
- After initial request fix, logs showed:
  - `Skipping snapshot for <TICKER>: 'LastTrade' object has no attribute 'timestamp'`

## Root Causes

### 1. `market_type` enum compatibility bug

The code called:

```python
get_snapshot_all(market_type=SnapshotMarketType.STOCKS, ...)
```

In the active SDK/runtime (`massive==2.2.0`), this call path produced a `404 page not found`.

The same call succeeded when `market_type` used the literal API value:

```python
get_snapshot_all(market_type="stocks", ...)
```

### 2. Timestamp field schema mismatch

The code assumed:

```python
snap.last_trade.timestamp
```

But the SDK response model exposed alternate fields such as:

- `last_trade.sip_timestamp`
- `last_trade.participant_timestamp`
- `last_trade.trf_timestamp`
- snapshot-level `updated`

Because `timestamp` was missing, snapshots were skipped and cache never refreshed.

## Code Changes Applied

File: `backend/app/market/massive_client.py`

1. Changed snapshot request market type from enum to literal:

```python
market_type="stocks"
```

2. Added robust timestamp extraction function with field fallbacks:

- `last_trade.timestamp`
- `last_trade.sip_timestamp`
- `last_trade.participant_timestamp`
- `last_trade.trf_timestamp`
- `snapshot.updated`
- fallback to `time.time()`

3. Added timestamp unit normalization logic:

- nanoseconds -> seconds
- microseconds -> seconds
- milliseconds -> seconds
- seconds passthrough

## Poll Cadence Change

File: `backend/app/market/factory.py`

- Massive poll interval now supports env override:
  - `MASSIVE_POLL_INTERVAL_SECONDS`
- Default set to:
  - `0.5` seconds

File: `docker-compose.yml`

- Added environment pass-through:
  - `MASSIVE_POLL_INTERVAL_SECONDS: ${MASSIVE_POLL_INTERVAL_SECONDS:-0.5}`

## Validation Performed

After patch + rebuild/restart:

- `/api/health` confirmed `MassiveDataSource` active.
- `/api/watchlist` returned live non-seed prices for tracked symbols.
- `/api/portfolio` reflected live valuations.
- SSE payloads contained live data and numeric timestamps.
- No recurring Massive `404` poll failures.
- Timed SSE capture confirmed ~0.5s event cadence.

Additional dynamic watchlist validation:

- Added `IBM` to watchlist.
- Initially `price: null` before first poll.
- Populated with live price on next poll cycle.
- Removed cleanly.

## Reuse Checklist for Other Projects

When integrating Massive snapshot polling elsewhere:

1. Prefer `market_type="stocks"` over enum object if you see unexplained 404s.
2. Do not assume `last_trade.timestamp` exists.
3. Implement timestamp fallbacks (`sip_timestamp`, `participant_timestamp`, `trf_timestamp`, `updated`).
4. Normalize timestamp units to Unix seconds before downstream use.
5. Make poll interval configurable with a sane default tied to your plan/rate limits.
6. Verify with both logs and live stream payload inspection (not only health checks).

## Relevant Files

- `backend/app/market/massive_client.py`
- `backend/app/market/factory.py`
- `docker-compose.yml`
- `planning/MASSIVE_API_FIX.md` (this file)
