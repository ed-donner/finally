"""FinAlly persistence layer.

A thin SQLite wrapper. No ORM — just module-level functions per table that take
and return plain dataclasses or primitives. The schema is created and seeded
lazily on first use via `ensure_initialized()`.
"""

from . import chat, positions, profile, snapshots, trades, watchlist
from .chat import ChatMessage
from .init import (
    DEFAULT_CASH_BALANCE,
    DEFAULT_USER_ID,
    DEFAULT_WATCHLIST,
    ensure_initialized,
    reset_initialization_state,
)
from .positions import Position
from .snapshots import Snapshot
from .trades import Trade

__all__ = [
    "ensure_initialized",
    "reset_initialization_state",
    "DEFAULT_USER_ID",
    "DEFAULT_CASH_BALANCE",
    "DEFAULT_WATCHLIST",
    "ChatMessage",
    "Position",
    "Snapshot",
    "Trade",
    "chat",
    "positions",
    "profile",
    "snapshots",
    "trades",
    "watchlist",
]
