# Testing Patterns

**Analysis Date:** 2026-02-11

## Test Framework

**Runner:**
- pytest 8.3.0+
- Config: `pyproject.toml` under `[tool.pytest.ini_options]`

**Assertion Library:**
- pytest built-in assertions; no external assertion library needed

**Run Commands:**
```bash
# All tests
uv run pytest

# Verbose output
uv run pytest -v

# Watch mode (with pytest-watch or manual re-run)
uv run pytest --tb=short

# Coverage report
uv run pytest --cov=app --cov-report=html

# Specific test file
uv run pytest tests/market/test_simulator.py

# Specific test class
uv run pytest tests/market/test_simulator.py::TestGBMSimulator

# Specific test function
uv run pytest tests/market/test_simulator.py::TestGBMSimulator::test_step_returns_all_tickers
```

**Async Test Configuration:**
- `pytest-asyncio>=0.24.0` configured with `asyncio_mode = "auto"`
- `asyncio_default_fixture_loop_scope = "function"` — each async test gets its own event loop
- `event_loop_policy` fixture in `conftest.py` ensures clean event loop state

## Test File Organization

**Location:**
- Tests co-located with source in parallel directory structure
- `backend/app/market/` source → `backend/tests/market/` tests
- Each source module has corresponding test module: `cache.py` → `test_cache.py`

**Naming:**
- Test files: `test_*.py`
- Test classes: `Test*` (e.g., `TestGBMSimulator`, `TestPriceCache`, `TestFactory`)
- Test functions: `test_*` (e.g., `test_step_returns_all_tickers`, `test_prices_are_positive`)

**Structure:**
```
backend/tests/
├── __init__.py               # Empty marker file
├── conftest.py               # Shared fixtures and config
├── market/
│   ├── __init__.py
│   ├── test_cache.py         # PriceCache tests
│   ├── test_models.py        # PriceUpdate tests
│   ├── test_simulator.py     # GBMSimulator tests
│   ├── test_simulator_source.py  # SimulatorDataSource integration tests
│   ├── test_factory.py       # Factory selection logic tests
│   └── test_massive.py       # MassiveDataSource tests (mocked)
```

## Test Structure

**Suite Organization:**

```python
@pytest.mark.asyncio  # For async tests
class TestSimulatorDataSource:
    """Integration tests for the SimulatorDataSource."""

    async def test_start_populates_cache(self):
        """Test that start() immediately populates the cache."""
        # Arrange
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)

        # Act
        await source.start(["AAPL", "GOOGL"])

        # Assert
        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None

        # Cleanup
        await source.stop()
```

**Patterns:**

- **Setup**: Create fixtures/mocks at start of test method (no setup fixtures used; inline creation preferred)
- **Teardown**: Explicit cleanup at end (e.g., `await source.stop()`)
- **Naming**: Test names describe what is being tested: `test_start_populates_cache` → what method, what result
- **Docstrings**: Every test has single-line docstring explaining the test scenario
- **Arrange-Act-Assert**: Clear separation of phases (sometimes with comments, sometimes implicit)

**Non-Async Tests (synchronous):**
```python
class TestGBMSimulator:
    """Unit tests for the GBM price simulator."""

    def test_step_returns_all_tickers(self):
        """Test that step() returns prices for all tickers."""
        sim = GBMSimulator(tickers=["AAPL", "GOOGL"])
        result = sim.step()
        assert set(result.keys()) == {"AAPL", "GOOGL"}
```

## Mocking

**Framework:** `unittest.mock` (stdlib)

**Patterns:**

- **Creating mocks**: `MagicMock()` for simple attribute stubs
- **Patching environment**: `patch.dict(os.environ, {...}, clear=True)` for environment variables
- **Patching methods**: `patch.object(source, "_fetch_snapshots", return_value=[...])` for method replacement
- **Partial mocks**: Set specific attributes: `source._client = MagicMock()` then set up return values

**Example from `test_factory.py`:**
```python
def test_creates_simulator_when_no_api_key(self):
    cache = PriceCache()
    with patch.dict(os.environ, {}, clear=True):
        source = create_market_data_source(cache)
    assert isinstance(source, SimulatorDataSource)
```

**Example from `test_massive.py`:**
```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    """Create a mock Massive snapshot object."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap

async def test_poll_updates_cache(self):
    cache = PriceCache()
    source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)
    source._tickers = ["AAPL", "GOOGL"]
    source._client = MagicMock()

    mock_snapshots = [
        _make_snapshot("AAPL", 190.50, 1707580800000),
        _make_snapshot("GOOGL", 175.25, 1707580800000),
    ]

    with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
        await source._poll_once()

    assert cache.get_price("AAPL") == 190.50
```

**What to Mock:**
- External API calls (Massive client responses)
- Environment variables
- Private methods when testing error paths

**What NOT to Mock:**
- Internal state when it's not the focus (e.g., don't mock PriceCache in SimulatorDataSource tests)
- Standard library classes (use real `asyncio` tasks, real `Lock` objects)
- User-defined classes that are part of the code under test

## Fixtures and Factories

**Test Data:**

No pytest fixtures used; tests create fixtures inline. Example from `test_cache.py`:
```python
def test_update_and_get(self):
    cache = PriceCache()  # Inline fixture
    update = cache.update("AAPL", 190.50)
    assert update.ticker == "AAPL"
```

**Helper Functions:**

Reusable mock builders extracted as module-level functions. Example from `test_massive.py`:
```python
def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    """Create a mock Massive snapshot object."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap
```

**Conftest:**

`tests/conftest.py` contains:
```python
@pytest.fixture
def event_loop_policy():
    """Use the default event loop policy for all async tests."""
    import asyncio
    return asyncio.DefaultEventLoopPolicy()
```

This ensures clean event loop state for each async test.

## Coverage

**Requirements:**
- No explicit target enforced in config
- Coverage tracking enabled via pytest-cov: `[tool.coverage.run]` in `pyproject.toml`
- Excluded from coverage: test files, `__repr__`, `raise NotImplementedError`, `if __name__ == "__main__":`

**View Coverage:**
```bash
uv run pytest --cov=app --cov-report=html
# Opens htmlcov/index.html
```

**Current Coverage:**
- Market data subsystem fully tested (6 test files)
- Models: 100% coverage (PriceUpdate all paths tested)
- Cache: 100% coverage (all operations, version tracking, thread safety)
- Simulator: Comprehensive tests for GBM math, correlation, ticker management
- Data sources: Integration tests for start/stop lifecycle, add/remove operations
- Factory: All three scenarios tested (no key, empty key, whitespace, valid key)

## Test Types

**Unit Tests:**

Scope: Single function or class in isolation

Example from `test_models.py` — `PriceUpdate` dataclass:
```python
class TestPriceUpdate:
    def test_change_calculation(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        assert update.change == 0.50

    def test_direction_up(self):
        update = PriceUpdate(ticker="AAPL", price=191.00, previous_price=190.00, timestamp=1234567890.0)
        assert update.direction == "up"

    def test_immutability(self):
        update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        with pytest.raises(AttributeError):
            update.price = 200.00
```

Example from `test_cache.py` — `PriceCache` operations:
```python
def test_update_and_get(self):
    cache = PriceCache()
    update = cache.update("AAPL", 190.50)
    assert update.ticker == "AAPL"
    assert cache.get("AAPL") == update

def test_direction_up(self):
    cache = PriceCache()
    cache.update("AAPL", 190.00)
    update = cache.update("AAPL", 191.00)
    assert update.direction == "up"
    assert update.change == 1.00

def test_version_increments(self):
    cache = PriceCache()
    v0 = cache.version
    cache.update("AAPL", 190.00)
    assert cache.version == v0 + 1
```

Example from `test_simulator.py` — `GBMSimulator` logic:
```python
def test_prices_are_positive(self):
    """GBM prices can never go negative (exp() is always positive)."""
    sim = GBMSimulator(tickers=["AAPL"])
    for _ in range(10_000):
        prices = sim.step()
        assert prices["AAPL"] > 0

def test_pairwise_correlation_tech_stocks(self):
    corr = GBMSimulator._pairwise_correlation("AAPL", "GOOGL")
    assert corr == 0.6

def test_prices_rounded_to_two_decimals(self):
    sim = GBMSimulator(tickers=["AAPL"])
    result = sim.step()
    price_str = str(result["AAPL"])
    if '.' in price_str:
        decimal_part = price_str.split('.')[1]
        assert len(decimal_part) <= 2
```

**Integration Tests:**

Scope: Multiple components working together (but not end-to-end)

File: `test_simulator_source.py` — Tests SimulatorDataSource lifecycle with actual PriceCache

```python
@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_start_populates_cache(self):
        """Test that start() immediately populates the cache."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL", "GOOGL"])

        assert cache.get("AAPL") is not None
        assert cache.get("GOOGL") is not None
        await source.stop()

    async def test_prices_update_over_time(self):
        """Test that prices are updated periodically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.3)  # Several update cycles

        assert cache.version > initial_version
        await source.stop()

    async def test_add_ticker(self):
        """Test adding a ticker dynamically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["AAPL"])

        await source.add_ticker("TSLA")
        assert "TSLA" in source.get_tickers()
        assert cache.get("TSLA") is not None

        await source.stop()
```

**E2E Tests:**

Not yet implemented in backend. Frontend will have Playwright E2E tests in `test/` directory.

## Common Patterns

**Async Testing:**

Use `@pytest.mark.asyncio` decorator on class or method. Tests are actual async functions:

```python
@pytest.mark.asyncio
class TestSimulatorDataSource:
    async def test_prices_update_over_time(self):
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["AAPL"])

        initial_version = cache.version
        await asyncio.sleep(0.3)  # Wait for updates

        assert cache.version > initial_version
        await source.stop()
```

**Error Testing:**

Use `pytest.raises()` for expected exceptions:

```python
def test_immutability(self):
    """Test that PriceUpdate is immutable."""
    update = PriceUpdate(ticker="AAPL", price=190.50, previous_price=190.00, timestamp=1234567890.0)

    with pytest.raises(AttributeError):
        update.price = 200.00  # Should raise error
```

**Boundary Testing:**

Test edge cases and limits:

```python
def test_change_percent_zero_previous(self):
    """Test percentage change with zero previous price."""
    update = PriceUpdate(ticker="AAPL", price=100.00, previous_price=0.00, timestamp=1234567890.0)
    assert update.change_percent == 0.0  # Returns 0, not infinity

def test_empty_step(self):
    """Test stepping with no tickers."""
    sim = GBMSimulator(tickers=[])
    result = sim.step()
    assert result == {}

def test_remove_nonexistent_is_noop(self):
    """Test that removing a non-existent ticker is a no-op."""
    sim = GBMSimulator(tickers=["AAPL"])
    sim.remove_ticker("NOPE")  # Should not raise
```

**Deterministic Testing:**

Tests use specific seed prices and fixed parameters to ensure deterministic behavior:

```python
def test_initial_prices_match_seeds(self):
    sim = GBMSimulator(tickers=["AAPL"])
    assert sim.get_price("AAPL") == SEED_PRICES["AAPL"]

def test_add_duplicate_is_noop(self):
    sim = GBMSimulator(tickers=["AAPL"])
    sim.add_ticker("AAPL")
    assert len(sim._tickers) == 1
```

## Missing Coverage / Test Gaps

**Areas not yet tested:**
- Portfolio management (trades, positions, P&L calculations) — Not implemented yet
- LLM integration and structured output parsing — Not implemented yet
- Chat message handling and action execution — Not implemented yet
- Database schema and initialization — Not implemented yet
- API endpoints (FastAPI routes beyond SSE) — Not implemented yet
- Watchlist CRUD operations — Not implemented yet
- Frontend components — Will use React Testing Library (not yet implemented)

**Why gaps exist:**
Backend currently only has market data subsystem complete. Portfolio, chat, and API endpoints are pending implementation.

---

*Testing analysis: 2026-02-11*
