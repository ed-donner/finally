"""Watchlist API endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db.connection import get_db
from ..market.cache import PriceCache

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_price_cache: PriceCache | None = None


def set_price_cache(cache: PriceCache) -> None:
    global _price_cache
    _price_cache = cache


class AddTickerRequest(BaseModel):
    ticker: str


@router.get("")
def get_watchlist() -> list[dict[str, Any]]:
    """Return the current watchlist with latest prices."""
    with get_db() as db:
        rows = db.execute(
            "SELECT ticker, added_at FROM watchlist WHERE user_id = 'default' ORDER BY added_at"
        ).fetchall()

    result = []
    for row in rows:
        ticker = row["ticker"]
        price_update = _price_cache.get(ticker) if _price_cache else None
        entry: dict[str, Any] = {
            "ticker": ticker,
            "added_at": row["added_at"],
        }
        if price_update:
            entry.update(price_update.to_dict())
        else:
            entry.update({"price": None, "previous_price": None, "change": None, "change_percent": None, "direction": "flat"})
        result.append(entry)

    return result


@router.post("", status_code=201)
async def add_ticker(request: AddTickerRequest, market_source: Any = None) -> dict[str, Any]:
    """Add a ticker to the watchlist."""
    ticker = request.ticker.upper().strip()
    if not ticker:
        raise HTTPException(status_code=400, detail="Ticker cannot be empty")

    now = datetime.now(timezone.utc).isoformat()
    try:
        with get_db() as db:
            db.execute(
                "INSERT INTO watchlist (id, user_id, ticker, added_at) VALUES (?, 'default', ?, ?)",
                (str(uuid.uuid4()), ticker, now),
            )
    except Exception as e:
        if "UNIQUE constraint" in str(e):
            raise HTTPException(status_code=409, detail=f"{ticker} is already in watchlist")
        raise

    # Add to market data source if available
    from ..main import get_market_source
    source = get_market_source()
    if source:
        await source.add_ticker(ticker)

    return {"ticker": ticker, "added_at": now}


@router.delete("/{ticker}", status_code=204)
async def remove_ticker(ticker: str) -> None:
    """Remove a ticker from the watchlist."""
    ticker = ticker.upper().strip()

    with get_db() as db:
        result = db.execute(
            "DELETE FROM watchlist WHERE user_id = 'default' AND ticker = ?",
            (ticker,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"{ticker} not in watchlist")

    # Remove from market data source and cache
    from ..main import get_market_source
    source = get_market_source()
    if source:
        await source.remove_ticker(ticker)
