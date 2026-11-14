"""Chat endpoints: POST /api/chat and GET /api/chat/history."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app.llm import get_chat_history, handle_chat

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


@router.post("")
async def post_chat(body: ChatRequest, request: Request) -> dict:
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="Message must not be empty")
    return await handle_chat(
        body.message, request.app.state.price_cache, request.app.state.market_source
    )


@router.get("/history")
async def chat_history() -> dict:
    return {"messages": get_chat_history()}
