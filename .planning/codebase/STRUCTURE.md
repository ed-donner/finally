# Codebase Structure

**Analysis Date:** 2026-02-11

## Directory Layout

```
finally/
├── backend/                  # FastAPI Python project (uv-managed)
│   ├── app/                  # Main application package
│   │   ├── __init__.py       # Package marker
│   │   ├── market/           # Market data subsystem (complete)
│   │   │   ├── __init__.py   # Public API exports
│   │   │   ├── interface.py  # MarketDataSource abstract base class
│   │   │   ├── cache.py      # PriceCache thread-safe store
│   │   │   ├── models.py     # PriceUpdate immutable dataclass
│   │   │   ├── simulator.py  # GBMSimulator + SimulatorDataSource
│   │   │   ├── massive_client.py # MassiveDataSource (REST API poller)
│   │   │   ├── factory.py    # create_market_data_source() selector
│   │   │   ├── seed_prices.py # Seed prices, correlations, params
│   │   │   └── stream.py     # SSE endpoint factory
│   │   ├── db/               # Database (skeleton, to be implemented)
│   │   │   ├── __init__.py   # Package marker
│   │   │   ├── schema.sql    # (to be created) SQLite schema
│   │   │   └── init.py       # (to be created) Lazy init + seed logic
│   │   ├── routes/           # API route handlers (skeleton, to be implemented)
│   │   │   ├── __init__.py   # Package marker
│   │   │   ├── portfolio.py  # (to be created) /api/portfolio/* endpoints
│   │   │   ├── watchlist.py  # (to be created) /api/watchlist/* endpoints
│   │   │   ├── chat.py       # (to be created) /api/chat endpoint
│   │   │   └── health.py     # (to be created) /api/health
│   │   └── llm/              # LLM integration (skeleton, to be implemented)
│   │       ├── __init__.py   # Package marker
│   │       ├── client.py     # (to be created) LiteLLM setup, OpenRouter calls
│   │       └── schema.py     # (to be created) Structured output schema definitions
│   ├── tests/                # Pytest unit tests (mirrors app/ structure)
│   │   ├── __init__.py       # Package marker
│   │   ├── conftest.py       # Pytest configuration, fixtures
│   │   └── market/           # Market data tests (complete)
│   │       ├── test_models.py
│   │       ├── test_cache.py
│   │       ├── test_simulator.py
│   │       ├── test_simulator_source.py
│   │       ├── test_massive.py
│   │       └── test_factory.py
│   ├── pyproject.toml        # uv project manifest (dependencies, metadata)
│   ├── uv.lock               # Locked dependency versions
│   ├── market_data_demo.py   # Standalone demo with rich terminal dashboard
│   └── README.md             # Backend-specific documentation
├── frontend/                 # Next.js project (skeleton, to be created)
│   ├── app/                  # (to be created) App directory structure
│   ├── components/           # (to be created) React components
│   ├── package.json          # (to be created) Node dependencies
│   ├── next.config.js        # (to be created) Next.js config (static export)
│   ├── tsconfig.json         # (to be created) TypeScript config
│   └── tailwind.config.js    # (to be created) Tailwind CSS config
├── test/                     # Playwright E2E tests
│   ├── playwright.config.ts  # Playwright configuration
│   ├── tests/                # (to be created) Test files
│   └── docker-compose.test.yml # (to be created) Test infrastructure
├── db/                       # SQLite volume mount point (runtime)
│   └── .gitkeep              # Directory exists in git; finally.db is gitignored
├── scripts/                  # Start/stop helper scripts
│   ├── start_mac.sh          # (to be created) macOS/Linux startup
│   └── stop_mac.sh           # (to be created) macOS/Linux shutdown
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # Complete project specification
│   └── MARKET_DATA_SUMMARY.md # Market data implementation summary
├── .planning/                # Codebase reference documents (for orchestrator)
│   └── codebase/             # (This directory)
│       ├── ARCHITECTURE.md   # Architecture patterns and layers
│       └── STRUCTURE.md      # This file
├── Dockerfile                # (to be created) Multi-stage build
├── docker-compose.yml        # (to be created) Optional convenience wrapper
├── .env                      # (gitignored) Environment variables
├── .env.example              # (gitignored) Template for .env
├── .gitignore                # Git ignore patterns
├── CLAUDE.md                 # Project-specific developer instructions
└── README.md                 # Project overview (user-facing)
```

## Directory Purposes

**backend/app/market:**
- Purpose: Market data acquisition, caching, streaming. This is the only complete subsystem.
- Contains: Interface, implementations (simulator and Massive), cache, models, SSE endpoint
- Key files: `interface.py` (contract), `cache.py` (thread-safe store), `simulator.py` (price generation), `stream.py` (SSE)

**backend/app/db:**
- Purpose: Database initialization, schema, migrations, seed data
- Contains: (To be created) SQLite schema SQL, lazy init on first request, seed functions
- Key files: (To be created) `schema.sql`, `init.py`

**backend/app/routes:**
- Purpose: FastAPI route handlers for all API endpoints
- Contains: (To be created) Portfolio CRUD, watchlist management, chat, health check
- Key files: (To be created) `portfolio.py`, `watchlist.py`, `chat.py`, `health.py`

**backend/app/llm:**
- Purpose: LLM integration via LiteLLM → OpenRouter/Cerebras
- Contains: (To be created) Client initialization, structured output schema, message construction
- Key files: (To be created) `client.py`, `schema.py`

**backend/tests:**
- Purpose: Unit tests using pytest, async test fixtures
- Contains: Test modules mirroring app structure, conftest with fixtures
- Key files: `conftest.py` (test config), `market/` (complete market data tests)

**frontend:**
- Purpose: Next.js TypeScript static export served by FastAPI
- Contains: (To be created) React components, pages, styling
- Key files: (To be created) `next.config.js` (static export mode), `components/`, `app/`

**test:**
- Purpose: Playwright end-to-end tests
- Contains: (To be created) Test specs, docker-compose for test environment
- Key files: (To be created) `tests/`, `docker-compose.test.yml`

**planning:**
- Purpose: Project-wide specification and documentation for agents
- Contains: PLAN.md (complete spec), market data summary, archive of superseded docs
- Key files: `PLAN.md` (read by all agents), `MARKET_DATA_SUMMARY.md`

**.planning/codebase:**
- Purpose: Codebase reference documents generated by mapping agents (this directory)
- Contains: ARCHITECTURE.md, STRUCTURE.md, CONVENTIONS.md, TESTING.md, STACK.md, INTEGRATIONS.md, CONCERNS.md
- Used by: `/gsd:plan-phase` and `/gsd:execute-phase` commands to contextualize code generation

## Key File Locations

**Entry Points:**
- `backend/main.py`: (To be created) FastAPI app initialization, middleware setup, route registration
- `backend/app/__init__.py`: Currently empty, will hold app initialization code
- `backend/market_data_demo.py`: Standalone demo script (runnable: `uv run market_data_demo.py`)

**Configuration:**
- `backend/pyproject.toml`: Project manifest, dependencies, test/lint config
- `Dockerfile`: (To be created) Multi-stage build: Node stage (frontend) → Python stage (backend + static files)
- `.env`: Environment variables (gitignored, contains OPENROUTER_API_KEY, optional MASSIVE_API_KEY)

**Core Logic:**
- `backend/app/market/cache.py`: Thread-safe price cache, version counter
- `backend/app/market/simulator.py`: GBM price generation, SimulatorDataSource background loop
- `backend/app/market/stream.py`: SSE endpoint generator
- `backend/app/market/interface.py`: MarketDataSource abstract contract

**Testing:**
- `backend/tests/conftest.py`: pytest config, fixtures
- `backend/tests/market/`: Complete test suite for market data layer
- `test/playwright.config.ts`: (To be created) Playwright config

## Naming Conventions

**Files:**
- Module files: `lowercase_with_underscores.py` (e.g., `price_cache.py`, `simulator.py`)
- Test files: `test_<module>.py` (e.g., `test_cache.py`, `test_simulator.py`)
- Config files: `<name>.config.js`, `<name>.config.ts`, `<name>rc` (e.g., `tailwind.config.js`)
- Route handlers: `<resource>.py` (e.g., `portfolio.py`, `watchlist.py`, `chat.py`)

**Directories:**
- Package/module dirs: `lowercase` (e.g., `market`, `routes`, `llm`)
- Feature dirs: `lowercase` (e.g., `components`, `pages`)
- Test mirrors: `tests/` mirrors `app/` structure (e.g., `tests/market/` for `app/market/`)

**Python Classes:**
- Data models: `PascalCase` (e.g., `PriceUpdate`, `PriceCache`)
- Abstract bases: `PascalCase` + "Source"/"Interface" (e.g., `MarketDataSource`)
- Implementations: `<Name>DataSource` (e.g., `SimulatorDataSource`, `MassiveDataSource`)
- Test classes: `Test<Subject>` (e.g., `TestPriceCache`, `TestGBMSimulator`)

**Functions:**
- Public functions: `snake_case` (e.g., `create_market_data_source()`, `update()`)
- Private methods: `_snake_case` (e.g., `_run_loop()`, `_add_ticker_internal()`)
- Factory functions: `create_<thing>` (e.g., `create_stream_router()`)

## Where to Add New Code

**New Feature (e.g., portfolio endpoints):**
- Primary code: `backend/app/routes/portfolio.py`
- Tests: `backend/tests/routes/test_portfolio.py`
- Database models: `backend/app/db/init.py`
- Database schema: `backend/app/db/schema.sql`

**New Component (React):**
- Implementation: `frontend/components/<Component>.tsx`
- Tests: `frontend/__tests__/components/<Component>.test.tsx`
- Styles: Inline Tailwind classes in component file

**New Utility or Helper:**
- Shared backend helpers: `backend/app/utils.py` (or submodule `backend/app/utils/<module>.py`)
- Shared frontend helpers: `frontend/lib/<helper>.ts` (or `frontend/lib/<category>/<helper>.ts`)

**New Database Table:**
- Add schema to: `backend/app/db/schema.sql`
- Add ORM model to: `backend/app/db/init.py` (if using ORM, otherwise just raw SQL)
- Add seed function to: `backend/app/db/init.py`

**New LLM-related Code:**
- Schema definitions: `backend/app/llm/schema.py`
- Client methods: `backend/app/llm/client.py`
- Test fixtures: `backend/tests/llm/conftest.py`

## Special Directories

**backend/.venv:**
- Purpose: Virtual environment (created by `uv sync`)
- Generated: Yes (by uv)
- Committed: No (in .gitignore)

**backend/__pycache__:**
- Purpose: Python bytecode cache
- Generated: Yes (by Python)
- Committed: No (in .gitignore)

**db/:**
- Purpose: Runtime SQLite database volume mount point
- Generated: Yes (by backend on first request)
- Committed: No (`db/finally.db` is gitignored, only `.gitkeep` committed)

**frontend/.next:**
- Purpose: Next.js build cache and build output (before static export)
- Generated: Yes (by `npm run build`)
- Committed: No (in .gitignore)

**node_modules/** (both frontend/ and test/):
- Purpose: npm dependencies
- Generated: Yes (by `npm install`)
- Committed: No (in .gitignore)

**planning/archive/:**
- Purpose: Superseded planning documents (for reference/history)
- Generated: No (created by agents)
- Committed: Yes

## Import Patterns

**Backend market data (the only complete layer):**
```python
# From within market package
from .cache import PriceCache
from .interface import MarketDataSource
from .models import PriceUpdate

# From outside market package
from app.market import PriceCache, PriceUpdate, create_market_data_source
```

**Future route imports (pattern to follow):**
```python
# In backend/main.py or route registration:
from app.routes import create_portfolio_router, create_watchlist_router, create_chat_router
from app.llm import create_llm_client
from app.db import initialize_database
```

**Test imports (follow structure):**
```python
# Test a module
from app.market.cache import PriceCache

# Use fixtures
def test_something(event_loop_policy):  # from conftest.py
    ...
```

---

*Structure analysis: 2026-02-11*
