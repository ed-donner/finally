"""Structured-output schema and robust parsing."""

import json

from app.llm.schemas import FALLBACK_MESSAGE, ChatLLMResponse, parse_llm_response


def test_full_response():
    raw = json.dumps({
        "message": "Bought.",
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 10}],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    })
    r = parse_llm_response(raw)
    assert r.message == "Bought."
    assert r.trades[0].ticker == "AAPL" and r.trades[0].side == "buy" and r.trades[0].quantity == 10
    assert r.watchlist_changes[0].ticker == "PYPL" and r.watchlist_changes[0].action == "add"


def test_message_only_missing_optional_lists():
    r = parse_llm_response('{"message": "Hello"}')
    assert r.message == "Hello" and r.trades == [] and r.watchlist_changes == []


def test_empty_lists_and_null_lists():
    r = parse_llm_response('{"message": "x", "trades": null, "watchlist_changes": []}')
    assert r.trades == [] and r.watchlist_changes == []


def test_fractional_and_string_quantity_and_case_normalised():
    raw = '{"message": "m", "trades": [{"ticker": " nvda ", "side": "SELL", "quantity": "2.5"}],' \
          ' "watchlist_changes": [{"ticker": "pypl", "action": "Remove"}]}'
    r = parse_llm_response(raw)
    assert r.trades[0].ticker == "NVDA" and r.trades[0].side == "sell" and r.trades[0].quantity == 2.5
    assert r.watchlist_changes[0].ticker == "PYPL" and r.watchlist_changes[0].action == "remove"


def test_multiple_trades():
    raw = json.dumps({"message": "m", "trades": [
        {"ticker": "AAPL", "side": "buy", "quantity": 1},
        {"ticker": "MSFT", "side": "sell", "quantity": 3},
    ], "watchlist_changes": []})
    assert [t.ticker for t in parse_llm_response(raw).trades] == ["AAPL", "MSFT"]


def test_code_fenced_json():
    r = parse_llm_response('```json\n{"message": "fenced", "trades": []}\n```')
    assert r.message == "fenced"


def test_json_with_surrounding_prose():
    r = parse_llm_response('Sure! {"message": "inner", "trades": []} hope that helps')
    assert r.message == "inner"


def test_malformed_items_dropped_valid_kept():
    raw = json.dumps({"message": "m", "trades": [
        {"ticker": "AAPL", "side": "hold", "quantity": 1},
        {"ticker": "AAPL", "quantity": 1},
        "garbage",
        {"ticker": "MSFT", "side": "buy", "quantity": "lots"},
        {"ticker": "TSLA", "side": "buy", "quantity": 2},
    ], "watchlist_changes": [{"ticker": "X", "action": "delete"}, 5]})
    r = parse_llm_response(raw)
    assert [t.ticker for t in r.trades] == ["TSLA"]
    assert r.watchlist_changes == []


def test_plain_prose_becomes_message():
    r = parse_llm_response("I think you should diversify.")
    assert r.message == "I think you should diversify." and r.trades == []


def test_broken_json_gives_fallback():
    r = parse_llm_response('{"message": "unterminated')
    assert r.message == FALLBACK_MESSAGE and r.trades == [] and r.watchlist_changes == []


def test_empty_and_none_give_fallback():
    assert parse_llm_response("").message == FALLBACK_MESSAGE
    assert parse_llm_response(None).message == FALLBACK_MESSAGE


def test_non_object_json_gives_fallback():
    assert parse_llm_response("[1, 2]").message == FALLBACK_MESSAGE
    assert parse_llm_response('{"trades": []}').message == FALLBACK_MESSAGE


def test_json_schema_is_strict_compatible():
    schema = ChatLLMResponse.model_json_schema()
    assert set(schema["required"]) == {"message", "trades", "watchlist_changes"}
    assert schema["additionalProperties"] is False
    for name in ("TradeInstruction", "WatchlistChange"):
        sub = schema["$defs"][name]
        assert sub["additionalProperties"] is False
        assert set(sub["required"]) == set(sub["properties"])
