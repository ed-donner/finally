"""Tests for the GBMSimulator maths engine."""

import math

import numpy as np
import pytest

from app.market.seed_prices import SEED_PRICES, seed_price_for
from app.market.simulator import GBMSimulator

DT = GBMSimulator.DEFAULT_DT


def path(sim: GBMSimulator, tickers: list[str], steps: int) -> np.ndarray:
    """Unrounded price path, shape (steps + 1, len(tickers))."""
    rows = [[sim.get_price(t) for t in tickers]]
    for _ in range(steps):
        sim.step()
        rows.append([sim.get_price(t) for t in tickers])
    return np.array(rows)


class TestBasics:
    def test_dt_is_half_second_of_trading_year(self):
        assert DT == pytest.approx(0.5 / (252 * 6.5 * 3600))

    def test_step_returns_all_tickers_rounded(self):
        sim = GBMSimulator(["AAPL", "MSFT"], seed=1)
        prices = sim.step()
        assert set(prices) == {"AAPL", "MSFT"}
        for p in prices.values():
            assert round(p, 2) == p

    def test_empty_simulator(self):
        assert GBMSimulator([]).step() == {}

    def test_starts_at_seed_prices(self):
        sim = GBMSimulator(["AAPL", "PYPL"])
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]
        assert sim.get_price("PYPL") == seed_price_for("PYPL")

    def test_initial_prices_override(self):
        sim = GBMSimulator(["PYPL", "AAPL"], initial_prices={"PYPL": 71.5})
        assert sim.get_price("PYPL") == 71.5
        assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

    def test_unknown_ticker_gets_default_params(self):
        sim = GBMSimulator(["PYPL"])
        assert sim._params["PYPL"] == {"sigma": 0.25, "mu": 0.05}

    def test_per_tick_moves_are_small(self):
        sim = GBMSimulator(["AAPL"], event_probability=0.0, seed=3)
        prev = sim.get_price("AAPL")
        for _ in range(500):
            sim.step()
            cur = sim.get_price("AAPL")
            assert abs(cur / prev - 1) < 0.005
            prev = cur

    def test_prices_stay_positive_under_extreme_volatility(self):
        sim = GBMSimulator(["X"], dt=0.01, event_probability=0.0, seed=5)
        sim._params["X"]["sigma"] = 3.0
        for _ in range(5_000):
            sim.step()
        assert sim.get_price("X") > 0


class TestDeterminism:
    def test_same_seed_same_path(self):
        a = GBMSimulator(["AAPL", "TSLA"], event_probability=0.01, seed=99)
        b = GBMSimulator(["AAPL", "TSLA"], event_probability=0.01, seed=99)
        assert [a.step() for _ in range(200)] == [b.step() for _ in range(200)]

    def test_different_seed_different_path(self):
        a = GBMSimulator(["AAPL"], seed=1)
        b = GBMSimulator(["AAPL"], seed=2)
        assert [a.step() for _ in range(50)] != [b.step() for _ in range(50)]


class TestStatistics:
    def test_log_return_volatility_matches_sigma(self):
        sim = GBMSimulator(["AAPL"], event_probability=0.0, seed=42)
        r = np.diff(np.log(path(sim, ["AAPL"], 20_000)[:, 0]))
        assert r.std() == pytest.approx(0.22 * math.sqrt(DT), rel=0.05)

    def test_sector_correlations(self):
        tickers = ["AAPL", "MSFT", "JPM", "V", "TSLA"]
        sim = GBMSimulator(tickers, event_probability=0.0, seed=7)
        c = np.corrcoef(np.diff(np.log(path(sim, tickers, 10_000)), axis=0).T)
        assert c[0, 1] == pytest.approx(0.6, abs=0.05)  # tech-tech
        assert c[2, 3] == pytest.approx(0.5, abs=0.05)  # finance-finance
        assert c[0, 2] == pytest.approx(0.3, abs=0.05)  # cross-sector
        assert c[0, 4] == pytest.approx(0.3, abs=0.05)  # TSLA


class TestEvents:
    def test_events_always_fire_when_probability_one(self):
        sim = GBMSimulator(["AAPL"], event_probability=1.0, seed=11)
        prev = sim.get_price("AAPL")
        for _ in range(100):
            sim.step()
            cur = sim.get_price("AAPL")
            assert 0.019 < abs(cur / prev - 1) < 0.051
            prev = cur

    def test_events_are_symmetric(self):
        sim = GBMSimulator(["AAPL"], event_probability=1.0, seed=12)
        ups = 0
        prev = sim.get_price("AAPL")
        for _ in range(400):
            sim.step()
            cur = sim.get_price("AAPL")
            ups += cur > prev
            prev = cur
        assert 150 < ups < 250


class TestCorrelationMatrix:
    def test_pairwise_rules(self):
        pc = GBMSimulator._pairwise_correlation
        assert pc("AAPL", "MSFT") == 0.6
        assert pc("JPM", "V") == 0.5
        assert pc("AAPL", "JPM") == 0.3
        assert pc("TSLA", "AAPL") == 0.3
        assert pc("PYPL", "AMD") == 0.3

    def test_full_default_set_factorises(self):
        sim = GBMSimulator(list(SEED_PRICES))
        assert sim._cholesky is not None
        assert sim._cholesky.shape == (10, 10)

    def test_single_ticker_has_no_matrix(self):
        assert GBMSimulator(["AAPL"])._cholesky is None

    def test_many_unknown_tickers_factorise(self):
        sim = GBMSimulator(list(SEED_PRICES) + [f"X{i}" for i in range(30)])
        assert sim._cholesky.shape == (40, 40)

    def test_non_positive_definite_falls_back_to_independent(self, monkeypatch):
        monkeypatch.setattr(GBMSimulator, "_pairwise_correlation", staticmethod(lambda a, b: -0.9))
        sim = GBMSimulator(["A", "B", "C"])
        assert sim._cholesky is None
        assert set(sim.step()) == {"A", "B", "C"}


class TestDynamicTickers:
    def test_add_ticker(self):
        sim = GBMSimulator(["AAPL"])
        sim.add_ticker("MSFT")
        assert sim.get_tickers() == ["AAPL", "MSFT"]
        assert sim._cholesky.shape == (2, 2)
        assert set(sim.step()) == {"AAPL", "MSFT"}

    def test_add_is_idempotent(self):
        sim = GBMSimulator(["AAPL"])
        sim.add_ticker("AAPL")
        assert sim.get_tickers() == ["AAPL"]

    def test_duplicate_constructor_tickers(self):
        assert GBMSimulator(["AAPL", "AAPL"]).get_tickers() == ["AAPL"]

    def test_remove_ticker(self):
        sim = GBMSimulator(["AAPL", "MSFT", "JPM"])
        sim.remove_ticker("MSFT")
        assert sim.get_tickers() == ["AAPL", "JPM"]
        assert sim.get_price("MSFT") is None
        assert sim._cholesky.shape == (2, 2)

    def test_remove_unknown_is_noop(self):
        sim = GBMSimulator(["AAPL"])
        sim.remove_ticker("NOPE")
        assert sim.get_tickers() == ["AAPL"]

    def test_remove_to_one_drops_matrix(self):
        sim = GBMSimulator(["AAPL", "MSFT"])
        sim.remove_ticker("MSFT")
        assert sim._cholesky is None

    def test_get_tickers_returns_copy(self):
        sim = GBMSimulator(["AAPL"])
        sim.get_tickers().append("X")
        assert sim.get_tickers() == ["AAPL"]
