# FinAlly — Multi-Agent Architecture

This document replaces the single-LLM chat flow in PLAN.md §9 with five collaborating agents. The HTTP API (PLAN.md §8), the structured-output schema and the auto-execution behavior are unchanged. Only the internals of `POST /api/chat` and `POST /api/portfolio/trade` change.

## 1. Design Principles

- **One LLM call per chat turn.** Only the Orchestrator calls the LLM (LiteLLM → OpenRouter → Cerebras, per the `cerebras` skill). The other four agents are deterministic Python. That makes them testable and cheap, and it keeps latency to a single round trip.
- **Money moves through one path only.** Every trade, whether it comes from the trade bar, the chat or a future automation, goes Risk Agent → Portfolio Agent. Nothing else writes to `positions` or `portfolio_state`.
- **Agents are plain classes with typed methods**, not autonomous loops. They share a DB connection and the market-data price cache, and they communicate through Pydantic models.
- **Everything is auditable.** Each agent call writes a row to `agent_logs`, and each trade carries the `agent_id` and `source` that produced it.

```
                      POST /api/chat                 POST /api/portfolio/trade
                            │                                   │
                            ▼                                   │
              ┌──────────────────────────┐                      │
              │  Chat Orchestrator Agent │                      │
              │  (the only LLM caller)   │                      │
              └──┬────────┬───────────┬──┘                      │
   context       │        │ trades    │ watchlist_changes       │
                 ▼        ▼           ▼                         │
        ┌──────────┐  ┌────────┐  ┌───────────────┐             │
        │ Analyzer │  │  Risk  │  │   Watchlist   │             │
        └──────────┘  └───┬────┘  └───────────────┘             │
                          │ approved                            │
                          ▼                                     ▼
                    ┌───────────┐  ◄──────────────────────── Risk ◄─ manual trade
                    │ Portfolio │
                    └───────────┘
                          │
                     SQLite + price cache
```

## 2. Shared Types

```python
Side = Literal["buy", "sell"]
Source = Literal["manual", "chat", "system"]

class TradeRequest(BaseModel):
    ticker: str          # normalized to upper-case, trimmed
    side: Side
    quantity: float      # fractional shares allowed
    source: Source

class RiskVerdict(BaseModel):
    approved: bool
    reasons: list[str]   # hard failures; approved is False if non-empty
    warnings: list[str]  # soft flags, e.g. "AAPL would be 38% of the portfolio"

class TradeResult(BaseModel):
    ok: bool
    trade_id: str | None
    ticker: str
    side: Side
    quantity: float
    price: float | None
    error: str | None    # a Risk reason or a Portfolio failure

class WatchlistChange(BaseModel):
    ticker: str
    action: Literal["add", "remove"]

class WatchlistResult(BaseModel):
    ok: bool
    ticker: str
    action: str
    error: str | None
```

## 3. Agents

Every agent has a stable `agent_id` used in `trades.agent_id` and `agent_logs.agent_id`:
`portfolio`, `analyzer`, `risk`, `watchlist`, `orchestrator`.

### 3.1 Portfolio Agent (`portfolio`)

**Responsibility.** It is the only writer of cash, positions and trades. It fills validated market orders at the current cached price. It maintains average cost, cash balance and realized/unrealized P&L, and it records portfolio snapshots for the P&L chart. It never decides whether a trade is *allowed*. That is the Risk Agent's job. It only checks that the trade is *mechanically possible* inside its DB transaction.

**Key inputs.** `TradeRequest` and a `RiskVerdict` (it refuses to run without an approved verdict). It also reads the price cache and the `positions` and `portfolio_state` rows.

**Key outputs.** `TradeResult`, and a `PortfolioSummary` (cash, positions with current price, unrealized P&L and % change, total value, total P&L). It also writes `trades`, `positions`, `portfolio_state` and `portfolio_snapshots` rows.

**Core methods.**

| Method | Purpose |
|---|---|
| `execute_trade(req, verdict) -> TradeResult` | Atomic transaction: fill at the cached price, update cash, upsert or delete the position, append the trade, refresh `portfolio_state`, record a snapshot |
| `get_summary() -> PortfolioSummary` | Current portfolio with live prices |
| `get_position(ticker) -> Position \| None` | Used by Risk and Analyzer |
| `refresh_prices()` | Update `positions.current_price` and `portfolio_state` from the cache (called by the SSE/snapshot loop) |
| `record_snapshot()` | Insert a `portfolio_snapshots` row; called every 30 s and after each trade |
| `get_history(limit) -> list[Snapshot]` | Backs `GET /api/portfolio/history` |

**Validation rules (mechanical, re-checked inside the transaction).**
- The price cache has a price for the ticker; otherwise the trade fails with "no price available".
- The verdict is approved, and the request quantity matches the one that was verified.
- The cash and quantity checks are re-run inside the transaction as a guard against races. The SQLite `CHECK` constraints are the last line of defense.
- A buy recomputes the average cost as `(old_qty*old_avg + qty*price) / (old_qty + qty)`. A sell leaves the average cost unchanged. A position whose quantity reaches ~0 (`< 1e-9`) is deleted.

### 3.2 Analyzer Agent (`analyzer`)

**Responsibility.** It turns raw portfolio and market data into compact, factual analysis: concentration, P&L attribution, cash drag and watchlist movers. It is deterministic and read-only. The Orchestrator injects its output into the LLM prompt as context, so the LLM reasons over computed numbers rather than doing arithmetic itself. Trade *suggestions* are the LLM's output. The Analyzer only supplies the facts and screening flags that support them.

**Key inputs.** `PortfolioSummary`, watchlist tickers with latest and previous prices from the price cache, and optionally recent `trades`.

**Key outputs.** An `AnalysisReport`:
```python
class AnalysisReport(BaseModel):
    total_value: float
    cash_pct: float
    positions: list[PositionMetric]      # weight_pct, unrealized_pnl, pnl_pct, day_change_pct
    top_concentration: PositionMetric | None
    herfindahl_index: float              # 0–1 concentration score
    best_performer: PositionMetric | None
    worst_performer: PositionMetric | None
    watchlist_movers: list[Mover]        # largest absolute % moves among unheld tickers
    flags: list[str]                     # e.g. "NVDA is 46% of portfolio", "82% cash"
```

**Core methods.**

| Method | Purpose |
|---|---|
| `analyze(summary, watchlist_prices) -> AnalysisReport` | Full report used for chat context |
| `concentration(summary)` | Weights and Herfindahl index |
| `pnl_attribution(summary)` | Which positions drive total P&L |
| `movers(watchlist_prices, held)` | Notable unheld tickers |
| `to_prompt_context(report) -> str` | Compact text block for the LLM prompt |

**Validation rules.** None. It is read-only and tolerates an empty portfolio (all-cash report) and missing prices (the ticker is skipped and flagged).

### 3.3 Risk Agent (`risk`)

**Responsibility.** It is the gatekeeper for every trade. It checks a proposed trade against the current portfolio and the configured limits, and returns an approve/reject verdict with human-readable reasons that the chat can relay verbatim. It is pure logic with read-only DB access and no side effects.

**Key inputs.** `TradeRequest`, the current `PortfolioSummary` and the latest price. Limits come from a `RiskLimits` config object.

**Key outputs.** `RiskVerdict` (`approved`, hard `reasons`, soft `warnings`).

**Core methods.**

| Method | Purpose |
|---|---|
| `validate_trade(req) -> RiskVerdict` | Runs every rule below and collects all failures rather than stopping at the first |
| `validate_batch(reqs) -> list[RiskVerdict]` | Validates LLM trades in order against a *simulated* running portfolio, so "buy 5 AAPL then sell 5 AAPL" and cumulative cash use are judged correctly |
| `projected_weight(req, summary) -> float` | Post-trade weight of the ticker |
| `limits() -> RiskLimits` | Defaults below, overridable through env or config |

**Validation rules.**

*Hard (reject):*
1. `quantity` is finite and `> 0`, and no smaller than `MIN_QUANTITY` (default `0.0001`).
2. `side` is `buy` or `sell`.
3. The ticker matches `^[A-Z]{1,5}(\.[A-Z])?$` and has a price in the cache (that is, it is known to the market data source).
4. **Buy:** `quantity * price <= cash_balance`.
5. **Sell:** a position exists and `quantity <= position.quantity`.
6. **Max order size:** the order value is at most `MAX_ORDER_PCT` of total portfolio value (default 100%, which is the cash limit, and configurable lower).
7. **Max position weight (buys only):** projected position value / total value is at most `MAX_POSITION_PCT` (default 50%).

*Soft (approve with warning):*
- Projected weight above `WARN_POSITION_PCT` (default 25%).
- The buy leaves under 5% of total value in cash.
- A sell closes the position at a loss.

Limits default to permissive values, so a new user with $10k can still make the demo trades. They are tunable without code changes.

### 3.4 Watchlist Agent (`watchlist`)

**Responsibility.** It owns all reads and writes of the `watchlist` table. It validates and normalizes tickers, keeps the set of tracked symbols in sync with the market data source, and records who added each ticker (`user`, `ai` or `system`).

**Key inputs.** `WatchlistChange` items (from the REST API or from the Orchestrator's `watchlist_changes`), plus an `added_by` value. It also reads the market data source's list of supported tickers.

**Key outputs.** `WatchlistResult`, and the current watchlist enriched with prices (`list[WatchlistEntry]`) for `GET /api/watchlist`.

**Core methods.**

| Method | Purpose |
|---|---|
| `add(ticker, added_by) -> WatchlistResult` | Validate, insert and register the ticker with the market data source |
| `remove(ticker) -> WatchlistResult` | Delete the row; unregister from market data if no position holds it |
| `apply_changes(changes, added_by) -> list[WatchlistResult]` | Apply a batch; a failure in one item doesn't block the others |
| `list_with_prices() -> list[WatchlistEntry]` | Watchlist plus latest price, previous price and direction |
| `tickers() -> list[str]` | Union used by the market data poller |

**Validation rules.**
- The ticker is normalized to upper-case and matches the ticker regex from §3.3.
- **Add:** the ticker is not already on the list (idempotent: report `ok=True` with "already watching"), the list has at most `MAX_WATCHLIST` (default 50) entries, and the market data source can price it. The simulator falls back to a default seed price for unknown tickers. Massive rejects unknown symbols.
- **Remove:** removal is allowed even if a position exists. Held tickers keep streaming prices, because the market data layer takes the union of watchlist and positions.

### 3.5 Chat Orchestrator Agent (`orchestrator`)

**Responsibility.** It handles one `/api/chat` turn end to end. It gathers context (portfolio, Analyzer report, watchlist, history), makes the single structured-output LLM call, and routes the LLM's requested trades and watchlist changes to the right agents. It then merges the outcomes into one response, persists the exchange and returns JSON. It contains no trading logic of its own.

**Key inputs.** The user's message. It also gets the `PortfolioSummary` from Portfolio, the `AnalysisReport` from Analyzer, `WatchlistEntry` items from Watchlist, and recent history from `chat_messages`.

**Key outputs.** A `ChatResponse`:
```python
class ChatResponse(BaseModel):
    message: str                      # LLM text, plus appended notes for any failures
    executed_trades: list[TradeResult]      # includes failed trades with error set
    watchlist_changes: list[WatchlistResult]
    warnings: list[str]               # Risk soft warnings
```
The LLM output schema is exactly the PLAN.md §9 schema (`message`, `trades`, `watchlist_changes`).

**Core methods.**

| Method | Purpose |
|---|---|
| `handle_message(text) -> ChatResponse` | The full pipeline below |
| `build_prompt(text, summary, report, watchlist, history) -> list[dict]` | System prompt ("FinAlly, an AI trading assistant") plus context and history |
| `call_llm(messages) -> LLMOutput` | LiteLLM call with structured output; mock path when `LLM_MOCK=true` |
| `route_trades(trades) -> list[TradeResult]` | Risk `validate_batch`, then Portfolio `execute_trade` for each approved trade |
| `route_watchlist(changes) -> list[WatchlistResult]` | Delegates to Watchlist |
| `persist(user_msg, response)` | Writes `chat_messages` |

**Pipeline.**
1. Portfolio → `get_summary()`; Analyzer → `analyze()`; Watchlist → `list_with_prices()`; load the last N chat messages.
2. `build_prompt` → `call_llm` → parse and validate as `LLMOutput`.
3. Risk → `validate_batch(trades)` (trades are processed in the order the LLM gave them).
4. Portfolio → `execute_trade` for each approved trade. Rejected trades become `TradeResult(ok=False, error=reason)`.
5. Watchlist → `apply_changes(changes, added_by="ai")`.
6. Append any failures to `message` (e.g. "I couldn't buy 500 TSLA: insufficient cash") so the user sees them even if the LLM's text assumed success.
7. Persist to `chat_messages`, then return.

**Validation rules.**
- LLM output must parse against the schema. On a malformed response, retry once, then return a graceful error message with no actions executed.
- At most `MAX_TRADES_PER_TURN` trades (default 10) and `MAX_WATCHLIST_CHANGES_PER_TURN` changes (default 10) are executed. Extras are dropped and reported.
- Trades from chat use `source="chat"` and `agent_id="portfolio"`. The Orchestrator never touches the DB tables for positions or cash directly.

## 4. Manual Trade Path

`POST /api/portfolio/trade` skips the Orchestrator and the LLM entirely:
`Risk.validate_trade(req[source="manual"])` → `Portfolio.execute_trade` → `TradeResult`. A rejection returns HTTP 400 with `reasons`.

## 5. Audit Logging

Each agent method that does meaningful work writes one `agent_logs` row through a small `AuditLog.record(agent_id, action, status, input, output, correlation_id)` helper.
- `correlation_id` is generated at the start of an API request and shared by every agent call in it. Filtering on it reconstructs a whole chat turn.
- Logged actions: `portfolio.execute_trade`, `risk.validate_trade`, `watchlist.add`, `watchlist.remove`, `analyzer.analyze`, `orchestrator.llm_call`, `orchestrator.handle_message`.
- Payloads are stored as JSON. Never log the API key.

## 6. Proposed Backend Layout

```
backend/app/
├── main.py                  # FastAPI app, routes, lifespan (starts market data + snapshot loop)
├── db.py                    # connection helper, lazy init from db/schema.sql, seed
├── agents/
│   ├── __init__.py
│   ├── models.py            # shared Pydantic types (§2)
│   ├── base.py              # BaseAgent: agent_id, db handle, audit logging
│   ├── portfolio.py
│   ├── analyzer.py
│   ├── risk.py
│   ├── watchlist.py
│   └── orchestrator.py
└── market/                  # existing
```

## 7. Deviations from PLAN.md

These are deliberate and need sign-off:

| PLAN.md | This design | Reason |
|---|---|---|
| `users_profile` (cash) | `portfolio_state` (cash, total value, P&L) | Requested; caches the aggregates the header needs |
| `chat_messages`: one row per message with a `role` | One row per exchange: `user_message` + `ai_response` | Requested; simpler to render and to log actions against |
| `positions` has no price | `positions.current_price` denormalized | Requested; refreshed by `Portfolio.refresh_prices()` |
| Single LLM agent | Five agents, one LLM call | Requested |
| `trades` has no source info | `agent_id`, `source` columns | Auditing |
| New `agent_logs`, `watchlist.added_by` | Added | Auditing |

`user_id` (default `"default"`) is kept on every table, as PLAN.md §7 requires, so the schema stays multi-user-ready.

## 8. Open Questions

1. Should the Analyzer also expose its own LLM-backed `/api/analyze` endpoint for on-demand deep analysis, or is context injection enough? (I assumed enough.)
2. Are the default Risk limits acceptable (50% max position, 25% warn)? They are permissive on purpose, to avoid blocking the demo.
3. Should a Risk rejection abort the whole chat batch, or just skip the rejected trade? (I assumed skip, and the rest execute.)
