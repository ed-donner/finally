# FinAlly Market Data Layer — Code Review v2

**Reviewer:** Claude (Opus 4.8) · **Date:** 2026-06-06
**Scope reviewed:** `backend/app/market/{__init__,cache,factory,interface,massive_client,models,seed_prices,simulator,stream}.py`, `backend/tests/market/*`, `backend/tests/conftest.py`, `backend/pyproject.toml`, `backend/market_data_demo.py`.
**Method:** Read every source and test module; set up the toolchain (`uv`), installed deps, **ran the full suite with coverage and ruff**, and **independently verified** the highest-risk prior-review claims (H4 against the real installed SDK; M1 and M3 empirically; H5/M5 by inspection).

---

> ## ✅ Resolution status (branch `fix/market-data-review-v2-followups`)
>
> All actionable items below have since been implemented. **M1 was intentionally left alone** (verified the simulator already moves visibly on ~80% of ticks — see §4). Summary of what changed:
>
> - **H5** — `_poll_once` now snapshots the ticker list before `asyncio.to_thread`; `_fetch_snapshots(tickers)` iterates the private copy.
> - **M5** — explicit connect/read timeouts on `RESTClient`; `stop()` bounds the wait with `asyncio.wait_for(SHUTDOWN_TIMEOUT)`.
> - **N1** — `create_stream_router()` builds a fresh `APIRouter` per call (no module global).
> - **N2** — `MarketDataSource.health()` added; `MassiveDataSource` tracks `last_update` + `consecutive_failures`, the simulator reports loop liveness.
> - **N3** — added a test that drives the real `/prices` route handler and asserts the SSE headers.
> - **H3** — `finally` cleanup in the SSE generator.
> - **M2** — single seedable `np.random.Generator` drives GBM **and** event draws; tests pin a seed and assert a 2–5% shock fired.
> - **M3** — Cholesky wrapped with an epsilon-nudge → uncorrelated fallback (with a test forcing a non-PD matrix).
> - **M6 / L5** — shared `normalize_ticker()` used by both sources (incl. `start()`); double-`start()` now raises.
> - **L1** — dead `event_loop_policy` fixture removed. **L2** — `PriceUpdate.timestamp` is now required. **L3** — rounding happens once, in the cache. **L4** — Massive `start()` logs the deduped count. **L6** — `rich` moved to a `demo` extra.
>
> **Result: 102 tests pass, ruff clean, 96% coverage.** The detailed findings below are retained for the record.

---

## 1. Verdict

The market-data subsystem is **well-built, clean, and genuinely tested**. All 85 tests pass, ruff is clean, and coverage is 96%. The four "must-fix before frontend integration" items from the prior `REVIEW.md` (C1, H1, H2, H4) are **correctly implemented and I confirmed them** — including validating the Massive SDK field/timestamp assumptions against the *actual* installed `massive==2.2.0` package, which is the one claim the prior review could not fully verify.

It is **ready for the frontend agent to build against** the SSE contract. The remaining items are real but are reliability/polish concerns on the optional Massive path and the simulator — none block the simulator-default happy path that students will run.

**One scope note up front:** only the market-data layer exists. There is no FastAPI app entrypoint (no `main.py`), no DB, no portfolio/watchlist/chat/health endpoints, no frontend, no Docker, and no E2E tests. That matches `CLAUDE.md` ("the remainder of the platform is still to be developed"), so it is expected — but it means the SSE router is currently **never mounted into a running app** and has only been exercised by unit tests, not end-to-end.

---

## 2. Test & lint results (reproduced)

Environment had no `uv`/venv; I installed `uv 0.11.19` and ran `uv sync --extra dev` (pulled `massive==2.2.0`, `numpy==2.4.2`, `fastapi`/`starlette`, etc. on Python 3.13.7).

```
85 passed in 3.69s
ruff check app/ tests/  →  All checks passed!
```

Coverage (`--cov=app`):

| Module | Cover | Untested lines |
|---|---|---|
| cache.py | 100% | — |
| factory.py | 100% | — |
| interface.py | 100% | — |
| models.py | 100% | — |
| seed_prices.py | 100% | — |
| simulator.py | 98% | 149 (dup-add guard), 268-269 (loop `except`) |
| massive_client.py | 93% | 85-87 (`_poll_loop` body), 104, 142 (`_fetch_snapshots`) |
| stream.py | 83% | 27-49 (route handler — never called by a real request), 98-100 (`CancelledError`) |
| **TOTAL** | **96%** | |

The two real coverage holes are the **route handler itself** (`stream_prices` in `stream.py:27-49` is never invoked — tests drive the underlying `_generate_events` generator directly) and the **`_poll_loop` interval body** in `massive_client.py`. Both are acceptable for unit tests but reinforce that nothing has run as a mounted FastAPI app.

The 73→85 test count and "84%→96%" coverage improvement over the original summary is real and verified.

---

## 3. Verification of prior review items

### Confirmed FIXED and validated

- **C1 — daily-change contract.** ✅ `day_open` is now a first-class field on `PriceUpdate` with `day_change`/`day_change_percent` properties, carried through `PriceCache.update()` (provided → carry-forward → first-tick default-to-price) and emitted in `to_dict()`/SSE. The frontend can compute the watchlist "daily change %" directly. Well tested (`test_models.py`, `test_cache.py`, `test_stream.py`).
- **H1 — atomic version+snapshot read.** ✅ `PriceCache.snapshot()` returns `(version, dict-copy)` under one lock; `_generate_events` consumes it instead of separate `version` + `get_all()` reads. Torn-read race closed. Covered by `test_snapshot_returns_version_and_prices`.
- **H2 — SSE keep-alive.** ✅ `_generate_events` emits `: keep-alive\n\n` after `heartbeat` (15s) of no data. Covered by `test_data_sent_once_then_heartbeat_when_version_unchanged`.
- **H4 — Massive schema/timestamp.** ✅ **Independently validated against the real `massive==2.2.0` SDK.** I inspected the actual models:
  - `LastTrade` fields: `…, sip_timestamp, participant_timestamp, …, price, …` — **no `timestamp` attribute**, exactly as the fix assumed. The code's `last_trade.sip_timestamp or last_trade.participant_timestamp` path is correct, and `/ 1e9` (nanoseconds→seconds) matches Polygon's SIP unit.
  - `TickerSnapshot` fields: `day, last_quote, last_trade, min, prev_day, ticker, todays_change, todays_change_percent, …` — `snap.day` / `snap.prev_day` / `snap.last_trade` / `snap.ticker` all exist.
  - `Agg` fields: `open, high, low, close, …` — `snap.day.open` / `snap.prev_day.close` are correct.
  - `RESTClient.get_snapshot_all(market_type, tickers=…)` signature matches the call in `_fetch_snapshots`.

  So the prior review's central worry — that the entire real-data path was only tested against a self-mirroring mock — is now resolved on the schema side. (It is still only *exercised* against a mock; see §5.)

### Still open (unchanged from prior review)

`H3, H5, M1–M6, L1–L6` were explicitly listed as not addressed. My findings on the ones that matter:

---

## 4. Remaining issues — re-assessed with evidence

### Worth fixing (reliability)

- **H5 — Massive ticker-list race (real, low-frequency).** `add_ticker`/`remove_ticker` mutate `self._tickers` in place while `_poll_once` hands the **same list object** to `asyncio.to_thread(self._fetch_snapshots)`, where the SDK iterates it to build the request. A concurrent `add_ticker` (`self._tickers.append(...)`) during that thread's iteration can raise `RuntimeError: list changed size during iteration` or drop a ticker. The window is small but real on the Massive path. **Fix:** snapshot to a local list before the thread — `tickers = list(self._tickers); await asyncio.to_thread(self._fetch_snapshots, tickers)`. (The simulator is safe: single asyncio loop, `step()` has no `await`.)

- **M5 — hanging Massive call blocks shutdown (real).** `RESTClient` is created with no request timeout. A stuck HTTP call inside `asyncio.to_thread` cannot be cancelled, so `stop()`'s `await self._task` blocks until the worker returns → shutdown can hang indefinitely. **Fix:** configure a timeout on `RESTClient` / wrap the poll in `asyncio.wait_for`. Affects clean container shutdown.

### Lower than the prior review rated them (verified)

- **M1 — "most ticks round to flat" is OVERSTATED.** I instrumented 20,000 ticks across the 10 default tickers with events disabled: **only 20% of ticks are flat after 2-dp rounding; 80% produce a visible move** (mean non-zero move ≈ **$0.032**, median **$0.02**). The green/red flash will fire on ~4 of every 5 ticks at 2 ticks/sec — the UX is *not* static. This is a non-issue at current parameters; no change needed. (If anything, you may later want *fewer* flashes, not more.)

- **M3 — Cholesky non-PD risk is largely THEORETICAL.** I stress-tested: 40 dynamically-added unknown tickers (0.3 cross-correlation) and a 30-ticker all-0.6 "tech" block both produced valid Cholesky factorizations. Equicorrelation matrices with ρ ∈ {0.3, 0.5, 0.6} stay positive-definite regardless of `n` (since ρ > −1/(n−1)), and the block structure here doesn't break that. The unguarded `np.linalg.cholesky` is still a fragility (a future exotic correlation table *could* go non-PD, and the exception would escape `add_ticker` or get swallowed by the loop's broad `except`), so a cheap guard (try/except → identity fallback, or `corr += εI`) is worth ~3 lines, but it is **low priority**, not Medium.

### Polish (agree, low priority)

- **H3** — no `finally:` in `_generate_events`; normal disconnect path does log, so this is cosmetic resource-accounting. Low.
- **M2** — two unseedable global RNGs (`np.random.*` and `random.*`); inject a single `np.random.Generator` so the event-shock and drift tests can pin a seed and assert exact magnitudes. Improves testability, not correctness.
- **M4** — first-tick `flat`/`change=0` semantics for a newly added ticker: confirm the frontend treats it as "new ticker," not "no data." One-line doc note.
- **M6** — ticker normalization asymmetry: `MassiveDataSource` upper/strips; the simulator path does **not**, so `add_ticker("aapl")` under the simulator invents a distinct lowercase entry at a `random.uniform(50,300)` price. Normalize in one shared place and define the unknown-ticker policy (PLAN open-question #3). Real inconsistency, but only reachable via direct lowercase input.
- **L1** — `conftest.py`'s `event_loop_policy` fixture returns the default policy: a genuine no-op under `pytest-asyncio` auto mode. Remove to avoid implying behavior it doesn't have.
- **L2/L3** — `PriceUpdate.timestamp` default factory is never used in prod (cache always passes a ts); price is rounded in both `simulator.step()` and `cache.update()` (Massive rounds once). Pick cache-only rounding for symmetry. Cosmetic.
- **L5** — double `start()` silently leaks the first background task (`self._task` overwritten without cancel). A one-line guard makes the documented "call once" contract enforceable.
- **L6** — `rich` is a **core** dependency but is only used by `market_data_demo.py`. Move it to an optional `demo` extra to keep the runtime/container image lean.

---

## 5. New findings (not in the prior review)

- **N1 — `stream.py` uses a module-level shared `router`; the factory mutates global state.** `router = APIRouter(...)` is defined at module scope, and `create_stream_router()` registers `@router.get("/prices")` on that shared instance each call, then returns it. Calling the factory **twice** (e.g., a test plus app startup, or app re-init) registers the `/prices` route **twice on the same router** → duplicate routes / startup warning. The factory's stated goal ("inject the PriceCache without globals") is undercut by the global router. **Fix:** create the `APIRouter` *inside* `create_stream_router` so each call yields a fresh, independent router. Low severity today (called once) but a latent footgun.

- **N2 — Massive `_poll_once` swallows per-snapshot AND whole-batch errors silently to the log only.** Reasonable for resilience, but combined with no metric/health signal, a persistently failing real-data feed (bad key → repeated 401) is invisible to the user — the UI would just show stale/no prices with no indication. When the API/health layer is built, surface data-source health (last-successful-poll timestamp) so the frontend's connection dot can reflect a dead upstream, not just a dead SSE socket. Forward-looking, not a bug.

- **N3 — Coverage blind spot on the actual route handler.** `stream_prices` (`stream.py:27-49`) — the function that sets SSE headers and wraps the generator in `StreamingResponse` — has **zero** execution coverage; only the inner generator is tested. A FastAPI `TestClient` test that hits `GET /api/stream/prices` and asserts `content-type: text/event-stream` + the `Cache-Control`/`X-Accel-Buffering` headers would close the highest-value remaining gap and is the natural first test once an app entrypoint exists.

---

## 6. Test-suite assessment

Strong and honest. Notable gaps that remain (consistent with the prior review):

1. **No end-to-end SSE test through FastAPI** (only the generator is driven directly). → N3.
2. **Massive path is exercised only against mocks** — schema is now SDK-validated (§3), but no recorded-fixture/integration test runs the real response decode. Acceptable given no API key in CI, but worth a captured-fixture test.
3. **No concurrency test for H5** (add/remove ticker during an in-flight poll).
4. **No shutdown-under-hang test for M5.**
5. **Determinism (M2):** `test_custom_event_probability` still only asserts "starts/stops cleanly," never that a 2–5% shock actually moved the price — because the RNG can't be seeded.

None of these are blockers; they are the next increment of hardening for the optional/real-data path.

---

## 7. Prioritized recommendations

**Before wiring the real-data (Massive) path in production:**
1. H5 — snapshot `self._tickers` to a local list before `to_thread`. *(small, real bug)*
2. M5 — add a `RESTClient` request timeout / `wait_for` around the poll so shutdown can't hang. *(small, real)*

**Cheap correctness/hygiene, any time:**
3. N1 — build the `APIRouter` inside `create_stream_router` (drop the module global).
4. M6 + L5 — normalize tickers in one shared place; guard double-`start()`.
5. L6 — move `rich` to a `demo` extra.
6. L1 — delete the dead `event_loop_policy` fixture.

**When the app/API layer lands:**
7. N3 — `TestClient` test for `GET /api/stream/prices` (headers + first frame).
8. N2 — expose data-source health (last-successful-poll) for the connection indicator.
9. M2 — inject a seedable RNG and assert event-shock magnitude.

**Explicitly do NOT do:**
- Don't inflate per-tick volatility for M1 — verified that 80% of ticks already move visibly; the current parameters are good.
- M3's identity-fallback guard is optional polish, not a real risk at the current correlation values.

---

## 8. Bottom line

The market-data layer is solid, the must-fix contract items are correctly resolved and **independently verified against the real SDK**, and the suite is green (85 passed, ruff clean, 96% coverage). It is safe to build the frontend and the rest of the backend against the current `PriceCache` / SSE / `MarketDataSource` contracts. The open items are a short, well-understood list of reliability touch-ups concentrated on the optional Massive path — fix H5 and M5 before anyone runs with a real `MASSIVE_API_KEY`; the rest is polish.
