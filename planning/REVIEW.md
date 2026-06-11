# FinAlly — Project Review

Snapshot review covering plan vs. implementation, the in-flight `planning/PLAN.md` edits, code quality of the completed market data subsystem, and the gap to ship the rest of the workstation.

## High Priority

### H1. `SIMULATOR_SEED` is documented but not wired in
`planning/PLAN.md` Section 5 (env vars) and Section 12 (E2E) now promise that setting `SIMULATOR_SEED=<int>` makes GBM moves and shock events reproducible so Playwright assertions don't flake. The code does not honour it:

- `backend/app/market/simulator.py:84` uses `np.random.standard_normal(n)` (global numpy RNG) and `random.random()` / `random.uniform` / `random.choice` at lines 105–107 (global stdlib RNG).
- `backend/app/market/factory.py:31` constructs `SimulatorDataSource(price_cache=price_cache)` and never reads `SIMULATOR_SEED`.
- `grep` confirms zero references to `SIMULATOR_SEED`, `random.seed`, or `np.random.seed` anywhere in `backend/`.

Either land a small follow-up that seeds both RNGs (or, better, a per-instance `random.Random` + `np.random.default_rng(seed)` to avoid polluting global state) before the E2E work starts, or roll the promise back in the plan. Today the plan and code disagree.

### H2. `MASSIVE_POLL_SECONDS` is documented but also not wired
Section 5 introduces `MASSIVE_POLL_SECONDS` (default `15`). `backend/app/market/massive_client.py:32` hard-codes `poll_interval: float = 15.0` and `backend/app/market/factory.py:28` calls `MassiveDataSource(api_key=api_key, price_cache=price_cache)` without forwarding any env override. Same fix shape as H1: small factory change to read `os.environ.get("MASSIVE_POLL_SECONDS", "15")`.

### H3. Most of the system is still TODO — `frontend/`, `db/`, `scripts/`, `test/`, `Dockerfile`, `docker-compose.yml`, `.env.example` are all absent
`ls /Users/chrisbarnum/projectd/finally` shows only `backend/`, `planning/`, `.github/`, `CLAUDE.md`, `LICENSE`, `README.md`. Of the directory tree in Section 4 of the plan, only `backend/` (market data slice) and `planning/` exist. There is no FastAPI app entry point either — `backend/app/__init__.py` is empty and no `main.py`/`app.py` exists yet. The `create_stream_router` factory in `backend/app/market/stream.py:20` is plumbed but never mounted. This is the single largest delivery risk.

## Medium Priority

### M1. The "Why These Choices" rationale table was deleted from the plan
Diff removes the table at PLAN.md ~line 72 (SSE vs WebSockets, SQLite vs Postgres, market orders only, etc.). The other edits are tightening and are consistent. Losing this table costs new agents the "why" for decisions they're expected to honour (e.g., "no order book", "no migrations"). Recommend restoring it, or moving the bullets inline next to the choices they explain. Otherwise the diff is coherent and matches the resolved items now summarized in Section 13.

### M2. `Massive` rate-limit comment in code drifted from the plan
`backend/app/market/massive_client.py:23–26` still says "Paid tiers: higher limits → poll every 2-5s". The plan now says "2-15s". Trivial, but worth aligning when H2 is fixed.

### M3. `MARKET_DATA_SUMMARY.md` undercounts modules
`planning/MARKET_DATA_SUMMARY.md:7` claims "8 modules". The directory has 8 `.py` files including `__init__.py`. Fine, just verify after re-reading — the listed table at lines 27–35 shows 8 entries but omits `__init__.py`. Not a defect, just inconsistent.

### M4. `PriceCache.version` is read without the lock
`backend/app/market/cache.py:65` returns `self._version` outside the `Lock`. Python ints are atomic in CPython so reads won't tear, but the SSE generator (`stream.py:75`) reads `version` then calls `get_all()` separately, so a writer can squeeze in between. The current "always re-snapshot when version differs" pattern recovers naturally (worst case: one extra event), so this is acceptable — flagging only so a future reviewer doesn't try to "tighten" it without thinking.

### M5. `PriceUpdate` rounds twice
`PriceCache.update` (cache.py:36–38) rounds price/previous to 2 decimals; `PriceUpdate.change` and `change_percent` (models.py:21,28) re-round to 4. Harmless, but slight smell — change is bounded by 0.01 precision so the 4-dp rounding adds no information. Low impact.

## Low Priority

### L1. CLAUDE.md style — emoji + `print()` audit is clean
`grep` for emojis in `backend/app/` returns zero hits; `print()` is only used in the demo script (acceptable — it's a CLI). All log calls use `logger.*` with lazy `%s` formatting. Conforms to CLAUDE.md.

### L2. Tests pass and are fast
`uv run --extra dev pytest -q` → `73 passed in 1.72s`. Coverage targets in `pyproject.toml` look right.

### L3. `.claude/agents/reviewer.md` and `.claude/commands/doc-review.md` are tiny and consistent
Both untracked files are one-liner agent/command definitions. Safe to commit as-is.

### L4. Logging in `massive_client._poll_once` swallows the traceback
`backend/app/market/massive_client.py:119` uses `logger.error("Massive poll failed: %s", e)`. CLAUDE.md says exception managers only when needed — this one is justified (loop must keep running), but `logger.exception(...)` would preserve the traceback for free.

## Suggested Next Increments

1. Wire H1 + H2 (tiny — factory reads env, simulator takes an optional seed). Add a regression test that two simulators with the same seed produce identical sequences.
2. Stand up `backend/app/main.py` with a FastAPI app, lifespan hook to construct `PriceCache` + `create_market_data_source`, and mount `create_stream_router(price_cache)`. Add `/api/health`. That alone unblocks the frontend agent.
3. Add `backend/app/db/` (schema SQL + lazy init) before any portfolio/trade route — every other API depends on it.
4. Restore the "Why These Choices" table to PLAN.md (M1) before committing the planning diff.
