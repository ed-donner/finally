# Coding Conventions

**Analysis Date:** 2026-02-11

## Naming Patterns

**Files:**
- Snake_case for all Python files: `cache.py`, `simulator.py`, `seed_prices.py`
- Test files follow convention: `test_*.py` (e.g., `test_cache.py`, `test_simulator.py`)
- Underscore prefix for private/internal functions: `_generate_events()` in `stream.py`, `_rebuild_cholesky()` in `simulator.py`

**Classes:**
- PascalCase for all classes: `PriceCache`, `PriceUpdate`, `MarketDataSource`, `SimulatorDataSource`, `GBMSimulator`
- Abstract base classes explicitly named with ABC: `MarketDataSource(ABC)` in `interface.py`

**Functions:**
- snake_case for all functions and methods: `get_price()`, `add_ticker()`, `remove_ticker()`
- Factory functions explicitly named `create_*`: `create_market_data_source()`, `create_stream_router()`
- Private methods use leading underscore: `_run_loop()`, `_poll_loop()`, `_add_ticker_internal()`, `_pairwise_correlation()`
- Static methods use `@staticmethod` decorator: `_pairwise_correlation()` in `simulator.py`

**Variables:**
- snake_case for local variables and attributes: `price`, `ticker`, `tickers`, `previous_price`, `api_key`
- Module-level constants in UPPER_CASE: `SEED_PRICES`, `DEFAULT_PARAMS`, `TRADING_SECONDS_PER_YEAR`, `INTRA_TECH_CORR`
- Private attributes with leading underscore: `_prices`, `_cache`, `_task`, `_client`, `_cholesky`
- Default tuple values for constants: see `CORRELATION_GROUPS` in `seed_prices.py`

**Types:**
- Full type hints on all function signatures: `def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:`
- Union types use `|` syntax (PEP 604): `float | None` instead of `Optional[float]`
- Generic types fully specified: `dict[str, float]`, `list[str]`, `AsyncGenerator[str, None]`
- Use `from __future__ import annotations` at top of every module for forward compatibility

## Code Style

**Formatting:**
- Tool: Ruff formatter (paired with Ruff linter)
- Line length: 100 characters (configured in `pyproject.toml` as `line-length = 100`)
- No trailing semicolons or unnecessary parentheses

**Linting:**
- Tool: Ruff with strict defaults
- Config in `pyproject.toml` under `[tool.ruff]` and `[tool.ruff.lint]`
- Selected rules: `["E", "F", "I", "N", "W"]` (errors, undefined names, imports, naming, warnings)
- Ignored: `["E501"]` (line too long — handled by formatter)
- Target version: Python 3.12

**Docstring Style:**
- Triple-quoted docstrings for modules, classes, and public functions
- Single-line docstrings for simple functions, immediately followed by implementation
- Multi-line docstrings include description followed by blank line, then additional details
- Example from `cache.py`: Clear, concise description of what the class does and its purpose (writer/reader contract)
- Example from `stream.py`: Function docstring includes what it does, the format of data sent, and special headers

**Comments:**
- Minimal inline comments; code clarity is preferred
- Use docstrings instead of comments for explaining behavior
- Math-heavy code includes brief comments explaining variables: see `GBMSimulator.step()` where GBM formula is documented
- Debug logging preferred over commented code

## Import Organization

**Order:**
1. `from __future__ import annotations` (if used — always used here)
2. Standard library imports: `asyncio`, `json`, `logging`, `math`, `random`, `os`, `time`, `threading`
3. Third-party imports: `numpy`, `fastapi`, `pytest`, `massive`
4. Relative imports: `from .models import PriceUpdate`, `from .cache import PriceCache`

**Examples:**
- `simulator.py`: `from __future__` → stdlib (`asyncio`, `logging`, `math`, `random`) → third-party (`numpy`) → relative (local imports)
- `factory.py`: `from __future__` → stdlib (`logging`, `os`) → relative imports
- `stream.py`: `from __future__` → stdlib → third-party (`fastapi`) → relative imports

**Path Aliases:**
- No path aliases configured; all imports use relative paths within `app/` or full stdlib imports
- Module organization relies on clear package structure: `app.market.models`, `app.market.cache`, etc.

## Error Handling

**Patterns:**
- Silent failures with logging for non-critical errors: `except Exception:` followed by `logger.exception()`
- Specific exception handling for known error cases: `except asyncio.CancelledError:` in shutdown flows
- Tuple exception handling for multiple related exceptions: `except (AttributeError, TypeError) as e:` in `massive_client.py`
- No bare `except:` clauses; always specify exception types
- Errors in background loops (simulator, poller) are logged but do not crash the loop

**Error Handling by Layer:**

- **Cache operations** (`cache.py`): No exceptions raised; graceful returns (`None` for missing values)
- **Data sources** (`simulator.py`, `massive_client.py`): Background tasks catch exceptions and continue; error logged via `logger.exception()`
- **API endpoints** (`stream.py`): Detect client disconnection and exit cleanly; log on disconnect
- **Factory functions** (`factory.py`): Select data source based on config; no validation errors (environment-driven)

## Logging

**Framework:** Python `logging` module via `getattr(logging, '__name__')`

**Pattern:**
- Every module creates logger at top: `logger = logging.getLogger(__name__)`
- Used for lifecycle events (start, stop, add, remove): `logger.info("Simulator started with %d tickers", len(tickers))`
- Used for debug info in hot paths: `logger.debug("Random event on %s: %.1f%% %s", ticker, shock_magnitude * 100, ...)`
- Used for exceptions in background tasks: `logger.exception("Simulator step failed")`
- Used for SSE connection lifecycle: `logger.info("SSE client connected: %s", client_ip)`

**Levels:**
- `info` — Lifecycle events, configuration changes, normal operation milestones
- `debug` — Per-tick events (random shocks), detailed state changes
- `exception` — Background task failures (preserves traceback)

## Comments

**When to Comment:**
- Math-heavy code: GBM formula in `simulator.py` includes full explanation of variables
- Complex algorithms: Cholesky decomposition rebuild includes comment explaining why it's done when tickers change
- Non-obvious data structures: `CORRELATION_GROUPS` includes comment explaining sector groupings
- Correlation definitions include comments explaining rationale: "Tech stocks move together", "TSLA does its own thing"

**When NOT to Comment:**
- Self-documenting code: variable names and function names make intent clear
- Property methods: `@property` makes intent obvious
- Standard library usage: no comments needed for `Lock()`, `asyncio.create_task()`, etc.

## Function Design

**Size:** Short functions with single responsibility
- Most functions 10-30 lines
- Hot-path functions kept tight: `step()` in simulator is 45 lines but handles entire GBM iteration
- Helper functions extracted for clarity: `_pairwise_correlation()` isolated from main logic

**Parameters:**
- Use named parameters for optional arguments: `update_interval: float = 0.5`
- Dataclass instances preferred over multiple scalar params
- Pass objects for dependency injection: `price_cache: PriceCache` passed to all data sources

**Return Values:**
- Explicit `None` returns for side-effect functions: `async def stop(self) -> None:`
- Immutable return types: `to_dict()` returns a new dictionary, not a reference
- Return collections as new instances: `get_all()` returns `dict(self._prices)` (shallow copy)

## Module Design

**Exports:**
- Explicit `__all__` lists in package `__init__.py`: see `app/market/__init__.py`
- Public API documented in module docstring with what's exported
- No private module imports from test code; only public API

**Barrel Files:**
- `app/market/__init__.py` exports: `PriceUpdate`, `PriceCache`, `MarketDataSource`, `create_market_data_source`, `create_stream_router`
- Single import location for consumers: `from app.market import PriceCache, PriceUpdate`
- Reduces coupling; internal module structure can change without affecting consumers

**Module Organization:**
- `models.py` — Data structures only (immutable dataclasses)
- `interface.py` — Abstract contracts (ABC definitions)
- `cache.py` — Thread-safe state management
- `simulator.py` — GBM algorithm + SimulatorDataSource implementation
- `massive_client.py` — Massive API client + MassiveDataSource implementation
- `factory.py` — Selection logic based on configuration
- `stream.py` — FastAPI endpoint and SSE generation
- `seed_prices.py` — Configuration data (constants)

## Concurrency & Threading

**Patterns:**
- Thread-safe operations use `threading.Lock`: see `PriceCache` for all dict access protected by `with self._lock:`
- Async/await for background tasks: `asyncio.create_task()` for long-lived loops
- No blocking operations in async code; use `asyncio.sleep()` instead of `time.sleep()`
- Task cancellation handled explicitly: catch `asyncio.CancelledError` and clean up

**Example from `cache.py`:**
```python
def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
    with self._lock:
        # atomic operations on dict
        self._version += 1
```

**Example from `simulator.py`:**
```python
self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
# ... later
await self._task.cancel()
```

---

*Convention analysis: 2026-02-11*
