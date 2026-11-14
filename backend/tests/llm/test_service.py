"""handle_chat end-to-end against a temp DB (mock mode and monkeypatched LLM)."""

import json
from types import SimpleNamespace

import pytest

from app import db
from app.llm import client, handle_chat
from app.llm.client import EXTRA_BODY, MODEL
from app.llm.mock import MOCK_DEFAULT_MESSAGE
from app.llm.schemas import ChatLLMResponse
from app.llm.service import LLM_ERROR_MESSAGE, NOT_CONFIGURED_MESSAGE


def _fake_completion(content=None, exc=None, calls=None):
    async def fake(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if exc is not None:
            raise exc
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])

    return fake


@pytest.fixture
def live_mode(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


# ---- mock mode -------------------------------------------------------------

async def test_mock_default(mock_mode, price_cache, source):
    r = await handle_chat("hello", price_cache, source)
    assert r == {"message": MOCK_DEFAULT_MESSAGE, "trades": [], "watchlist_changes": []}


async def test_mock_buy_executes(mock_mode, price_cache, source):
    r = await handle_chat("buy 5 NVDA", price_cache, source)
    assert r["message"].startswith("Mock: buying")
    assert r["trades"] == [{"ticker": "NVDA", "side": "buy", "quantity": 5.0,
                            "status": "executed", "price": 880.0, "error": None}]
    assert db.get_cash_balance() == pytest.approx(10000 - 5 * 880)
    assert db.get_position("NVDA")["quantity"] == 5


async def test_mock_buy_insufficient_cash_fails_inline(mock_mode, price_cache, source):
    r = await handle_chat("buy 1000 NVDA", price_cache, source)
    t = r["trades"][0]
    assert t["status"] == "failed" and t["error"] and t["price"] is None
    assert "could not be completed" in r["message"]
    assert db.get_cash_balance() == pytest.approx(10000)
    assert db.get_position("NVDA") is None


async def test_mock_sell_more_than_owned_fails(mock_mode, price_cache, source):
    r = await handle_chat("sell 3 AAPL", price_cache, source)
    assert r["trades"][0]["status"] == "failed" and r["trades"][0]["error"]


async def test_mock_buy_then_sell(mock_mode, price_cache, source):
    await handle_chat("buy 10 AAPL", price_cache, source)
    r = await handle_chat("sell 4 AAPL", price_cache, source)
    assert r["trades"][0]["status"] == "executed"
    assert db.get_position("AAPL")["quantity"] == pytest.approx(6)
    assert db.get_cash_balance() == pytest.approx(10000 - 6 * 190)


async def test_mock_watchlist_add_and_remove(mock_mode, price_cache, source):
    r = await handle_chat("add PYPL", price_cache, source)
    assert r["watchlist_changes"] == [
        {"ticker": "PYPL", "action": "add", "status": "executed", "error": None}]
    assert "PYPL" in db.get_watchlist()
    assert "PYPL" in source.get_tickers()

    r = await handle_chat("remove PYPL", price_cache, source)
    assert r["watchlist_changes"][0]["status"] == "executed"
    assert "PYPL" not in db.get_watchlist()
    assert "PYPL" not in source.get_tickers()


async def test_mock_remove_absent_ticker_fails(mock_mode, price_cache, source):
    r = await handle_chat("remove ZZZZ", price_cache, source)
    c = r["watchlist_changes"][0]
    assert c["status"] == "failed" and c["error"]


async def test_messages_persisted_with_actions(mock_mode, price_cache, source):
    await handle_chat("buy 2 AAPL", price_cache, source)
    msgs = db.get_chat_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"] == "buy 2 AAPL" and msgs[0]["actions"] is None
    assert msgs[1]["content"].startswith("Mock: buying")
    assert msgs[1]["actions"]["trades"][0]["status"] == "executed"
    assert msgs[1]["actions"]["watchlist_changes"] == []


# ---- real-LLM path (network monkeypatched) ---------------------------------

async def test_llm_call_parameters_and_execution(live_mode, monkeypatch, price_cache, source):
    calls = []
    content = json.dumps({
        "message": "Buying 1 MSFT and watching PYPL.",
        "trades": [{"ticker": "MSFT", "side": "buy", "quantity": 1}],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    })
    monkeypatch.setattr(client, "_acompletion", _fake_completion(content, calls=calls))
    db.add_chat_message("user", "earlier question")
    db.add_chat_message("assistant", "earlier answer", {"trades": [], "watchlist_changes": []})

    r = await handle_chat("buy a share of microsoft and watch paypal", price_cache, source)

    assert r["message"] == "Buying 1 MSFT and watching PYPL."
    assert r["trades"][0]["status"] == "executed" and r["trades"][0]["price"] == 420.0
    assert r["watchlist_changes"][0]["status"] == "executed"
    assert "PYPL" in source.get_tickers()

    kw = calls[0]
    assert kw["model"] == MODEL == "openrouter/openai/gpt-oss-120b"
    assert kw["extra_body"] == EXTRA_BODY == {"provider": {"order": ["cerebras"]}}
    assert kw["reasoning_effort"] == "low"
    assert kw["response_format"] is ChatLLMResponse
    msgs = kw["messages"]
    assert msgs[0]["role"] == "system" and "FinAlly" in msgs[0]["content"]
    context = msgs[1]["content"]
    assert "Cash" in context and "AAPL" in context and "$190" in context
    assert [m["content"] for m in msgs[2:4]] == ["earlier question", "earlier answer"]
    assert msgs[-1] == {"role": "user", "content": "buy a share of microsoft and watch paypal"}


async def test_context_includes_positions(live_mode, monkeypatch, price_cache, source):
    from app.services.portfolio import execute_trade

    execute_trade("AAPL", "buy", 10, price_cache)
    calls = []
    monkeypatch.setattr(client, "_acompletion", _fake_completion('{"message": "ok"}', calls=calls))
    await handle_chat("how am I doing?", price_cache, source)
    context = calls[0]["messages"][1]["content"]
    assert "AAPL: 10" in context


async def test_history_limited_to_20(live_mode, monkeypatch, price_cache, source):
    for i in range(30):
        db.add_chat_message("user", f"m{i}")
    calls = []
    monkeypatch.setattr(client, "_acompletion", _fake_completion('{"message": "ok"}', calls=calls))
    await handle_chat("latest", price_cache, source)
    msgs = calls[0]["messages"]
    assert len(msgs) == 2 + 20 + 1
    assert msgs[2]["content"] == "m10"


async def test_llm_trade_validation_failure_inline(live_mode, monkeypatch, price_cache, source):
    content = json.dumps({"message": "Selling.", "trades": [
        {"ticker": "TSLA", "side": "sell", "quantity": 5}], "watchlist_changes": []})
    monkeypatch.setattr(client, "_acompletion", _fake_completion(content))
    r = await handle_chat("sell my tesla", price_cache, source)
    assert r["trades"][0]["status"] == "failed"
    assert r["trades"][0]["error"] in r["message"]


async def test_llm_invalid_watchlist_ticker_fails_inline(live_mode, monkeypatch, price_cache, source):
    content = json.dumps({"message": "Adding.", "trades": [],
                          "watchlist_changes": [{"ticker": "NOT A TICKER!", "action": "add"}]})
    monkeypatch.setattr(client, "_acompletion", _fake_completion(content))
    r = await handle_chat("add junk", price_cache, source)
    assert r["watchlist_changes"][0]["status"] == "failed"


async def test_llm_exception_returns_apology(live_mode, monkeypatch, price_cache, source):
    monkeypatch.setattr(client, "_acompletion", _fake_completion(exc=RuntimeError("boom")))
    r = await handle_chat("hi", price_cache, source)
    assert r == {"message": LLM_ERROR_MESSAGE, "trades": [], "watchlist_changes": []}
    assert [m["role"] for m in db.get_chat_messages()] == ["user", "assistant"]


async def test_llm_malformed_output_graceful(live_mode, monkeypatch, price_cache, source):
    monkeypatch.setattr(client, "_acompletion", _fake_completion('{"message": broken'))
    r = await handle_chat("hi", price_cache, source)
    assert r["message"] and r["trades"] == [] and r["watchlist_changes"] == []


async def test_llm_none_content_graceful(live_mode, monkeypatch, price_cache, source):
    monkeypatch.setattr(client, "_acompletion", _fake_completion(None))
    r = await handle_chat("hi", price_cache, source)
    assert r["message"] and r["trades"] == []


async def test_missing_api_key_friendly(monkeypatch, price_cache, source):
    called = []
    monkeypatch.setattr(client, "_acompletion", _fake_completion("{}", calls=called))
    r = await handle_chat("hi", price_cache, source)
    assert r == {"message": NOT_CONFIGURED_MESSAGE, "trades": [], "watchlist_changes": []}
    assert called == []


async def test_buy_untracked_ticker_starts_tracking(mock_mode, price_cache, source):
    price_cache.remove("PYPL")
    r = await handle_chat("buy 2 PYPL", price_cache, source)
    assert r["trades"][0]["status"] == "executed"
    assert "PYPL" in source.get_tickers()  # held position stays tracked
    assert "PYPL" not in db.get_watchlist()


async def test_failed_buy_untracked_ticker_is_untracked_again(mock_mode, price_cache, source):
    price_cache.remove("PYPL")
    r = await handle_chat("buy 100000 PYPL", price_cache, source)
    assert r["trades"][0]["status"] == "failed"
    assert "PYPL" not in source.get_tickers()
