"""Watchlist API endpoints."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.db import add_to_watchlist, get_watchlist, remove_from_watchlist

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


class AddTickerRequest(BaseModel):
    ticker: str


@router.get("")
async def list_watchlist(request: Request):
    """Current watchlist tickers with latest prices from PriceCache."""
    cache = request.app.state.price_cache
    watchlist = await get_watchlist()

    items = []
    for entry in watchlist:
        ticker = entry["ticker"]
        update = cache.get(ticker)
        items.append({
            "ticker": ticker,
            "price": update.price if update else None,
            "previous_price": update.previous_price if update else None,
            "change": update.change if update else None,
            "change_percent": update.change_percent if update else None,
            "direction": update.direction if update else None,
        })

    return {"watchlist": items}


@router.post("")
async def add_ticker(body: AddTickerRequest, request: Request):
    """Add a ticker to the watchlist and market data source."""
    ticker = body.ticker.upper()
    source = request.app.state.market_source

    try:
        entry = await add_to_watchlist(ticker)
    except Exception:
        raise HTTPException(status_code=409, detail=f"{ticker} already in watchlist")

    await source.add_ticker(ticker)

    return {"ticker": ticker, "added_at": entry["added_at"]}


@router.delete("/{ticker}")
async def remove_ticker(ticker: str, request: Request):
    """Remove a ticker from the watchlist and market data source."""
    ticker = ticker.upper()
    source = request.app.state.market_source
    cache = request.app.state.price_cache

    removed = await remove_from_watchlist(ticker)
    if not removed:
        raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")

    await source.remove_ticker(ticker)
    cache.remove(ticker)

    return {"ticker": ticker, "removed": True}
