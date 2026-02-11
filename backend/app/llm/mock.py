"""Deterministic mock LLM responses for testing and development."""

import json

MOCK_RESPONSES = {
    "default": {
        "message": "I can see your portfolio. You have cash available. How can I help?",
        "trades": [],
        "watchlist_changes": [],
    },
    "buy": {
        "message": "Done! I've bought 5 shares of AAPL for you.",
        "trades": [{"ticker": "AAPL", "side": "buy", "quantity": 5}],
        "watchlist_changes": [],
    },
    "sell": {
        "message": "Done! I've sold 5 shares of AAPL for you.",
        "trades": [{"ticker": "AAPL", "side": "sell", "quantity": 5}],
        "watchlist_changes": [],
    },
    "add": {
        "message": "I've added PYPL to your watchlist.",
        "trades": [],
        "watchlist_changes": [{"ticker": "PYPL", "action": "add"}],
    },
    "remove": {
        "message": "I've removed PYPL from your watchlist.",
        "trades": [],
        "watchlist_changes": [{"ticker": "PYPL", "action": "remove"}],
    },
}


def get_mock_response(user_message: str) -> str:
    """Return a deterministic mock response based on keyword matching.

    Matches keywords in order: buy, sell, add/watch, remove, then default.
    """
    lower = user_message.lower()
    if "buy" in lower:
        key = "buy"
    elif "sell" in lower:
        key = "sell"
    elif "add" in lower or "watch" in lower:
        key = "add"
    elif "remove" in lower:
        key = "remove"
    else:
        key = "default"
    return json.dumps(MOCK_RESPONSES[key])
