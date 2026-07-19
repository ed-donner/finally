"""Fixtures for service-layer tests."""

import pytest

from app import runtime
from app.db import init_db
from app.db.connection import _reset_for_tests
from tests.support import FakeSource, seeded_cache


@pytest.fixture
def env(tmp_path):
    """Fresh temp DB + a deterministic price cache + runtime registered.

    Yields the PriceCache so tests can adjust prices.
    """
    _reset_for_tests()
    init_db(str(tmp_path / "test.db"))
    cache = seeded_cache()
    source = FakeSource(cache)
    runtime.set_runtime(cache, source)
    yield cache
    runtime.reset_runtime()
    _reset_for_tests()
