"""Unit tests for MassiveClient."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
import httpx

from app.market.cache import PriceCache
from app.market.massive_client import MassiveClient
from app.market.models import DailyBar, PriceUpdate


@pytest.fixture
def cache():
    return PriceCache()


@pytest.fixture
def client(cache):
    return MassiveClient(api_key="test-key", cache=cache, poll_interval=0.1)


# ---------------------------------------------------------------------------
# Snapshot response payload factory
# ---------------------------------------------------------------------------

def make_snapshot_response(tickers: list[dict]) -> dict:
    return {"status": "OK", "count": len(tickers), "tickers": tickers}


def make_ticker_entry(
    symbol: str,
    price: float,
    prev_close: float,
    updated_ns: int = 1_700_000_000_000_000_000,
) -> dict:
    return {
        "ticker": symbol,
        "updated": updated_ns,
        "day": {"o": price - 1, "h": price + 1, "l": price - 2, "c": price, "v": 1_000_000},
        "prevDay": {"c": prev_close},
        "lastTrade": {"p": price, "s": 100, "t": updated_ns},
        "lastQuote": {"P": price + 0.01, "p": price - 0.01},
    }


# ---------------------------------------------------------------------------
# _parse_snapshots
# ---------------------------------------------------------------------------

class TestParseSnapshots:
    def test_parses_price_from_last_trade(self, client):
        data = make_snapshot_response([make_ticker_entry("AAPL", 190.0, 189.0)])
        updates = client._parse_snapshots(data)
        assert len(updates) == 1
        assert updates[0].ticker == "AAPL"
        assert updates[0].price == 190.0

    def test_falls_back_to_day_close_when_no_last_trade(self, client):
        entry = {
            "ticker": "MSFT",
            "updated": 1_700_000_000_000_000_000,
            "day": {"c": 415.0},
            "prevDay": {"c": 412.0},
            # no lastTrade key
        }
        data = make_snapshot_response([entry])
        updates = client._parse_snapshots(data)
        assert updates[0].price == 415.0

    def test_computes_change_correctly(self, client):
        data = make_snapshot_response([make_ticker_entry("AAPL", 191.0, 190.0)])
        updates = client._parse_snapshots(data)
        u = updates[0]
        assert abs(u.change - 1.0) < 1e-9
        assert abs(u.change_pct - (1.0 / 190.0 * 100)) < 1e-6

    def test_parses_timestamp_from_nanoseconds(self, client):
        ns = 1_700_000_000_500_000_000  # ~2023-11-14
        data = make_snapshot_response([make_ticker_entry("AAPL", 190.0, 189.0, updated_ns=ns)])
        updates = client._parse_snapshots(data)
        ts = updates[0].timestamp
        assert isinstance(ts, datetime)
        assert ts.tzinfo is not None

    def test_skips_malformed_entry_and_continues(self, client):
        entries = [
            {"ticker": "BAD"},  # missing day, prevDay
            make_ticker_entry("MSFT", 415.0, 412.0),
        ]
        data = make_snapshot_response(entries)
        updates = client._parse_snapshots(data)
        assert len(updates) == 1
        assert updates[0].ticker == "MSFT"

    def test_returns_empty_list_for_empty_tickers(self, client):
        data = {"status": "OK", "count": 0, "tickers": []}
        assert client._parse_snapshots(data) == []

    def test_parses_multiple_tickers(self, client):
        entries = [
            make_ticker_entry("AAPL", 190.0, 189.0),
            make_ticker_entry("TSLA", 250.0, 248.0),
            make_ticker_entry("NVDA", 875.0, 870.0),
        ]
        data = make_snapshot_response(entries)
        updates = client._parse_snapshots(data)
        assert len(updates) == 3
        symbols = {u.ticker for u in updates}
        assert symbols == {"AAPL", "TSLA", "NVDA"}

    def test_change_pct_zero_when_prev_price_zero(self, client):
        entry = make_ticker_entry("AAPL", 190.0, 0.0)
        entry["prevDay"]["c"] = 0.0
        data = make_snapshot_response([entry])
        updates = client._parse_snapshots(data)
        assert updates[0].change_pct == 0.0


# ---------------------------------------------------------------------------
# add_ticker / remove_ticker
# ---------------------------------------------------------------------------

class TestTickerManagement:
    def test_add_ticker_uppercases(self, client):
        client.add_ticker("aapl")
        assert "AAPL" in client._tickers

    def test_remove_ticker_uppercases(self, client):
        client._tickers.add("AAPL")
        client.remove_ticker("aapl")
        assert "AAPL" not in client._tickers

    def test_remove_nonexistent_ticker_is_safe(self, client):
        client.remove_ticker("ZZZZ")  # should not raise

    def test_add_ticker_does_not_duplicate(self, client):
        client.add_ticker("AAPL")
        client.add_ticker("AAPL")
        assert list(client._tickers).count("AAPL") == 1


# ---------------------------------------------------------------------------
# _fetch_snapshots — HTTP error handling (using mocked httpx)
# ---------------------------------------------------------------------------

class TestFetchSnapshotsErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_empty_on_rate_limit(self, client):
        mock_response = MagicMock()
        mock_response.status_code = 429
        http = AsyncMock()
        http.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._fetch_snapshots(http)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_auth_error(self, client):
        mock_response = MagicMock()
        mock_response.status_code = 403
        http = AsyncMock()
        http.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("403", request=MagicMock(), response=mock_response)
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._fetch_snapshots(http)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_server_error(self, client):
        mock_response = MagicMock()
        mock_response.status_code = 500
        http = AsyncMock()
        http.get = AsyncMock(
            side_effect=httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._fetch_snapshots(http)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_on_network_error(self, client):
        http = AsyncMock()
        http.get = AsyncMock(
            side_effect=httpx.ConnectError("connection refused")
        )
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await client._fetch_snapshots(http)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_updates_on_success(self, client):
        payload = make_snapshot_response([make_ticker_entry("AAPL", 190.0, 189.0)])
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status = MagicMock()
        http = AsyncMock()
        http.get = AsyncMock(return_value=mock_response)
        result = await client._fetch_snapshots(http)
        assert len(result) == 1
        assert result[0].ticker == "AAPL"


# ---------------------------------------------------------------------------
# get_daily_bars — HTTP interactions
# ---------------------------------------------------------------------------

class TestGetDailyBars:
    @pytest.mark.asyncio
    async def test_returns_bars_on_success(self, client):
        payload = {
            "ticker": "AAPL",
            "status": "OK",
            "results": [
                {"o": 189.0, "h": 191.0, "l": 188.0, "c": 190.0, "v": 50_000_000, "vw": 190.1, "t": 1_700_006_400_000},
                {"o": 190.5, "h": 192.0, "l": 189.5, "c": 191.5, "v": 48_000_000, "vw": 191.0, "t": 1_700_092_800_000},
            ],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_ctx

            bars = await client.get_daily_bars("AAPL", "2023-11-01", "2023-11-30")

        assert len(bars) == 2
        assert all(isinstance(b, DailyBar) for b in bars)
        assert bars[0].ticker == "AAPL"
        assert bars[0].close == 190.0
        assert bars[0].volume == 50_000_000
        assert bars[0].vwap == 190.1

    @pytest.mark.asyncio
    async def test_returns_empty_on_http_error(self, client):
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.get = AsyncMock(side_effect=httpx.ConnectError("timeout"))
            mock_client_class.return_value = mock_ctx

            bars = await client.get_daily_bars("AAPL", "2023-01-01", "2023-12-31")

        assert bars == []

    @pytest.mark.asyncio
    async def test_bar_date_format(self, client):
        # t=1700006400000 ms → 2023-11-14 in UTC
        payload = {
            "ticker": "AAPL",
            "status": "OK",
            "results": [
                {"o": 189.0, "h": 191.0, "l": 188.0, "c": 190.0, "v": 50_000_000, "t": 1_700_006_400_000},
            ],
        }
        mock_response = MagicMock()
        mock_response.json.return_value = payload
        mock_response.raise_for_status = MagicMock()

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=mock_ctx)
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_ctx.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_ctx

            bars = await client.get_daily_bars("AAPL", "2023-11-14", "2023-11-14")

        assert len(bars[0].date) == 10  # YYYY-MM-DD
        assert bars[0].date.count("-") == 2


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

class TestLifecycle:
    @pytest.mark.asyncio
    async def test_start_sets_tickers(self, client):
        await client.start(["AAPL", "MSFT"])
        assert client._tickers == {"AAPL", "MSFT"}
        await client.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self, client):
        await client.start(["AAPL"])
        task = client._task
        await client.stop()
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self, client):
        await client.stop()  # should not raise


# ---------------------------------------------------------------------------
# Factory integration
# ---------------------------------------------------------------------------

class TestFactory:
    def test_factory_returns_simulator_without_key(self, cache):
        import os
        from app.market.factory import create_market_data_source
        from app.market.simulator import MarketSimulator
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("MASSIVE_API_KEY", None)
            source = create_market_data_source(cache)
        assert isinstance(source, MarketSimulator)

    def test_factory_returns_massive_client_with_key(self, cache):
        import os
        from app.market.factory import create_market_data_source
        from app.market.massive_client import MassiveClient
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key"}):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveClient)

    def test_factory_respects_poll_interval_env(self, cache):
        import os
        from app.market.factory import create_market_data_source
        from app.market.massive_client import MassiveClient
        with patch.dict(os.environ, {"MASSIVE_API_KEY": "test-key", "MASSIVE_POLL_INTERVAL": "5.0"}):
            source = create_market_data_source(cache)
        assert isinstance(source, MassiveClient)
        assert source._poll_interval == 5.0
