"""Portfolio service layer for FinAlly.

Provides trade execution, portfolio querying, portfolio history retrieval,
and background snapshot recording.
"""

from .models import (
    PortfolioHistoryResponse,
    PortfolioResponse,
    PositionResponse,
    SnapshotResponse,
    TradeRequest,
    TradeResponse,
)
from .service import execute_trade, get_portfolio, get_portfolio_history
from .snapshots import record_snapshot, start_snapshot_task, stop_snapshot_task

__all__ = [
    "TradeRequest",
    "TradeResponse",
    "PositionResponse",
    "PortfolioResponse",
    "SnapshotResponse",
    "PortfolioHistoryResponse",
    "execute_trade",
    "get_portfolio",
    "get_portfolio_history",
    "record_snapshot",
    "start_snapshot_task",
    "stop_snapshot_task",
]
