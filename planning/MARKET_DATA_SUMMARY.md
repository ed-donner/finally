# Market Data Backend — Summary

Status: **Implemented** (`backend/market/`, `backend/config.py`, `backend/main.py`,
`backend/db/`). Full design rationale and copy-pasteable reference code are
archived in `planning/archive/` (`MARKET_DATA_DESIGN.md`, `MARKET_INTERFACE.md`,
`MARKET_SIMULATOR.md`, `MASSIVE_API.md`) — read those only if you need the *why*
behind a decision below. This file is the quick-reference for other agents
building on top of the market data subsystem (portfolio, chat, frontend).

## What exists

```
backend/
├── pyproject.toml       # uv project: fastapi, uvicorn, httpx, python-dotenv
├── config.py            # Settings — the only place env vars are read
├── main.py              # FastAPI app, lifespan wiring, GET /api/health
├── db/
│   ├── connection.py    # lazy sqlite schema init + seed (users_profile,
│   │                     watchlist, positions) at db/finally.db
│   └── reads.py         # load_active_tickers() — watchlist ∪ open positions
├── market/
│   ├── types.py          # PriceTick, Bar, Direction
│   ├── source.py          # MarketDataSource ABC: get_prices, get_history, aclose
│   ├── cache.py            # PriceCache — in-memory, lock-guarded, one PriceTick/ticker
│   ├── feed.py              # MarketFeed — single background writer to the cache
│   ├── factory.py            # make_source() — env-driven source selection
│   ├── seeds.py                # per-ticker GBM seed params (the 10 default tickers)
│   ├── gbm.py                    # SimEngine — correlated GBM + synthetic history
│   ├── simulator.py                # SimulatedSource (default, no key needed)
│   ├── massive.py                    # MassiveSource (used when MASSIVE_API_KEY is set)
│   └── routes.py                       # GET /api/stream/prices, GET /api/history/{ticker}
└── tests/                # pytest + pytest-asyncio; no network, deterministic (seeded)
```

## How to consume it (for the portfolio/chat/frontend agents)

- **Live prices**: read `app.state.price_cache` (a `PriceCache`). Call
  `await cache.snapshot()` to get `{ticker: PriceTick}` for portfolio valuation,
  or `await cache.get(ticker)` for one ticker. Never call a source directly.
- **Historical bars for a chart**: `GET /api/history/{ticker}?days=90` — works
  identically whether the simulator or Massive is active.
- **Live stream for the frontend**: `GET /api/stream/prices` (SSE). Each event's
  `data:` payload is a `PriceTick` JSON object:
  `{ticker, price, previous_price, direction, timestamp}`.
- **Watchlist/positions table access**: `db/reads.py` only exposes
  `load_active_tickers()`. The portfolio agent owns the rest of `db/` (trades,
  portfolio_snapshots, chat_messages, and the full CRUD needed for watchlist
  management and trade execution) — extend `db/connection.py`'s schema rather
  than replacing it, since `watchlist` and `positions` are already relied on by
  the market feed.
- **Source selection**: automatic. `MASSIVE_API_KEY` set and non-empty →
  `MassiveSource`; otherwise → `SimulatedSource`. Nothing downstream needs to
  know which one is active.

## Key behaviors to rely on

- The feed primes the cache synchronously on startup (`feed.prime()`), so the
  first request always has data — no empty-cache race.
- `load_active_tickers()` returns the **union** of watchlist tickers and tickers
  with an open position (`quantity > 0`), so a position in a de-watchlisted
  ticker stays priced for valuation.
- Source failures (Massive rate limits, bad key, transport errors) never raise
  into the feed loop — they return `{}`/`[]` and the cache simply holds its last
  values.
- `SIM_SEED` env var makes the simulator fully deterministic — useful for E2E
  tests that need reproducible price sequences.

## Testing

`cd backend && uv sync && uv run pytest` — all tests are deterministic, mock
network calls (`httpx.MockTransport` for Massive), and require no API key.
