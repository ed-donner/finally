"""Watchlist endpoints: list with live prices, add, remove."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.db import repo
from app.runtime import get_market_source, get_price_cache

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddBody(BaseModel):
    ticker: str = Field(..., min_length=1)


def build_watchlist() -> dict:
    """Join the persisted watchlist with the latest cached prices."""
    cache = get_price_cache()
    items = []
    for ticker in repo.get_watchlist():
        update = cache.get(ticker)
        if update is not None:
            items.append(
                {
                    "ticker": ticker,
                    "price": update.price,
                    "previous_price": update.previous_price,
                    "change": update.change,
                    "change_percent": update.change_percent,
                    "direction": update.direction,
                }
            )
        else:
            items.append(
                {
                    "ticker": ticker,
                    "price": None,
                    "previous_price": None,
                    "change": None,
                    "change_percent": None,
                    "direction": None,
                }
            )
    return {"watchlist": items}


@router.get("")
async def get_watchlist() -> dict:
    return build_watchlist()


@router.post("")
async def add_ticker(body: AddBody) -> dict:
    ticker = body.ticker.strip().upper()
    if not ticker.isalnum():
        raise HTTPException(status_code=400, detail=f"Invalid ticker '{ticker}'.")
    if not repo.add_watchlist(ticker):
        raise HTTPException(status_code=400, detail=f"{ticker} is already on the watchlist.")
    await get_market_source().add_ticker(ticker)
    return build_watchlist()


@router.delete("/{ticker}")
async def remove_ticker(ticker: str) -> dict:
    ticker = ticker.strip().upper()
    if not repo.remove_watchlist(ticker):
        raise HTTPException(status_code=400, detail=f"{ticker} is not on the watchlist.")
    await get_market_source().remove_ticker(ticker)
    return build_watchlist()
