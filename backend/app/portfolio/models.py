"""Pydantic request/response schemas for trade and portfolio."""

from pydantic import BaseModel, Field


class TradeRequest(BaseModel):
    """Request to execute a market order."""

    ticker: str = Field(min_length=1, max_length=10)
    side: str = Field(pattern=r"^(buy|sell)$")
    quantity: float = Field(gt=0)


class TradeResponse(BaseModel):
    """Result of a completed trade execution."""

    ticker: str
    side: str
    quantity: float
    price: float
    total: float


class PositionResponse(BaseModel):
    """A single portfolio position with live valuation."""

    ticker: str
    quantity: float
    avg_cost: float
    current_price: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_percent: float


class PortfolioResponse(BaseModel):
    """Full portfolio state with positions and cash."""

    cash_balance: float
    positions: list[PositionResponse]
    total_value: float


class SnapshotResponse(BaseModel):
    """A single portfolio value snapshot."""

    total_value: float
    recorded_at: str


class PortfolioHistoryResponse(BaseModel):
    """Portfolio value over time."""

    snapshots: list[SnapshotResponse]
