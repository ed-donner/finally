"""Database layer for FinAlly — SQLite with aiosqlite."""

from app.db.connection import get_db_path, set_db_path
from app.db.init_db import init_db
from app.db.crud import (
    get_user_profile,
    update_cash_balance,
    list_watchlist,
    add_watchlist_ticker,
    remove_watchlist_ticker,
    get_positions,
    get_position_by_ticker,
    upsert_position,
    delete_position,
    insert_trade,
    list_trades,
    insert_portfolio_snapshot,
    list_portfolio_snapshots,
    insert_chat_message,
    get_recent_chat_messages,
)

__all__ = [
    "get_db_path",
    "set_db_path",
    "init_db",
    "get_user_profile",
    "update_cash_balance",
    "list_watchlist",
    "add_watchlist_ticker",
    "remove_watchlist_ticker",
    "get_positions",
    "get_position_by_ticker",
    "upsert_position",
    "delete_position",
    "insert_trade",
    "list_trades",
    "insert_portfolio_snapshot",
    "list_portfolio_snapshots",
    "insert_chat_message",
    "get_recent_chat_messages",
]
