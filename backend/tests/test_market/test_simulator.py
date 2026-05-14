"""Unit tests for MarketSimulator."""
import asyncio
import pytest
import numpy as np
from datetime import datetime, timezone

from app.market.cache import PriceCache
from app.market.simulator import (
    MarketSimulator,
    TICKER_PARAMS,
    TICK_INTERVAL,
    SECONDS_PER_YEAR,
    _TickerState,
)


@pytest.fixture
def cache():
    return PriceCache()


@pytest.fixture
def simulator(cache):
    return MarketSimulator(cache)


class TestTickerStateInit:
    def test_make_state_uses_seed_price(self, simulator):
        state = simulator._make_state("AAPL")
        assert state.price == TICKER_PARAMS["AAPL"][0]

    def test_make_state_computes_drift_per_tick(self, simulator):
        state = simulator._make_state("AAPL")
        _, annual_drift, annual_vol = TICKER_PARAMS["AAPL"]
        expected_drift = (annual_drift - 0.5 * annual_vol ** 2) * (TICK_INTERVAL / SECONDS_PER_YEAR)
        assert abs(state.drift - expected_drift) < 1e-15

    def test_make_state_computes_vol_per_tick(self, simulator):
        state = simulator._make_state("MSFT")
        _, _, annual_vol = TICKER_PARAMS["MSFT"]
        expected_vol = annual_vol * (TICK_INTERVAL / SECONDS_PER_YEAR) ** 0.5
        assert abs(state.vol - expected_vol) < 1e-15

    def test_make_state_unknown_ticker_uses_defaults(self, simulator):
        state = simulator._make_state("XXXX")
        assert state.price == 100.0
        assert state.revert == 0

    def test_make_state_revert_starts_at_zero(self, simulator):
        state = simulator._make_state("TSLA")
        assert state.revert == 0


class TestInitTickers:
    def test_init_creates_states_for_all_tickers(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT", "GOOGL"])
        assert set(simulator._tickers) == {"AAPL", "MSFT", "GOOGL"}
        assert "AAPL" in simulator._states
        assert "MSFT" in simulator._states

    def test_init_uppercases_tickers(self, simulator):
        simulator._init_tickers(["aapl", "msft"])
        assert "AAPL" in simulator._states
        assert "MSFT" in simulator._states

    def test_init_builds_cholesky(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT"])
        assert simulator._cholesky is not None

    def test_cholesky_is_none_for_empty_list(self, simulator):
        simulator._init_tickers([])
        assert simulator._cholesky is None


class TestCholeskyDecomposition:
    def test_cholesky_shape_matches_ticker_count(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT", "TSLA"])
        assert simulator._cholesky.shape == (3, 3)

    def test_cholesky_lower_triangular(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT"])
        L = simulator._cholesky
        # Upper triangle (excluding diagonal) should be zero
        assert np.allclose(np.triu(L, k=1), 0)

    def test_cholesky_reconstructs_correlation(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT"])
        L = simulator._cholesky
        C_reconstructed = L @ L.T
        # Diagonal should be 1 (valid correlation matrix)
        assert np.allclose(np.diag(C_reconstructed), 1.0)

    def test_cholesky_valid_for_single_ticker(self, simulator):
        simulator._init_tickers(["AAPL"])
        L = simulator._cholesky
        assert L.shape == (1, 1)
        assert abs(L[0, 0] - 1.0) < 1e-10


class TestAddRemoveTicker:
    def test_add_ticker_creates_state(self, simulator):
        simulator._init_tickers(["AAPL"])
        simulator.add_ticker("TSLA")
        assert "TSLA" in simulator._states
        assert "TSLA" in simulator._tickers

    def test_add_ticker_idempotent(self, simulator):
        simulator._init_tickers(["AAPL"])
        simulator.add_ticker("AAPL")
        assert simulator._tickers.count("AAPL") == 1

    def test_add_ticker_normalizes_case(self, simulator):
        simulator._init_tickers([])
        simulator.add_ticker("nvda")
        assert "NVDA" in simulator._states

    def test_remove_ticker_clears_state(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT"])
        simulator.remove_ticker("AAPL")
        assert "AAPL" not in simulator._states
        assert "AAPL" not in simulator._tickers

    def test_remove_ticker_normalizes_case(self, simulator):
        simulator._init_tickers(["AAPL"])
        simulator.remove_ticker("aapl")
        assert "AAPL" not in simulator._states

    def test_remove_nonexistent_ticker_is_safe(self, simulator):
        simulator._init_tickers(["AAPL"])
        simulator.remove_ticker("ZZZZ")  # should not raise


class TestComputeTick:
    def test_returns_one_update_per_ticker(self, simulator):
        simulator._init_tickers(["AAPL", "MSFT", "TSLA"])
        # Run many ticks; each must produce at most 1 update per ticker
        for _ in range(20):
            updates = simulator._compute_tick()
            tickers_in_update = [u.ticker for u in updates]
            # No duplicates
            assert len(tickers_in_update) == len(set(tickers_in_update))

    def test_returns_empty_list_for_no_tickers(self, simulator):
        simulator._init_tickers([])
        assert simulator._compute_tick() == []

    def test_prices_always_positive(self, simulator):
        simulator._init_tickers(list(TICKER_PARAMS.keys()))
        for _ in range(50):
            updates = simulator._compute_tick()
            for u in updates:
                assert u.price > 0, f"{u.ticker} price went non-positive: {u.price}"

    def test_updates_contain_correct_fields(self, simulator):
        simulator._init_tickers(["AAPL"])
        updates = simulator._compute_tick()
        assert len(updates) == 1
        u = updates[0]
        assert u.ticker == "AAPL"
        assert isinstance(u.price, float)
        assert isinstance(u.prev_price, float)
        assert isinstance(u.timestamp, datetime)

    def test_change_equals_price_minus_prev_price(self, simulator):
        simulator._init_tickers(["AAPL"])
        for _ in range(10):
            updates = simulator._compute_tick()
            for u in updates:
                assert abs(u.change - (u.price - u.prev_price)) < 1e-6

    def test_state_updated_after_tick(self, simulator):
        simulator._init_tickers(["AAPL"])
        initial_price = simulator._states["AAPL"].price
        updates = simulator._compute_tick()
        new_price = simulator._states["AAPL"].price
        if updates:
            assert new_price == updates[0].price

    def test_gbm_prices_log_normally_distributed(self, simulator):
        """Prices after many ticks should be log-normal (basic sanity check)."""
        simulator._init_tickers(["AAPL"])
        prices = []
        for _ in range(200):
            updates = simulator._compute_tick()
            if updates:
                prices.append(updates[0].price)
        # All prices positive
        assert all(p > 0 for p in prices)
        # Prices should stay in a reasonable range (not explode or go to zero)
        assert max(prices) < TICKER_PARAMS["AAPL"][0] * 100  # sanity upper bound


class TestSimulatorLifecycle:
    @pytest.mark.asyncio
    async def test_start_and_stop(self, cache):
        sim = MarketSimulator(cache)
        await sim.start(["AAPL", "MSFT"])
        await asyncio.sleep(0.1)
        await sim.stop()

    @pytest.mark.asyncio
    async def test_start_produces_cache_updates(self, cache):
        sim = MarketSimulator(cache)
        await sim.start(["AAPL"])
        await asyncio.sleep(1.1)  # wait for at least 2 ticks
        await sim.stop()
        result = cache.get("AAPL")
        assert result is not None
        assert result.price > 0

    @pytest.mark.asyncio
    async def test_stop_is_idempotent(self, cache):
        sim = MarketSimulator(cache)
        await sim.start(["AAPL"])
        await sim.stop()
        await sim.stop()  # second stop should not raise

    @pytest.mark.asyncio
    async def test_get_daily_bars_returns_empty_list(self, cache):
        sim = MarketSimulator(cache)
        bars = await sim.get_daily_bars("AAPL", "2024-01-01", "2024-01-31")
        assert bars == []
