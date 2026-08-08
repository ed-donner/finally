from config import Settings
from market import factory
from market.massive import MassiveSource
from market.simulator import SimulatedSource


def _settings(massive_api_key: str = "") -> Settings:
    return Settings(
        massive_api_key=massive_api_key,
        massive_poll_seconds=15.0,
        sim_seed=None,
        sse_push_seconds=0.5,
        openrouter_api_key="",
        llm_mock=False,
    )


def test_no_key_selects_simulator(monkeypatch):
    monkeypatch.setattr(factory, "settings", _settings(massive_api_key=""))
    assert isinstance(factory.make_source(), SimulatedSource)


def test_key_present_selects_massive(monkeypatch):
    monkeypatch.setattr(factory, "settings", _settings(massive_api_key="secret"))
    source = factory.make_source()
    assert isinstance(source, MassiveSource)
    assert source.poll_interval_seconds == 15.0


