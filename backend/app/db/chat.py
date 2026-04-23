"""Chat messages repository."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .connection import connect
from .init import DEFAULT_USER_ID

Role = Literal["user", "assistant"]


@dataclass(frozen=True)
class ChatMessage:
    id: str
    role: Role
    content: str
    actions: dict[str, Any] | None
    created_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def append_message(
    role: Role,
    content: str,
    actions: dict[str, Any] | None = None,
    user_id: str = DEFAULT_USER_ID,
) -> ChatMessage:
    msg = ChatMessage(
        id=str(uuid.uuid4()),
        role=role,
        content=content,
        actions=actions,
        created_at=_now(),
    )
    with connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, user_id, role, content, actions, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                msg.id, user_id, msg.role, msg.content,
                json.dumps(actions) if actions is not None else None,
                msg.created_at,
            ),
        )
    return msg


def list_recent(limit: int = 50, user_id: str = DEFAULT_USER_ID) -> list[ChatMessage]:
    """Return up to `limit` most recent messages, oldest-first (chat-render order)."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, role, content, actions, created_at FROM chat_messages "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()

    out: list[ChatMessage] = []
    for r in reversed(rows):
        actions = json.loads(r["actions"]) if r["actions"] else None
        out.append(
            ChatMessage(
                id=r["id"],
                role=r["role"],
                content=r["content"],
                actions=actions,
                created_at=r["created_at"],
            )
        )
    return out
