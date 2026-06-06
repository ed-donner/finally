"""Tests for GBMSimulator."""

import numpy as np

from app.market.seed_prices import SEED_PRICES
from app.market.simulator import GBMSimulator


class TestGBMSimulator:
    """Unit tests for the GBM price simulator."""

    def test_step_returns_all_tickers(self):
        """Test that step() returns prices for all tickers."""
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["AAPL"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["AAPL"] > 0

    def test_initial_prices_match_seeds(self):
        """Test that initial prices match seed prices."""
        sim = GBMSimulator(tickers=["AAPL"])
        # Before any step, price should be the seed price
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_add_ticker(self):
        """Test adding a ticker dynamically."""
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("TSLA")
        result = sim.step()
        assert "TSLA" in result

    def test_remove_ticker(self):
        """Test removing a ticker."""
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        sim.remove_ticker("GOOGL")
        result = sim.step()
        assert "GOOGL" not in result
        assert "AAPL" in result

    def test_add_duplicate_is_noop(self):
        """Test that adding a duplicate ticker is a no-op."""
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("AAPL")
        assert len(sim._tickers) == 1

    def test_remove_nonexistent_is_noop(self):
        """Test that removing a non-existent ticker is a no-op."""
        sim = GBMSimulator(tickers=["AAPL"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_unknown_ticker_gets_random_seed_price(self):
        """Test that unknown tickers get random seed prices."""
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert price is not None
        assert 50.0 <= price <= 300.0

    def test_empty_step(self):
        """Test stepping with no tickers."""
        sim = GBMSimulator(tickers=[])
        result = sim.step()
        assert result == {}

    def test_prices_change_over_time(self):
        """After many steps, prices should have drifted from their seeds."""
        sim = GBMSimulator(tickers=["AAPL"])
        initial_price = sim.get_price("AAPL")

        for _ in range(1000):
            sim.step()

        final_price = sim.get_price("AAPL")
        # Price should have changed (extremely unlikely to be exactly the seed)
        assert final_price != initial_price

    def test_cholesky_rebuilds_on_add(self):
        """Test that Cholesky matrix is rebuilt when tickers are added."""
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None  # Only 1 ticker, no correlation matrix
        sim.add_ticker("GOOGL")
        assert sim._cholesky is not None  # Now 2 tickers, matrix exists

    def test_cholesky_none_with_one_ticker(self):
        """Test that Cholesky is None with only one ticker."""
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim._cholesky is None

    def test_get_price_returns_none_for_unknown(self):
        """Test that get_price returns None for unknown ticker."""
        sim = GBMSimulator(tickers=["AAPL"])
        assert sim.get_price("UNKNOWN") is None

    def test_pairwise_correlation_tech_stocks(self):
        """Test that tech stocks have high correlation."""
        corr = GBMSimulator._pairwise_correlation("AAPL", "GOOGL")
        assert corr == 0.6

    def test_pairwise_correlation_finance_stocks(self):
        """Test that finance stocks have moderate correlation."""
        corr = GBMSimulator._pairwise_correlation("JPM", "V")
        assert corr == 0.5

    def test_pairwise_correlation_tsla(self):
        """Test that TSLA has lower correlation with everything."""
        corr = GBMSimulator._pairwise_correlation("TSLA", "AAPL")
        assert corr == 0.3
        corr = GBMSimulator._pairwise_correlation("TSLA", "JPM")
        assert corr == 0.3

    def test_pairwise_correlation_cross_sector(self):
        """Test cross-sector correlation."""
        corr = GBMSimulator._pairwise_correlation("AAPL", "JPM")
        assert corr == 0.3

    def test_default_dt_is_reasonable(self):
        """Test that default dt is a reasonable small value."""
        assert 0 < GBMSimulator.DEFAULT_DT < 0.0001

    def test_seed_makes_steps_deterministic(self):
        """Same seed → identical price paths (M2: seedable RNG)."""
        a = GBMSimulator(tickers=["AAPL", "GOOGL"], seed=123)
        b = GBMSimulator(tickers=["AAPL", "GOOGL"], seed=123)
        for _ in range(50):
            assert a.step() == b.step()

    def test_different_seeds_diverge(self):
        """Different seeds → different paths."""
        a = GBMSimulator(tickers=["AAPL"], seed=1)
        b = GBMSimulator(tickers=["AAPL"], seed=2)
        for _ in range(10):
            a.step()
            b.step()
        assert a.get_price("AAPL") != b.get_price("AAPL")

    def test_event_shock_fires_with_expected_magnitude(self):
        """With event_probability=1 a 2–5% shock fires every tick (M2).

        The combined drift+diffusion over one ~8.5e-8 dt step is ~1e-4, so the
        per-tick move is dominated by the shock and must land in the 2–5% band.
        """
        sim = GBMSimulator(tickers=["AAPL"], event_probability=1.0, seed=99)
        prev = sim.get_price("AAPL")
        moves = []
        for _ in range(200):
            price = sim.step()["AAPL"]
            moves.append(abs(price / prev - 1.0))
            prev = price
        # Every tick moved, and the typical move sits in the shock band.
        assert min(moves) > 0.015
        assert np.median(moves) <= 0.06

    def test_cholesky_stays_pd_under_many_correlated_tickers(self):
        """Cholesky guard (M3): a large correlated set still factorizes."""
        sim = GBMSimulator(tickers=["AAPL"], seed=0)
        for i in range(40):
            sim.add_ticker(f"TECH{i}")
        # Should not raise and should still produce a price for every ticker.
        result = sim.step()
        assert len(result) == 41

    def test_add_ticker_normalizes(self):
        """Lowercase / padded tickers collapse to the canonical form (M6)."""
        sim = GBMSimulator(tickers=["AAPL"])
        sim.add_ticker("  tsla ")
        assert "TSLA" in sim.get_tickers()
        assert sim.get_price("tsla") == sim.get_price("TSLA")
        assert sim.get_price("TSLA") == SEED_PRICES["TSLA"]

    def test_cholesky_falls_back_when_not_positive_definite(self, monkeypatch):
        """A non-PD correlation table degrades to uncorrelated moves, not a crash (M3)."""
        # Inconsistent correlations (A~B +0.9, A~C +0.9, B~C −0.9) make the
        # 3x3 matrix non-positive-definite, beyond what the epsilon nudge fixes.
        bad = {("A", "B"): 0.9, ("A", "C"): 0.9, ("B", "C"): -0.9}
        monkeypatch.setattr(
            GBMSimulator,
            "_pairwise_correlation",
            staticmethod(lambda t1, t2: bad[tuple(sorted((t1, t2)))]),
        )

        sim = GBMSimulator(tickers=["A", "B", "C"], seed=0)
        assert sim._cholesky is None  # fell back to uncorrelated
        result = sim.step()  # must still run
        assert set(result.keys()) == {"A", "B", "C"}

    def test_step_returns_full_precision(self):
        """step() returns full-precision prices; rounding is the cache's job (L3)."""
        sim = GBMSimulator(tickers=["AAPL"], seed=42)
        result = sim.step()
        # A GBM step off a $190 seed virtually never lands exactly on 2 dp.
        assert round(result["AAPL"], 2) != result["AAPL"] or result["AAPL"] == 190.00

    def test_prices_rounded_to_two_decimals_via_cache(self):
        """Prices are rounded to 2 dp exactly once, in PriceCache."""
        from app.market.cache import PriceCache

        sim = GBMSimulator(tickers=["AAPL"], seed=7)
        cache = PriceCache()
        for _ in range(20):
            for ticker, price in sim.step().items():
                update = cache.update(ticker=ticker, price=price)
                decimal_part = str(update.price).split(".")[1] if "." in str(update.price) else ""
                assert len(decimal_part) <= 2
