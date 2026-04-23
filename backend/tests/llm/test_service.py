"""Chat orchestration tests using mock LLM mode."""

from __future__ import annotations

import pytest

from app import db
from app.llm import service as llm_service
from app.market import PriceCache

from .conftest import StubMarketSource


async def test_user_and_assistant_persisted(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    user, assistant, actions = await llm_service.handle_user_message(
        "hello", primed_cache, stub_source
    )
    assert user.role == "user"
    assert assistant.role == "assistant"
    assert actions.is_empty()
    history = db.chat.list_recent()
    assert [m.id for m in history[-2:]] == [user.id, assistant.id]


async def test_trade_requires_explicit_approval(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, assistant, actions = await llm_service.handle_user_message(
        "buy 1 AAPL", primed_cache, stub_source
    )
    assert len(actions.trades) == 1
    assert actions.trades[0].status == "approval_required"
    assert "explicit approval" in (actions.trades[0].error or "").lower()
    assert "not approved for execution" in assistant.content.lower()
    assert db.positions.get_position("AAPL") is None


async def test_buy_executed_when_llm_requests_with_approval(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, assistant, actions = await llm_service.handle_user_message(
        "buy 1 AAPL", primed_cache, stub_source, allow_trade_execution=True
    )
    assert len(actions.trades) == 1
    assert actions.trades[0].status == "executed"
    assert actions.trades[0].cash_balance_after == pytest.approx(9_900.0)
    pos = db.positions.get_position("AAPL")
    assert pos is not None and pos.quantity == 1
    # Actions are persisted on the assistant message for rehydration on reload
    assert assistant.actions is not None
    assert assistant.actions["trades"][0]["status"] == "executed"


async def test_failed_trade_attached_not_raised(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, assistant, actions = await llm_service.handle_user_message(
        "buy 10000 AAPL", primed_cache, stub_source, allow_trade_execution=True
    )
    assert len(actions.trades) == 1
    assert actions.trades[0].status == "failed"
    assert "insufficient" in actions.trades[0].error.lower()
    # No position created on failure
    assert db.positions.get_position("AAPL") is None
    # The assistant message still got persisted with the failure recorded
    assert assistant.actions["trades"][0]["status"] == "failed"


async def test_partial_success_one_trade_each(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, _, actions = await llm_service.handle_user_message(
        "buy 1 AAPL and sell 5 TSLA",
        primed_cache,
        stub_source,
        allow_trade_execution=True,
    )
    by_ticker = {t.ticker: t for t in actions.trades}
    assert by_ticker["AAPL"].status == "executed"
    assert by_ticker["TSLA"].status == "failed"  # no shares to sell


async def test_watchlist_add_executed(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, _, actions = await llm_service.handle_user_message(
        "add PYPL to my watchlist", primed_cache, stub_source
    )
    assert len(actions.watchlist_changes) == 1
    assert actions.watchlist_changes[0].ok is True
    assert "PYPL" in db.watchlist.list_tickers()
    assert "PYPL" in stub_source.added


async def test_watchlist_add_duplicate_failure(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    _, _, actions = await llm_service.handle_user_message(
        "add AAPL to my watchlist", primed_cache, stub_source
    )
    assert actions.watchlist_changes[0].ok is False


async def test_history_included_in_prompt(
    temp_db: str, primed_cache: PriceCache, stub_source: StubMarketSource, mock_mode: None
) -> None:
    """Sanity check that prior turns end up in the message list passed to the LLM."""
    db.chat.append_message("user", "prior question")
    db.chat.append_message("assistant", "prior answer")
    messages = llm_service._build_messages("now what?", primed_cache)
    contents = [m["content"] for m in messages]
    assert "prior question" in contents
    assert "prior answer" in contents
    assert contents[-1] == "now what?"
