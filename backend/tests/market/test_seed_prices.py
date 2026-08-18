"""Tests for seed_prices.py constants."""

from app.market.seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)


class TestSeedPrices:
    def test_default_watchlist_tickers_present(self):
        """The 10 default watchlist tickers from PLAN.md §7 must all be seeded."""
        expected = {
            "AAPL",
            "GOOGL",
            "MSFT",
            "AMZN",
            "TSLA",
            "NVDA",
            "META",
            "JPM",
            "V",
            "NFLX",
        }
        assert expected == set(SEED_PRICES.keys())

    def test_seed_prices_are_positive(self):
        for ticker, price in SEED_PRICES.items():
            assert price > 0, f"{ticker} seed price must be positive"

    def test_every_seed_ticker_has_params(self):
        """Every ticker with a seed price must also have GBM params."""
        assert set(SEED_PRICES.keys()) == set(TICKER_PARAMS.keys())

    def test_ticker_params_are_valid(self):
        for ticker, params in TICKER_PARAMS.items():
            assert "sigma" in params
            assert "mu" in params
            assert params["sigma"] > 0, f"{ticker} sigma must be positive"

    def test_default_params_valid(self):
        assert "sigma" in DEFAULT_PARAMS
        assert "mu" in DEFAULT_PARAMS
        assert DEFAULT_PARAMS["sigma"] > 0

    def test_correlation_groups_reference_known_tickers(self):
        """Every ticker in a correlation group must have a seed price."""
        for group, tickers in CORRELATION_GROUPS.items():
            for ticker in tickers:
                assert ticker in SEED_PRICES, (
                    f"{ticker} in correlation group {group!r} has no seed price"
                )

    def test_correlation_groups_are_disjoint(self):
        """A ticker should not belong to more than one sector group."""
        tech = CORRELATION_GROUPS["tech"]
        finance = CORRELATION_GROUPS["finance"]
        assert tech.isdisjoint(finance)

    def test_correlation_coefficients_are_valid(self):
        """Correlation coefficients must be valid correlation values."""
        for corr in (INTRA_TECH_CORR, INTRA_FINANCE_CORR, CROSS_GROUP_CORR, TSLA_CORR):
            assert -1.0 <= corr <= 1.0
