"""HTTP routes for chat: GET history, POST message."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from . import db
from .llm import service as llm_service
from .state import AppState, get_state


class ChatPostRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    allow_trade_execution: bool = False


class ChatMessageResponse(BaseModel):
    id: str
    role: Literal["user", "assistant"]
    content: str
    actions: dict[str, Any] | None
    created_at: str


class ChatPostResponse(BaseModel):
    user_message: ChatMessageResponse
    assistant_message: ChatMessageResponse


router = APIRouter(prefix="/api/chat", tags=["chat"])


def _to_response(msg: db.ChatMessage) -> ChatMessageResponse:
    return ChatMessageResponse(
        id=msg.id,
        role=msg.role,
        content=msg.content,
        actions=msg.actions,
        created_at=msg.created_at,
    )


@router.get("", response_model=list[ChatMessageResponse])
def get_history(limit: int = 50) -> list[ChatMessageResponse]:
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="limit must be between 1 and 500",
        )
    return [_to_response(m) for m in db.chat.list_recent(limit=limit)]


@router.post("", response_model=ChatPostResponse)
async def post_message(
    body: ChatPostRequest, state: AppState = Depends(get_state)
) -> ChatPostResponse:
    user_row, assistant_row, _ = await llm_service.handle_user_message(
        body.message,
        state.price_cache,
        state.market_source,
        allow_trade_execution=body.allow_trade_execution,
    )
    return ChatPostResponse(
        user_message=_to_response(user_row),
        assistant_message=_to_response(assistant_row),
    )
