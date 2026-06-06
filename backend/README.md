# FinAlly Backend

FastAPI backend for the FinAlly AI Trading Workstation.

## Structure

- `app/` - Application code
  - `market/` - Market data subsystem
    - `models.py` - PriceUpdate dataclass (per-tick + day-open change metrics)
    - `cache.py` - Thread-safe price cache (sole 2-dp rounding layer)
    - `interface.py` - MarketDataSource abstract interface, `normalize_ticker()`, `health()`
    - `simulator.py` - GBM-based market simulator (seedable RNG)
    - `massive_client.py` - Massive/Polygon.io API client (timeouts, health tracking)
    - `factory.py` - Data source factory
    - `stream.py` - SSE streaming endpoint (fresh router per call, keep-alive)
    - `seed_prices.py` - Default ticker prices and parameters

- `tests/` - Unit and integration tests (102 tests, 97% coverage)
  - `market/` - Market data tests

## Running Tests

```bash
# Install dependencies (test/lint tools live in the `dev` extra)
uv sync --extra dev

# Run all tests
uv run --extra dev pytest

# Run with coverage
uv run --extra dev pytest --cov=app --cov-report=html

# Run specific test file
uv run --extra dev pytest tests/market/test_simulator.py

# Run with verbose output
uv run --extra dev pytest -v
```

## Demos

**Terminal dashboard** — Rich TUI (`rich` lives in the optional `demo` extra, kept out of the runtime image):

```bash
uv run --extra demo market_data_demo.py
```

**Browser** — a single-file FastAPI app that serves one HTML page streaming live
prices from the real SSE endpoint (`GET /api/stream/prices`). No extra needed:

```bash
uv run web_demo.py        # then open http://localhost:8000  (set PORT to change)
```

## Environment Variables

- `MASSIVE_API_KEY` - Optional. If set, use real market data from Massive API. If not set, use the built-in simulator.

## Development

```bash
# Install dependencies
uv sync --extra dev

# Run linter
uv run --extra dev ruff check .

# Format code
uv run --extra dev ruff format .
```
