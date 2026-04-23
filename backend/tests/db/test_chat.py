"""Chat messages repository tests."""

from __future__ import annotations

from app import db


def test_append_user_message(temp_db: str) -> None:
    msg = db.chat.append_message("user", "hello")
    assert msg.role == "user"
    assert msg.actions is None


def test_append_assistant_with_actions(temp_db: str) -> None:
    actions = {"trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10, "ok": True}]}
    msg = db.chat.append_message("assistant", "Bought AAPL.", actions=actions)
    assert msg.actions == actions


def test_list_recent_returns_oldest_first(temp_db: str) -> None:
    db.chat.append_message("user", "first")
    db.chat.append_message("assistant", "second")
    db.chat.append_message("user", "third")
    listed = db.chat.list_recent()
    assert [m.content for m in listed] == ["first", "second", "third"]


def test_list_recent_respects_limit(temp_db: str) -> None:
    for i in range(10):
        db.chat.append_message("user", f"msg {i}")
    listed = db.chat.list_recent(limit=3)
    assert len(listed) == 3
    # Limit picks the most-recent N, but returns them oldest-first
    assert [m.content for m in listed] == ["msg 7", "msg 8", "msg 9"]


def test_actions_roundtrip_json(temp_db: str) -> None:
    actions = {
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 1.5}],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    }
    db.chat.append_message("assistant", "ok", actions=actions)
    listed = db.chat.list_recent()
    assert listed[-1].actions == actions
