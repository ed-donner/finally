import pytest

from market.gbm import SimEngine


def test_determinism():
    a = SimEngine(seed=42).step(["AAPL", "MSFT"])
    b = SimEngine(seed=42).step(["AAPL", "MSFT"])
    assert a == b


def test_prices_stay_positive_over_many_steps():
    eng = SimEngine(seed=1)
    for _ in range(10_000):
        for price in eng.step(["TSLA", "NVDA"]).values():
            assert price > 0


def test_lazy_seeding_of_unknown_ticker():
    eng = SimEngine(seed=7)
    p1 = eng.step(["FOO"])["FOO"]
    p2 = eng.step(["FOO"])["FOO"]
    assert p1 > 0 and p2 > 0            # priced, and continues from p1


def test_history_last_close_matches_live_price():
    eng = SimEngine(seed=3)
    eng.step(["AAPL"])                   # establish a live price
    bars = eng.history("AAPL", days=30, end_ms=1_700_000_000_000)
    assert len(bars) == 30
    assert bars[-1].c == pytest.approx(eng._prices["AAPL"], rel=1e-9)
    assert all(b.h >= b.o and b.h >= b.c for b in bars)   # OHLC sanity
    assert all(b.low <= b.o and b.low <= b.c for b in bars)


def test_history_bar_timestamps_are_daily_and_ordered():
    eng = SimEngine(seed=11)
    eng.step(["AAPL"])
    bars = eng.history("AAPL", days=10, end_ms=1_700_000_000_000)
    assert bars[-1].t == 1_700_000_000_000
    diffs = [b2.t - b1.t for b1, b2 in zip(bars, bars[1:])]
    assert all(d == 86_400_000 for d in diffs)


def test_history_deterministic_per_ticker():
    eng = SimEngine(seed=3)
    eng.step(["AAPL"])
    a = eng.history("AAPL", days=15, end_ms=1_700_000_000_000)
    b = eng.history("AAPL", days=15, end_ms=1_700_000_000_000)
    assert a == b


def test_same_sector_moves_correlate():
    eng = SimEngine(seed=99)
    same = cross = 0
    prev = eng.step(["AAPL", "MSFT", "JPM"])
    for _ in range(500):
        nxt = eng.step(["AAPL", "MSFT", "JPM"])
        d = {k: nxt[k] - prev[k] for k in nxt}
        same += (d["AAPL"] > 0) == (d["MSFT"] > 0)       # both tech
        cross += (d["AAPL"] > 0) == (d["JPM"] > 0)        # tech vs financial
        prev = nxt
    assert same > cross                                   # tech co-moves more


def test_price_state_persists_across_steps():
    eng = SimEngine(seed=21)
    eng.step(["AAPL"])
    second = eng.step(["AAPL"])["AAPL"]
    assert eng._prices["AAPL"] == second
