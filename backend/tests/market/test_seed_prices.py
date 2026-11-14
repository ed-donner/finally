"""Tests for simulator seed data."""

import pytest

from app.market.seed_prices import (
    CORRELATION_GROUPS,
    DEFAULT_PARAMS,
    SEED_PRICES,
    TICKER_PARAMS,
    seed_price_for,
)

DEFAULT_WATCHLIST = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]


def test_default_watchlist_has_seed_and_params():
    for t in DEFAULT_WATCHLIST:
        assert SEED_PRICES[t] > 0
        assert 0 < TICKER_PARAMS[t]["sigma"] < 1
        assert "mu" in TICKER_PARAMS[t]


def test_default_params_sane():
    assert 0 < DEFAULT_PARAMS["sigma"] < 1


def test_correlation_groups_disjoint():
    assert not (CORRELATION_GROUPS["tech"] & CORRELATION_GROUPS["finance"])


def test_known_ticker_uses_table():
    assert seed_price_for("AAPL") == SEED_PRICES["AAPL"]


@pytest.mark.parametrize("ticker", ["PYPL", "AMD", "BRK.B", "ZZZZ"])
def test_unknown_ticker_seed_is_stable_and_in_range(ticker):
    price = seed_price_for(ticker)
    assert price == seed_price_for(ticker)
    assert 50.0 <= price < 300.0
    assert round(price, 2) == price


def test_unknown_tickers_differ():
    assert len({seed_price_for(t) for t in ["PYPL", "AMD", "INTC", "ORCL", "CRM"]}) > 1
