-- FinAlly SQLite schema (multi-agent design; see planning/AGENT_ARCHITECTURE.md)
-- Applied lazily on first request: every statement is idempotent (IF NOT EXISTS / OR IGNORE).
-- Conventions: ids are UUID text; timestamps are ISO-8601 UTC text; user_id defaults to 'default'
-- (single-user today, multi-user-ready). Fractional shares are supported.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- portfolio_state: one row per user. Cash is the source of truth; total_value
-- and pnl are cached aggregates refreshed by the Portfolio Agent.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_state (
    user_id         TEXT PRIMARY KEY DEFAULT 'default',
    cash_balance    REAL NOT NULL DEFAULT 10000.0 CHECK (cash_balance >= 0),
    starting_cash   REAL NOT NULL DEFAULT 10000.0,          -- baseline for total P&L
    total_value     REAL NOT NULL DEFAULT 10000.0,          -- cash + positions at market
    unrealized_pnl  REAL NOT NULL DEFAULT 0.0,
    realized_pnl    REAL NOT NULL DEFAULT 0.0,
    total_pnl       REAL NOT NULL DEFAULT 0.0,              -- total_value - starting_cash
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- positions: current holdings, one row per ticker. Rows are deleted when the
-- quantity reaches zero. current_price is denormalized and refreshed from the
-- market data cache.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    ticker          TEXT NOT NULL,
    quantity        REAL NOT NULL CHECK (quantity > 0),
    avg_cost        REAL NOT NULL CHECK (avg_cost >= 0),
    current_price   REAL,                                    -- NULL until the first price refresh
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, ticker)
);

-- ---------------------------------------------------------------------------
-- trades: append-only execution log.
--   agent_id = which agent executed it (always 'portfolio' today)
--   source   = who asked for it: the trade bar, the chat, or an automated process
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trades (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity        REAL NOT NULL CHECK (quantity > 0),
    price           REAL NOT NULL CHECK (price > 0),
    realized_pnl    REAL,                                    -- sells only: (price - avg_cost) * quantity
    timestamp       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    agent_id        TEXT NOT NULL DEFAULT 'portfolio',
    source          TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'chat', 'system')),
    correlation_id  TEXT                                     -- joins to agent_logs for the same request
);
CREATE INDEX IF NOT EXISTS idx_trades_user_time   ON trades (user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_trades_user_ticker ON trades (user_id, ticker);

-- ---------------------------------------------------------------------------
-- portfolio_snapshots: total value over time for the P&L chart. Written every
-- 30 s by a background task and immediately after each trade.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    total_value     REAL NOT NULL,
    cash_balance    REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_pnl       REAL NOT NULL,
    recorded_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_time ON portfolio_snapshots (user_id, recorded_at);

-- ---------------------------------------------------------------------------
-- watchlist: tracked tickers. added_by = 'user' | 'ai' | 'system'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS watchlist (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    ticker          TEXT NOT NULL,
    added_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    added_by        TEXT NOT NULL DEFAULT 'user' CHECK (added_by IN ('user', 'ai', 'system')),
    UNIQUE (user_id, ticker)
);

-- ---------------------------------------------------------------------------
-- chat_messages: one row per exchange (user message + AI response).
-- executed_trades / watchlist_changes are JSON arrays of the agents' results
-- (including failed items with their error), NULL/'[]' when there were none.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS chat_messages (
    id                 TEXT PRIMARY KEY,
    user_id            TEXT NOT NULL DEFAULT 'default',
    user_message       TEXT NOT NULL,
    ai_response        TEXT NOT NULL,
    executed_trades    TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(executed_trades)),
    watchlist_changes  TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(watchlist_changes)),
    correlation_id     TEXT,
    created_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_user_time ON chat_messages (user_id, created_at);

-- ---------------------------------------------------------------------------
-- agent_logs: audit trail of which agent did what. All agent calls within one
-- API request share a correlation_id, so a full chat turn can be reconstructed.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agent_logs (
    id              TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL DEFAULT 'default',
    correlation_id  TEXT NOT NULL,
    agent_id        TEXT NOT NULL CHECK (agent_id IN ('portfolio', 'analyzer', 'risk', 'watchlist', 'orchestrator')),
    action          TEXT NOT NULL,                           -- e.g. 'execute_trade', 'validate_trade'
    status          TEXT NOT NULL CHECK (status IN ('success', 'rejected', 'error')),
    input           TEXT CHECK (input  IS NULL OR json_valid(input)),
    output          TEXT CHECK (output IS NULL OR json_valid(output)),
    error           TEXT,
    duration_ms     INTEGER,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_agent_logs_corr  ON agent_logs (correlation_id);
CREATE INDEX IF NOT EXISTS idx_agent_logs_agent ON agent_logs (agent_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Seed data (only inserted if absent)
-- ---------------------------------------------------------------------------
INSERT OR IGNORE INTO portfolio_state (user_id, cash_balance, starting_cash, total_value)
VALUES ('default', 10000.0, 10000.0, 10000.0);

INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_by) VALUES
    ('seed-aapl',  'default', 'AAPL',  'system'),
    ('seed-googl', 'default', 'GOOGL', 'system'),
    ('seed-msft',  'default', 'MSFT',  'system'),
    ('seed-amzn',  'default', 'AMZN',  'system'),
    ('seed-tsla',  'default', 'TSLA',  'system'),
    ('seed-nvda',  'default', 'NVDA',  'system'),
    ('seed-meta',  'default', 'META',  'system'),
    ('seed-jpm',   'default', 'JPM',   'system'),
    ('seed-v',     'default', 'V',     'system'),
    ('seed-nflx',  'default', 'NFLX',  'system');
