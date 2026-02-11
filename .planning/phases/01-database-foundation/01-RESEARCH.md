# Phase 1: Database Foundation - Research

**Researched:** 2026-02-11
**Domain:** Async SQLite database layer with lazy initialization for FastAPI
**Confidence:** HIGH

## Summary

Phase 1 establishes the async SQLite persistence layer that all subsequent backend phases depend on. The core task is straightforward: create a module at `backend/app/db/` that opens a single aiosqlite connection, creates 6 tables via `CREATE TABLE IF NOT EXISTS`, seeds default data idempotently, and configures WAL mode with busy_timeout for safe concurrent access.

The existing codebase already has the market data subsystem complete at `backend/app/market/` with 73 tests. The database layer follows the same modular pattern: separate files for connection management, schema definition, and seed data, exposed through a clean `__init__.py` public API. No ORM -- raw SQL with parameterized queries, thin async functions returning dicts.

**Primary recommendation:** Use aiosqlite >=0.22.0 with a single shared connection (opened at startup, closed at shutdown). Configure `PRAGMA journal_mode=WAL` and `PRAGMA busy_timeout=5000` on connection open. Use `CREATE TABLE IF NOT EXISTS` for idempotent schema creation. Use `isolation_level=None` to disable Python's implicit transaction management and handle transactions explicitly.

## Standard Stack

### Core

| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| aiosqlite | >=0.22.0 | Async SQLite access | The standard asyncio bridge to stdlib sqlite3. Wraps sqlite3 in a background thread with an async API. v0.22.1 is the latest (released Dec 2025). Passes `**kwargs` through to `sqlite3.connect()`, so all sqlite3 parameters (including `isolation_level`) work. |
| sqlite3 (stdlib) | Python 3.12 built-in | Underlying SQLite engine | Ships with Python. No additional install. SQLite version depends on OS/Python build, but all modern versions support WAL mode (available since SQLite 3.7.0, 2010). |

### Supporting

| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| pytest-asyncio | >=0.24.0 | Async test support | Already installed as dev dependency. Tests for the db layer are async. `asyncio_mode = "auto"` is already configured in pyproject.toml. |
| uuid (stdlib) | Python 3.12 built-in | Generate UUIDs for primary keys | All table primary keys are TEXT UUIDs per the PLAN. Use `str(uuid.uuid4())`. |

### Alternatives Considered

| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| aiosqlite (raw SQL) | SQLAlchemy async + aiosqlite dialect | Massive ORM overhead for 6 simple tables. ORM async sessions add greenlet bridging complexity. Raw SQL is clearer and faster for this scale. |
| aiosqlite (raw SQL) | Tortoise ORM | Adds Django-style ORM complexity for no benefit at this scale. |
| Single shared connection | aiosqlitepool (connection pool) | Connection pooling adds complexity for a single-user app. SQLite is single-writer anyway. Pool only benefits apps serving >5-10 req/sec concurrently. |

**Installation:**
```bash
cd backend
uv add aiosqlite
```

That is the only new dependency. Everything else (uuid, sqlite3, pytest-asyncio) is already available.

## Architecture Patterns

### Recommended Project Structure

```
backend/app/db/
├── __init__.py         # Public API: get_db(), init_db(), close_db()
├── connection.py       # aiosqlite connection management, PRAGMA config
├── schema.py           # CREATE TABLE IF NOT EXISTS for all 6 tables
└── seed.py             # Default user + 10 watchlist tickers (idempotent)
```

```
backend/tests/db/
├── __init__.py
├── test_schema.py      # Tables created, schema correct
├── test_seed.py        # Default data present, idempotent re-seed
├── test_connection.py  # WAL mode, busy_timeout, concurrent access
└── conftest.py         # Shared db fixture (tmp_path for isolated test DBs)
```

### Pattern 1: Single Shared Connection with Lifespan

**What:** Open one aiosqlite connection at app startup, attach to `app.state`, close at shutdown. All route handlers and background tasks use this single connection.

**When to use:** Always for this project. SQLite allows only one writer at a time; a single connection serializes all writes naturally, avoiding lock contention entirely.

**Confidence:** HIGH (verified by prior architecture research, SQLite WAL docs, and aiosqlite docs)

**Example:**
```python
# backend/app/db/connection.py
import aiosqlite

async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open the database, configure pragmas, create tables if missing."""
    db = await aiosqlite.connect(db_path, isolation_level=None)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")
    return db
```

**Why `isolation_level=None`:** Disables Python's implicit transaction management (the legacy `sqlite3` behavior where DML statements auto-open transactions). With `isolation_level=None`, the connection is in true autocommit mode -- each statement commits immediately unless wrapped in explicit `BEGIN`/`COMMIT`. This is simpler to reason about and avoids the "upgrade deadlock" pitfall where two implicit transactions both try to promote from reader to writer.

### Pattern 2: Idempotent Schema Initialization with CREATE TABLE IF NOT EXISTS

**What:** Use `CREATE TABLE IF NOT EXISTS` for all tables. Run the schema SQL on every startup. If tables already exist, the statements are no-ops. No migration tooling needed.

**When to use:** When the schema is static and the app is a capstone/demo project without schema evolution requirements.

**Confidence:** HIGH (standard SQLite pattern)

**Example:**
```python
# backend/app/db/schema.py
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users_profile (
    id TEXT PRIMARY KEY,
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE(user_id, ticker)
);
-- ... remaining 4 tables ...
"""

async def create_tables(db: aiosqlite.Connection) -> None:
    """Create all tables if they don't exist."""
    await db.executescript(SCHEMA_SQL)
```

**Note on executescript:** `executescript` first issues a `COMMIT` if there's a pending transaction, then executes the SQL script. It does NOT wrap the script in a transaction itself. Since we use `isolation_level=None` (autocommit mode), each statement executes and auto-commits individually. This is fine for DDL statements like CREATE TABLE.

### Pattern 3: Idempotent Seed Data

**What:** Seed data uses `INSERT OR IGNORE` (or check-then-insert). Running seed multiple times does not duplicate data or overwrite existing data.

**When to use:** Always. The success criteria explicitly require: "Restarting the backend with an existing database preserves all data without re-seeding."

**Confidence:** HIGH

**Example:**
```python
# backend/app/db/seed.py
async def seed_default_data(db: aiosqlite.Connection) -> None:
    """Insert default user and watchlist if not already present."""
    # Check if default user exists
    cursor = await db.execute(
        "SELECT id FROM users_profile WHERE id = ?", ("default",)
    )
    if await cursor.fetchone() is None:
        await db.execute(
            "INSERT INTO users_profile (id, cash_balance, created_at) VALUES (?, ?, ?)",
            ("default", 10000.0, _now_iso()),
        )

    # Seed watchlist tickers (INSERT OR IGNORE skips duplicates due to UNIQUE constraint)
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
    for ticker in tickers:
        await db.execute(
            "INSERT OR IGNORE INTO watchlist (id, user_id, ticker, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), "default", ticker, _now_iso()),
        )
    await db.commit()
```

### Pattern 4: Row Factory for Dict-Like Access

**What:** Set `db.row_factory = aiosqlite.Row` on connection open. All query results become `sqlite3.Row` objects that support both index-based and name-based (dict-like) column access.

**When to use:** Always. Accessing columns by name (`row["ticker"]`) is far more readable than by index (`row[2]`).

**Confidence:** HIGH (verified via Context7 aiosqlite docs and Python sqlite3 docs)

**Example:**
```python
# With row_factory = aiosqlite.Row
async with db.execute("SELECT ticker, cash_balance FROM users_profile WHERE id = ?", ("default",)) as cursor:
    row = await cursor.fetchone()
    if row:
        cash = row["cash_balance"]  # dict-like access by column name
```

### Anti-Patterns to Avoid

- **Connection per request:** Do NOT open a new `aiosqlite.connect()` for each API request. SQLite is single-writer; multiple connections add locking overhead. One connection opened at startup, closed at shutdown.

- **Module-level globals for the connection:** Do NOT store the db connection as a module-level variable. Store it on `app.state` and access via `request.app.state.db` in route handlers. This keeps dependencies explicit and testable.

- **String interpolation in SQL:** NEVER use f-strings or `.format()` to build SQL queries. ALWAYS use parameterized queries with `?` placeholders. Even though this is a single-user app, ticker symbols come from user input.

- **Forgetting to commit after writes:** With `isolation_level=None`, single statements auto-commit. But if you use explicit `BEGIN`/`COMMIT` blocks for multi-statement transactions, forgetting `COMMIT` means the writes are lost.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| UUID generation | Custom ID scheme | `str(uuid.uuid4())` | Standard, collision-free, matches PLAN spec |
| ISO timestamp strings | Manual datetime formatting | `datetime.now(timezone.utc).isoformat()` | Consistent format, timezone-aware, sortable |
| Schema migration | Custom versioning system | `CREATE TABLE IF NOT EXISTS` | Schema is static for this project; lazy init is sufficient |
| Connection pooling | Custom pool wrapper | Single connection via aiosqlite | SQLite is single-writer; pooling adds complexity for zero benefit here |

**Key insight:** The database layer for this project is intentionally simple. Six tables, one user, raw SQL. Resist the urge to add ORMs, migration tools, or connection pools. The complexity budget should go toward the trading logic and LLM integration.

## Common Pitfalls

### Pitfall 1: "Database is Locked" Under Concurrent Async Writes

**What goes wrong:** Multiple async tasks (trade execution, portfolio snapshots, watchlist changes) write simultaneously. SQLite returns `SQLITE_BUSY`.

**Why it happens:** SQLite allows only one writer. Without WAL mode, readers also block writers. The default `BEGIN DEFERRED` transaction mode creates upgrade deadlocks that ignore busy_timeout entirely.

**How to avoid:**
1. `PRAGMA journal_mode=WAL` -- readers never block writers
2. `PRAGMA busy_timeout=5000` -- writes retry for 5 seconds before failing
3. `isolation_level=None` -- avoids implicit deferred transactions that cause upgrade deadlocks
4. Single shared connection -- serializes all writes naturally through aiosqlite's internal request queue

**Warning signs:** Intermittent errors in tests that pass individually but fail together. Portfolio snapshot task colliding with trade execution.

### Pitfall 2: Schema Not Created on Fresh Start

**What goes wrong:** App starts, first API request hits a table that doesn't exist. 500 error.

**Why it happens:** Schema initialization was tied to a separate step (e.g., a CLI command) instead of being lazy/automatic.

**How to avoid:** Call `create_tables()` inside `init_db()`, which runs at app startup. `CREATE TABLE IF NOT EXISTS` is idempotent. Every startup ensures schema exists.

**Warning signs:** "no such table" errors after `docker volume rm`.

### Pitfall 3: Re-Seeding Overwrites User Data

**What goes wrong:** On restart, the seed function resets the user's cash balance back to $10,000, erasing trades.

**Why it happens:** Seed function uses `INSERT OR REPLACE` instead of `INSERT OR IGNORE`, or doesn't check if data already exists.

**How to avoid:** Use `INSERT OR IGNORE` for watchlist (UNIQUE constraint handles duplicates). Check `SELECT` before `INSERT` for the user profile. The seed function must be a no-op if data already exists.

**Warning signs:** Cash balance resets after container restart.

### Pitfall 4: WAL Mode Not Persisting Across Connections

**What goes wrong:** Developer sets WAL mode, but the database reverts to rollback mode.

**Why it happens:** Confusion about when WAL mode is persistent. Actually, WAL mode IS persistent -- once set, it remains across connections. However, you must check it was actually set (the PRAGMA returns the mode name).

**How to avoid:** Set `PRAGMA journal_mode=WAL` on init and verify the return value. WAL mode persists in the database file itself. Subsequent connections will already be in WAL mode, but setting it again is harmless.

**Warning signs:** `PRAGMA journal_mode` returns "delete" (the default rollback mode) instead of "wal".

### Pitfall 5: Forgetting to Create `db/` Directory

**What goes wrong:** aiosqlite.connect() fails because the parent directory doesn't exist.

**Why it happens:** The PLAN specifies `db/finally.db` at the project root. In Docker, this maps to `/app/db/`. If the directory doesn't exist, sqlite3 cannot create the database file.

**How to avoid:** The `init_db()` function should create the parent directory if it doesn't exist using `os.makedirs(os.path.dirname(db_path), exist_ok=True)` before calling `aiosqlite.connect()`.

**Warning signs:** `FileNotFoundError` or `OperationalError: unable to open database file`.

### Pitfall 6: .gitignore Missing db/finally.db

**What goes wrong:** The SQLite database file (and WAL/SHM companion files) get committed to git.

**Why it happens:** The current `.gitignore` has `db.sqlite3` and `db.sqlite3-journal` (Django defaults) but NOT `db/finally.db`, `*.db-wal`, or `*.db-shm`.

**How to avoid:** Add these entries to `.gitignore`:
```
db/finally.db
*.db-wal
*.db-shm
```

**Warning signs:** `git status` shows database files as untracked.

## Code Examples

Verified patterns from official sources:

### Complete init_db Function

```python
# Source: aiosqlite official docs + SQLite WAL docs + prior architecture research
import os
import aiosqlite

async def init_db(db_path: str) -> aiosqlite.Connection:
    """Open database, configure for async safety, create tables, seed data.

    This is the single entry point for database initialization.
    Called once at app startup from the FastAPI lifespan.
    """
    # Ensure parent directory exists
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

    # Open connection with explicit transaction control
    db = await aiosqlite.connect(db_path, isolation_level=None)
    db.row_factory = aiosqlite.Row

    # Configure SQLite for concurrent async access
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    await db.execute("PRAGMA foreign_keys=ON")

    # Lazy schema init + seed
    await create_tables(db)
    await seed_default_data(db)

    return db
```

### Complete Schema SQL

```sql
-- Source: PLAN.md section 7 (Schema) - all 6 tables
CREATE TABLE IF NOT EXISTS users_profile (
    id TEXT PRIMARY KEY,
    cash_balance REAL NOT NULL DEFAULT 10000.0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS watchlist (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    added_at TEXT NOT NULL,
    UNIQUE(user_id, ticker)
);

CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    ticker TEXT NOT NULL,
    side TEXT NOT NULL,
    quantity REAL NOT NULL,
    price REAL NOT NULL,
    executed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    total_value REAL NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL DEFAULT 'default',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    actions TEXT,
    created_at TEXT NOT NULL
);
```

### Test Pattern: Isolated Database Per Test

```python
# Source: project convention (matches existing market data test patterns)
import pytest
import aiosqlite

@pytest.fixture
async def db(tmp_path):
    """Provide a fresh database for each test."""
    db_path = str(tmp_path / "test.db")
    conn = await init_db(db_path)
    yield conn
    await conn.close()

async def test_tables_created(db):
    """Verify all 6 tables exist after init."""
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in await cursor.fetchall()]
    assert "users_profile" in tables
    assert "watchlist" in tables
    assert "positions" in tables
    assert "trades" in tables
    assert "portfolio_snapshots" in tables
    assert "chat_messages" in tables
```

### Test Pattern: Concurrent Access Verification

```python
import asyncio

async def test_concurrent_access_no_lock_errors(db):
    """Simulate concurrent reads and writes to verify no lock errors."""
    async def write_task(i: int):
        await db.execute(
            "INSERT INTO trades (id, user_id, ticker, side, quantity, price, executed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "default", "AAPL", "buy", 1.0, 190.0 + i, _now_iso()),
        )
        await db.commit()

    async def read_task():
        async with db.execute("SELECT * FROM trades") as cursor:
            await cursor.fetchall()

    # Run reads and writes concurrently
    tasks = [write_task(i) for i in range(10)] + [read_task() for _ in range(10)]
    await asyncio.gather(*tasks)  # Should complete without "database is locked"
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `isolation_level=""` (implicit transactions) | `isolation_level=None` (explicit autocommit) | Python 3.12 added `autocommit` param; `isolation_level=None` has been available since Python 2.5 | Avoids upgrade deadlock pitfall with deferred transactions. Simpler mental model. |
| aiosqlite Connection inherits from Thread | aiosqlite Connection is a standalone class | aiosqlite 0.22.0 (2025) | Cleaner API, no thread inheritance. Use as async context manager or explicit open/close. |
| `on_startup` / `on_shutdown` event handlers | `lifespan` async context manager | FastAPI >= 0.95 (deprecated events); events not called if lifespan provided | Single unified resource management. Already established as pattern in this project's architecture research. |

**Deprecated/outdated:**
- `on_startup`/`on_shutdown` handlers in FastAPI: Replaced by `lifespan` parameter. If `lifespan` is provided, `startup` and `shutdown` events are NOT called.
- aiosqlite `loop` parameter: Deprecated. Do not pass `loop=` to `aiosqlite.connect()`.

## Open Questions

1. **Database path during testing vs. production**
   - What we know: PLAN says `db/finally.db` relative to project root. Docker mounts to `/app/db/`. Tests use `tmp_path` fixtures.
   - What's unclear: The `init_db()` function receives a path string. During integration, the caller (lifespan in `main.py`) will determine the actual path. For Phase 1, tests use `tmp_path` so this is not blocking.
   - Recommendation: `init_db()` accepts any path string. The lifespan (Phase 4) will resolve the actual path, likely `db/finally.db` relative to the working directory.

2. **Concurrent write behavior with single connection + isolation_level=None**
   - What we know: aiosqlite internally uses a request queue on a single background thread. With one connection, all operations are serialized. WAL mode + busy_timeout provides safety net.
   - What's unclear: When multiple coroutines `await db.execute(INSERT...)` concurrently, does aiosqlite queue them safely? The aiosqlite architecture uses a single thread + queue, so YES -- writes are serialized by the queue.
   - Recommendation: The single-connection pattern is correct. Test with concurrent asyncio.gather() to verify. This is covered in the success criteria.

3. **Need for explicit BEGIN IMMEDIATE**
   - What we know: Prior research flagged `BEGIN IMMEDIATE` to avoid upgrade deadlocks. With `isolation_level=None`, statements auto-commit individually.
   - What's unclear: Are there multi-statement transactions in the db layer that need explicit BEGIN/COMMIT?
   - Recommendation: For Phase 1 (schema + seed only), individual auto-committed statements are sufficient. Phase 2 (trade execution) will need explicit transactions for atomicity (deduct cash + create position + log trade). At that point, use `await db.execute("BEGIN IMMEDIATE")` / `await db.commit()`. Document this as a pattern for Phase 2.

## Sources

### Primary (HIGH confidence)
- [aiosqlite official docs - API Reference](https://aiosqlite.omnilib.dev/en/stable/api.html) - Connection methods, row_factory, executescript
- [aiosqlite GitHub - core.py](https://github.com/omnilib/aiosqlite/blob/main/aiosqlite/core.py) - `connect()` passes `**kwargs` to sqlite3, `isolation_level` property
- [aiosqlite PyPI](https://pypi.org/project/aiosqlite/) - Latest version 0.22.1 (Dec 2025), requires Python >=3.9
- [SQLite WAL mode official docs](https://sqlite.org/wal.html) - WAL is persistent, allows concurrent readers + 1 writer, limitations
- [SQLite PRAGMA reference](https://www.sqlite.org/pragma.html) - journal_mode, busy_timeout, foreign_keys
- [Python sqlite3 docs](https://docs.python.org/3.12/library/sqlite3.html) - isolation_level, autocommit (3.12+), Row factory
- [FastAPI lifespan docs](https://fastapi.tiangolo.com/advanced/events/) - @asynccontextmanager lifespan replaces startup/shutdown
- Context7: `/omnilib/aiosqlite` - Connection patterns, row_factory, executescript

### Secondary (MEDIUM confidence)
- [SQLite concurrent writes deep-dive](https://tenthousandmeters.com/blog/sqlite-concurrent-writes-and-database-is-locked-errors/) - Upgrade deadlock explanation, BEGIN IMMEDIATE recommendation
- [Simon Willison - Enabling WAL mode](https://til.simonwillison.net/sqlite/enabling-wal-mode) - WAL persistence, one-time setup
- [Bert Hubert - SQLITE_BUSY despite timeout](https://berthub.eu/articles/posts/a-brief-post-on-sqlite3-database-locked-despite-timeout/) - busy_timeout per-connection, not per-database

### Tertiary (LOW confidence)
- None needed -- this domain is well-documented with primary sources.

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH -- aiosqlite is the only viable option for async SQLite in Python; version and API verified via PyPI and GitHub
- Architecture: HIGH -- Single-connection + WAL + lazy init pattern is well-established and verified by multiple authoritative sources; matches prior architecture research for this project
- Pitfalls: HIGH -- All pitfalls verified against SQLite official docs and aiosqlite issue tracker; upgrade deadlock pitfall is the critical one, addressed by isolation_level=None

**Research date:** 2026-02-11
**Valid until:** 2026-03-11 (stable domain, 30 days)
