"""Tests for system prompt and portfolio context formatting."""

from app.llm.prompts import SYSTEM_PROMPT, format_portfolio_context


def test_system_prompt_mentions_finally():
    assert "FinAlly" in SYSTEM_PROMPT


def test_system_prompt_mentions_json():
    assert "JSON" in SYSTEM_PROMPT


def test_format_empty_portfolio():
    ctx = {"cash_balance": 10000, "positions": [], "watchlist": [], "total_value": 10000}
    result = format_portfolio_context(ctx)
    assert "$10,000.00" in result
    assert "Positions: None" in result
    assert "Watchlist: Empty" in result


def test_format_with_positions():
    ctx = {
        "cash_balance": 5000,
        "total_value": 15000,
        "positions": [
            {
                "ticker": "AAPL",
                "quantity": 10,
                "avg_cost": 180.0,
                "current_price": 195.0,
                "unrealized_pnl": 150.0,
                "pnl_percent": 8.3,
            }
        ],
        "watchlist": [],
    }
    result = format_portfolio_context(ctx)
    assert "AAPL" in result
    assert "10 shares" in result
    assert "$180.00" in result
    assert "$195.00" in result


def test_format_with_watchlist():
    ctx = {
        "cash_balance": 10000,
        "total_value": 10000,
        "positions": [],
        "watchlist": [
            {"ticker": "GOOGL", "price": 175.50},
            {"ticker": "MSFT", "price": None},
        ],
    }
    result = format_portfolio_context(ctx)
    assert "GOOGL: $175.50" in result
    assert "MSFT: no price data" in result


def test_format_defaults_on_missing_keys():
    ctx = {}
    result = format_portfolio_context(ctx)
    assert "Cash: $0.00" in result
