"""Database layer for FinAlly.

Public API:
    init_db              - Create tables and seed default data
    get_connection       - Get an async SQLite connection
    set_db_path          - Override DB file path (for testing)

    get_cash_balance     - Get user's cash balance
    update_cash_balance  - Set user's cash balance

    get_watchlist        - List watchlist tickers
    add_to_watchlist     - Add a ticker
    remove_from_watchlist - Remove a ticker

    get_positions        - List all positions
    get_position         - Get one position by ticker
    upsert_position      - Create or update a position
    delete_position      - Remove a position

    insert_trade         - Record a trade

    insert_snapshot      - Record a portfolio value snapshot
    get_portfolio_history - Get snapshot history

    insert_chat_message  - Store a chat message
    get_chat_history     - Get recent chat history
"""

from .connection import get_connection, init_db, set_db_path
from .repository import (
    add_to_watchlist,
    delete_position,
    get_cash_balance,
    get_chat_history,
    get_portfolio_history,
    get_position,
    get_positions,
    get_watchlist,
    insert_chat_message,
    insert_snapshot,
    insert_trade,
    remove_from_watchlist,
    update_cash_balance,
    upsert_position,
)
from .schema import DEFAULT_CASH_BALANCE, DEFAULT_TICKERS, DEFAULT_USER_ID

__all__ = [
    "init_db",
    "get_connection",
    "set_db_path",
    "get_cash_balance",
    "update_cash_balance",
    "get_watchlist",
    "add_to_watchlist",
    "remove_from_watchlist",
    "get_positions",
    "get_position",
    "upsert_position",
    "delete_position",
    "insert_trade",
    "insert_snapshot",
    "get_portfolio_history",
    "insert_chat_message",
    "get_chat_history",
    "DEFAULT_USER_ID",
    "DEFAULT_TICKERS",
    "DEFAULT_CASH_BALANCE",
]
