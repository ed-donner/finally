"""Watchlist API endpoints."""

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.db import add_watchlist_ticker, list_watchlist, remove_watchlist_ticker

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


class AddTickerRequest(BaseModel):
    ticker: str = Field(..., min_length=1, max_length=5)


@router.get("")
async def get_watchlist(request: Request):
    """Current watchlist tickers enriched with latest prices."""
    cache = request.app.state.price_cache
    items = await list_watchlist()
    result = []
    for item in items:
        ticker = item["ticker"]
        price_update = cache.get(ticker)
        entry = {
            "id": item["id"],
            "ticker": ticker,
            "added_at": item["added_at"],
        }
        if price_update:
            pd = price_update.to_dict()
            entry["price"] = pd["price"]
            entry["previous_price"] = pd["previous_price"]
            entry["change"] = pd["change"]
            entry["change_pct"] = pd["change_percent"]
            entry["direction"] = pd["direction"]
        result.append(entry)
    return result


@router.post("")
async def add_ticker(body: AddTickerRequest, request: Request):
    """Add a ticker to the watchlist."""
    ticker = body.ticker.upper()
    if not TICKER_RE.match(ticker):
        raise HTTPException(status_code=400, detail="Ticker must be 1-5 uppercase letters")

    try:
        entry = await add_watchlist_ticker(ticker)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))

    # Add to market data source so prices start flowing
    source = request.app.state.market_source
    await source.add_ticker(ticker)

    return entry


@router.delete("/{ticker}")
async def delete_ticker(ticker: str, request: Request):
    """Remove a ticker from the watchlist."""
    ticker = ticker.upper()
    removed = await remove_watchlist_ticker(ticker)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Ticker {ticker} not in watchlist")

    # Remove from market data source
    source = request.app.state.market_source
    await source.remove_ticker(ticker)

    return {"removed": ticker}
