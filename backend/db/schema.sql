-- FinAlly database schema (SQLite)
-- All tables carry a user_id column (default "default") to enable future
-- multi-user support without a schema migration. See PLAN.md §7.
-- Statements are idempotent (CREATE TABLE IF NOT EXISTS) so init is safe to
-- run repeatedly against an existing database.

-- User state: cash balance and creation timestamp.
CREATE TABLE IF NOT EXISTS users_profile (
    id           TEXT PRIMARY KEY,        -- default "default"
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at   TEXT NOT NULL            -- ISO-8601 UTC
);

-- Tickers the user is watching.
CREATE TABLE IF NOT EXISTS watchlist (
    id       TEXT PRIMARY KEY,            -- uuid4 hex
    user_id  TEXT NOT NULL DEFAULT 'default',
    ticker   TEXT NOT NULL,
    added_at TEXT NOT NULL,               -- ISO-8601 UTC
    UNIQUE (user_id, ticker)
);

-- Current holdings: one row per ticker per user.
CREATE TABLE IF NOT EXISTS positions (
    id         TEXT PRIMARY KEY,          -- uuid4 hex
    user_id    TEXT NOT NULL DEFAULT 'default',
    ticker     TEXT NOT NULL,
    quantity   REAL NOT NULL,             -- fractional shares supported
    avg_cost   REAL NOT NULL,
    updated_at TEXT NOT NULL,             -- ISO-8601 UTC
    UNIQUE (user_id, ticker)
);

-- Append-only trade history.
CREATE TABLE IF NOT EXISTS trades (
    id          TEXT PRIMARY KEY,         -- uuid4 hex
    user_id     TEXT NOT NULL DEFAULT 'default',
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,            -- "buy" or "sell"
    quantity    REAL NOT NULL,            -- fractional shares supported
    price       REAL NOT NULL,
    executed_at TEXT NOT NULL             -- ISO-8601 UTC
);

-- Portfolio total value over time (for the P&L chart).
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id          TEXT PRIMARY KEY,         -- uuid4 hex
    user_id     TEXT NOT NULL DEFAULT 'default',
    total_value REAL NOT NULL,
    recorded_at TEXT NOT NULL             -- ISO-8601 UTC
);

-- Conversation history with the LLM assistant.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         TEXT PRIMARY KEY,          -- uuid4 hex
    user_id    TEXT NOT NULL DEFAULT 'default',
    role       TEXT NOT NULL,             -- "user" or "assistant"
    content    TEXT NOT NULL,
    actions    TEXT,                      -- JSON blob (trades / watchlist changes); NULL for user messages
    created_at TEXT NOT NULL              -- ISO-8601 UTC
);

-- Helpful indexes for the time-ordered reads.
CREATE INDEX IF NOT EXISTS idx_watchlist_user      ON watchlist (user_id);
CREATE INDEX IF NOT EXISTS idx_positions_user      ON positions (user_id);
CREATE INDEX IF NOT EXISTS idx_trades_user_time    ON trades (user_id, executed_at);
CREATE INDEX IF NOT EXISTS idx_snapshots_user_time ON portfolio_snapshots (user_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_chat_user_time      ON chat_messages (user_id, created_at);
