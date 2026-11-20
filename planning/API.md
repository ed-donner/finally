# FinAlly — HTTP and SSE Contract

This file is the contract between the backend and the frontend. Business rules are in [`PLAN.md`](PLAN.md) Section 8. If the two disagree, fix this file first, then the code.

## Conventions

- **Base path:** all routes are under `/api`, same origin, no CORS.
- **Content type:** request and response bodies are `application/json; charset=utf-8`, except the SSE stream.
- **Timestamps:** ISO 8601 in UTC with a `Z` suffix, e.g. `2026-11-18T14:30:00Z`. The SSE `history` arrays use epoch milliseconds for compactness.
- **Money:** JSON numbers, rounded to 2 decimal places. Prices may carry more precision and are rounded to cents for display only.
- **Quantities:** JSON numbers, up to 4 decimal places.
- **Host allowlist:** requests whose `Host` header is not `localhost:8000`, `127.0.0.1:8000`, or the bare names are rejected with `400 invalid_request`. This blocks DNS-rebinding attacks.
- **Validation errors:** FastAPI's default `422` responses are overridden. Every request validation failure returns `400 invalid_request` in the error format below, with the field path in `details.field`.
- **Tickers:** uppercase strings matching `^[A-Z]{1,5}(\.[A-Z])?$`. Lowercase input is normalized to uppercase before validation.

### Error Format

Every non-2xx response, including validation errors, uses this shape:

```json
{
  "error": {
    "code": "insufficient_cash",
    "message": "Buy requires $1,234.56; available cash is $100.00.",
    "details": {"required": 1234.56, "available": 100.00}
  }
}
```

- `code` is a stable machine-readable string. Clients branch on `code`, never on `message`.
- `message` is human-readable and may change.
- `details` is optional.

### Error Codes

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `invalid_request` | Body or query failed validation (bad ticker format, quantity out of bounds, missing field) |
| 400 | `reset_not_confirmed` | Reset body did not contain `"confirm": "RESET"` |
| 415 | `unsupported_media_type` | Request with a body was not `Content-Type: application/json` |
| 429 | `rate_limited` | `POST /api/chat` exceeded 10 requests per minute. Response includes `Retry-After` in seconds. |
| 404 | `not_found` | Route resource does not exist (for example, DELETE of a ticker not on the watchlist) |
| 404 | `unknown_ticker` | Data source cannot price this ticker (Massive mode) |
| 409 | `already_in_watchlist` | POST of an existing watchlist ticker |
| 409 | `insufficient_cash` | Buy costs more than available cash |
| 409 | `insufficient_shares` | Sell exceeds held quantity |
| 409 | `price_unavailable` | No cached price for the ticker yet; nothing was filled |
| 503 | `llm_unavailable` | `OPENROUTER_API_KEY` is not configured and `LLM_MOCK` is not `true` |
| 500 | `internal_error` | Unexpected failure; details are logged server-side, not returned |

---

## Market Data

### `GET /api/stream/prices` — SSE price stream

Long-lived `text/event-stream`. Clients use native `EventSource`.

**Response headers:** `Content-Type: text/event-stream`, `Cache-Control: no-cache`.

**Stream preamble:** the server sends `retry: 1000` on connect.

**Event types:** `snapshot` (once per connection), `prices` (per tick, when something changed), `heartbeat` (every 5 s, always). Comment lines are not used for liveness, because browsers do not expose them to `EventSource` code.

#### Event: `snapshot` (once, on connect)

Sent first on every connection, including reconnects. The client replaces its entire price state with this event.

```
event: snapshot
data: {"source":"simulator","source_status":"ok","tickers":{"AAPL":{"price":191.42,"previous_price":191.30,"prev_close":190.10,"change_pct":0.7,"direction":"up","timestamp":"2026-11-18T14:30:00Z","history":[[1763476200000,190.80],[1763476200500,190.95],[1763476201000,191.42]]}, "...":{}}}
```

- `source` is `"simulator"` or `"massive"`. `source_status` is `"ok"` or `"degraded"` (see `heartbeat`).
- `tickers` covers every ticker in the watchlist **and** every ticker with a non-zero position. A ticker with no cached price yet is still listed, with `price: null`, `prev_close: null`, `change_pct: null`, `direction: null`, and `history: []`.
- `prev_close` and `change_pct` are `null` until the baseline is known (Massive: until the baseline call succeeds).
- `history` is the rolling buffer, oldest first, up to 600 points per ticker.
- If a ticker's price is restored from the persisted `price_state` table after a restart, its `timestamp` is the stored time and `history` starts empty. The first live update arrives with the next tick.

#### Event: `prices` (per tick)

Sent once per tick. Contains only the tickers whose price changed since the previous event. Omitted entirely if nothing changed.

```
event: prices
id: 18342
data: [{"ticker":"AAPL","price":191.55,"previous_price":191.42,"prev_close":190.10,"change_pct":0.76,"direction":"up","timestamp":"2026-11-18T14:30:00.500Z"},{"ticker":"MSFT","price":418.07,"previous_price":418.20,"prev_close":420.00,"change_pct":-0.46,"direction":"down","timestamp":"2026-11-18T14:30:00.500Z"}]
```

| Field | Type | Definition |
|---|---|---|
| `ticker` | string | Symbol |
| `price` | number | Latest price in the cache |
| `previous_price` | number | Price before the most recent cache update for this ticker, at any cadence |
| `prev_close` | number \| null | Daily-change baseline (Massive: previous close; simulator: starting price). `null` until known. |
| `change_pct` | number \| null | `(price − prev_close) / prev_close × 100`, 2 dp. `null` when `prev_close` is `null`. |
| `direction` | `"up"` \| `"down"` \| `"flat"` | Comparison of `price` with `previous_price` |
| `timestamp` | string | ISO 8601 UTC, millisecond precision |

`id` is a monotonically increasing sequence number for the connection, so clients can detect gaps if they choose to.

#### Event: `heartbeat` (every 5 seconds, always)

```
event: heartbeat
data: {"ts":"2026-11-18T14:30:05Z","source_status":"ok","consecutive_failures":0}
```

| Field | Type | Definition |
|---|---|---|
| `ts` | string | Server time, ISO 8601 UTC |
| `source_status` | `"ok"` \| `"degraded"` | `"degraded"` when the market source has failed 3 or more polls in a row. Always `"ok"` in simulator mode. |
| `consecutive_failures` | integer | Count of consecutive failed upstream polls; `0` when healthy |

The heartbeat is sent whether or not prices changed. It is how the client knows the stream is alive, and how the connection indicator learns about upstream degradation.

#### Client rules

- On every `snapshot`, replace all local price state. Do not merge.
- On `prices`, apply each entry. Flash only when `direction` is `up` or `down`.
- On `heartbeat`, update the liveness timestamp and `source_status`. No other state changes.
- If no event (including `heartbeat`) has arrived for 20 s, the connection indicator goes yellow, even while `EventSource` reports `OPEN`.
- Reconnection is automatic (`EventSource`). The server keeps no per-client state, so no resume token is needed.

---

## Portfolio

### `GET /api/portfolio`

Current holdings and totals. Prices are the latest cache values.

**200 OK**

```json
{
  "cash_balance": 7458.20,
  "total_value": 10124.55,
  "unrealized_pnl": 124.55,
  "priced": true,
  "positions": [
    {
      "ticker": "AAPL",
      "quantity": 10,
      "avg_cost": 190.00,
      "current_price": 191.55,
      "market_value": 1915.50,
      "unrealized_pnl": 15.50,
      "unrealized_pnl_pct": 0.82,
      "weight": 0.189,
      "stale": false
    }
  ],
  "as_of": "2026-11-18T14:30:00Z"
}
```

- `weight` is `market_value / total_value`, as a fraction in [0, 1].
- `positions` is an empty array when nothing is held.
- `stale` is `true` when `current_price` comes from the persisted `price_state` after a restart and no live price has arrived yet (PLAN §7 Restart Rules). Otherwise `false`.
- **No cached price:** `current_price`, `market_value`, `unrealized_pnl`, `unrealized_pnl_pct`, and `weight` are `null` for that position. `total_value` and `unrealized_pnl` at the top level are computed over priced positions only, and `priced: false` is set on the response when any position is unpriced.

### `POST /api/portfolio/trade`

Execute a market order. Fills immediately at the current cached price.

**Request**

```json
{"ticker": "AAPL", "quantity": 10, "side": "buy"}
```

| Field | Type | Rules |
|---|---|---|
| `ticker` | string | Required; format rule above |
| `quantity` | number | Required; `> 0`; at most 4 decimal places; at most 1,000,000 |
| `side` | string | Required; `"buy"` or `"sell"` |

**200 OK**

```json
{
  "trade": {
    "id": 42,
    "ticker": "AAPL",
    "side": "buy",
    "quantity": 10,
    "price": 191.55,
    "executed_at": "2026-11-18T14:30:01Z"
  },
  "position": {"ticker": "AAPL", "quantity": 10, "avg_cost": 191.55},
  "cash_balance": 7458.20,
  "total_value": 10124.55
}
```

`position` is `null` if the trade closed the position.

**Errors:** `400 invalid_request`, `409 insufficient_cash`, `409 insufficient_shares`, `409 price_unavailable`.

A rejected trade leaves no trace in `trades` and does not change cash or positions.

### `GET /api/portfolio/history`

Snapshots for the P&L chart, oldest first.

**Query:** `?since=<ISO timestamp>` (optional; default: 7 days ago).

**200 OK**

```json
{
  "snapshots": [
    {"total_value": 10000.00, "recorded_at": "2026-11-18T13:00:00Z"},
    {"total_value": 10031.12, "recorded_at": "2026-11-18T13:00:30Z"}
  ]
}
```

### `POST /api/portfolio/reset`

Restores the seed state: cash to $10,000, no positions, no trades, default watchlist, no chat messages, and a single $10,000 snapshot. Prices are not reset.

**Request**

```json
{"confirm": "RESET"}
```

**200 OK**

```json
{"status": "reset", "cash_balance": 10000.00, "watchlist": ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]}
```

**Errors:** `400 reset_not_confirmed` (wrong or missing `confirm`).

---

## Watchlist

### `GET /api/watchlist`

Tickers only. Prices come from the SSE `snapshot`.

**200 OK**

```json
{
  "tickers": [
    {"ticker": "AAPL", "added_at": "2026-11-18T12:00:00Z"},
    {"ticker": "GOOGL", "added_at": "2026-11-18T12:00:00Z"}
  ]
}
```

### `POST /api/watchlist`

**Request**

```json
{"ticker": "PYPL"}
```

**201 Created**

```json
{"ticker": "PYPL", "added_at": "2026-11-18T14:31:00Z"}
```

**Errors:** `400 invalid_request`, `404 unknown_ticker` (Massive mode), `409 already_in_watchlist`.

The new ticker begins streaming on the next `prices` event after the source confirms it.

### `DELETE /api/watchlist/{ticker}`

**204 No Content** on success.

**Errors:** `400 invalid_request` (malformed ticker), `404 not_found` (not on the watchlist).

Removing a ticker does not close a position. If the user still holds it, it keeps streaming.

---

## Chat

### `GET /api/chat/messages`

Recent history for first paint, oldest first.

**Query:** `?limit=50` (default 50, max 200).

**200 OK**

```json
{
  "messages": [
    {"id": 1, "role": "user", "content": "How is my portfolio?", "actions": null, "created_at": "2026-11-18T14:20:00Z"},
    {"id": 2, "role": "assistant", "content": "You are 19% in AAPL...", "actions": {"trades": [], "watchlist_changes": []}, "created_at": "2026-11-18T14:20:02Z"}
  ]
}
```

### `POST /api/chat`

Send a message. The response is complete; there is no streaming.

**Request**

```json
{"message": "Buy 5 NVDA and add PYPL to my watchlist"}
```

| Field | Rules |
|---|---|
| `message` | Required; string; 1–2000 characters |

**200 OK**

```json
{
  "message": "Added PYPL to your watchlist and bought 5 NVDA at $131.02.",
  "trades": [
    {"ticker": "NVDA", "side": "buy", "quantity": 5, "status": "filled", "price": 131.02}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add", "status": "applied"}
  ],
  "portfolio": {"cash_balance": 9345.90, "total_value": 10124.55}
}
```

**Result item shapes**

- Trade: `{ticker, side, quantity, status: "filled" | "rejected", price?, error?}`. `error` is an error object as in the Error Format, present when `status` is `rejected`.
- Watchlist change: `{ticker, action: "add" | "remove", status: "applied" | "rejected", error?}`.

Watchlist changes execute first, then trades, each in array order. A rejected item does not stop the rest.

**Errors:** `400 invalid_request` (message empty or too long), `503 llm_unavailable`, `500 internal_error`. If the model's output is malformed twice, the endpoint still returns `200` with the fixed fallback message and empty `trades`/`watchlist_changes` arrays. It does not return an error.

---

## System

### `GET /api/health`

**200 OK**

```json
{
  "status": "ok",
  "db": "ok",
  "market_source": "simulator",
  "llm_configured": true
}
```

- `status` is `"ok"` only when `db` is `"ok"`. If initialization has not finished, the endpoint returns `503` with `status: "starting"`.
- `market_source` is `"simulator"` or `"massive"`.
- `llm_configured` is `true` when `OPENROUTER_API_KEY` is set or `LLM_MOCK=true`.

---

## Static Files

Any `GET` that does not begin with `/api/` serves the static export from `/app/static`. Unknown paths return `index.html` so that client-side routing works. Missing assets under `/_next/` return `404`.
