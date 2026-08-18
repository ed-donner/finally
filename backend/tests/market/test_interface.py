"""Tests for the MarketDataSource abstract interface contract."""

import pytest

from app.market.interface import MarketDataSource
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


class TestMarketDataSourceABC:
    """MarketDataSource is an ABC — it must not be directly instantiable."""

    def test_cannot_instantiate_abstract_class(self):
        with pytest.raises(TypeError):
            MarketDataSource()

    def test_incomplete_implementation_cannot_be_instantiated(self):
        """A subclass missing an abstract method must fail to instantiate."""

        class IncompleteSource(MarketDataSource):
            async def start(self, tickers):
                pass

            async def stop(self):
                pass

            async def add_ticker(self, ticker):
                pass

            # remove_ticker and get_tickers intentionally omitted

        with pytest.raises(TypeError):
            IncompleteSource()


class TestConformance:
    """Both concrete implementations must satisfy the full interface contract."""

    @pytest.mark.parametrize(
        "cls,kwargs",
        [
            (SimulatorDataSource, {"price_cache": None}),
            (MassiveDataSource, {"api_key": "test-key", "price_cache": None}),
        ],
    )
    def test_is_instance_of_market_data_source(self, cls, kwargs):
        from app.market.cache import PriceCache

        kwargs = {**kwargs, "price_cache": PriceCache()}
        instance = cls(**kwargs)
        assert isinstance(instance, MarketDataSource)

    @pytest.mark.parametrize("cls", [SimulatorDataSource, MassiveDataSource])
    def test_implements_all_abstract_methods(self, cls):
        for method_name in MarketDataSource.__abstractmethods__:
            assert hasattr(cls, method_name)
            assert callable(getattr(cls, method_name))
