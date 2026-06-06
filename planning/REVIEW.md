# FinAlly Market Data Layer — Code Review

Reviewed files: `backend/app/market/{cache,factory,interface,massive_client,models,seed_prices,simulator,stream}.py`, all of `backend/tests/market/*`, and `backend/pyproject.toml`. The code is generally clean and well-structured, with a sound strategy pattern and good docstrings. The issues below are grouped by severity.

---

## Critical

### C1. `previous_price` is the previous *tick*, but the contract and downstream consumers need a stable reference — and there is NO day_open/prev_close anywhere
**Status: FIXED.** Added a `day_open` field to `PriceUpdate` with `day_change`/`day_change_percent` properties (the watchlist's "daily change %") and `day_open` carried through `PriceCache.update()` and the SSE payload. Simulator uses the session-open price (first-tick default + carry-forward); Massive uses the snapshot's `day.open`, falling back to `prev_day.close`.

**Files:** `cache.py:31-38`, `models.py:24-37`, `seed_prices.py` (whole file)

The plan's open question (Section 13, item 1) explicitly flags that "daily change %" needs a `day_open`/`prev_close` source. The implementation provides none. `PriceUpdate.previous_price` is the *immediately preceding tick* (`cache.py:31-32`), so `change_percent` (`models.py:24-28`) is a tick-to-tick delta of typically a fraction of a cent — useless as the "daily change %" the watchlist UI requires (PLAN Sections 2 and 10).

This is the single most important gap: the frontend cannot compute the required "daily change %" from anything the cache or SSE event exposes. Either:
- add a `day_open` (simulator: the seed/session-open price; Massive: the snapshot's `day.open` or `prev_day.close` field, both of which the Polygon snapshot already returns), carry it through `PriceUpdate` and `to_dict()`, and add a `day_change_percent` property; or
- formally redefine the metric as "change since page load" and compute it frontend-side.

This must be resolved before the frontend agent builds the watchlist, because it changes the SSE payload contract. The Massive snapshot response already contains `todays_change_percent` / `day.open` / `prev_day.close`, so the data is available on the real-data path and is simply being discarded at `massive_client.py:99-108`.

---

## High

### H1. `PriceCache` exposes the `version` counter without holding the lock — torn reads / missed updates on the SSE path
**Status: FIXED.** Added `PriceCache.snapshot() -> (version, prices-copy)` that reads both under a single lock; the SSE generator now consumes that instead of separate `version` + `get_all()` calls.

**File:** `cache.py:64-67` (read) vs `cache.py:41` (write under lock)

`version` is incremented inside the lock in `update()` but the `version` property reads `self._version` with no lock. In CPython an `int` read is atomic enough not to crash, but the real problem is a memory-ordering / visibility gap against `get_all()`: the SSE generator does `current_version = price_cache.version` (`stream.py:75`) and *then* `price_cache.get_all()` (`stream.py:78`) as two separate locked operations. The version/snapshot pair is not read atomically. A writer can bump the version and write the dict between those two calls, so a client can latch `last_version` to a value whose data it then fails to re-fetch on the next loop because the number didn't change again.

Add a method like `snapshot() -> tuple[int, dict]` that returns `(self._version, dict(self._prices))` under a single lock, and have the SSE loop consume that, instead of reading `version` and `get_all()` separately. This removes the race and the unlocked attribute read.

### H2. SSE generator has no heartbeat/keep-alive; idle connections can be dropped
**Status: FIXED.** The generator now emits a `: keep-alive` comment when no data has been sent for `heartbeat` seconds (default 15s). Added a `test_stream.py` covering the data-then-heartbeat path (stream previously had zero tests).

**File:** `stream.py:62-85`

On connect the generator yields `retry: 1000` (line 62), then enters the loop. The first data frame is sent on the first iteration because `last_version = -1`, and on reconnect a new generator instance also emits the current snapshot on its first loop iteration — both correct, and this satisfies the PLAN item 18 reconnection requirement.

The concern is that there is **no heartbeat/keep-alive**: if prices stop changing (e.g., Massive returns identical snapshots, or the simulator task died), the connection sends nothing for an unbounded time. Proxies and some browsers will drop an idle event-stream. Add a periodic comment ping (`yield ": keep-alive\n\n"`) every N seconds regardless of version change.

### H3. `is_disconnected()` is the only disconnect path and there is no `finally` cleanup
**File:** `stream.py:69-85`

Each SSE client runs its own `while True` loop with its own `asyncio.sleep(interval)`. Disconnect detection relies entirely on `request.is_disconnected()`, polled at most every 500ms. There is no `finally:` block to log/clean up on *normal* exit (only `except CancelledError`). Add a `finally:` that handles both the normal-break path (line 73) and the cancelled path so resource accounting/logging is correct.

### H4. Massive snapshot field access assumes a schema/timestamp unit that is unvalidated and likely wrong
**Status: FIXED (confirmed against the installed SDK).** Verified the real `massive.rest.models`: `LastTrade` has **no** `timestamp` attribute (it has `sip_timestamp`/`participant_timestamp`, in **nanoseconds**) — so the old `snap.last_trade.timestamp / 1000.0` raised `AttributeError` and silently skipped *every* snapshot, meaning the real-data path produced zero updates. Now reads `sip_timestamp` (fallback `participant_timestamp`), divides by 1e9, and pulls `day_open` from `snap.day.open` / `snap.prev_day.close`. Test mocks updated to the real shape, plus new tests for ns conversion, participant fallback, and day-open resolution.

**File:** `massive_client.py:99-108`, `_fetch_snapshots` at `124-128`

The code reads `snap.last_trade.price` and `snap.last_trade.timestamp` and divides the timestamp by `1000.0` assuming **milliseconds** (`massive_client.py:103`). In the Polygon.io snapshot model (which `massive` wraps), the trade timestamp is typically in **nanoseconds**, not milliseconds — so the resulting timestamp would be off by 10^6. The `massive` package is not vendored in the repo, so the exact field/unit could not be confirmed, but this is a real risk: the unit and attribute names are asserted only against a hand-rolled `MagicMock` in `test_massive.py:11-18`, which will pass regardless of the real SDK's shape.

Action: confirm the real `massive` snapshot model's attribute path and timestamp unit against the installed SDK, and add an integration test (or recorded fixture) that exercises the real response shape. As written, the entire real-data path is only tested against a mock that mirrors the code's own assumptions.

### H5. `add_ticker`/`remove_ticker` mutate `self._tickers` with no synchronization against the background task that iterates it
**Files:** `massive_client.py:66-79` vs `massive_client.py:91, 125`; `simulator.py:242-255` vs `260-270`

For Massive: `add_ticker` does `self._tickers.append(...)` (line 69), an in-place mutation that can race with `len(self._tickers)`/iteration in `_poll_once`. The only true concurrency is the `asyncio.to_thread(self._fetch_snapshots)` window — during which the list can be appended to. `list.append` during another thread's iteration of the same list can raise `RuntimeError: list changed size during iteration` or skip elements.

For the simulator: both `add_ticker` and `_run_loop`/`step()` are coroutines on one loop, and `step()` has no `await`, so they cannot interleave — currently safe *by accident*. It is fragile: any future `await` inside `step` or a move to threads breaks it.

Recommend documenting the single-thread assumption explicitly, and for Massive, snapshot `self._tickers` into a local list before handing it to the thread (`tickers = list(self._tickers); await asyncio.to_thread(self._fetch_snapshots, tickers)`).

---

## Medium

### M1. Simulator per-tick GBM moves are sub-cent and round away to `flat`
**Files:** `simulator.py:50-57, 207-223`, rounding at `simulator.py:116`

With `DEFAULT_DT ≈ 8.5e-8` and per-tick diffusion `sigma*sqrt(dt)*Z`, a `sigma=0.22` ticker moves on the order of `6e-5` (relative) per tick — i.e. ~1 cent on a $190 stock only occasionally. Rounded to 2 decimals, **most ticks produce no visible change at all**, so `direction` will frequently be `"flat"` and the UI's green/red flash will rarely fire except on event ticks. This under-delivers the "prices flash green/red" UX (PLAN Section 2). Consider scaling `dt` up so per-tick changes are visible, or the demo will look static between rare events.

### M2. Two unseedable global RNGs; tests can't assert deterministic behavior
**Files:** `simulator.py:84` (`np.random.standard_normal`), `simulator.py:105-107` (`random.random`, `random.uniform`, `random.choice`)

Two independent global RNGs are used, neither seedable. `test_prices_change_over_time` (`test_simulator.py:68-78`) relies on probabilistic non-equality and `test_custom_event_probability` can't assert an event actually occurred. Inject a `numpy.random.Generator` (and use it for both the normals and the event draws) so tests can pin a seed and assert exact behavior, including that an event of 2-5% magnitude fired.

### M3. Cholesky can fail for pathological correlation structures, crashing the loop step
**File:** `simulator.py:154-172`

`_rebuild_cholesky` builds a correlation matrix from fixed pairwise values (0.6/0.5/0.3) and calls `np.linalg.cholesky` (line 172). The construction does not guarantee positive-definiteness for arbitrary added tickers — a large set with many 0.6 off-diagonals can push the matrix non-PD, making `cholesky` raise `LinAlgError`. This propagates out of `add_ticker` (caught nowhere) or out of `step()` (caught by the broad `except` in `_run_loop` at `simulator.py:268`, which would then silently stop producing correlated updates while logging every tick). Wrap the Cholesky in a try/except that falls back to identity (uncorrelated) decomposition, or nudge the diagonal (`corr += epsilon*I`) to ensure PD.

### M4. `previous_price` semantics on first tick after add — confirm frontend handling
**Files:** `cache.py:31-37`, `simulator.py:224-228, 245-248`

When a ticker is first seeded, the cache stores `previous_price == price` (flat). The next update computes change against the seed, which is correct. The seeding update bumps `version` and emits a `flat` event for the new ticker (the intended "appears immediately" behavior). Confirm the frontend treats a `flat`/`change=0` first event as "new ticker" and not "no data." Worth a one-line doc note; not a bug.

### M5. Hanging Massive REST call blocks shutdown
**File:** `massive_client.py:83-97`, `stop()` at `55-64`

`_poll_once` runs the synchronous REST call via `asyncio.to_thread` (line 97). No timeout is configured on `RESTClient`. If that call hangs, `stop()` cancels the task (line 57) but `await self._task` (line 59) blocks until the worker thread returns, because `to_thread` cannot be cancelled. Net effect: shutdown can hang indefinitely on a stuck Massive call. Configure a request timeout on the `RESTClient`, and/or wrap `_poll_once` shutdown with `asyncio.wait_for`. No test covers a hanging poll vs. shutdown.

### M6. No consistent ticker normalization; only Massive normalizes
**Files:** `massive_client.py:67, 73` (uppercases/strips) vs `simulator.py:120-152` (no normalization), `cache.py:23` (no normalization)

`MassiveDataSource.add_ticker` uppercases and strips; `SimulatorDataSource.add_ticker`/`GBMSimulator.add_ticker` do **not**. So with the simulator, `add_ticker("aapl")` creates a distinct `"aapl"` entry with a random seed price (`simulator.py:151`), diverging from the Massive path and from the seed table. This is PLAN open-question #3. Normalize consistently in both sources (ideally in one shared place), and decide the policy for unknown tickers (the simulator silently invents a `random.uniform(50,300)` price, which the plan flags as needing an explicit rule).

---

## Low

### L1. `event_loop_policy` fixture in `conftest.py` is dead / non-functional
**File:** `tests/conftest.py:6-11`

With `pytest-asyncio` in `auto` mode, `event_loop_policy` returning the default policy is a no-op. If the intent was to customize loop behavior it does nothing; otherwise remove it to avoid confusion.

### L2. `PriceUpdate.timestamp` default factory never exercised
**File:** `models.py:16`

`field(default_factory=time.time)` is never used in production (cache always passes `ts`, `cache.py:30`). Harmless, but the time-based default invites accidental "now" timestamps in tests that look deterministic but aren't.

### L3. Price rounded twice (simulator and cache)
**Files:** `simulator.py:116` (`round(...,2)`) and `cache.py:36` (`round(price,2)`)

The simulator rounds to 2 decimals in `step()`, then the cache rounds again. The Massive path rounds only once (in cache). Pick one layer (recommend cache-only) so both sources behave identically; the simulator's `round` at line 116 is purely cosmetic and duplicative (`self._prices` keeps full precision, which is correct).

### L4. `MassiveDataSource.start` logs `len(tickers)` from the argument, not `self._tickers`
**File:** `massive_client.py:49-53`

Cosmetic: if `start` is ever called with duplicates, the log count differs from the deduped active set. Use `len(self._tickers)`.

### L5. `start()` double-call is undefined per docs but unguarded
**Files:** `interface.py:30-31`, `simulator.py:219`, `massive_client.py:41`

Calling `start()` twice silently leaks the first background task (the `self._task` reference is overwritten without cancelling). A cheap guard (`if self._task is not None: raise RuntimeError`) would make the documented contract enforceable.

### L6. `rich` is a runtime dependency only for the demo script
**File:** `pyproject.toml:12`

`rich` is needed only by `market_data_demo.py`, not the app, yet it ships in the production image's core deps. Move it to an optional `demo` extra to keep the runtime/container lean.

---

## Test coverage gaps

The suite is broad (73 tests) and the cache/models/factory are well covered. Notable gaps:

1. **`stream.py` has essentially no tests.** No test exercises the SSE generator: not the initial `retry:` frame, not version-change gating, not the `is_disconnected` break, not the `to_dict` payload shape, not multi-ticker JSON. Given H1/H2/H3 live here, this is the biggest coverage hole. Add tests driving the async generator with a fake `Request` whose `is_disconnected` flips to `True`, asserting it emits one data frame then stops.
2. **Massive real-schema risk (H4) is untested.** Every Massive test mocks `_fetch_snapshots` or builds a `MagicMock` snapshot that mirrors the code's own attribute assumptions (`test_massive.py:11-18`), so they cannot catch a wrong field path or wrong timestamp unit. Add at least one test against a captured real response fixture (or pin the SDK model and assert field names).
3. **Concurrency (H5) untested.** No test adds/removes a ticker while the poll/step loop is running and asserts no `RuntimeError`/lost ticker.
4. **Cholesky robustness (M3) untested.** No test with many highly-correlated tickers to prove the matrix stays PD.
5. **Determinism (M2).** Event-shock behavior (`event_probability=1.0` test at `test_simulator_source.py:127-138`) only asserts "starts and stops cleanly" — it never asserts a shock actually moved the price by 2-5%.
6. **Daily-change (C1).** No test asserts any day-open semantics because the feature doesn't exist — a contract gap, not a missing test per se.

---

## Summary of must-fix before frontend integration

All four resolved (85 tests pass, ruff clean):

- **C1** (day_open/daily-change contract) — ✅ `day_open` + `day_change_percent` wired through cache → `PriceUpdate` → SSE payload.
- **H4** (Massive timestamp unit + field path) — ✅ confirmed against the SDK; fixed nanosecond conversion and the wrong `last_trade.timestamp` attribute (which had been skipping every snapshot), plus day-open from the snapshot.
- **H1** (atomic version+snapshot read in the cache) — ✅ added `PriceCache.snapshot()`, SSE consumes it.
- **H2** (SSE keep-alive heartbeat) — ✅ 15s keep-alive comment; stream test coverage added.

Remaining (not yet addressed): H3, H5, M1–M6, L1–L6, and the other test-coverage gaps.
