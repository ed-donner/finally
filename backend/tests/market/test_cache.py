"""Tests for PriceCache."""

import threading

from app.market.cache import PriceCache


class TestUpdate:
    def test_first_update_is_flat(self):
        cache = PriceCache()
        u = cache.update("AAPL", 190.0)
        assert u.previous_price == 190.0
        assert u.direction == "flat"

    def test_previous_price_tracks_last_update(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        u = cache.update("AAPL", 191.0)
        assert u.previous_price == 190.0
        assert u.direction == "up"

    def test_rounds_to_cents(self):
        cache = PriceCache()
        u = cache.update("AAPL", 190.12345, reference_price=180.009)
        assert u.price == 190.12
        assert u.reference_price == 180.01

    def test_explicit_timestamp(self):
        assert PriceCache().update("AAPL", 1.0, timestamp=1234.5).timestamp == 1234.5

    def test_default_timestamp(self):
        assert PriceCache().update("AAPL", 1.0).timestamp > 1_600_000_000

    def test_returns_stored_update(self):
        cache = PriceCache()
        u = cache.update("AAPL", 190.0)
        assert cache.get("AAPL") is u


class TestReferencePrice:
    def test_carried_forward_when_omitted(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0, reference_price=185.0)
        assert cache.update("AAPL", 191.0).reference_price == 185.0

    def test_replaced_when_given(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0, reference_price=185.0)
        assert cache.update("AAPL", 191.0, reference_price=188.0).reference_price == 188.0

    def test_cleared_with_none(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0, reference_price=185.0)
        assert cache.update("AAPL", 191.0, reference_price=None).reference_price is None

    def test_absent_by_default(self):
        assert PriceCache().update("AAPL", 190.0).reference_price is None


class TestReads:
    def test_get_unknown(self):
        assert PriceCache().get("NOPE") is None

    def test_get_price(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        assert cache.get_price("AAPL") == 190.0
        assert cache.get_price("NOPE") is None

    def test_get_all_is_a_copy(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        snapshot = cache.get_all()
        cache.update("MSFT", 420.0)
        assert set(snapshot) == {"AAPL"}
        assert set(cache.get_all()) == {"AAPL", "MSFT"}

    def test_len_and_contains(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        assert len(cache) == 1
        assert "AAPL" in cache
        assert "MSFT" not in cache


class TestRemoveAndVersion:
    def test_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.remove("AAPL")
        assert cache.get("AAPL") is None

    def test_remove_unknown_is_noop(self):
        cache = PriceCache()
        cache.remove("NOPE")
        assert cache.version == 0

    def test_version_bumps_on_update(self):
        cache = PriceCache()
        assert cache.version == 0
        cache.update("AAPL", 190.0)
        cache.update("AAPL", 191.0)
        assert cache.version == 2

    def test_version_bumps_on_remove(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        v = cache.version
        cache.remove("AAPL")
        assert cache.version == v + 1


class TestThreadSafety:
    def test_concurrent_writers(self):
        cache = PriceCache()
        n_threads, n_updates = 8, 500

        def writer(i: int) -> None:
            for k in range(n_updates):
                cache.update(f"T{i}", 100.0 + k)
                cache.get_all()

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert cache.version == n_threads * n_updates
        assert len(cache) == n_threads
        assert all(cache.get_price(f"T{i}") == 100.0 + n_updates - 1 for i in range(n_threads))
