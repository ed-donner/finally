"""FastAPI router factory for chat endpoint."""

from __future__ import annotations

import aiosqlite
from fastapi import APIRouter

from app.market.cache import PriceCache
from app.market.interface import MarketDataSource

from .models import ChatRequest, ChatResponse
from .service import process_chat_message


def create_chat_router(
    db: aiosqlite.Connection,
    price_cache: PriceCache,
    market_source: MarketDataSource,
) -> APIRouter:
    """Create the chat router with injected dependencies.

    Follows the same closure-based factory pattern as create_portfolio_router.
    """
    router = APIRouter(prefix="/api", tags=["chat"])

    @router.post("/chat", response_model=ChatResponse)
    async def post_chat(request: ChatRequest):
        """Send a message to the AI assistant and receive a response with actions."""
        return await process_chat_message(
            db, price_cache, market_source, request.message
        )

    return router
