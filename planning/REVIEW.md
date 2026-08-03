# FinAlly — Comprehensive Project Review

**Date:** 2026-08-03
**Scope:** repo state vs. `planning/PLAN.md`, correctness of shipped code, plan/code consistency, test coverage, uncommitted working-tree changes.
**Method:** read every source and test file in `backend/`, the plan and its archived predecessor, the git diff, and `.gitignore` (verified with `git check-ignore`). Tests were **not** executed — `uv` is not on PATH in this environment and the dependencies are not installed locally, so all findings below come from reading code, not from a test run.

---

## 1. What exists vs. what §§4–12 specify

### Built

| Plan section | Status | Evidence |
|---|---|---|
| §6 Market data — simulator, Massive client, cache, abstract interface, factory | Built | `backend/app/market/{simulator,massive_client,cache,interface,factory,seed_prices,models}.py` |
| §6 SSE streaming generator | Built as a **router factory**, never mounted | `backend/app/market/stream.py` |
| §12 Backend unit tests (market only) | Built — 73 tests across 6 modules | `backend/tests/market/` |

### Absent

| Plan section | Item | Note |
|---|---|---|
| §3, §11 | **`backend/app/main.py` — there is no FastAPI app object anywhere in the repo** | `backend/app/` contains only `__init__.py` and `market/`. `create_stream_router()` is never called. `uvicorn` has nothing to serve; the app cannot start. |
| §4, §7 | `backend/app/db.py`, all six tables, seed data, lifespan startup init | Absent |
| §7 | 30s snapshot task, startup snapshot, 30-day retention task | Absent |
| §8 | 11 of 12 endpoints: portfolio, trade, history, watchlist ×3, chat ×2, health, reset | Only `GET /api/stream/prices` exists (unmounted) |
| §9 | LLM integration, structured output, mock mode | Absent. `litellm` / `pydantic` are not in `backend/pyproject.toml` dependencies and `pydantic` appears in `uv.lock` only transitively via FastAPI |
| §10 | `frontend/` | Directory does not exist |
| §11 | `Dockerfile`, `docker-compose.yml`, `.env`, `.env.example` | All absent |
| §4 | `db/` bind-mount dir + `.gitkeep` | Absent |
| §12 | `test/`, Playwright, `docker-compose.test.yml` | Absent |

Roughly one subsystem of eight is built. The plan is accurate about this; the README is not (finding 4).

---

## 2. Findings, most severe first

### 1. HIGH — `stream.py` shares a single module-level router across all factory calls (bug, existing code)

`backend/app/market/stream.py:17`
```python
router = APIRouter(prefix="/api/stream", tags=["streaming"])

def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """... This factory pattern lets us inject the PriceCache without globals."""
    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse: ...
    return router
```

The router is module-global (`:17`), so every call to `create_stream_router` appends **another** `/prices` route to the *same* object and hands back the *same* object. The docstring's claim of "without globals" is wrong — the cache is injected, the router is not.

FastAPI does not error on a duplicate path; it matches the first registered route. So the second app built in a process streams from the **first** app's `PriceCache`, silently and with no error. This exact issue was raised as §3.6 (Low) in `planning/archive/MARKET_DATA_REVIEW.md:120` and never fixed, despite `planning/MARKET_DATA_SUMMARY.md:62` asserting all review issues were resolved (finding 13).

Severity is now higher than "Low" because PLAN §12 makes an SSE backend integration test a required deliverable — the natural way to write it is one app per cache per test, which is precisely the case that breaks.

**Fix:** move `APIRouter(...)` inside the factory.

---

### 2. HIGH — `previous_close` does not exist; §10's "daily change %" column has no data source (known gap, Q1)

`backend/app/market/models.py:13-16` defines `ticker`, `price`, `previous_price`, `timestamp` only; `to_dict()` at `:39-49` emits seven keys, none of them `previous_close`. Nothing in `cache.py`, `simulator.py`, or `massive_client.py` computes one.

PLAN §6 documents the field in a table alongside fields that *do* exist and only says "This field does not exist yet" three paragraphs later, while §10 instructs the frontend agent to render "daily change % (computed from `previous_close`, §6)". A frontend agent reading §10 will code against a field the payload does not contain.

Note the fix is not a lookup. §6 says the simulator's `previous_close` is "that ticker's seed price (`SEED_PRICES`, **or the random $50–300 draw for non-seeded tickers**)". That random draw is generated inline at `simulator.py:151` and stored nowhere — so implementing this requires new simulator state, not just reading `SEED_PRICES`. On the Massive side it is nearly free: `snap.prev_day.close` is available at the same call site as `snap.last_trade.price` (`massive_client.py:101`).

**Recommend:** answer Q1 before any frontend work starts, and until then mark the field inline in the §6 frame example as not-yet-present.

---

### 3. HIGH — `.gitignore` is stock Python and will silently swallow frontend source

`.gitignore:17` is `lib/`, with no leading slash, so it matches at any depth. Verified:

```
$ git check-ignore -v frontend/lib/utils.ts frontend/src/lib/utils.ts
.gitignore:17:lib/      frontend/lib/utils.ts
.gitignore:17:lib/      frontend/src/lib/utils.ts
```

`lib/utils.ts` is the default location for the `cn()` helper in shadcn/Tailwind Next.js setups — exactly the stack §10 specifies. Files there would be dropped from commits with no warning and no error.

Also missing entirely: `node_modules/`, `.next/`, `out/`, and `db/finally.db`. PLAN §4 states "`finally.db` is gitignored" — this is currently **false**, so the first `docker compose up` will offer a SQLite binary for commit.

**Fix before the frontend and db work land**, not after.

---

### 4. MEDIUM-HIGH — `README.md` contradicts the revised plan on four of the six items the doc review just resolved

| README | Says | Plan says |
|---|---|---|
| `:22` | "SQLite with lazy initialization" | §7 / A4: startup init in the lifespan handler; lazy init explicitly rejected as unworkable |
| `:34-35` | `docker build` + `docker run -v finally-data:/app/db` | §11 / A2 + D4: bind mount `./db:/app/db`, `docker compose up` as the *only* documented entry point |
| `:44` | `OPENROUTER_API_KEY` — Required: **Yes** | §5 / A6: optional; absent → mock mode; the app never refuses to start |
| `:57` | `scripts/` — Start/stop helpers | D4: the four scripts were deleted |
| `:30` | `cp .env.example .env` | C9: `.env.example` does not exist in the repo |

The README is the only user-facing doc and the first thing a student follows. It now points them at a named volume, a required API key, and two files that do not exist.

---

### 5. MEDIUM — Simulator `dt` is decoupled from `update_interval` (bug, existing code)

`backend/app/market/simulator.py:219-223`
```python
async def start(self, tickers: list[str]) -> None:
    self._sim = GBMSimulator(
        tickers=tickers,
        event_probability=self._event_prob,
    )   # dt never passed
```

`GBMSimulator` accepts `dt` but `SimulatorDataSource` never supplies it, so `DEFAULT_DT` — 500 ms of simulated time (`simulator.py:48`) — is used regardless of the configured `update_interval` (`simulator.py:210`). The production default of `0.5` makes the two agree by coincidence.

Any other interval mis-scales volatility and drift. `tests/market/test_simulator_source.py:116` runs at `update_interval=0.01` while each step still advances 500 ms of simulated time — 50× too much movement per wall-clock second. Nothing asserts price magnitudes, so no test fails.

**Fix:** `dt=self._interval / GBMSimulator.TRADING_SECONDS_PER_YEAR`.

---

### 6. MEDIUM — §6's "no heartbeat needed" reasoning holds only in simulator mode

PLAN §6 justifies the absence of keepalive frames with "the ~500ms full-snapshot cadence *is* the keepalive; an idle connection only occurs if the data source has stopped."

That is true for the simulator and false for Massive, whose default poll interval is **15 seconds** (`massive_client.py:32`). In Massive mode the SSE connection is silent for 15 s at a stretch, which is longer than the idle timeout of several common proxies. `EventSource` will reconnect, so no data is lost — but the §2 connection indicator will flap yellow, which is user-visible and looks like a defect.

A `: keepalive\n\n` comment frame every ~10 s inside `_generate_events` costs nothing and makes the plan's claim true for both sources.

Related, same file: `Connection: keep-alive` (`stream.py:44`) is a hop-by-hop header — forbidden under HTTP/2 and ignored by ASGI servers. Harmless, but dead.

---

### 7. MEDIUM — The two `MarketDataSource` implementations normalize tickers differently

- `MassiveDataSource.add_ticker` uppercases and strips (`massive_client.py:67`)
- `SimulatorDataSource.add_ticker` (`simulator.py:242`) and `GBMSimulator.add_ticker` (`simulator.py:120`) do neither

So in simulator mode `add_ticker("aapl")` creates a distinct price series under cache key `"aapl"`, which will never match a watchlist row stored as `"AAPL"` — a position whose price silently never resolves.

§8's "trimmed and uppercased" rule at the API boundary mitigates this only if *every* caller obeys, and §12 explicitly requires "both implementations conform to the abstract interface". `tests/market/test_massive.py:114-128` has three normalization tests for one source and there is no equivalent for the other, and no cross-implementation parity test at all — the asymmetry looks accidental rather than intended.

**Fix:** normalize once, in the interface or in `PriceCache`.

---

### 8. MEDIUM — `stream.py` has zero tests, and the plan now requires them

There is no `tests/market/test_stream.py`. The archived review listed an SSE test as nice-to-have #6 (`MARKET_DATA_REVIEW.md:172`) at 31 % coverage; the revised PLAN §12 upgrades it to a **required** deliverable:

> a client disconnecting mid-stream terminates the generator cleanly (`request.is_disconnected()`), and a fresh connection immediately receives a full snapshot frame.

Neither behaviour is covered. Blocking detail: `httpx` is not in `[project.optional-dependencies].dev` and does not appear in `uv.lock`. Both `fastapi.testclient.TestClient` and `httpx.AsyncClient` require it, so this test cannot be written until the dependency is added.

Other gaps carried over from the archived review and still open: no concurrency test for `PriceCache`, and no test that the Cholesky decomposition succeeds for the full 10-ticker default set (all simulator tests use 1–2 tickers).

*(For the record: the correlation matrix is provably positive-definite by construction — base 0.3 everywhere, +0.3 within tech, +0.2 within finance leaves strictly positive residual variance of 0.4/0.5/0.7 on the diagonal — so `np.linalg.cholesky` cannot raise for any ticker set. That is worth a test asserting it, not a bug.)*

---

### 9. LOW-MEDIUM — Nothing runs the tests automatically

`.github/workflows/` contains only `claude.yml` and `claude-code-review.yml`. There is no workflow that runs `pytest` or `ruff`. `MARKET_DATA_SUMMARY.md:47` asserts "73 tests, all passing" with nothing re-verifying it on push. This matters more as the backend, frontend, and Playwright suites land.

---

### 10. LOW-MEDIUM — Massive mode returns `PRICE_UNAVAILABLE` for up to 15 s after a ticker is added

`SimulatorDataSource.add_ticker` seeds the cache immediately (`simulator.py:245-248`). `MassiveDataSource.add_ticker` (`massive_client.py:66-70`) only appends to `_tickers` and waits for the next scheduled poll.

§8 characterises `PRICE_UNAVAILABLE` as "the first moments after startup, or a just-added ticker before its first tick". In Massive mode "moments" is up to a full 15 s poll interval, during which `POST /api/portfolio/trade` on the new ticker returns 503. Consider an immediate targeted fetch on `add_ticker`, or widen the plan's wording so the frontend retry strategy is sized correctly.

---

### 11. LOW — `PriceCache.update` treats a falsy timestamp as absent

`backend/app/market/cache.py:30`
```python
ts = timestamp or time.time()
```
`timestamp=0.0` silently becomes "now". Unreachable with real epoch values, but it is the wrong idiom; `if timestamp is None` is one line.

---

### 12. LOW — `CancelledError` is swallowed without re-raising

`backend/app/market/stream.py:86-87` catches `asyncio.CancelledError`, logs, and returns. Suppressing cancellation is an anti-pattern — the task then reports as completed rather than cancelled. Starlette masks the consequences here, but prefer logging in a `finally:` block, or `raise` after the log.

---

### 13. LOW — `MARKET_DATA_SUMMARY.md` overstates the review outcome

`MARKET_DATA_SUMMARY.md:60-70` states "A comprehensive code review identified 7 issues. All were resolved." Cross-checked against `planning/archive/MARKET_DATA_REVIEW.md` §3:

- **3.4** (`version` read outside the lock) — still present, `cache.py:64-67`
- **3.6** (module-level router) — still present, `stream.py:17` → finding 1

Meanwhile two entries on the "resolved" list (lazy imports removed, correlation constants cleaned up) do not correspond to numbered issues in the review at all. `CLAUDE.md` points every downstream agent at this summary as the authoritative status of the subsystem, so an overstated "all resolved" is the mechanism by which finding 1 stayed unfixed. Prefer an explicit "resolved / accepted / deferred" breakdown.

---

### 14. LOW — SSE frames can be torn across a simulation step

`cache.py:41` bumps `_version` inside `update()`, and `SimulatorDataSource._run_loop` (`simulator.py:266-267`) calls `update()` once per ticker — so one simulation step advances the version ~10 times. The SSE generator (`stream.py:75-83`) can sample between those writes and emit a frame mixing step *N* and step *N−1* prices.

Harmless today (prices are independent, the next frame is 500 ms behind) and not worth restructuring — but worth recording before anyone builds a "consistent cross-ticker snapshot" assumption on top of the feed.

---

### 15. LOW — Packaging and dead code

- `backend/pyproject.toml:12` puts `rich>=13.0.0` in `[project].dependencies`, but its only consumer is `backend/market_data_demo.py`, which lives outside the `app` package. Per §11 the Dockerfile will `uv sync` and ship Rich into the production image. Move it to the `dev` extra.
- `backend/tests/conftest.py:6-11` defines an `event_loop_policy` fixture that nothing requests — `asyncio_mode = "auto"` (`pyproject.toml:35`) supplies its own. Dead code.
- `backend/market_data_demo.py:57` — `build_table(..., elapsed)` parameter is unused.

---

## 3. Plan ↔ shipped-code consistency (§6 SSE contract)

The §6 SSE section is an accurate transcription of `stream.py` on every point I checked:

| §6 claim | Code | Verdict |
|---|---|---|
| One frame = all priced tickers, object keyed by symbol | `stream.py:81` `{ticker: update.to_dict() ...}` over `get_all()` | Accurate |
| Default `message` event, no named type | `stream.py:83` `f"data: {payload}\n\n"` | Accurate |
| `retry: 1000` as the first frame | `stream.py:62` | Accurate |
| `timestamp` = float epoch seconds | `models.py:16` `default_factory=time.time` | Accurate |
| `change` / `change_percent` / `direction` are tick-to-tick | `models.py:18-37`, relative to `previous_price` | Accurate |
| Frame emitted only when `version` advances; wake every 500 ms | `stream.py:75-85` | Accurate |
| `X-Accel-Buffering: no`, `Cache-Control: no-cache` | `stream.py:42-44` | Accurate |
| No heartbeat frames | Correct, but the *justification* is simulator-only | See finding 6 |
| A fresh connection gets a snapshot immediately | `last_version = -1` at `stream.py:64` guarantees it | Accurate, untested |

The example frame's arithmetic also checks out: `0.04 / 190.48 × 100 = 0.021`, confirming `change_percent` is in percent units, matching `models.py:28` and `tests/market/test_models.py:69`.

**The one gap is `previous_close`** (finding 2).

---

## 4. Test coverage assessment

73 tests, count verified per module — matches `MARKET_DATA_SUMMARY.md:47`. Quality is good: `test_massive.py` correctly patches `_fetch_snapshots` rather than the SDK, error paths are covered (`test_api_error_does_not_crash`, `test_malformed_snapshot_skipped`), and `test_simulator.py:16` runs 10 000 GBM steps to assert prices stay positive.

Gaps, in priority order:

1. **`stream.py`: no tests at all** — now a §12 requirement (finding 8); needs `httpx` added first
2. **No cross-implementation parity test** for `MarketDataSource`, which §12 explicitly asks for — this is what would have caught finding 7
3. **No concurrency test for `PriceCache`**, despite thread-safety being its entire purpose (`massive_client.py:97` writes from `asyncio.to_thread`)
4. **No full-10-ticker simulator test** — the default watchlist path is never exercised end to end
5. **No numerical assertions on GBM output scale** — which is why finding 5 is invisible to the suite
6. RNG is the process-global `numpy.random` / `random`, so nothing is reproducible; a seeded generator per `GBMSimulator` would make distributional assertions possible

---

## 5. Uncommitted changes

### `.claude/skills/cerebras/SKILL.md` — sound, and necessary

Renames the frontmatter `name:` from `cerebras-inference` to `cerebras`, removes a trailing double-space, adds a trailing newline. This makes the skill's declared name match its directory and match PLAN §9/A1, which now reads "use the **`cerebras`** skill (`.claude/skills/cerebras/SKILL.md`)". Before this change the plan's reference did not resolve. No concerns.

### `planning/PLAN.md` — +355 / −81, sound and a clear improvement

The revision converts the largest ambiguities into contracts the Backend and Frontend agents can build against independently: the SSE frame shape (§6), the error envelope and nine codes (§8), the `actions` blob schema (§7), the priced-set = watchlist ∪ non-zero-positions rule (§6), and trade execution rules (§8). Each of those was a place where two agents would otherwise have invented incompatible answers. The Resolution Log is a genuinely good artefact — it records reasoning rather than just outcomes.

Nits:

- **§2 says "Two breakpoints"** and then lists three ranges, while Resolution Log C11 says "Three breakpoints". Self-inconsistent within one document. (Two breakpoints producing three ranges is the correct reading; make both say the same thing.)
- **The Simplifications table jumps D6 → D8.** D7 exists only in the "Deliberately not applied" section below. A forward pointer in the table would stop it reading as a dropped row.
- **§6 introduces `previous_close` in a table alongside real fields**, and the "does not exist yet" caveat is three paragraphs away and easy to miss — see finding 2.
- **Q1–Q4 are still open.** Q1 in particular blocks frontend work: the watchlist's daily-change column cannot be built until it is answered either way.

### Untracked `.claude/agents/reviewer.md`, `.claude/commands/doc-review.md`

Content is fine and consistent with the project's agent-driven workflow. `.claude/settings.json` is already tracked, so `.claude/` is clearly meant to be in the repo. **These should be committed** — otherwise the agent definitions that produced the PLAN revision are not reproducible for anyone else who clones the project, which cuts against §1's premise that the whole thing is built and documented by orchestrated agents.

---

## 6. Recommended order of work

1. Fix `.gitignore` (finding 3) — before any frontend file is written
2. Answer Q1 and implement `previous_close` (finding 2) — before the frontend agent starts
3. Fix the `stream.py` module-level router (finding 1) and add `httpx` + the SSE integration test (finding 8) — before more routers are written against the same pattern
4. Rewrite `README.md` against the revised plan, and create `.env.example` and `db/.gitkeep` (finding 4)
5. Build `backend/app/db.py` + `backend/app/main.py` — nothing can be exercised end to end until an ASGI app exists
6. Sweep the small fixes: simulator `dt` (5), ticker normalization (7), keepalive (6), `rich` to dev extras (15)
