"""Watchlist endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.services import watchlist as watchlist_service

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


@router.get("")
async def get_watchlist(request: Request) -> dict:
    return {"tickers": watchlist_service.get_watchlist_with_prices(request.app.state.price_cache)}


@router.post("", status_code=201)
async def add_ticker(body: AddTickerRequest, request: Request) -> dict:
    try:
        added = await watchlist_service.add_ticker(body.ticker, request.app.state.market_source)
    except watchlist_service.DuplicateTickerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return watchlist_service.watchlist_item(added["ticker"], request.app.state.price_cache)


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request) -> dict:
    norm = ticker.strip().upper()
    if not await watchlist_service.remove_ticker(norm, request.app.state.market_source):
        raise HTTPException(status_code=404, detail=f"{norm} is not in the watchlist")
    return {"ticker": norm, "removed": True}
