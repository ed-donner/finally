"""Chat API endpoint."""

import json

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.db import get_chat_history, insert_chat_message
from app.llm import chat_with_llm

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str


@router.post("")
async def chat(body: ChatRequest, request: Request):
    """Send a message to the AI assistant."""
    price_cache = request.app.state.price_cache

    # Store user message
    await insert_chat_message(role="user", content=body.message)

    # Get LLM response and execute actions
    result = await chat_with_llm(body.message, price_cache)

    # Store assistant response with actions
    actions = None
    if result["trades"] or result["watchlist_changes"]:
        actions = json.dumps({
            "trades": result["trades"],
            "watchlist_changes": result["watchlist_changes"],
        })

    await insert_chat_message(role="assistant", content=result["message"], actions=actions)

    return result


@router.get("/history")
async def chat_history():
    """Get recent chat messages."""
    messages = await get_chat_history()
    return {"messages": messages}
