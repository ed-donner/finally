"""Chat API routes."""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.market import PriceCache

from .service import ChatService


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)


def create_chat_router(price_cache: PriceCache) -> APIRouter:
    """Create chat API router with injected dependencies."""
    router = APIRouter(prefix="/api/chat", tags=["chat"])

    def _service() -> ChatService:
        from app.db import get_db
        return ChatService(get_db(), price_cache)

    @router.post("")
    async def send_message(req: ChatRequest):
        try:
            return await _service().send_message(req.message)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    @router.get("/history")
    def get_history():
        return _service().get_history()

    return router
